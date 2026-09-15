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
    GovernedAsset,
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
from app.held_asset_materiality import (
    DUST_PRICE_SAFETY_MULTIPLIER,
    DUST_THRESHOLD_USDC,
    WETH_ADDRESS,
    DexScreenerDustPriceReader,
    ExactContractPriceReader,
)
from app.live_portfolio_risk import record_live_portfolio_value
from app.live_trading_config import (
    BASE_USDC_ADDRESS,
    LiveTradingConfig,
    load_live_trading_config,
)
from app.portfolio_trading import (
    MIN_EXIT_LIQUIDITY_USD,
    PortfolioPosition,
    ResearchSignal,
    VerifiedPortfolio,
    execute_research_portfolio_signal,
    is_entry_signal,
    is_exit_signal,
)
from app.position_cost_basis import (
    PositionCostBasis,
    PositionCostBasisError,
    reconstruct_position_cost_basis,
)
from app.research_state import ValuationSignal, load_research_state
from app.research_transport import configured_research_adapter
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
LIVE_WORKER_INTERVAL_SECONDS = 60


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
    portfolio_value_usdc: Decimal | None
    reason: str
    transaction_hash: str | None = None
    held_required: int = 0
    held_covered: int = 0
    missing_valuations: tuple[str, ...] = ()
    dust_holdings: tuple[QuarantinedDustHolding, ...] = ()
    quarantined_material_holdings: tuple[QuarantinedMaterialHolding, ...] = ()
    quarantined_unknown_holdings: tuple[str, ...] = ()
    unresolved_holdings: tuple[str, ...] = ()
    macro_available: bool | None = None


@dataclass(frozen=True)
class QuarantinedDustHolding:
    symbol: str
    token_address: str | None
    estimated_value_usdc: Decimal
    conservative_value_usdc: Decimal


@dataclass(frozen=True)
class QuarantinedMaterialHolding:
    symbol: str
    token_address: str
    value_usdc: Decimal
    conservative_value_usdc: Decimal


@dataclass(frozen=True)
class PortfolioCoverage:
    portfolio: VerifiedPortfolio | None
    covered_value_usdc: Decimal
    held_required: int
    held_covered: int
    missing_valuations: tuple[str, ...]
    dust_holdings: tuple[QuarantinedDustHolding, ...]
    quarantined_material_holdings: tuple[QuarantinedMaterialHolding, ...]
    quarantined_unknown_holdings: tuple[str, ...]
    unresolved_holdings: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.portfolio is not None

    @property
    def dust_identities(self) -> set[tuple[str, str | None]]:
        return {
            (item.symbol, item.token_address)
            for item in self.dust_holdings
            if not item.symbol.startswith("UNKNOWN:")
        }


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
    return load_research_state(payload, universe, now=now).candidate_signals()


