from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError

from app.asset_lifecycle import (
    AssetLifecycle,
    HistoricalGovernedContract,
    LifecycleAsset,
)
from app.agent_commerce_research import (
    AgentCommerceResearchGate,
    build_research_gate,
    research_public_status,
)
from app.base_asset_universe import (
    AssetUniverseError,
    GovernedAssetUniverse,
    load_governed_asset_universe,
)
from app.base_asset_universe_refresh import refresh_governed_asset_universe
from app.controlled_live_execution import (
    CDP_NETWORK_ID,
    NATIVE_ETH_ADDRESS,
    CdpAgentKitBackend,
    ControlledLiveResult,
    SwapBackend,
)
from app.live_portfolio_risk import record_live_portfolio_value
from app.live_execution_journal import read_live_execution_events
from app.live_trading_config import (
    BASE_USDC_ADDRESS,
    LiveTradingConfig,
    load_live_trading_config,
)
from app.portfolio_trading import (
    PortfolioPosition,
    ResearchSignal,
    VerifiedPortfolio,
    execute_research_portfolio_signal,
    research_signal_from_packet,
    valuation_signal_from_packet,
)
from app.research_feed import get_research_payload
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    MAX_TRADING_CAPITAL_USDC,
    ExecutorConfig,
    load_executor_config,
)


CYCLE_NO_FUNDS = "NO_FUNDS_READY"
CYCLE_POLICY_BLOCKED = "POLICY_BLOCKED"
CYCLE_NO_SIGNAL = "NO_ELIGIBLE_SIGNAL"
CYCLE_VALUATION_BLOCKED = "VALUATION_BLOCKED"
CYCLE_QUARANTINED = "QUARANTINED_HOLDINGS"
LIVE_WORKER_INTERVAL_SECONDS = 60
RESEARCH_ENVELOPE_FIELDS = {
    "service",
    "schema_version",
    "mode",
    "execution",
    "generated_at",
    "packets",
}
RESEARCH_WETH_ADDRESS = "0x4200000000000000000000000000000000000006"


@dataclass(frozen=True)
class OnchainTokenBalance:
    token_address: str
    amount: Decimal
    decimals: int


@dataclass(frozen=True)
class LiveCycleResult:
    status: str
    wallet_address: str
    network_id: str
    portfolio_value_usdc: Decimal
    reason: str
    transaction_hash: str | None = None
    trading_readiness: str = "blocked"
    held_required: int = 0
    held_covered: int = 0
    quarantined_count: int = 0


class LiveRuntime(SwapBackend, Protocol):
    wallet_address: str
    network_id: str

    def list_token_balances(self) -> tuple[OnchainTokenBalance, ...]:
        ...


def _research_signals(
    payload: object,
    universe: GovernedAssetUniverse,
    *,
    now: datetime,
    valuation_contracts: frozenset[str] = frozenset(),
) -> tuple[ResearchSignal, ...]:
    if not isinstance(payload, dict) or set(payload) != RESEARCH_ENVELOPE_FIELDS:
        raise ValueError("Research envelope fields are invalid.")
    if (
        payload.get("service") != "lumen-base-research-agent"
        or payload.get("schema_version") != 2
        or payload.get("mode") != "observation_only"
        or payload.get("execution") != "disabled"
    ):
        raise ValueError("Research envelope authority is invalid.")
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
    except ValueError as error:
        raise ValueError("Research envelope timestamp is invalid.") from error
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("Research envelope timestamp must include a timezone.")
    generated_at = generated_at.astimezone(timezone.utc)
    age = (now - generated_at).total_seconds()
    if age > 120 or age < -30:
        raise ValueError("Research envelope is stale or from the future.")
    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise ValueError("Research packets are unavailable.")
    accepted: dict[tuple[str, str | None], ResearchSignal] = {}
    for packet in packets:
        contract = (
            str(packet.get("contract_address", "")).lower()
            if isinstance(packet, dict)
            else ""
        )
        try:
            if contract in valuation_contracts:
                signal = valuation_signal_from_packet(packet, contract, now=now)
                if contract == RESEARCH_WETH_ADDRESS:
                    signal = replace(signal, token_address=None)
            else:
                signal = research_signal_from_packet(packet, universe, now=now)
        except ValueError:
            continue
        identity = (signal.symbol, signal.token_address)
        previous = accepted.get(identity)
        if previous is None or signal.observed_at > previous.observed_at:
            accepted[identity] = signal
        elif (
            signal.observed_at == previous.observed_at
            and signal.packet_id != previous.packet_id
        ):
            raise ValueError("Research packets are ambiguous for a governed asset.")
    return tuple(accepted.values())


