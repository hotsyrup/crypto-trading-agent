from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.paper_portfolio import PaperPortfolio
from app.paper_trader import STARTING_BALANCE


RISK_STATE_PATH = Path("data/paper_risk_state.json")
STATE_VERSION = 1
MAX_DAILY_LOSS_PERCENT = Decimal("5")
MAX_DRAWDOWN_PERCENT = Decimal("20")
PERCENT_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class RiskAccountingState:
    high_water_mark: Decimal
    daily_start_value: Decimal
    daily_date: date
    last_portfolio_value: Decimal
    updated_at: datetime


@dataclass(frozen=True)
class RiskAccountingDecision:
    ready: bool
    reason: str
    current_value: Decimal | None = None
    high_water_mark: Decimal | None = None
    daily_start_value: Decimal | None = None
    drawdown_percent: Decimal | None = None
    daily_loss_percent: Decimal | None = None
    daily_date: date | None = None
    drawdown_halt: bool = False
    daily_loss_halt: bool = False


def portfolio_value(
    portfolio: PaperPortfolio,
    reference_price: Decimal,
) -> Decimal:
    if not reference_price.is_finite() or reference_price <= 0:
        raise ValueError("Reference price must be finite and positive.")
    if not portfolio.usdc_balance.is_finite() or portfolio.usdc_balance < 0:
        raise ValueError("USDC balance must be finite and nonnegative.")
    if not portfolio.eth_balance.is_finite() or portfolio.eth_balance < 0:
        raise ValueError("ETH balance must be finite and nonnegative.")
    value = portfolio.usdc_balance + portfolio.eth_balance * reference_price
    if not value.is_finite() or value < 0:
        raise ValueError("Portfolio value must be finite and nonnegative.")
    return value


def _positive_decimal(data: dict[str, object], key: str) -> Decimal:
    try:
        value = Decimal(str(data[key]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Risk state field {key} is invalid.") from error
    if not value.is_finite() or value <= 0:
        raise ValueError(f"Risk state field {key} must be finite and positive.")
    return value


def _nonnegative_decimal(data: dict[str, object], key: str) -> Decimal:
    try:
        value = Decimal(str(data[key]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Risk state field {key} is invalid.") from error
    if not value.is_finite() or value < 0:
        raise ValueError(
            f"Risk state field {key} must be finite and nonnegative."
        )
    return value


def _parse_state(data: object) -> RiskAccountingState:
    if not isinstance(data, dict):
        raise ValueError("Risk state must be a JSON object.")
    if data.get("version") != STATE_VERSION:
        raise ValueError("Risk state version is unsupported.")

    try:
        daily_date = date.fromisoformat(str(data["daily_date"]))
        updated_at = datetime.fromisoformat(str(data["updated_at"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Risk state dates are invalid.") from error
    if updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise ValueError("Risk state update time must include a timezone.")

    high_water_mark = _positive_decimal(data, "high_water_mark")
    daily_start_value = _positive_decimal(data, "daily_start_value")
    last_portfolio_value = _nonnegative_decimal(
        data,
        "last_portfolio_value",
    )
    if high_water_mark < daily_start_value:
        raise ValueError("Risk state daily start exceeds its high-water mark.")
    if high_water_mark < last_portfolio_value:
        raise ValueError("Risk state portfolio value exceeds its high-water mark.")
    if updated_at.astimezone(timezone.utc).date() != daily_date:
        raise ValueError("Risk state date does not match its update time.")

    return RiskAccountingState(
        high_water_mark=high_water_mark,
        daily_start_value=daily_start_value,
        daily_date=daily_date,
        last_portfolio_value=last_portfolio_value,
        updated_at=updated_at,
    )


def _load_state() -> RiskAccountingState | None:
    if not RISK_STATE_PATH.exists():
        return None
    with RISK_STATE_PATH.open("r", encoding="utf-8") as state_file:
        return _parse_state(json.load(state_file))


def _save_state(state: RiskAccountingState) -> None:
    RISK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "high_water_mark": str(state.high_water_mark),
        "daily_start_value": str(state.daily_start_value),
        "daily_date": state.daily_date.isoformat(),
        "last_portfolio_value": str(state.last_portfolio_value),
        "updated_at": state.updated_at.isoformat(),
    }

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=RISK_STATE_PATH.parent,
            prefix=f".{RISK_STATE_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(payload, temporary_file, indent=2)
            temporary_file.write("\n")
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(RISK_STATE_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _loss_percent(start: Decimal, current: Decimal) -> Decimal:
    if current >= start:
        return Decimal("0")
    return ((start - current) / start * Decimal("100")).quantize(
        PERCENT_QUANTUM
    )


def evaluate_portfolio_risk(
    portfolio: PaperPortfolio,
    reference_price: Decimal,
    *,
    now: datetime | None = None,
) -> RiskAccountingDecision:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        return RiskAccountingDecision(
            ready=False,
            reason="Risk-accounting time must include a timezone.",
        )
    current_time = current_time.astimezone(timezone.utc)

    try:
        current_value = portfolio_value(portfolio, reference_price)
        state = _load_state()

        if state is None:
            state = RiskAccountingState(
                high_water_mark=max(STARTING_BALANCE, current_value),
                daily_start_value=current_value,
                daily_date=current_time.date(),
                last_portfolio_value=current_value,
                updated_at=current_time,
            )
        else:
            if current_time.date() < state.daily_date:
                raise ValueError("Risk-accounting clock moved before its saved day.")
            if current_time < state.updated_at.astimezone(timezone.utc):
                raise ValueError("Risk-accounting clock moved before its last update.")

            daily_start_value = state.daily_start_value
            if current_time.date() > state.daily_date:
                daily_start_value = state.last_portfolio_value

            state = RiskAccountingState(
                high_water_mark=max(state.high_water_mark, current_value),
                daily_start_value=daily_start_value,
                daily_date=current_time.date(),
                last_portfolio_value=current_value,
                updated_at=current_time,
            )

        drawdown_percent = _loss_percent(
            state.high_water_mark,
            current_value,
        )
        daily_loss_percent = _loss_percent(
            state.daily_start_value,
            current_value,
        )
        _save_state(state)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return RiskAccountingDecision(
            ready=False,
            reason=f"Risk accounting unavailable: {error}",
        )

    drawdown_halt = drawdown_percent >= MAX_DRAWDOWN_PERCENT
    daily_loss_halt = daily_loss_percent >= MAX_DAILY_LOSS_PERCENT
    if drawdown_halt:
        reason = "High-water-mark drawdown limit reached."
    elif daily_loss_halt:
        reason = "Daily loss limit reached; new positions are blocked."
    else:
        reason = "Portfolio risk accounting passed."

    return RiskAccountingDecision(
        ready=True,
        reason=reason,
        current_value=current_value,
        high_water_mark=state.high_water_mark,
        daily_start_value=state.daily_start_value,
        drawdown_percent=drawdown_percent,
        daily_loss_percent=daily_loss_percent,
        daily_date=state.daily_date,
        drawdown_halt=drawdown_halt,
        daily_loss_halt=daily_loss_halt,
    )