def _portfolio_coverage(
    balances: tuple[OnchainTokenBalance, ...],
    signals: tuple[ValuationSignal | ResearchSignal, ...],
    universe: GovernedAssetUniverse,
    *,
    wallet_address: str,
    native_gas_reserve_eth: Decimal,
    now: datetime,
    dust_price_reader: ExactContractPriceReader | None = None,
    cost_bases: dict[str, PositionCostBasis] | None = None,
) -> PortfolioCoverage:
    """Classify holdings, then expose a total only when material coverage is safe."""

    if not native_gas_reserve_eth.is_finite() or native_gas_reserve_eth < 0:
        raise ValueError("Native gas reserve must be finite and non-negative.")
    signal_by_identity = {
        (signal.symbol, signal.token_address): signal for signal in signals
    }
    seen: set[str] = set()
    usdc = Decimal("0")
    positions: list[PortfolioPosition] = []
    missing: list[str] = []
    dust: list[QuarantinedDustHolding] = []
    quarantined_material: list[QuarantinedMaterialHolding] = []
    quarantined_unknown: list[str] = []
    unresolved: list[str] = []
    held_required = 0
    normalized: list[
        tuple[OnchainTokenBalance, str, Decimal, GovernedAsset | None]
    ] = []
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
        if matched is not None and balance.decimals != matched.decimals:
            raise ValueError("Governed token decimals do not match the CDP balance.")
        normalized.append((balance, address, spendable_amount, matched))

    fallback_addresses = tuple(
        (WETH_ADDRESS if address == NATIVE_ETH_ADDRESS else address)
        for _balance, address, _spendable, matched in normalized
        if matched is None
        or signal_by_identity.get((matched.symbol, matched.token_address)) is None
    )
    fallback_prices = {}
    if dust_price_reader is not None and fallback_addresses:
        try:
            fallback_prices = dust_price_reader.read_conservative_prices(
                fallback_addresses,
                now=now,
            )
        except (RuntimeError, ValueError, OSError):
            fallback_prices = {}

    for _balance, address, spendable_amount, matched in normalized:
        query_address = WETH_ADDRESS if address == NATIVE_ETH_ADDRESS else address
        if matched is None:
            evidence = fallback_prices.get(query_address)
            if evidence is not None:
                estimated = spendable_amount * evidence.price_usd
                conservative = spendable_amount * evidence.conservative_price_usd
                if conservative < DUST_THRESHOLD_USDC:
                    dust.append(
                        QuarantinedDustHolding(
                            symbol=evidence.symbol or f"UNKNOWN:{address[:10]}",
                            token_address=(
                                None if address == NATIVE_ETH_ADDRESS else address
                            ),
                            estimated_value_usdc=estimated,
                            conservative_value_usdc=conservative,
                        )
                    )
                    continue
                symbol = evidence.symbol or f"UNKNOWN:{address[:10]}"
                held_required += 1
                positions.append(
                    PortfolioPosition(
                        symbol=symbol,
                        token_address=address,
                        token_balance=spendable_amount,
                        value_usdc=estimated,
                        average_entry_price_usdc=evidence.price_usd,
                    )
                )
                quarantined_material.append(
                    QuarantinedMaterialHolding(
                        symbol=symbol,
                        token_address=address,
                        value_usdc=estimated,
                        conservative_value_usdc=conservative,
                    )
                )
                continue
            quarantined_unknown.append(address)
            continue
        signal = signal_by_identity.get((matched.symbol, matched.token_address))
        if signal is None:
            evidence = fallback_prices.get(matched.token_address or WETH_ADDRESS)
            if evidence is not None:
                estimated = spendable_amount * evidence.price_usd
                conservative = spendable_amount * evidence.conservative_price_usd
                if conservative < DUST_THRESHOLD_USDC:
                    dust.append(
                        QuarantinedDustHolding(
                            symbol=matched.symbol,
                            token_address=matched.token_address,
                            estimated_value_usdc=estimated,
                            conservative_value_usdc=conservative,
                        )
                    )
                    continue
            held_required += 1
            missing.append(matched.symbol)
            continue
        value = spendable_amount * signal.price_usd
        conservative_value = value * DUST_PRICE_SAFETY_MULTIPLIER
        if conservative_value < DUST_THRESHOLD_USDC:
            dust.append(
                QuarantinedDustHolding(
                    symbol=matched.symbol,
                    token_address=matched.token_address,
                    estimated_value_usdc=value,
                    conservative_value_usdc=conservative_value,
                )
            )
            continue
        held_required += 1
        basis = (cost_bases or {}).get(address)
        quantum = Decimal(1).scaleb(-balance.decimals)
        basis_quantity = basis.confirmed_quantity if basis is not None else Decimal("0")
        basis_tolerance = max(quantum * Decimal("10"), basis_quantity * Decimal("0.001"))
        basis_verified = bool(
            basis is not None
            and basis.verified
            and basis_quantity > 0
            and abs(spendable_amount - basis_quantity) <= basis_tolerance
        )
        positions.append(
            PortfolioPosition(
                symbol=matched.symbol,
                token_address=matched.token_address,
                token_balance=spendable_amount,
                value_usdc=value,
                average_entry_price_usdc=(
                    basis.average_entry_price_usdc
                    if basis_verified and basis is not None
                    else Decimal("0")
                ),
                cost_basis_verified=basis_verified,
                last_entry_at=(basis.last_entry_at if basis is not None else None),
                confirmed_buy_count=(
                    basis.confirmed_buy_count if basis is not None else 0
                ),
            )
        )
    covered_value = usdc + sum(
        (item.value_usdc for item in positions),
        Decimal("0"),
    )
    portfolio = None
    if not missing and not unresolved:
        portfolio = VerifiedPortfolio(
            observed_at=now,
            treasury_address=wallet_address,
            total_value_usdc=covered_value,
            usdc_balance=usdc,
            positions=tuple(positions),
        )
    return PortfolioCoverage(
        portfolio=portfolio,
        covered_value_usdc=covered_value,
        held_required=held_required,
        held_covered=len(positions),
        missing_valuations=tuple(missing),
        dust_holdings=tuple(dust),
        quarantined_material_holdings=tuple(quarantined_material),
        quarantined_unknown_holdings=tuple(quarantined_unknown),
        unresolved_holdings=tuple(unresolved),
    )


