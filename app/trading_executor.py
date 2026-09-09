from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.base_asset_universe import GovernedAssetUniverse
from app.live_asset_policy import evaluate_asset_identity
from app.live_trading_config import (
    BASE_USDC_ADDRESS,
    LiveTradingConfig,
    load_live_trading_config,
)


BASE_MAINNET_CHAIN_ID = 8453
AUTHORIZED_TREASURY_ADDRESS = (
    "0x716b5d6bf67a4c01103b52365c8fb5fdfef0ff06"
)
EXECUTOR_MODE_SHADOW_ONLY = "shadow_only"
EXECUTOR_MODE_CONTROLLED_LIVE = "controlled_live"
KILL_SWITCH_ARMED = "armed"
KILL_SWITCH_HALTED = "halted"
STATUS_REJECTED = "REJECTED"
STATUS_SHADOW_APPROVED = "SHADOW_APPROVED"
STATUS_CONTROLLED_LIVE_APPROVED = "CONTROLLED_LIVE_APPROVED"
STATUS_DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
STATUS_JOURNAL_FAILURE = "JOURNAL_FAILURE"
DEFAULT_MAX_DATA_AGE_SECONDS = 120
DEFAULT_MAX_FUTURE_SKEW_SECONDS = 30
MIN_DATA_AGE_SECONDS = 10
MAX_DATA_AGE_SECONDS = 3600
MAX_TRADE_NOTIONAL_USDC = Decimal("20")
MAX_TRADING_CAPITAL_USDC = Decimal("500")
ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}")


@dataclass(frozen=True)
class ExecutorConfig:
    mode: str
    kill_switch_state: str
    max_data_age_seconds: int
    max_future_skew_seconds: int


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    strategy_id: str
    strategy_version: str
    side: str
    asset_symbol: str
    asset_token_address: str | None
    settlement_symbol: str
    settlement_token_address: str | None
    notional_usdc: Decimal
    current_position_usdc: Decimal
    treasury_value_usdc: Decimal
    new_strategy: bool
    treasury_address: str
    recipient_address: str
    chain_id: int
    market_data_observed_at: datetime
    created_at: datetime
    source_refs: tuple[str, ...]
    unsolicited_asset: bool = False
    product: str = "spot"
    strategy_profile: str = "cautious_v1"
    entry_score: int | None = None
    exit_reason: str | None = None


@dataclass(frozen=True)
class RiskSnapshot:
    daily_loss_percent: Decimal
    drawdown_percent: Decimal
    observed_at: datetime
    complete: bool = True
    contradictory: bool = False
    trading_capital_usdc: Decimal | None = None
    portfolio_value_usdc: Decimal | None = None


@dataclass(frozen=True)
class ExecutionDecision:
    status: str
    reasons: tuple[str, ...]
    intent_id: str
    intent_fingerprint: str
    mode: str = EXECUTOR_MODE_SHADOW_ONLY
    executable: bool = False
    signing_authority: str = "none"

    @property
    def shadow_approved(self) -> bool:
        return self.status == STATUS_SHADOW_APPROVED


@dataclass(frozen=True)
class ExecutorRunResult:
    decision: ExecutionDecision
    journal_recorded: bool
    duplicate_blocked: bool
    journal_sequence: int | None
    ready_for_submission: bool = False