def _verified_portfolio(
    balances: tuple[OnchainTokenBalance, ...],
    signals: tuple[ResearchSignal, ...],
    universe: GovernedAssetUniverse,
    *,
    wallet_address: str,
    native_gas_reserve_eth: Decimal,
    now: datetime,
    lifecycle_assets: tuple[LifecycleAsset, ...] | None = None,
) -> VerifiedPortfolio:
    if not native_gas_reserve_eth.is_finite() or native_gas_reserve_eth < 0:
        raise ValueError("Native gas reserve must be finite and non-negative.")
    signal_by_contract = {
        (signal.token_address or NATIVE_ETH_ADDRESS).lower(): signal
        for signal in signals
    }
    lifecycle_by_contract = {
        (asset.token_address or NATIVE_ETH_ADDRESS).lower(): asset
        for asset in lifecycle_assets or ()
    }
    seen: set[str] = set()
    usdc = Decimal("0")
    positions: list[PortfolioPosition] = []
    for balance in balances:
        address = balance.token_address.strip().lower()
        if address in seen:
            raise ValueError("CDP returned a duplicate token balance.")
        seen.add(address)
        if (
            not balance.amount.is_finite()
            or balance.amount < 0
            or type(balance.decimals) is not int
            or not 0 <= balance.decimals <= 36
        ):
            raise ValueError("CDP returned an invalid token balance.")
        spendable_amount = balance.amount
        if address == NATIVE_ETH_ADDRESS:
            spendable_amount = max(
                Decimal("0"),
                balance.amount - native_gas_reserve_eth,
            )
        if spendable_amount == 0:
            continue
        if address == BASE_USDC_ADDRESS:
            if balance.decimals != 6:
                raise ValueError("Official Base USDC decimals do not match.")
            usdc = balance.amount
            continue
        matched = lifecycle_by_contract.get(address)
        if matched is None and lifecycle_assets is None:
            for asset in universe.assets:
                expected = (asset.token_address or NATIVE_ETH_ADDRESS).lower()
                if address == expected:
                    matched = asset
                    break
        if matched is None:
            continue
        if balance.decimals != matched.decimals:
            raise ValueError("Governed token decimals do not match the CDP balance.")
        signal = signal_by_contract.get(address)
        if signal is None:
            raise ValueError("A held governed asset has no fresh valuation signal.")
        value = spendable_amount * signal.price_usd
        positions.append(
            PortfolioPosition(
                symbol=signal.symbol,
                token_address=signal.token_address,
                token_balance=spendable_amount,
                value_usdc=value,
                average_entry_price_usdc=signal.price_usd,
            )
        )
    return VerifiedPortfolio(
        observed_at=now,
        treasury_address=wallet_address,
        total_value_usdc=usdc
        + sum((item.value_usdc for item in positions), Decimal("0")),
        usdc_balance=usdc,
        positions=tuple(positions),
    )


def _historical_governance(path: Path) -> tuple[HistoricalGovernedContract, ...]:
    records: dict[str, HistoricalGovernedContract] = {}
    for event in read_live_execution_events(path=path):
        if event.get("event") != "RESERVED":
            continue
        recorded_at = datetime.fromisoformat(str(event.get("recorded_at")))
        for token_field, decimals_field in (
            ("from_token", "from_decimals"),
            ("to_token", "to_decimals"),
        ):
            address = str(event.get(token_field, "")).lower()
            if address == BASE_USDC_ADDRESS:
                continue
            decimals = event.get(decimals_field)
            if len(address) != 42 or not address.startswith("0x"):
                raise ValueError("Live journal contains an invalid governed contract.")
            if type(decimals) is not int or not 0 <= decimals <= 36:
                raise ValueError("Live journal contains invalid governed decimals.")
            previous = records.get(address)
            if previous is not None and previous.decimals != decimals:
                raise ValueError("Live journal governed decimals are contradictory.")
            records[address] = HistoricalGovernedContract(
                token_address=address,
                decimals=decimals,
                governed_at=recorded_at,
            )
    return tuple(records.values())