def _verified_portfolio(
    balances: tuple[OnchainTokenBalance, ...],
    signals: tuple[ResearchSignal, ...],
    universe: GovernedAssetUniverse,
    *,
    wallet_address: str,
    native_gas_reserve_eth: Decimal,
    now: datetime,
) -> VerifiedPortfolio:
    coverage = _portfolio_coverage(
        balances,
        signals,
        universe,
        wallet_address=wallet_address,
        native_gas_reserve_eth=native_gas_reserve_eth,
        now=now,
    )
    if coverage.portfolio is None:
        raise ValueError("A held governed asset has no fresh valuation signal.")
    return coverage.portfolio


def _ordered_signals(
    signals: tuple[ResearchSignal, ...],
    portfolio: VerifiedPortfolio,
    universe: GovernedAssetUniverse,
) -> tuple[ResearchSignal, ...]:
    held = {(item.symbol, item.token_address) for item in portfolio.positions}
    positions = {
        (item.symbol, item.token_address): item for item in portfolio.positions
    }
    rank = {(item.symbol, item.token_address): item.rank for item in universe.assets}

    def priority(signal: ResearchSignal) -> tuple[int, Decimal, int]:
        identity = (signal.symbol, signal.token_address)
        exit_signal = identity in held and is_exit_signal(
            signal, positions.get(identity)
        )
        buy_signal = is_entry_signal(signal)
        category = 0 if exit_signal else 1 if buy_signal else 2
        momentum = -(signal.change_h6_percent + signal.change_h24_percent)
        return category, momentum, rank.get(identity, 10_000)

    return tuple(sorted(signals, key=priority))


