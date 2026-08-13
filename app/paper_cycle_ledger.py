"""Append-only, idempotent source of truth for paper acceptance cycles."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


LEDGER_PATH = Path("data/paper_cycle_ledger_v2.jsonl")
LOCK_PATH = Path("data/paper_cycle_ledger_v2.lock")
SCHEMA_VERSION = 2
STRATEGY_ID = "eth_usd_sma_3_5"
STRATEGY_VERSION = "1.0.0"
ACCEPTANCE_POLICY_VERSION = "2.0.0"
TARGET_DAYS = 7
TARGET_SIGNALS = 50
MIN_DAILY_CYCLES = 20
STARTING_USDC = Decimal("10000.00")
STARTING_ETH = Decimal("0")


class LedgerConflictError(ValueError):
    """Raised when a concurrent or stale cycle attempts an inconsistent append."""


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _entry_hash(entry: dict[str, object]) -> str:
    unsigned = {key: value for key, value in entry.items() if key != "entry_hash"}
    return hashlib.sha256(_canonical(unsigned).encode()).hexdigest()


def _portfolio(payload: object, field: str) -> tuple[Decimal, Decimal]:
    if not isinstance(payload, dict):
        raise ValueError(f"Ledger {field} must be an object.")
    try:
        usdc = Decimal(str(payload["usdc_balance"]))
        eth = Decimal(str(payload["eth_balance"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Ledger {field} balances are invalid.") from error
    if not usdc.is_finite() or not eth.is_finite() or usdc < 0 or eth < 0:
        raise ValueError(f"Ledger {field} balances must be finite and nonnegative.")
    return usdc, eth


def _load_unlocked() -> list[dict[str, Any]]:
    if not LEDGER_PATH.exists():
        return []
    records: list[dict[str, Any]] = []
    previous_hash = "0" * 64
    previous_portfolio = (STARTING_USDC, STARTING_ETH)
    cycle_ids: set[str] = set()
    with LEDGER_PATH.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                raise ValueError(f"Paper cycle ledger line {line_number} is incomplete.")
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Paper cycle ledger line {line_number} is invalid JSON."
                ) from error
            if not isinstance(entry, dict):
                raise ValueError(f"Paper cycle ledger line {line_number} is not an object.")
            if entry.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("Paper cycle ledger schema is unsupported.")
            if entry.get("sequence") != line_number:
                raise ValueError("Paper cycle ledger sequence is invalid.")
            if entry.get("previous_hash") != previous_hash:
                raise ValueError("Paper cycle ledger hash chain is invalid.")
            if entry.get("entry_hash") != _entry_hash(entry):
                raise ValueError("Paper cycle ledger entry hash is invalid.")
            cycle_id = str(entry.get("cycle_id", ""))
            if len(cycle_id) != 64 or cycle_id in cycle_ids:
                raise ValueError("Paper cycle ledger contains an invalid duplicate cycle ID.")
            before = _portfolio(entry.get("portfolio_before"), "portfolio_before")
            after = _portfolio(entry.get("portfolio_after"), "portfolio_after")
            if before != previous_portfolio:
                raise ValueError("Paper cycle ledger portfolio continuity is invalid.")
            cycle_ids.add(cycle_id)
            previous_hash = str(entry["entry_hash"])
            previous_portfolio = after
            records.append(entry)
    return records


def read_ledger() -> list[dict[str, Any]]:
    return _load_unlocked()


def current_portfolio() -> tuple[Decimal, Decimal]:
    records = _load_unlocked()
    if not records:
        return STARTING_USDC, STARTING_ETH
    return _portfolio(records[-1]["portfolio_after"], "portfolio_after")


def make_signal_id(
    *,
    signal: str,
    reference_price: Decimal,
    market_data_observed_at: datetime | None,
) -> str:
    if market_data_observed_at is None:
        raise ValueError("A market observation timestamp is required for a cycle ID.")
    if market_data_observed_at.tzinfo is None or market_data_observed_at.utcoffset() is None:
        raise ValueError("Market observation timestamp must include a timezone.")
    payload = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "signal": signal,
        "reference_price": str(reference_price),
        "market_data_observed_at": market_data_observed_at.astimezone(timezone.utc).isoformat(),
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _acceptance_summary(
    records: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, object]:
    credited = [record for record in records if record.get("acceptance_credit_enabled") is True]
    eligible_signal_ids = {
        str(record["signal_id"])
        for record in credited
        if record.get("paper_eligible") is True
    }
    per_day: dict[date, list[dict[str, Any]]] = {}
    for record in credited:
        recorded_at = datetime.fromisoformat(str(record["recorded_at"])).astimezone(timezone.utc)
        per_day.setdefault(recorded_at.date(), []).append(record)

    today = now.astimezone(timezone.utc).date()
    qualifying_days = {
        day
        for day, day_records in per_day.items()
        if day < today
        and len(day_records) >= MIN_DAILY_CYCLES
        and all(record.get("system_healthy") is True for record in day_records)
    }
    consecutive_days = 0
    cursor = today - timedelta(days=1)
    while cursor in qualifying_days:
        consecutive_days += 1
        cursor -= timedelta(days=1)

    signal_complete = len(eligible_signal_ids) >= TARGET_SIGNALS
    day_complete = consecutive_days >= TARGET_DAYS
    complete = signal_complete or day_complete
    if signal_complete:
        completion_reason = "50_UNIQUE_ELIGIBLE_SIGNALS"
    elif day_complete:
        completion_reason = "7_CONSECUTIVE_QUALIFYING_UTC_DAYS"
    else:
        completion_reason = None
    latest = credited[-1] if credited else None
    return {
        "policy_version": ACCEPTANCE_POLICY_VERSION,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "pre_fix_credit": "INVALIDATED",
        "credit_enabled": bool(credited),
        "credited_cycles": len(credited),
        "unique_eligible_signals": len(eligible_signal_ids),
        "qualifying_utc_days": len(qualifying_days),
        "consecutive_qualifying_utc_days": consecutive_days,
        "minimum_cycles_per_qualifying_day": MIN_DAILY_CYCLES,
        "paused": bool(latest and latest.get("system_healthy") is not True),
        "complete": complete,
        "completion_reason": completion_reason,
    }


def commit_cycle(payload: dict[str, object]) -> tuple[dict[str, Any], dict[str, object], bool]:
    """Append exactly one cycle, returning entry, acceptance summary, and duplicate."""
    cycle_id = str(payload.get("cycle_id", ""))
    signal_id = str(payload.get("signal_id", ""))
    if len(cycle_id) != 64 or cycle_id != signal_id:
        raise ValueError("Cycle and signal IDs must be the same stable SHA-256 value.")
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        records = _load_unlocked()
        for record in records:
            if record["cycle_id"] == cycle_id:
                return record, _acceptance_summary(records, now=datetime.now(timezone.utc)), True

        expected_before = (
            (STARTING_USDC, STARTING_ETH)
            if not records
            else _portfolio(records[-1]["portfolio_after"], "portfolio_after")
        )
        supplied_before = _portfolio(payload.get("portfolio_before"), "portfolio_before")
        _portfolio(payload.get("portfolio_after"), "portfolio_after")
        if supplied_before != expected_before:
            raise LedgerConflictError(
                "Paper cycle was computed from a stale portfolio; no entry was appended."
            )

        entry: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "sequence": len(records) + 1,
            "previous_hash": records[-1]["entry_hash"] if records else "0" * 64,
            **payload,
        }
        prospective = records + [entry]
        entry["acceptance"] = _acceptance_summary(
            prospective,
            now=datetime.fromisoformat(str(entry["recorded_at"])),
        )
        entry["entry_hash"] = _entry_hash(entry)
        encoded = json.dumps(entry, sort_keys=True) + "\n"
        with LEDGER_PATH.open("a", encoding="utf-8") as ledger_handle:
            ledger_handle.write(encoded)
            ledger_handle.flush()
            os.fsync(ledger_handle.fileno())
        records.append(entry)
        return entry, _acceptance_summary(records, now=datetime.now(timezone.utc)), False


def ledger_status() -> dict[str, object]:
    records = _load_unlocked()
    return {
        "ledger_schema": SCHEMA_VERSION,
        "ledger_entries": len(records),
        "ledger_head": records[-1]["entry_hash"] if records else None,
        "portfolio": {
            "usdc_balance": str(current_portfolio()[0]),
            "eth_balance": str(current_portfolio()[1]),
        },
        "acceptance": _acceptance_summary(records, now=datetime.now(timezone.utc)),
    }