def _ordered_signals(
    signals: tuple[ResearchSignal, ...],
    portfolio: VerifiedPortfolio,
    universe: GovernedAssetUniverse,
) -> tuple[ResearchSignal, ...]:
    held = {(item.symbol, item.token_address) for item in portfolio.positions}
    rank = {(item.symbol, item.token_address): item.rank for item in universe.assets}

    def priority(signal: ResearchSignal) -> tuple[int, Decimal, int]:
        identity = (signal.symbol, signal.token_address)
        exit_signal = (
            identity in held
            and signal.change_h6_percent < 0
            and signal.change_h24_percent < 0
        )
        buy_signal = (
            signal.change_h6_percent > 0
            and signal.change_h24_percent > 0
            and signal.buys_h24 > signal.sells_h24
        )
        category = 0 if exit_signal else 1 if buy_signal else 2
        momentum = -(signal.change_h6_percent + signal.change_h24_percent)
        return category, momentum, rank.get(identity, 10_000)

    return tuple(sorted(signals, key=priority))


def _execution_eligible_signals(
    signals: tuple[ResearchSignal, ...],
    universe: GovernedAssetUniverse,
) -> tuple[ResearchSignal, ...]:
    """Remove candidates that the downstream asset-liquidity policy must reject."""

    accepted = []
    for signal in signals:
        asset = universe.require(signal.symbol, signal.token_address)
        if signal.liquidity_usd >= asset.liquidity_usd / Decimal("2"):
            accepted.append(signal)
    return tuple(accepted)