def _execution_eligible_signals(
    signals: tuple[ResearchSignal, ...],
    universe: GovernedAssetUniverse,
    *,
    excluded_identities: set[tuple[str, str | None]] | None = None,
    held_identities: set[tuple[str, str | None]] | None = None,
) -> tuple[ResearchSignal, ...]:
    """Remove candidates that the downstream asset-liquidity policy must reject."""

    accepted = []
    excluded = excluded_identities or set()
    held = held_identities or set()
    for signal in signals:
        if (signal.symbol, signal.token_address) in excluded:
            continue
        asset = universe.require(signal.symbol, signal.token_address)
        identity = (signal.symbol, signal.token_address)
        if (
            identity in held
            and signal.liquidity_usd >= MIN_EXIT_LIQUIDITY_USD
        ) or signal.liquidity_usd >= asset.liquidity_usd / Decimal("2"):
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
    macro_required_for_new_entries: bool = False,
    dust_price_reader: ExactContractPriceReader | None = None,
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
    research_state = load_research_state(
        resolved_research,
        resolved_universe,
        now=current_time,
    )
    try:
        cost_bases = reconstruct_position_cost_basis(path=live_audit_path)
    except (OSError, PositionCostBasisError, ValueError) as error:
        raise ValueError(f"Persistent cost basis is unavailable: {error}") from error
    coverage = _portfolio_coverage(
        balances,
        research_state.valuation_signals,
        resolved_universe,
        wallet_address=wallet,
        native_gas_reserve_eth=native_gas_reserve_eth,
        now=current_time,
        dust_price_reader=dust_price_reader,
        cost_bases=cost_bases,
    )
    if not coverage.complete:
        return LiveCycleResult(
            CYCLE_VALUATION_BLOCKED,
            wallet,
            runtime.network_id,
            None,
            "Held-asset materiality or exact-contract valuation is unresolved.",
            held_required=coverage.held_required,
            held_covered=coverage.held_covered,
            missing_valuations=coverage.missing_valuations,
            dust_holdings=coverage.dust_holdings,
            quarantined_material_holdings=coverage.quarantined_material_holdings,
            quarantined_unknown_holdings=coverage.quarantined_unknown_holdings,
            unresolved_holdings=coverage.unresolved_holdings,
            macro_available=research_state.macro.available,
        )
    portfolio = coverage.portfolio
    assert portfolio is not None
    if portfolio.total_value_usdc == 0:
        return LiveCycleResult(
            CYCLE_NO_FUNDS,
            wallet,
            runtime.network_id,
            Decimal("0"),
            "Exact CDP wallet and Base network verified with no governed funds.",
            held_required=coverage.held_required,
            held_covered=coverage.held_covered,
            dust_holdings=coverage.dust_holdings,
            quarantined_material_holdings=coverage.quarantined_material_holdings,
            quarantined_unknown_holdings=coverage.quarantined_unknown_holdings,
            macro_available=research_state.macro.available,
        )
    risk = record_live_portfolio_value(
        portfolio.total_value_usdc,
        authorized_capital_usdc=authorized_capital_usdc,
        path=risk_journal_path,
        now=current_time,
    )
    execution_signals = _execution_eligible_signals(
        research_state.candidate_signals(),
        resolved_universe,
        excluded_identities=coverage.dust_identities,
        held_identities={
            (item.symbol, item.token_address) for item in portfolio.positions
        },
    )
    if not execution_signals:
        return LiveCycleResult(
            CYCLE_NO_SIGNAL,
            wallet,
            runtime.network_id,
            portfolio.total_value_usdc,
            "No fresh governed research signal passes execution liquidity policy.",
            held_required=coverage.held_required,
            held_covered=coverage.held_covered,
            dust_holdings=coverage.dust_holdings,
            quarantined_material_holdings=coverage.quarantined_material_holdings,
            quarantined_unknown_holdings=coverage.quarantined_unknown_holdings,
            macro_available=research_state.macro.available,
        )
    selected = _ordered_signals(execution_signals, portfolio, resolved_universe)[0]
    held = {(item.symbol, item.token_address) for item in portfolio.positions}
    if (
        macro_required_for_new_entries
        and (selected.symbol, selected.token_address) not in held
        and not research_state.macro.available
    ):
        return LiveCycleResult(
            CYCLE_POLICY_BLOCKED,
            wallet,
            runtime.network_id,
            portfolio.total_value_usdc,
            research_state.macro.reason,
            held_required=coverage.held_required,
            held_covered=coverage.held_covered,
            dust_holdings=coverage.dust_holdings,
            quarantined_material_holdings=coverage.quarantined_material_holdings,
            quarantined_unknown_holdings=coverage.quarantined_unknown_holdings,
            macro_available=False,
        )
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
        held_required=coverage.held_required,
        held_covered=coverage.held_covered,
        dust_holdings=coverage.dust_holdings,
        quarantined_material_holdings=coverage.quarantined_material_holdings,
        quarantined_unknown_holdings=coverage.quarantined_unknown_holdings,
        macro_available=research_state.macro.available,
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
    "held_required": None,
    "held_covered": None,
    "missing_valuations": [],
    "dust_threshold_usdc": str(DUST_THRESHOLD_USDC),
    "dust_holdings": [],
    "quarantined_material_holdings": [],
    "quarantined_unknown_holdings": [],
    "unresolved_holdings": [],
    "warnings": [],
    "macro_available": None,
    "last_reason": None,
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
                "last_cycle_at": STATE["last_cycle_at"],
                "held_required": STATE["held_required"],
                "held_covered": STATE["held_covered"],
                "missing_valuations": STATE["missing_valuations"],
                "dust_threshold_usdc": STATE["dust_threshold_usdc"],
                "dust_holdings": STATE["dust_holdings"],
                "quarantined_material_holdings": STATE[
                    "quarantined_material_holdings"
                ],
                "quarantined_unknown_holdings": STATE[
                    "quarantined_unknown_holdings"
                ],
                "unresolved_holdings": STATE["unresolved_holdings"],
                "warnings": STATE["warnings"],
                "macro_available": STATE["macro_available"],
                "last_reason": STATE["last_reason"],
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


