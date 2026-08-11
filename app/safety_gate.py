from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from app.trading_cycle import TradeProposal


DEFAULT_MAX_MARKET_DATA_AGE_SECONDS = 7200
DEFAULT_MAX_FUTURE_SKEW_SECONDS = 60
MIN_MAX_AGE_SECONDS = 60
MAX_MAX_AGE_SECONDS = 86400
KILL_SWITCH_ARMED = "armed"
KILL_SWITCH_HALTED = "halted"


@dataclass(frozen=True)
class SafetyGateDecision:
    allowed: bool
    reason: str
    kill_switch_state: str
    market_data_age_seconds: int | None = None


def _bounded_seconds(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error

    if value < MIN_MAX_AGE_SECONDS or value > MAX_MAX_AGE_SECONDS:
        raise ValueError(
            f"{name} must be between {MIN_MAX_AGE_SECONDS} and "
            f"{MAX_MAX_AGE_SECONDS}."
        )
    return value


def _kill_switch_state() -> str:
    state = os.getenv("PAPER_KILL_SWITCH", KILL_SWITCH_HALTED).strip().lower()
    if state not in {KILL_SWITCH_ARMED, KILL_SWITCH_HALTED}:
        raise ValueError("PAPER_KILL_SWITCH must be armed or halted.")
    return state


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def evaluate_safety_gate(
    proposal: TradeProposal,
    *,
    now: datetime | None = None,
) -> SafetyGateDecision:
    try:
        kill_switch_state = _kill_switch_state()
        maximum_age = _bounded_seconds(
            "PAPER_MAX_MARKET_DATA_AGE_SECONDS",
            DEFAULT_MAX_MARKET_DATA_AGE_SECONDS,
        )
        future_skew = _bounded_seconds(
            "PAPER_MAX_FUTURE_SKEW_SECONDS",
            DEFAULT_MAX_FUTURE_SKEW_SECONDS,
        )
    except ValueError as error:
        return SafetyGateDecision(
            allowed=False,
            reason=f"Safety configuration invalid: {error}",
            kill_switch_state=KILL_SWITCH_HALTED,
        )

    if kill_switch_state != KILL_SWITCH_ARMED:
        return SafetyGateDecision(
            allowed=False,
            reason="Paper kill switch is halted.",
            kill_switch_state=kill_switch_state,
        )

    if proposal.market_data_observed_at is None:
        return SafetyGateDecision(
            allowed=False,
            reason="Market-data timestamp is unavailable.",
            kill_switch_state=kill_switch_state,
        )
    if not _is_aware(proposal.market_data_observed_at):
        return SafetyGateDecision(
            allowed=False,
            reason="Market-data timestamp must include a timezone.",
            kill_switch_state=kill_switch_state,
        )

    current_time = now or datetime.now(timezone.utc)
    if not _is_aware(current_time):
        return SafetyGateDecision(
            allowed=False,
            reason="Safety evaluation time must include a timezone.",
            kill_switch_state=kill_switch_state,
        )

    age_seconds = int(
        (current_time - proposal.market_data_observed_at).total_seconds()
    )
    if age_seconds < -future_skew:
        return SafetyGateDecision(
            allowed=False,
            reason="Market-data timestamp is too far in the future.",
            kill_switch_state=kill_switch_state,
            market_data_age_seconds=age_seconds,
        )
    if age_seconds > maximum_age:
        return SafetyGateDecision(
            allowed=False,
            reason="Market data is stale.",
            kill_switch_state=kill_switch_state,
            market_data_age_seconds=age_seconds,
        )

    return SafetyGateDecision(
        allowed=True,
        reason="Paper safety gate passed.",
        kill_switch_state=kill_switch_state,
        market_data_age_seconds=max(age_seconds, 0),
    )