def run_live_cycle(
    *,
    runtime: LiveRuntime,
    research_payload: object | Callable[..., object],
    universe: GovernedAssetUniverse | Callable[[], GovernedAssetUniverse],
    authorized_capital_usdc: Decimal,
    decision_journal_path: Path,
    live_audit_path: Path,
    risk_journal_path: Path,
    native_gas_reserve_eth: Decimal = Decimal("0"),
    now: datetime | None = None,
    live_config: LiveTradingConfig | None = None,
    executor_config: ExecutorConfig | None = None,
    agent_commerce_research_gate: AgentCommerceResearchGate | None = None,
    lifecycle_registry_path: Path | None = None,
) -> LiveCycleResult:
    """Verify live inputs and make at most one governed execution attempt."""

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("Live cycle time must include a timezone.")
    current_time = current_time.astimezone(timezone.utc)
    wallet = runtime.wallet_address.strip().lower()
    if wallet != AUTHORIZED_TREASURY_ADDRESS or runtime.network_id != CDP_NETWORK_ID:
        raise ValueError("CDP runtime wallet or network is not authorized.")
    if (
        not authorized_capital_usdc.is_finite()
        or authorized_capital_usdc <= 0
        or authorized_capital_usdc > MAX_TRADING_CAPITAL_USDC
    ):
        raise ValueError("Authorized capital must be positive and at most $500.")
    balances = runtime.list_token_balances()
    if not balances:
        return LiveCycleResult(
            CYCLE_NO_FUNDS,
            wallet,
            runtime.network_id,
            Decimal("0"),
            "Exact CDP wallet and Base network verified with no governed funds.",
        )
    resolved_universe = universe() if callable(universe) else universe
    lifecycle = AssetLifecycle(
        lifecycle_registry_path or live_audit_path.parent / "asset_lifecycle.json"
    )
    lifecycle_assessment = lifecycle.evaluate(
        resolved_universe,
        balances,
        now=current_time,
        historical_governance=_historical_governance(live_audit_path),
    )
    held_research_contracts = frozenset(
        (
            RESEARCH_WETH_ADDRESS
            if item.token_address is None
            or item.token_address.lower() == NATIVE_ETH_ADDRESS
            else item.token_address.lower()
        )
        for item in lifecycle_assessment.held_governed
    )
    try:
        resolved_research = (
            research_payload(lifecycle_assessment.required_research_contracts)
            if callable(research_payload)
            else research_payload
        )
        valuation_signals = _research_signals(
            resolved_research,
            resolved_universe,
            now=current_time,
            valuation_contracts=held_research_contracts,
        )
        trade_signals = _research_signals(
            resolved_research,
            resolved_universe,
            now=current_time,
        )
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return LiveCycleResult(
            CYCLE_VALUATION_BLOCKED,
            wallet,
            runtime.network_id,
            Decimal("0"),
            "Fresh exact-contract research evidence is unavailable or invalid.",
            trading_readiness="blocked",
            held_required=len(lifecycle_assessment.held_governed),
            held_covered=0,
            quarantined_count=len(lifecycle_assessment.quarantined),
        )
    signal_contracts = {
        (signal.token_address or NATIVE_ETH_ADDRESS).lower()
        for signal in valuation_signals
    }
    missing_held = [
        item
        for item in lifecycle_assessment.held_governed
        if (item.token_address or NATIVE_ETH_ADDRESS).lower() not in signal_contracts
    ]
    if missing_held:
        return LiveCycleResult(
            CYCLE_VALUATION_BLOCKED,
            wallet,
            runtime.network_id,
            Decimal("0"),
            "Fresh exact-contract valuation is missing for a governed holding.",
            trading_readiness="blocked",
            held_required=len(lifecycle_assessment.held_governed),
            held_covered=len(lifecycle_assessment.held_governed) - len(missing_held),
            quarantined_count=len(lifecycle_assessment.quarantined),
        )
    portfolio = _verified_portfolio(
        balances,
        valuation_signals,
        resolved_universe,
        wallet_address=wallet,
        native_gas_reserve_eth=native_gas_reserve_eth,
        now=current_time,
        lifecycle_assets=lifecycle_assessment.held_governed,
    )
    if portfolio.total_value_usdc == 0:
        return LiveCycleResult(
            CYCLE_NO_FUNDS,
            wallet,
            runtime.network_id,
            Decimal("0"),
            "Exact CDP wallet and Base network verified with no governed funds.",
            trading_readiness="blocked",
            held_required=len(lifecycle_assessment.held_governed),
            held_covered=len(lifecycle_assessment.held_governed),
            quarantined_count=len(lifecycle_assessment.quarantined),
        )
    risk = record_live_portfolio_value(
        portfolio.total_value_usdc,
        authorized_capital_usdc=authorized_capital_usdc,
        path=risk_journal_path,
        now=current_time,
    )
    candidate_contracts = frozenset(lifecycle_assessment.candidate_contracts)
    execution_signals = _execution_eligible_signals(
        tuple(
            signal
            for signal in trade_signals
            if (
                RESEARCH_WETH_ADDRESS
                if signal.token_address is None
                else signal.token_address.lower()
            )
            in candidate_contracts
        ),
        resolved_universe,
    )
    if not execution_signals:
        return LiveCycleResult(
            CYCLE_NO_SIGNAL,
            wallet,
            runtime.network_id,
            portfolio.total_value_usdc,
            "No fresh governed research signal passes execution liquidity policy.",
            trading_readiness="blocked",
            held_required=len(lifecycle_assessment.held_governed),
            held_covered=len(lifecycle_assessment.held_governed),
            quarantined_count=len(lifecycle_assessment.quarantined),
        )
    selected = _ordered_signals(execution_signals, portfolio, resolved_universe)[0]
    result: ControlledLiveResult = execute_research_portfolio_signal(
        selected,
        portfolio,
        risk,
        resolved_universe,
        runtime,
        decision_journal_path=decision_journal_path,
        live_audit_path=live_audit_path,
        now=current_time,
        live_config=live_config,
        executor_config=executor_config,
        agent_commerce_research_gate=agent_commerce_research_gate,
    )
    status = result.status
    if status == "POLICY_REJECTED":
        status = CYCLE_POLICY_BLOCKED
    return LiveCycleResult(
        status,
        wallet,
        runtime.network_id,
        portfolio.total_value_usdc,
        " ".join(result.reasons),
        result.transaction_hash,
        trading_readiness=("ready" if status not in {CYCLE_POLICY_BLOCKED} else "blocked"),
        held_required=len(lifecycle_assessment.held_governed),
        held_covered=len(lifecycle_assessment.held_governed),
        quarantined_count=len(lifecycle_assessment.quarantined),
    )


