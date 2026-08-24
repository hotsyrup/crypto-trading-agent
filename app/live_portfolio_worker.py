from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Protocol

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
LIVE_WORKER_INTERVAL_SECONDS = 60
RESEARCH_ENVELOPE_FIELDS = {
    "service",
    "schema_version",
    "mode",
    "execution",
    "generated_at",
    "packets",
}


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
        try:
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
) -> VerifiedPortfolio:
    if not native_gas_reserve_eth.is_finite() or native_gas_reserve_eth < 0:
        raise ValueError("Native gas reserve must be finite and non-negative.")
    signal_by_identity = {
        (signal.symbol, signal.token_address): signal for signal in signals
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
        matched = None
        for asset in universe.assets:
            expected = (asset.token_address or NATIVE_ETH_ADDRESS).lower()
            if address == expected:
                matched = asset
                break
        if matched is None:
            continue
        if balance.decimals != matched.decimals:
            raise ValueError("Governed token decimals do not match the CDP balance.")
        signal = signal_by_identity.get((matched.symbol, matched.token_address))
        if signal is None:
            raise ValueError("A held governed asset has no fresh valuation signal.")
        value = spendable_amount * signal.price_usd
        positions.append(
            PortfolioPosition(
                symbol=matched.symbol,
                token_address=matched.token_address,
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
    research_payload: object | Callable[[], object],
    universe: GovernedAssetUniverse | Callable[[], GovernedAssetUniverse],
    authorized_capital_usdc: Decimal,
    decision_journal_path: Path,
    live_audit_path: Path,
    risk_journal_path: Path,
    native_gas_reserve_eth: Decimal = Decimal("0"),
    now: datetime | None = None,
    live_config: LiveTradingConfig | None = None,
    executor_config: ExecutorConfig | None = None,
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
    resolved_research = (
        research_payload() if callable(research_payload) else research_payload
    )
    signals = _research_signals(resolved_research, resolved_universe, now=current_time)
    portfolio = _verified_portfolio(
        balances,
        signals,
        resolved_universe,
        wallet_address=wallet,
        native_gas_reserve_eth=native_gas_reserve_eth,
        now=current_time,
    )
    if portfolio.total_value_usdc == 0:
        return LiveCycleResult(
            CYCLE_NO_FUNDS,
            wallet,
            runtime.network_id,
            Decimal("0"),
            "Exact CDP wallet and Base network verified with no governed funds.",
        )
    risk = record_live_portfolio_value(
        portfolio.total_value_usdc,
        authorized_capital_usdc=authorized_capital_usdc,
        path=risk_journal_path,
        now=current_time,
    )
    execution_signals = _execution_eligible_signals(signals, resolved_universe)
    if not execution_signals:
        return LiveCycleResult(
            CYCLE_NO_SIGNAL,
            wallet,
            runtime.network_id,
            portfolio.total_value_usdc,
            "No fresh governed research signal passes execution liquidity policy.",
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
    "last_cycle_at": None,
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/", "/health"}:
            self.send_error(404)
            return
        payload = json.dumps(
            {
                "service": "crypto-trading-agent",
                "schema_version": 1,
                "mode": STATE["mode"],
                "status": STATE["status"],
                "last_cycle_at": STATE["last_cycle_at"],
            }
        ).encode()
        self.send_response(200 if STATE["status"] != "failed" else 503)
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
        STATE.update(status="refreshing_universe")
        refresh_governed_asset_universe(path)
        return load_governed_asset_universe(path, now=datetime.now(timezone.utc))


def main() -> None:
    server = HTTPServer(  # nosec B104
        ("0.0.0.0", int(os.getenv("PORT", "8080"))),
        HealthHandler,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not _worker_enabled():
        STATE.update(status="disabled")
        while True:
            time.sleep(3600)
    runtime = CdpLiveRuntime()
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
        try:
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
                live_config=load_live_trading_config(),
                executor_config=load_executor_config(),
            )
            STATE.update(
                status=result.status.lower(),
                last_cycle_at=cycle_time.isoformat(),
            )
        except Exception as error:
            STATE.update(status="failed", last_cycle_at=cycle_time.isoformat())
            print(
                json.dumps(
                    {
                        "event": "live_cycle_failed",
                        "error_type": type(error).__name__,
                    }
                ),
                flush=True,
            )
        time.sleep(interval)


if __name__ == "__main__":
    main()
