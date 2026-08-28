from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

from app.agent_commerce_research import (
    AgentCommerceResearchGate,
    ResearchPolicyError,
    candidate_for_trade,
)
from app.base_asset_universe import (
    MAX_FUTURE_SKEW,
    MAX_SNAPSHOT_AGE,
    MINIMUM_DAILY_VOLUME_USD,
    MINIMUM_LIQUIDITY_USD,
    GovernedAssetUniverse,
)
from app.controlled_live_execution import (
    NATIVE_ETH_ADDRESS,
    ROUTE_ID,
    STATUS_POLICY_REJECTED,
    ApprovedSwap,
    ControlledLiveResult,
    SwapBackend,
    execute_controlled_live_trade,
)
from app.live_trading_config import BASE_USDC_ADDRESS, LiveTradingConfig
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    BASE_MAINNET_CHAIN_ID,
    MAX_TRADE_NOTIONAL_USDC,
    ExecutorConfig,
    RiskSnapshot,
    TradeIntent,
)


MAX_RESEARCH_AGE_SECONDS = 120
MAX_FUTURE_SKEW_SECONDS = 30
STOP_LOSS_PERCENT = Decimal("8")
TAKE_PROFIT_PERCENT = Decimal("15")
DEFAULT_SLIPPAGE_BPS = 50
STRATEGY_ID = "research-ranked-base-portfolio"
STRATEGY_VERSION = "1.0.0"
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
USDC_QUANTUM = Decimal("0.000001")
ALLOWED_RESEARCH_WARNINGS = {
    "CONTRACT_SECURITY_NOT_VERIFIED",
    "HOLDER_CONCENTRATION_NOT_VERIFIED",
}


@dataclass(frozen=True)
class ResearchSignal:
    packet_id: str
    observed_at: datetime
    symbol: str
    token_address: str | None
    price_usd: Decimal
    liquidity_usd: Decimal
    daily_volume_usd: Decimal
    change_h6_percent: Decimal
    change_h24_percent: Decimal
    buys_h24: int
    sells_h24: int


@dataclass(frozen=True)
class PortfolioPosition:
    symbol: str
    token_address: str | None
    token_balance: Decimal
    value_usdc: Decimal
    average_entry_price_usdc: Decimal


@dataclass(frozen=True)
class VerifiedPortfolio:
    observed_at: datetime
    treasury_address: str
    total_value_usdc: Decimal
    usdc_balance: Decimal
    positions: tuple[PortfolioPosition, ...]