def _bounded_seconds(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if value < MIN_DATA_AGE_SECONDS or value > MAX_DATA_AGE_SECONDS:
        raise ValueError(
            f"{name} must be between {MIN_DATA_AGE_SECONDS} and "
            f"{MAX_DATA_AGE_SECONDS}."
        )
    return value


def load_executor_config() -> ExecutorConfig:
    mode = os.getenv("TRADING_EXECUTOR_MODE", EXECUTOR_MODE_SHADOW_ONLY)
    mode = mode.strip().lower()
    if mode not in {
        EXECUTOR_MODE_SHADOW_ONLY,
        EXECUTOR_MODE_CONTROLLED_LIVE,
    }:
        raise ValueError(
            "TRADING_EXECUTOR_MODE must be shadow_only or controlled_live."
        )

    kill_switch_state = os.getenv(
        "TRADING_EXECUTOR_KILL_SWITCH",
        KILL_SWITCH_HALTED,
    ).strip().lower()
    if kill_switch_state not in {KILL_SWITCH_ARMED, KILL_SWITCH_HALTED}:
        raise ValueError(
            "TRADING_EXECUTOR_KILL_SWITCH must be armed or halted."
        )

    return ExecutorConfig(
        mode=mode,
        kill_switch_state=kill_switch_state,
        max_data_age_seconds=_bounded_seconds(
            "TRADING_EXECUTOR_MAX_DATA_AGE_SECONDS",
            DEFAULT_MAX_DATA_AGE_SECONDS,
        ),
        max_future_skew_seconds=_bounded_seconds(
            "TRADING_EXECUTOR_MAX_FUTURE_SKEW_SECONDS",
            DEFAULT_MAX_FUTURE_SKEW_SECONDS,
        ),
    )


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _decimal_is_nonnegative(value: Decimal) -> bool:
    return value.is_finite() and value >= 0


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if _is_aware(value):
            return value.astimezone(timezone.utc).isoformat()
        return str(value)
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    return value


def intent_fingerprint(intent: TradeIntent) -> str:
    intent_payload = asdict(intent)
    if intent.strategy_profile == "cautious_v1":
        # Preserve every pre-profile fingerprint so rollback cannot turn a
        # previously journaled cautious intent into a content conflict.
        intent_payload.pop("strategy_profile", None)
        intent_payload.pop("entry_score", None)
        intent_payload.pop("exit_reason", None)
    payload = _canonical_value(intent_payload)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _age_reason(
    label: str,
    value: datetime,
    *,
    now: datetime,
    maximum_age: int,
    maximum_future_skew: int,
) -> str | None:
    if not _is_aware(value):
        return f"{label} timestamp must include a timezone."
    age_seconds = (now - value).total_seconds()
    if age_seconds < -maximum_future_skew:
        return f"{label} timestamp is too far in the future."
    if age_seconds > maximum_age:
        return f"{label} data is stale."
    return None


def _validated_percent(value: Decimal, label: str) -> str | None:
    if not _decimal_is_nonnegative(value) or value > Decimal("100"):
        return f"{label} must be a finite percentage from 0 through 100."
    return None


def evaluate_trade_intent(
    intent: TradeIntent,
    risk: RiskSnapshot,
    *,
    now: datetime | None = None,
    live_config: LiveTradingConfig | None = None,
    executor_config: ExecutorConfig | None = None,
    asset_universe: GovernedAssetUniverse | None = None,
) -> ExecutionDecision:
    """Evaluate a proposed governed Base spot trade without executing it.

    Shadow decisions remain non-executable. Controlled-live decisions can only
    become executable through the separately journaled CDP submission layer.
    """

    fingerprint = intent_fingerprint(intent)
    reasons: list[str] = []

    try:
        live = live_config or load_live_trading_config()
        executor = executor_config or load_executor_config()
    except ValueError as error:
        return ExecutionDecision(
            status=STATUS_REJECTED,
            reasons=(f"Executor configuration invalid: {error}",),
            intent_id=intent.intent_id,
            intent_fingerprint=fingerprint,
        )

    current_time = now or datetime.now(timezone.utc)
    if not _is_aware(current_time):
        reasons.append("Executor evaluation time must include a timezone.")
    else:
        current_time = current_time.astimezone(timezone.utc)
        for label, value in (
            ("Market", intent.market_data_observed_at),
            ("Risk", risk.observed_at),
            ("Intent", intent.created_at),
        ):
            age_reason = _age_reason(
                label,
                value,
                now=current_time,
                maximum_age=executor.max_data_age_seconds,
                maximum_future_skew=executor.max_future_skew_seconds,
            )
            if age_reason:
                reasons.append(age_reason)

    if executor.mode == EXECUTOR_MODE_SHADOW_ONLY and live.enabled:
        reasons.append("shadow_only requires LIVE_TRADING_ENABLED=false.")
    if executor.mode == EXECUTOR_MODE_CONTROLLED_LIVE and not live.enabled:
        reasons.append("controlled_live requires LIVE_TRADING_ENABLED=true.")
    if executor.kill_switch_state != KILL_SWITCH_ARMED:
        reasons.append("Trading executor kill switch is halted.")

    if not intent.intent_id.strip():
        reasons.append("Intent ID is required for replay protection.")
    if not intent.strategy_id.strip() or not intent.strategy_version.strip():
        reasons.append("Strategy ID and version are required.")
    if not intent.strategy_profile.strip():
        reasons.append("Strategy profile is required.")
    if (
        intent.entry_score is not None
        and (type(intent.entry_score) is not int or not 0 <= intent.entry_score <= 100)
    ):
        reasons.append("Entry score must be an integer from 0 through 100.")
    if not intent.source_refs or any(
        not isinstance(item, str) or not item.strip()
        for item in intent.source_refs
    ):
        reasons.append("At least one non-empty source reference is required.")

    side = intent.side.strip().upper()
    if side not in {"BUY", "SELL"}:
        reasons.append("Only BUY or SELL directions are supported.")
    if side == "BUY" and intent.exit_reason is not None:
        reasons.append("A buy intent must not carry an exit reason.")
    if side == "SELL" and intent.strategy_profile == "medium_high_v1" and (
        not isinstance(intent.exit_reason, str) or not intent.exit_reason.strip()
    ):
        reasons.append("A sell intent requires an exit reason.")
    if intent.product.strip().lower() != "spot":
        reasons.append("Only unleveraged spot execution is supported.")

    treasury = intent.treasury_address.strip().lower()
    recipient = intent.recipient_address.strip().lower()
    if not ADDRESS_PATTERN.fullmatch(treasury):
        reasons.append("Treasury address is invalid.")
    elif treasury != AUTHORIZED_TREASURY_ADDRESS:
        reasons.append("Treasury is outside the adopted mandate.")
    if not ADDRESS_PATTERN.fullmatch(recipient):
        reasons.append("Recipient address is invalid.")
    elif recipient != treasury:
        reasons.append("Trade proceeds must return to the authorized treasury.")
    if intent.chain_id != BASE_MAINNET_CHAIN_ID:
        reasons.append("Only Base mainnet chain ID 8453 is authorized.")

    asset_decision = evaluate_asset_identity(
        symbol=intent.asset_symbol,
        token_address=intent.asset_token_address,
        unsolicited=intent.unsolicited_asset,
        config=live,
        universe=asset_universe,
    )
    if not asset_decision.allowed:
        reasons.append(asset_decision.reason)
    settlement_decision = evaluate_asset_identity(
        symbol=intent.settlement_symbol,
        token_address=intent.settlement_token_address,
        unsolicited=False,
        config=live,
        universe=asset_universe,
    )
    if not settlement_decision.allowed:
        reasons.append(f"Settlement asset rejected: {settlement_decision.reason}")
    if asset_universe is None and intent.asset_symbol.strip().upper() != "ETH":
        reasons.append("The legacy executor supports ETH as the traded asset.")
    if intent.asset_symbol.strip().upper() == "USDC":
        reasons.append("USDC is the settlement asset and cannot be traded into itself.")
    if intent.settlement_symbol.strip().upper() != "USDC":
        reasons.append("This first executor supports USDC settlement only.")
    if (
        intent.settlement_token_address is None
        or intent.settlement_token_address.strip().lower() != BASE_USDC_ADDRESS
    ):
        reasons.append("Settlement must use the official Base USDC contract.")

    for value, label in (
        (intent.notional_usdc, "Notional"),
        (intent.current_position_usdc, "Current position"),
        (intent.treasury_value_usdc, "Treasury value"),
    ):
        if not _decimal_is_nonnegative(value):
            reasons.append(f"{label} must be finite and nonnegative.")
    if intent.notional_usdc.is_finite() and intent.notional_usdc <= 0:
        reasons.append("Notional must be greater than zero.")
    if (
        intent.notional_usdc.is_finite()
        and intent.notional_usdc > MAX_TRADE_NOTIONAL_USDC
    ):
        reasons.append("Notional exceeds the absolute $20 per-trade limit.")
    if (
        intent.treasury_value_usdc.is_finite()
        and intent.treasury_value_usdc <= 0
    ):
        reasons.append("Treasury value must be greater than zero.")
    if (
        executor.mode == EXECUTOR_MODE_CONTROLLED_LIVE
        and asset_universe is None
        and side != "SELL"
    ):
        reasons.append(
            "The only controlled-live route is native Base ETH to official Base USDC."
        )
    if (
        asset_universe is not None
        and asset_universe.snapshot_sha256 not in intent.source_refs
    ):
        reasons.append("Trade intent is not bound to the governed universe snapshot.")

    daily_reason = _validated_percent(risk.daily_loss_percent, "Daily loss")
    drawdown_reason = _validated_percent(risk.drawdown_percent, "Drawdown")
    if daily_reason:
        reasons.append(daily_reason)
    if drawdown_reason:
        reasons.append(drawdown_reason)
    if not risk.complete:
        reasons.append("Risk snapshot is incomplete.")
    if risk.contradictory:
        reasons.append("Risk snapshot is contradictory.")
    if executor.mode == EXECUTOR_MODE_CONTROLLED_LIVE:
        if risk.trading_capital_usdc is None:
            reasons.append(
                "Controlled-live risk snapshot requires verified trading capital."
            )
        elif (
            not risk.trading_capital_usdc.is_finite()
            or risk.trading_capital_usdc <= 0
            or risk.trading_capital_usdc > MAX_TRADING_CAPITAL_USDC
        ):
            reasons.append(
                "Verified trading capital must be positive and at most $500."
            )
        verified_portfolio_value = (
            risk.portfolio_value_usdc
            if risk.portfolio_value_usdc is not None
            else risk.trading_capital_usdc
        )
        if (
            verified_portfolio_value is None
            or not verified_portfolio_value.is_finite()
            or verified_portfolio_value <= 0
        ):
            reasons.append("Controlled-live risk snapshot requires portfolio value.")
        elif verified_portfolio_value != intent.treasury_value_usdc:
            reasons.append(
                "Trade intent treasury value does not match verified portfolio value."
            )

    if not reasons:
        position_limit = (
            intent.treasury_value_usdc
            * live.max_position_percent
            / Decimal("100")
        )
        new_strategy_limit = (
            intent.treasury_value_usdc
            * live.max_new_strategy_percent
            / Decimal("100")
        )
        if risk.drawdown_percent >= live.max_drawdown_percent:
            reasons.append("Treasury drawdown halt has been reached.")
        elif (
            side == "BUY"
            and risk.daily_loss_percent >= live.max_daily_loss_percent
        ):
            reasons.append("Daily loss limit blocks new positions.")

        if side == "BUY":
            if intent.current_position_usdc + intent.notional_usdc > position_limit:
                reasons.append("The resulting position would exceed the 20% limit.")
            if intent.new_strategy and intent.notional_usdc > new_strategy_limit:
                reasons.append("New-strategy allocation would exceed the 5% limit.")
        elif side == "SELL" and intent.notional_usdc > intent.current_position_usdc:
            reasons.append("Sell notional exceeds the current position value.")

    approved_status = (
        STATUS_CONTROLLED_LIVE_APPROVED
        if executor.mode == EXECUTOR_MODE_CONTROLLED_LIVE
        else STATUS_SHADOW_APPROVED
    )
    return ExecutionDecision(
        status=STATUS_REJECTED if reasons else approved_status,
        reasons=tuple(reasons) if reasons else (
            "Intent passed the controlled-live deterministic policy."
            if executor.mode == EXECUTOR_MODE_CONTROLLED_LIVE
            else "Intent passed the shadow-only deterministic policy.",
        ),
        intent_id=intent.intent_id,
        intent_fingerprint=fingerprint,
        mode=executor.mode,
        executable=(
            not reasons and executor.mode == EXECUTOR_MODE_CONTROLLED_LIVE
        ),
        signing_authority=(
            "cdp_agentkit"
            if not reasons and executor.mode == EXECUTOR_MODE_CONTROLLED_LIVE
            else "none"
        ),
    )


def process_shadow_trade_intent(
    intent: TradeIntent,
    risk: RiskSnapshot,
    *,
    journal_path: Path,
    now: datetime | None = None,
    live_config: LiveTradingConfig | None = None,
    executor_config: ExecutorConfig | None = None,
    asset_universe: GovernedAssetUniverse | None = None,
) -> ExecutorRunResult:
    """Evaluate and durably record one shadow intent, blocking replay.

    This is the only composed entry point in the first executor layer. It
    still has no signer or submission path, and ``ready_for_submission`` is
    intentionally fixed to ``False``.
    """

    from app.execution_journal import (
        JournalIntegrityError,
        append_execution_decision,
    )

    decision = evaluate_trade_intent(
        intent,
        risk,
        now=now,
        live_config=live_config,
        executor_config=executor_config,
        asset_universe=asset_universe,
    )
    try:
        journal = append_execution_decision(
            decision,
            path=journal_path,
            recorded_at=now,
        )
    except (JournalIntegrityError, OSError, ValueError) as error:
        failed = ExecutionDecision(
            status=STATUS_JOURNAL_FAILURE,
            reasons=(f"Execution journal unavailable: {error}",),
            intent_id=decision.intent_id,
            intent_fingerprint=decision.intent_fingerprint,
        )
        return ExecutorRunResult(
            decision=failed,
            journal_recorded=False,
            duplicate_blocked=False,
            journal_sequence=None,
        )

    if journal.duplicate:
        duplicate = ExecutionDecision(
            status=STATUS_DUPLICATE_BLOCKED,
            reasons=("Intent ID was already recorded; replay blocked.",),
            intent_id=decision.intent_id,
            intent_fingerprint=decision.intent_fingerprint,
        )
        return ExecutorRunResult(
            decision=duplicate,
            journal_recorded=False,
            duplicate_blocked=True,
            journal_sequence=journal.sequence,
        )

    return ExecutorRunResult(
        decision=decision,
        journal_recorded=True,
        duplicate_blocked=False,
        journal_sequence=journal.sequence,
    )