def _macro_required_for_new_entries() -> bool:
    value = os.getenv(
        "RESEARCH_MACRO_REQUIRED_FOR_NEW_ENTRIES",
        "false",
    ).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(
            "RESEARCH_MACRO_REQUIRED_FOR_NEW_ENTRIES must be true or false."
        )
    return value == "true"


def load_or_refresh_universe(path: Path, *, now: datetime) -> GovernedAssetUniverse:
    """Keep execution halted until a current exact-25 snapshot is available."""

    try:
        return load_governed_asset_universe(path, now=now)
    except AssetUniverseError:
        STATE.update(status="refreshing_universe")
        refresh_governed_asset_universe(path)
        return load_governed_asset_universe(path, now=datetime.now(timezone.utc))


def main() -> None:
    server = HTTPServer(
        ("0.0.0.0", int(os.getenv("PORT", "8080"))),  # nosec B104
        HealthHandler,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not _worker_enabled():
        STATE.update(status="disabled")
        while True:
            time.sleep(3600)
    runtime = CdpLiveRuntime()
    research_adapter = configured_research_adapter()
    dust_price_reader = DexScreenerDustPriceReader()
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
                research_payload=research_adapter.fetch,
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
                macro_required_for_new_entries=_macro_required_for_new_entries(),
                dust_price_reader=dust_price_reader,
            )
            STATE.update(
                status=result.status.lower(),
                last_cycle_at=cycle_time.isoformat(),
                held_required=result.held_required,
                held_covered=result.held_covered,
                missing_valuations=list(result.missing_valuations),
                dust_holdings=[
                    {
                        "symbol": item.symbol,
                        "token_address": item.token_address,
                        "estimated_value_usdc": str(item.estimated_value_usdc),
                        "conservative_value_usdc": str(
                            item.conservative_value_usdc
                        ),
                    }
                    for item in result.dust_holdings
                ],
                quarantined_material_holdings=[
                    {
                        "symbol": item.symbol,
                        "token_address": item.token_address,
                        "value_usdc": str(item.value_usdc),
                        "conservative_value_usdc": str(
                            item.conservative_value_usdc
                        ),
                    }
                    for item in result.quarantined_material_holdings
                ],
                quarantined_unknown_holdings=list(
                    result.quarantined_unknown_holdings
                ),
                unresolved_holdings=list(result.unresolved_holdings),
                warnings=[
                    *(
                        ["DUST_HOLDINGS_QUARANTINED"]
                        if result.dust_holdings
                        else []
                    ),
                    *(
                        ["MATERIAL_UNKNOWN_HOLDINGS_QUARANTINED"]
                        if result.quarantined_material_holdings
                        else []
                    ),
                    *(
                        ["UNKNOWN_HOLDINGS_QUARANTINED"]
                        if result.quarantined_unknown_holdings
                        else []
                    ),
                ],
                macro_available=result.macro_available,
                last_reason=result.reason,
            )
        except Exception as error:
            STATE.update(
                status="failed",
                last_cycle_at=cycle_time.isoformat(),
                last_reason=f"{type(error).__name__}: live cycle failed closed.",
            )
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
