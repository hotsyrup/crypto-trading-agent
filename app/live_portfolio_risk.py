from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.trading_executor import MAX_TRADING_CAPITAL_USDC, RiskSnapshot


LIVE_PORTFOLIO_RISK_PATH = Path("data/live_portfolio_risk.jsonl")
SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
PERCENT_QUANTUM = Decimal("0.0001")


class LivePortfolioRiskError(RuntimeError):
    pass


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise LivePortfolioRiskError(f"{label} is invalid.") from error
    if not parsed.is_finite() or parsed <= 0:
        raise LivePortfolioRiskError(f"{label} must be finite and positive.")
    return parsed


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise LivePortfolioRiskError("Risk timestamp is invalid.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LivePortfolioRiskError("Risk timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _loss_percent(start: Decimal, current: Decimal) -> Decimal:
    if current >= start:
        return Decimal("0.0000")
    return ((start - current) / start * Decimal("100")).quantize(PERCENT_QUANTUM)


def _load(lines: list[str]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    previous_hash = GENESIS_HASH
    previous_time: datetime | None = None
    previous_value: Decimal | None = None
    high_water: Decimal | None = None
    daily_start: Decimal | None = None
    daily_date = None
    authorized_capital: Decimal | None = None
    for sequence, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise LivePortfolioRiskError("Risk journal contains invalid JSON.") from error
        if not isinstance(entry, dict):
            raise LivePortfolioRiskError("Risk journal entry must be an object.")
        if (
            entry.get("schema_version") != SCHEMA_VERSION
            or entry.get("sequence") != sequence
            or entry.get("previous_hash") != previous_hash
        ):
            raise LivePortfolioRiskError("Risk journal sequence or chain is invalid.")
        stored_hash = entry.get("entry_hash")
        unsigned = dict(entry)
        unsigned.pop("entry_hash", None)
        if stored_hash != _hash(unsigned):
            raise LivePortfolioRiskError("Risk journal hash is invalid.")
        recorded_at = _timestamp(entry.get("recorded_at"))
        current = _decimal(entry.get("portfolio_value_usdc"), "Portfolio value")
        capital = _decimal(entry.get("authorized_capital_usdc"), "Authorized capital")
        if capital > MAX_TRADING_CAPITAL_USDC:
            raise LivePortfolioRiskError("Authorized capital exceeds $500.")
        if authorized_capital is not None and capital != authorized_capital:
            raise LivePortfolioRiskError("Authorized capital changed without reconciliation.")
        if previous_time is not None and recorded_at < previous_time:
            raise LivePortfolioRiskError("Risk-accounting clock moved backwards.")
        if high_water is None:
            high_water = current
            daily_start = current
            daily_date = recorded_at.date()
        else:
            high_water = max(high_water, current)
            if recorded_at.date() != daily_date:
                assert previous_value is not None
                daily_start = previous_value
                daily_date = recorded_at.date()
        assert daily_start is not None
        expected_drawdown = _loss_percent(high_water, current)
        expected_daily = _loss_percent(daily_start, current)
        if (
            _decimal(entry.get("high_water_mark_usdc"), "High-water mark")
            != high_water
            or _decimal(entry.get("daily_start_value_usdc"), "Daily start value")
            != daily_start
            or Decimal(str(entry.get("drawdown_percent"))) != expected_drawdown
            or Decimal(str(entry.get("daily_loss_percent"))) != expected_daily
        ):
            raise LivePortfolioRiskError("Risk journal transition is inconsistent.")
        previous_hash = str(stored_hash)
        previous_time = recorded_at
        previous_value = current
        authorized_capital = capital
        entries.append(entry)
    return entries


def record_live_portfolio_value(
    portfolio_value_usdc: Decimal,
    *,
    authorized_capital_usdc: Decimal,
    path: Path = LIVE_PORTFOLIO_RISK_PATH,
    now: datetime | None = None,
) -> RiskSnapshot:
    """Append a verified mark and return deterministic live risk percentages."""

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise LivePortfolioRiskError("Risk-accounting time must include a timezone.")
    current_time = current_time.astimezone(timezone.utc)
    current = _decimal(portfolio_value_usdc, "Portfolio value")
    capital = _decimal(authorized_capital_usdc, "Authorized capital")
    if capital > MAX_TRADING_CAPITAL_USDC:
        raise LivePortfolioRiskError("Authorized capital exceeds $500.")

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("a+", encoding="utf-8")
    except OSError as error:
        raise LivePortfolioRiskError("Risk journal is unavailable.") from error
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            entries = _load([line.strip() for line in handle if line.strip()])
            if entries:
                previous = entries[-1]
                previous_time = _timestamp(previous["recorded_at"])
                if current_time < previous_time:
                    raise LivePortfolioRiskError("Risk-accounting clock moved backwards.")
                previous_capital = _decimal(
                    previous["authorized_capital_usdc"],
                    "Authorized capital",
                )
                if capital != previous_capital:
                    raise LivePortfolioRiskError(
                        "Authorized capital changed without reconciliation."
                    )
                previous_value = _decimal(
                    previous["portfolio_value_usdc"],
                    "Portfolio value",
                )
                high_water = max(
                    _decimal(previous["high_water_mark_usdc"], "High-water mark"),
                    current,
                )
                daily_start = _decimal(
                    previous["daily_start_value_usdc"],
                    "Daily start value",
                )
                if current_time.date() != previous_time.date():
                    daily_start = previous_value
            else:
                high_water = current
                daily_start = current
            drawdown = _loss_percent(high_water, current)
            daily_loss = _loss_percent(daily_start, current)
            unsigned: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "sequence": len(entries) + 1,
                "previous_hash": (
                    str(entries[-1]["entry_hash"]) if entries else GENESIS_HASH
                ),
                "recorded_at": current_time.isoformat(),
                "authorized_capital_usdc": str(capital),
                "portfolio_value_usdc": str(current),
                "high_water_mark_usdc": str(high_water),
                "daily_start_value_usdc": str(daily_start),
                "drawdown_percent": str(drawdown),
                "daily_loss_percent": str(daily_loss),
            }
            unsigned["entry_hash"] = _hash(unsigned)
            handle.seek(0, 2)
            handle.write(_canonical(unsigned) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, ValueError, InvalidOperation) as error:
            if isinstance(error, LivePortfolioRiskError):
                raise
            raise LivePortfolioRiskError("Risk journal update failed.") from error
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return RiskSnapshot(
        daily_loss_percent=daily_loss,
        drawdown_percent=drawdown,
        observed_at=current_time,
        complete=True,
        contradictory=False,
        trading_capital_usdc=capital,
        portfolio_value_usdc=current,
    )