def _research_signal_from_packet(
    packet: object,
    universe: GovernedAssetUniverse | None,
    *,
    required_contract: str | None = None,
    now: datetime | None = None,
) -> ResearchSignal:
    """Normalize one observation-only packet without granting it authority."""

    current_time = now or datetime.now(timezone.utc)
    if not _aware(current_time):
        raise ValueError("Research evaluation time must include a timezone.")
    current_time = current_time.astimezone(timezone.utc)
    if not isinstance(packet, dict):
        raise ValueError("Research packet must be an object.")
    packet_id = packet.get("packet_id")
    canonical = {
        key: value
        for key, value in packet.items()
        if key not in {"packet_id", "is_stale"}
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not isinstance(packet_id, str) or packet_id != digest:
        raise ValueError("Research packet digest is invalid.")
    if (
        packet.get("schema_version") != 2
        or packet.get("network") != "base"
        or packet.get("data_quality") != "complete"
        or packet.get("recommendation") != "OBSERVE_ONLY"
        or packet.get("execution_authorized") is not False
        or packet.get("is_stale") is not False
    ):
        raise ValueError("Research packet execution boundary is invalid.")
    try:
        observed_at = datetime.fromisoformat(str(packet.get("received_at")))
        expires_at = datetime.fromisoformat(str(packet.get("expires_at")))
    except ValueError as error:
        raise ValueError("Research packet timestamps are invalid.") from error
    if not _aware(observed_at) or not _aware(expires_at):
        raise ValueError("Research packet timestamps must include a timezone.")
    observed_at = observed_at.astimezone(timezone.utc)
    expires_at = expires_at.astimezone(timezone.utc)
    age = (current_time - observed_at).total_seconds()
    if age > MAX_RESEARCH_AGE_SECONDS or expires_at <= current_time:
        raise ValueError("Research packet is stale.")
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("Research packet timestamp is too far in the future.")

    contract = str(packet.get("contract_address", "")).lower()
    symbol = str(packet.get("symbol", "")).upper()
    matched_symbol = None
    matched_address = None
    if universe is not None:
        for asset in universe.assets:
            expected_contract = asset.token_address or WETH_ADDRESS
            expected_symbol = "WETH" if asset.token_address is None else asset.symbol
            if contract == expected_contract and symbol == expected_symbol:
                matched_symbol = asset.symbol
                matched_address = asset.token_address
                break
    elif required_contract is not None and contract == required_contract.lower():
        if not symbol or len(symbol) > 20:
            raise ValueError("Research token symbol is invalid.")
        matched_symbol = symbol
        matched_address = contract
    if matched_symbol is None:
        raise ValueError("Research token is outside the governed exact-contract set.")
    warnings = packet.get("warnings")
    if (
        not isinstance(warnings, list)
        or set(warnings) != ALLOWED_RESEARCH_WARNINGS
        or len(warnings) != len(ALLOWED_RESEARCH_WARNINGS)
    ):
        raise ValueError("Research packet contains a disallowed warning.")
    source = packet.get("source")
    if not isinstance(source, dict):
        raise ValueError("Research packet source is unavailable.")
    if (
        source.get("provider") != "dexscreener"
        or source.get("discovery") != "configured_watchlist"
        or source.get("marketing_influenced") is not False
        or str(source.get("base_contract_address", "")).lower() != contract
        or str(source.get("quote_contract_address", "")).lower()
        not in {BASE_USDC_ADDRESS, WETH_ADDRESS}
    ):
        raise ValueError("Research packet source identity is invalid.")
    metrics = packet.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("Research market metrics are unavailable.")
    try:
        active_boosts = int(metrics.get("active_boosts", 0))
        values = {
            "price": Decimal(str(metrics["price_usd"])),
            "liquidity": Decimal(str(metrics["liquidity_usd"])),
            "volume": Decimal(str(metrics["volume_h24_usd"])),
            "change_h6": Decimal(str(metrics["price_change_h6_percent"])),
            "change_h24": Decimal(str(metrics["price_change_h24_percent"])),
        }
        buys = int(metrics["buys_h24"])
        sells = int(metrics["sells_h24"])
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("Research market metrics are invalid.") from error
    if (
        active_boosts != 0
        or buys < 0
        or sells < 0
        or any(not value.is_finite() for value in values.values())
        or values["price"] <= 0
        or values["liquidity"] < MINIMUM_LIQUIDITY_USD
        or values["volume"] < MINIMUM_DAILY_VOLUME_USD
    ):
        raise ValueError("Research market metrics fail portfolio policy.")
    return ResearchSignal(
        packet_id=packet_id,
        observed_at=observed_at,
        symbol=matched_symbol,
        token_address=matched_address,
        price_usd=values["price"],
        liquidity_usd=values["liquidity"],
        daily_volume_usd=values["volume"],
        change_h6_percent=values["change_h6"],
        change_h24_percent=values["change_h24"],
        buys_h24=buys,
        sells_h24=sells,
    )


def research_signal_from_packet(
    packet: object,
    universe: GovernedAssetUniverse,
    *,
    now: datetime | None = None,
) -> ResearchSignal:
    """Normalize a current-candidate packet through the governed universe."""

    return _research_signal_from_packet(packet, universe, now=now)


def valuation_signal_from_packet(
    packet: object,
    token_address: str,
    *,
    now: datetime | None = None,
) -> ResearchSignal:
    """Normalize valuation-only evidence for a retained exact contract."""

    return _research_signal_from_packet(
        packet,
        None,
        required_contract=token_address,
        now=now,
    )


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _finite_positive(value: Decimal) -> bool:
    return value.is_finite() and value > 0


def _policy_rejected(*reasons: str) -> ControlledLiveResult:
    return ControlledLiveResult(status=STATUS_POLICY_REJECTED, reasons=tuple(reasons))


def _position_for(
    portfolio: VerifiedPortfolio,
    *,
    symbol: str,
    token_address: str | None,
) -> PortfolioPosition | None:
    normalized_symbol = symbol.strip().upper()
    normalized_address = token_address.strip().lower() if token_address else None
    matches = [
        position
        for position in portfolio.positions
        if position.symbol.strip().upper() == normalized_symbol
        and (
            position.token_address.strip().lower()
            if position.token_address is not None
            else None
        )
        == normalized_address
    ]
    if len(matches) > 1:
        raise ValueError("Verified portfolio contains duplicate asset positions.")
    return matches[0] if matches else None


def execute_research_portfolio_signal(
    signal: ResearchSignal,
    portfolio: VerifiedPortfolio,
    risk: RiskSnapshot,
    universe: GovernedAssetUniverse,
    backend: SwapBackend,
    *,
    decision_journal_path: Path,
    live_audit_path: Path,
    now: datetime | None = None,
    live_config: LiveTradingConfig | None = None,
    executor_config: ExecutorConfig | None = None,
    agent_commerce_research_gate: AgentCommerceResearchGate | None = None,
) -> ControlledLiveResult:
    """Turn one ranked research signal into one fully audited spot attempt.

    Research decides neither identity nor authority. This interface verifies the
    current universe and portfolio, applies a small deterministic momentum/exit
    policy, builds the exact intent and swap, then delegates authorization and
    submission to the controlled-live executor.
    """

    current_time = now or datetime.now(timezone.utc)
    if not _aware(current_time):
        return _policy_rejected("Portfolio evaluation time must include a timezone.")
    current_time = current_time.astimezone(timezone.utc)
    if not _aware(universe.observed_at):
        return _policy_rejected("Governed universe timestamp must include a timezone.")
    universe_age = current_time - universe.observed_at.astimezone(timezone.utc)
    if universe_age > MAX_SNAPSHOT_AGE:
        return _policy_rejected("Governed asset universe is stale.")
    if universe_age < -MAX_FUTURE_SKEW:
        return _policy_rejected("Governed asset universe is from the future.")
    for label, timestamp in (
        ("Research", signal.observed_at),
        ("Portfolio", portfolio.observed_at),
    ):
        if not _aware(timestamp):
            return _policy_rejected(f"{label} timestamp must include a timezone.")
        age = (current_time - timestamp.astimezone(timezone.utc)).total_seconds()
        if age > MAX_RESEARCH_AGE_SECONDS:
            return _policy_rejected(f"{label} data is stale.")
        if age < -MAX_FUTURE_SKEW_SECONDS:
            return _policy_rejected(f"{label} timestamp is too far in the future.")

    if portfolio.treasury_address.strip().lower() != AUTHORIZED_TREASURY_ADDRESS:
        return _policy_rejected("Verified portfolio belongs to the wrong treasury.")
    if (
        not _finite_positive(portfolio.total_value_usdc)
        or not portfolio.usdc_balance.is_finite()
        or portfolio.usdc_balance < 0
        or portfolio.usdc_balance > portfolio.total_value_usdc
    ):
        return _policy_rejected("Verified portfolio values are invalid.")
    verified_portfolio_value = (
        risk.portfolio_value_usdc
        if risk.portfolio_value_usdc is not None
        else risk.trading_capital_usdc
    )
    if verified_portfolio_value != portfolio.total_value_usdc:
        return _policy_rejected(
            "Risk snapshot and verified portfolio value do not match."
        )
    try:
        asset = universe.require(signal.symbol, signal.token_address)
        position = _position_for(
            portfolio,
            symbol=signal.symbol,
            token_address=signal.token_address,
        )
    except ValueError as error:
        return _policy_rejected(str(error))

    decimals = (
        signal.price_usd,
        signal.liquidity_usd,
        signal.daily_volume_usd,
        signal.change_h6_percent,
        signal.change_h24_percent,
    )
    if any(not value.is_finite() for value in decimals) or signal.price_usd <= 0:
        return _policy_rejected("Research signal contains invalid numeric values.")
    if (
        signal.liquidity_usd < MINIMUM_LIQUIDITY_USD
        or signal.daily_volume_usd < MINIMUM_DAILY_VOLUME_USD
        or signal.liquidity_usd < asset.liquidity_usd / Decimal("2")
    ):
        return _policy_rejected("Research liquidity or volume is below policy.")
    if (
        type(signal.buys_h24) is not int
        or type(signal.sells_h24) is not int
        or signal.buys_h24 < 0
        or signal.sells_h24 < 0
    ):
        return _policy_rejected("Research transaction counts are invalid.")

    current_position = position.value_usdc if position is not None else Decimal("0")
    buy_signal = (
        signal.change_h6_percent > 0
        and signal.change_h24_percent > 0
        and signal.buys_h24 > signal.sells_h24
    )
    stop_or_take_profit = bool(
        position is not None
        and _finite_positive(position.average_entry_price_usdc)
        and (
            signal.price_usd
            <= position.average_entry_price_usdc
            * (Decimal("1") - STOP_LOSS_PERCENT / Decimal("100"))
            or signal.price_usd
            >= position.average_entry_price_usdc
            * (Decimal("1") + TAKE_PROFIT_PERCENT / Decimal("100"))
        )
    )
    sell_signal = bool(
        position is not None
        and (
            (
                signal.change_h6_percent < 0
                and signal.change_h24_percent < 0
            )
            or stop_or_take_profit
        )
    )

    if sell_signal:
        if position is None:
            return _policy_rejected("Verified sell position is unavailable.")
        if (
            not _finite_positive(position.value_usdc)
            or not _finite_positive(position.token_balance)
        ):
            return _policy_rejected("Verified sell position is invalid.")
        side = "SELL"
        notional = min(MAX_TRADE_NOTIONAL_USDC, position.value_usdc).quantize(
            USDC_QUANTUM,
            rounding=ROUND_DOWN,
        )
        ratio = notional / position.value_usdc
        quantum = Decimal(1).scaleb(-asset.decimals)
        from_amount = (position.token_balance * ratio).quantize(
            quantum,
            rounding=ROUND_DOWN,
        )
        new_strategy = False
    elif buy_signal:
        position_limit = portfolio.total_value_usdc * Decimal("0.20")
        room = position_limit - current_position
        new_strategy = position is None
        new_strategy_limit = (
            portfolio.total_value_usdc * Decimal("0.05")
            if new_strategy
            else MAX_TRADE_NOTIONAL_USDC
        )
        notional = min(
            MAX_TRADE_NOTIONAL_USDC,
            portfolio.usdc_balance,
            room,
            new_strategy_limit,
        ).quantize(USDC_QUANTUM, rounding=ROUND_DOWN)
        if not _finite_positive(notional):
            return _policy_rejected("No portfolio capacity remains for this buy.")
        side = "BUY"
        from_amount = notional
    else:
        return _policy_rejected("Research signal does not meet buy or sell rules.")

    intent_seed = (
        f"{signal.packet_id}:{universe.snapshot_sha256}:{side}:"
        f"{asset.symbol}:{asset.token_address}"
    )
    intent_id = hashlib.sha256(intent_seed.encode()).hexdigest()
    paid_research_ref: str | None = None
    if agent_commerce_research_gate is not None:
        try:
            gate_decision = agent_commerce_research_gate.evaluate(
                candidate_for_trade(
                    symbol=asset.symbol,
                    token_address=asset.token_address or WETH_ADDRESS,
                    side=side,
                    trading_decision_id=intent_id,
                    requested_at=current_time,
                )
            )
        except (OSError, ResearchPolicyError, ValueError) as error:
            return _policy_rejected(
                f"Agent Commerce research audit or policy failed: {error}"
            )
        if not gate_decision.allowed:
            return _policy_rejected(
                f"Agent Commerce research vetoed candidate: {gate_decision.reason}"
            )
        if gate_decision.report is not None:
            paid_research_ref = f"agent-commerce-report:{gate_decision.report.report_id}"

    source_refs = [
        f"research:{signal.packet_id}",
        universe.snapshot_sha256,
        f"portfolio:{portfolio.observed_at.isoformat()}",
    ]
    if paid_research_ref is not None:
        source_refs.append(paid_research_ref)
    intent = TradeIntent(
        intent_id=intent_id,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        side=side,
        asset_symbol=asset.symbol,
        asset_token_address=asset.token_address,
        settlement_symbol="USDC",
        settlement_token_address=BASE_USDC_ADDRESS,
        notional_usdc=notional,
        current_position_usdc=current_position,
        treasury_value_usdc=portfolio.total_value_usdc,
        new_strategy=new_strategy,
        treasury_address=portfolio.treasury_address,
        recipient_address=portfolio.treasury_address,
        chain_id=BASE_MAINNET_CHAIN_ID,
        market_data_observed_at=signal.observed_at,
        created_at=current_time,
        source_refs=tuple(source_refs),
    )
    asset_token = asset.token_address or NATIVE_ETH_ADDRESS
    swap = ApprovedSwap(
        quote_id=f"research-{signal.packet_id[:32]}",
        quote_observed_at=signal.observed_at,
        route_id=ROUTE_ID,
        wallet_address=portfolio.treasury_address,
        chain_id=BASE_MAINNET_CHAIN_ID,
        from_token=BASE_USDC_ADDRESS if side == "BUY" else asset_token,
        to_token=asset_token if side == "BUY" else BASE_USDC_ADDRESS,
        from_amount=from_amount,
        from_decimals=6 if side == "BUY" else asset.decimals,
        to_decimals=asset.decimals if side == "BUY" else 6,
        notional_usdc=notional,
        slippage_bps=DEFAULT_SLIPPAGE_BPS,
    )
    return execute_controlled_live_trade(
        intent,
        risk,
        swap,
        backend,
        decision_journal_path=decision_journal_path,
        live_audit_path=live_audit_path,
        now=current_time,
        live_config=live_config,
        executor_config=executor_config,
        asset_universe=universe,
    )