class CdpLiveRuntime:
    def __init__(self) -> None:
        self._backend = CdpAgentKitBackend()
        self.wallet_address = self._backend.wallet_address
        self.network_id = self._backend.network_id

    def list_token_balances(self) -> tuple[OnchainTokenBalance, ...]:
        return tuple(
            OnchainTokenBalance(address, amount, decimals)
            for address, amount, decimals in self._backend.list_token_balances()
        )

    def submit_swap(self, request):
        return self._backend.submit_swap(request)


STATE: dict[str, object] = {
    "mode": "controlled_live_worker",
    "status": "starting",
    "operational_status": "starting",
    "trading_readiness": "blocked",
    "cycle_status": "starting",
    "last_cycle_at": None,
    "last_error": None,
    "last_error_message": None,
    "correlation_id": None,
    "held_required": 0,
    "held_covered": 0,
    "quarantined_count": 0,
    "safety_gates": {
        "live_trading_enabled": False,
        "executor_mode": "shadow_only",
        "kill_switch": "halted",
        "agent_commerce_research": "disabled",
    },
    "agent_commerce_research": research_public_status("disabled"),
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/", "/health"}:
            self.send_error(404)
            return
        payload = json.dumps(
            {
                "service": "crypto-trading-agent",
                "schema_version": 2,
                "mode": STATE["mode"],
                "status": STATE["status"],
                "operational_status": STATE["operational_status"],
                "trading_readiness": STATE["trading_readiness"],
                "cycle_status": STATE["cycle_status"],
                "last_cycle_at": STATE["last_cycle_at"],
                "last_error": STATE["last_error"],
                "last_error_message": STATE["last_error_message"],
                "correlation_id": STATE["correlation_id"],
                "held_required": STATE["held_required"],
                "held_covered": STATE["held_covered"],
                "quarantined_count": STATE["quarantined_count"],
                "safety_gates": STATE["safety_gates"],
                "agent_commerce_research": STATE["agent_commerce_research"],
            }
        ).encode()
        self.send_response(
            503 if STATE["operational_status"] == "failed" else 200
        )
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _authorized_capital() -> Decimal:
    try:
        value = Decimal(os.getenv("LIVE_AUTHORIZED_CAPITAL_USDC", "500"))
    except InvalidOperation as error:
        raise ValueError("LIVE_AUTHORIZED_CAPITAL_USDC is invalid.") from error
    if not value.is_finite() or value <= 0 or value > MAX_TRADING_CAPITAL_USDC:
        raise ValueError(
            "LIVE_AUTHORIZED_CAPITAL_USDC must be positive and at most 500."
        )
    return value


def _worker_enabled() -> bool:
    value = os.getenv("LIVE_WORKER_ENABLED", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("LIVE_WORKER_ENABLED must be true or false.")
    return value == "true"


def _native_gas_reserve() -> Decimal:
    try:
        value = Decimal(os.getenv("LIVE_NATIVE_GAS_RESERVE_ETH", "0"))
    except InvalidOperation as error:
        raise ValueError("LIVE_NATIVE_GAS_RESERVE_ETH is invalid.") from error
    if not value.is_finite() or value < 0:
        raise ValueError("LIVE_NATIVE_GAS_RESERVE_ETH must be finite and non-negative.")
    return value


def load_or_refresh_universe(path: Path, *, now: datetime) -> GovernedAssetUniverse:
    """Keep execution halted until a current exact-25 snapshot is available."""

    try:
        return load_governed_asset_universe(path, now=now)
    except AssetUniverseError:
        STATE.update(cycle_status="refreshing_universe", trading_readiness="blocked")
        refresh_governed_asset_universe(path)
        return load_governed_asset_universe(path, now=datetime.now(timezone.utc))


def _safety_gates(
    live_config: LiveTradingConfig,
    executor_config: ExecutorConfig,
    research_gate: AgentCommerceResearchGate,
) -> dict[str, object]:
    return {
        "live_trading_enabled": live_config.enabled,
        "executor_mode": executor_config.mode,
        "kill_switch": executor_config.kill_switch_state,
        "agent_commerce_research": research_gate.mode,
    }


def _safe_error_message(error: BaseException) -> str:
    message = str(error).strip() or "No diagnostic message supplied."
    message = re.sub(r"https?://[^\s]+", "[redacted-url]", message)
    message = re.sub(r"0x[0-9a-fA-F]{40,64}", "[redacted-hex]", message)
    return message[:240]


def _record_cycle_result(
    result: LiveCycleResult,
    *,
    cycle_time: datetime,
    correlation_id: str,
    safety_gates: dict[str, object],
) -> None:
    STATE.update(
        status="operational",
        operational_status="operational",
        trading_readiness=result.trading_readiness,
        cycle_status=result.status.lower(),
        last_cycle_at=cycle_time.isoformat(),
        last_error=None,
        last_error_message=None,
        correlation_id=correlation_id,
        held_required=result.held_required,
        held_covered=result.held_covered,
        quarantined_count=result.quarantined_count,
        safety_gates=safety_gates,
    )
    print(
        json.dumps(
            {
                "event": "live_cycle_completed",
                "correlation_id": correlation_id,
                "cycle_status": result.status,
                "trading_readiness": result.trading_readiness,
                "held_required": result.held_required,
                "held_covered": result.held_covered,
                "quarantined_count": result.quarantined_count,
                "transaction_submitted": result.transaction_hash is not None,
            }
        ),
        flush=True,
    )


def _record_cycle_failure(
    error: BaseException,
    *,
    cycle_time: datetime,
    correlation_id: str,
) -> None:
    message = _safe_error_message(error)
    STATE.update(
        status="failed",
        operational_status="failed",
        trading_readiness="blocked",
        cycle_status="integrity_failure",
        last_cycle_at=cycle_time.isoformat(),
        last_error=type(error).__name__,
        last_error_message=message,
        correlation_id=correlation_id,
    )
    print(
        json.dumps(
            {
                "event": "live_cycle_failed",
                "correlation_id": correlation_id,
                "stage": "integrity_or_runtime",
                "error_type": type(error).__name__,
                "message": message,
                "trading_readiness": "blocked",
            }
        ),
        flush=True,
    )


def main() -> None:
    server = HTTPServer(
        ("0.0.0.0", int(os.getenv("PORT", "8080"))),  # nosec B104
        HealthHandler,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not _worker_enabled():
        STATE.update(
            status="operational",
            operational_status="operational",
            trading_readiness="blocked",
            cycle_status="disabled",
        )
        while True:
            time.sleep(3600)
    runtime = CdpLiveRuntime()
    research_gate = build_research_gate(
        wallet_address=runtime.wallet_address,
        journal_path=Path(
            os.getenv(
                "LUMEN_AGENT_COMMERCE_RESEARCH_JOURNAL_PATH",
                "data/agent_commerce_research_v1.jsonl",
            )
        ),
    )
    STATE["agent_commerce_research"] = research_public_status(research_gate.mode)
    interval = int(
        os.getenv(
            "LIVE_WORKER_INTERVAL_SECONDS",
            str(LIVE_WORKER_INTERVAL_SECONDS),
        )
    )
    if not 30 <= interval <= 3600:
        raise ValueError("LIVE_WORKER_INTERVAL_SECONDS must be between 30 and 3600.")
    while True:
        cycle_time = datetime.now(timezone.utc)
        correlation_id = uuid.uuid4().hex[:16]
        try:
            live_config = load_live_trading_config()
            executor_config = load_executor_config()
            safety_gates = _safety_gates(
                live_config,
                executor_config,
                research_gate,
            )
            result = run_live_cycle(
                runtime=runtime,
                research_payload=get_research_payload,
                universe=lambda: load_or_refresh_universe(
                    Path(
                        os.getenv(
                            "LIVE_ASSET_UNIVERSE_PATH",
                            "data/base_top25_universe.json",
                        )
                    ),
                    now=cycle_time,
                ),
                authorized_capital_usdc=_authorized_capital(),
                decision_journal_path=Path("data/execution_decisions.jsonl"),
                live_audit_path=Path("data/live_execution_audit.jsonl"),
                risk_journal_path=Path("data/live_portfolio_risk.jsonl"),
                native_gas_reserve_eth=_native_gas_reserve(),
                now=cycle_time,
                live_config=live_config,
                executor_config=executor_config,
                agent_commerce_research_gate=research_gate,
            )
            _record_cycle_result(
                result,
                cycle_time=cycle_time,
                correlation_id=correlation_id,
                safety_gates=safety_gates,
            )
        except Exception as error:
            _record_cycle_failure(
                error,
                cycle_time=cycle_time,
                correlation_id=correlation_id,
            )
        time.sleep(interval)


if __name__ == "__main__":
    main()
