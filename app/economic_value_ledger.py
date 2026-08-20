"""Append-only economic-value events for agent work and external calls.

The ledger records metadata and hashes, never credentials or full provider
payloads. A request, its result, and later use are separate immutable events so
"used" is evidence rather than a field that can be silently overwritten.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
LEDGER_PATH = Path("data/economic_value_ledger_v1.jsonl")
LOCK_PATH = Path("data/economic_value_ledger_v1.lock")
GENESIS_HASH = "0" * 64
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EVENT_FIELDS = {
    "request_recorded": {
        "request_id", "requested_by_agent", "provider", "purpose",
        "cost", "cost_currency", "cost_status",
    },
    "result_recorded": {"request_id", "result_id", "output_type", "output_hash", "outcome"},
    "usage_recorded": {"request_id", "result_id", "used_by_agent", "use_type", "use_reference"},
}


def _timestamp(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Economic-value timestamps must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat()


def _identifier(value: object, field: str) -> str:
    text = str(value)
    if not ID_PATTERN.fullmatch(text):
        raise ValueError(f"{field} is not a bounded identifier.")
    return text


def _cost(value: object, status: str) -> str | None:
    if status not in {"known", "estimated", "unknown"}:
        raise ValueError("cost_status is invalid.")
    if status == "unknown":
        if value is not None:
            raise ValueError("Unknown cost must be null.")
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("cost must be numeric when known or estimated.") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("cost must be finite and nonnegative.")
    return format(amount, "f")


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _event_hash(event: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    return hashlib.sha256(_canonical(unsigned).encode()).hexdigest()


def _validate_event(event: object, *, sequence: int, previous_hash: str) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("Economic-value ledger event is not an object.")
    event_type = event.get("event_type")
    detail_fields = EVENT_FIELDS.get(str(event_type))
    if detail_fields is None:
        raise ValueError("Economic-value event type is invalid.")
    required = {
        "schema_version", "sequence", "event_id", "recorded_at", "event_type",
        "details", "previous_hash", "event_hash",
    }
    if set(event) != required or set(event.get("details", {})) != detail_fields:
        raise ValueError("Economic-value event fields do not match the strict schema.")
    if event["schema_version"] != SCHEMA_VERSION or event["sequence"] != sequence:
        raise ValueError("Economic-value ledger sequence or schema is invalid.")
    _identifier(event["event_id"], "event_id")
    _timestamp(str(event["recorded_at"]))
    if event["previous_hash"] != previous_hash or event["event_hash"] != _event_hash(event):
        raise ValueError("Economic-value ledger hash chain is invalid.")
    return event


def read_ledger(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or LEDGER_PATH
    if not target.exists():
        return []
    events: list[dict[str, Any]] = []
    previous_hash = GENESIS_HASH
    with target.open(encoding="utf-8") as handle:
        for sequence, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                raise ValueError("Economic-value ledger contains an incomplete line.")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError("Economic-value ledger contains invalid JSON.") from error
            events.append(_validate_event(event, sequence=sequence, previous_hash=previous_hash))
            previous_hash = event["event_hash"]
    return events


def append_event(
    event_type: str,
    details: dict[str, Any],
    *,
    event_id: str,
    recorded_at: str | None = None,
    path: Path | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    target = path or LEDGER_PATH
    lock_target = lock_path or LOCK_PATH
    if event_type not in EVENT_FIELDS or set(details) != EVENT_FIELDS[event_type]:
        raise ValueError("Economic-value event details do not match the strict schema.")
    normalized = dict(details)
    normalized["request_id"] = _identifier(details["request_id"], "request_id")
    if event_type == "request_recorded":
        normalized["requested_by_agent"] = _identifier(details["requested_by_agent"], "requested_by_agent")
        normalized["cost"] = _cost(details["cost"], str(details["cost_status"]))
        if details["cost_currency"] != "USD":
            raise ValueError("Initial economic-value ledger currency must be USD.")
    else:
        normalized["result_id"] = _identifier(details["result_id"], "result_id")
    if event_type == "result_recorded" and not HASH_PATTERN.fullmatch(str(details["output_hash"])):
        raise ValueError("output_hash must be a SHA-256 digest.")
    if event_type == "usage_recorded":
        normalized["used_by_agent"] = _identifier(details["used_by_agent"], "used_by_agent")
        if details["use_type"] not in {"considered", "cited", "input_to_decision", "discarded"}:
            raise ValueError("use_type is invalid.")

    target.parent.mkdir(parents=True, exist_ok=True)
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    with lock_target.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        events = read_ledger(target)
        event = {
            "schema_version": SCHEMA_VERSION,
            "sequence": len(events) + 1,
            "event_id": _identifier(event_id, "event_id"),
            "recorded_at": _timestamp(recorded_at),
            "event_type": event_type,
            "details": normalized,
            "previous_hash": events[-1]["event_hash"] if events else GENESIS_HASH,
        }
        event["event_hash"] = _event_hash(event)
        encoded = _canonical(event) + "\n"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return event
