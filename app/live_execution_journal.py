from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


LIVE_EXECUTION_JOURNAL_PATH = Path("data/live_execution_audit.jsonl")
SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
MAX_DAILY_EXECUTED_USDC = Decimal("100")


class LiveExecutionJournalError(RuntimeError):
    pass


class DailyExecutionLimitError(LiveExecutionJournalError):
    pass


@dataclass(frozen=True)
class LiveReservation:
    recorded: bool
    duplicate: bool
    sequence: int
    reserved_daily_usdc: Decimal


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _validate(lines: list[str]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    previous_hash = GENESIS_HASH
    for expected_sequence, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise LiveExecutionJournalError(
                "Live execution journal contains invalid JSON."
            ) from error
        if not isinstance(entry, dict):
            raise LiveExecutionJournalError(
                "Live execution journal entry must be an object."
            )
        if entry.get("schema_version") != SCHEMA_VERSION:
            raise LiveExecutionJournalError(
                "Live execution journal schema is unsupported."
            )
        if entry.get("sequence") != expected_sequence:
            raise LiveExecutionJournalError(
                "Live execution journal sequence is invalid."
            )
        if entry.get("previous_hash") != previous_hash:
            raise LiveExecutionJournalError(
                "Live execution journal hash chain is broken."
            )
        stored_hash = entry.get("entry_hash")
        unsigned = dict(entry)
        unsigned.pop("entry_hash", None)
        if stored_hash != _hash(unsigned):
            raise LiveExecutionJournalError(
                "Live execution journal entry hash is invalid."
            )
        previous_hash = str(stored_hash)
        entries.append(entry)
    return entries


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Live execution journal timestamp must include a timezone.")
    return value.astimezone(timezone.utc)


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise LiveExecutionJournalError(f"{label} is not a decimal.") from error
    if not parsed.is_finite() or parsed <= 0:
        raise LiveExecutionJournalError(f"{label} must be finite and positive.")
    return parsed


def _append_locked(
    handle,
    entries: list[dict[str, object]],
    payload: dict[str, object],
) -> int:
    sequence = len(entries) + 1
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "previous_hash": (
            str(entries[-1]["entry_hash"]) if entries else GENESIS_HASH
        ),
        **payload,
    }
    unsigned["entry_hash"] = _hash(unsigned)
    handle.seek(0, 2)
    handle.write(_canonical(unsigned) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    return sequence


def reserve_live_execution(
    *,
    intent_id: str,
    intent_fingerprint: str,
    notional_usdc: Decimal,
    route_id: str,
    wallet_address: str,
    chain_id: int,
    quote_id: str,
    quote_observed_at: datetime,
    from_token: str,
    to_token: str,
    from_amount: Decimal,
    from_decimals: int,
    to_decimals: int,
    slippage_bps: int,
    path: Path = LIVE_EXECUTION_JOURNAL_PATH,
    recorded_at: datetime | None = None,
) -> LiveReservation:
    """Atomically reserve daily capacity before any backend submission.

    Reservations are never automatically released. Failed or ambiguous calls
    continue consuming the daily ceiling until explicitly reconciled, which
    prevents retries and provider timeouts from bypassing the hard limit.
    """

    timestamp = _aware_utc(recorded_at or datetime.now(timezone.utc))
    quote_timestamp = _aware_utc(quote_observed_at)
    requested = _decimal(notional_usdc, "Reservation notional")
    requested_input = _decimal(from_amount, "Reservation input amount")
    if type(from_decimals) is not int or not 0 <= from_decimals <= 36:
        raise LiveExecutionJournalError("Input token decimals are invalid.")
    if type(to_decimals) is not int or not 0 <= to_decimals <= 36:
        raise LiveExecutionJournalError("Output token decimals are invalid.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            entries = _validate([line.strip() for line in handle if line.strip()])
            for entry in entries:
                if entry.get("event") != "RESERVED":
                    continue
                if entry.get("intent_id") != intent_id:
                    continue
                if entry.get("intent_fingerprint") != intent_fingerprint:
                    raise LiveExecutionJournalError(
                        "Intent ID was reused with different live trade content."
                    )
                reserved = sum(
                    _decimal(item["notional_usdc"], "Reserved notional")
                    for item in entries
                    if item.get("event") == "RESERVED"
                    and str(item.get("recorded_at", ""))[:10]
                    == timestamp.date().isoformat()
                )
                return LiveReservation(
                    recorded=False,
                    duplicate=True,
                    sequence=int(entry["sequence"]),
                    reserved_daily_usdc=reserved,
                )

            reserved = sum(
                _decimal(entry["notional_usdc"], "Reserved notional")
                for entry in entries
                if entry.get("event") == "RESERVED"
                and str(entry.get("recorded_at", ""))[:10]
                == timestamp.date().isoformat()
            )
            if reserved + requested > MAX_DAILY_EXECUTED_USDC:
                raise DailyExecutionLimitError(
                    "Execution would exceed the absolute $100 UTC daily limit."
                )
            sequence = _append_locked(
                handle,
                entries,
                {
                    "event": "RESERVED",
                    "recorded_at": timestamp.isoformat(),
                    "intent_id": intent_id,
                    "intent_fingerprint": intent_fingerprint,
                    "notional_usdc": str(requested),
                    "route_id": route_id,
                    "wallet_address": wallet_address,
                    "chain_id": chain_id,
                    "quote_id": quote_id,
                    "quote_observed_at": quote_timestamp.isoformat(),
                    "from_token": from_token,
                    "to_token": to_token,
                    "from_amount": str(requested_input),
                    "from_decimals": from_decimals,
                    "to_decimals": to_decimals,
                    "slippage_bps": slippage_bps,
                },
            )
            return LiveReservation(
                recorded=True,
                duplicate=False,
                sequence=sequence,
                reserved_daily_usdc=reserved + requested,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_live_execution_event(
    *,
    event: str,
    intent_id: str,
    intent_fingerprint: str,
    details: dict[str, object],
    path: Path = LIVE_EXECUTION_JOURNAL_PATH,
    recorded_at: datetime | None = None,
) -> int:
    if event not in {"CONFIRMED", "BACKEND_FAILED", "RECEIPT_REJECTED"}:
        raise ValueError("Unsupported live execution event.")
    timestamp = _aware_utc(recorded_at or datetime.now(timezone.utc))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            entries = _validate([line.strip() for line in handle if line.strip()])
            reservations = [
                item
                for item in entries
                if item.get("event") == "RESERVED"
                and item.get("intent_id") == intent_id
            ]
            if len(reservations) != 1:
                raise LiveExecutionJournalError(
                    "Exactly one live reservation is required before an outcome."
                )
            if reservations[0].get("intent_fingerprint") != intent_fingerprint:
                raise LiveExecutionJournalError(
                    "Live outcome fingerprint does not match its reservation."
                )
            if any(
                item.get("intent_id") == intent_id
                and item.get("event") in {
                    "CONFIRMED",
                    "BACKEND_FAILED",
                    "RECEIPT_REJECTED",
                }
                for item in entries
            ):
                raise LiveExecutionJournalError(
                    "Live execution outcome was already recorded."
                )
            return _append_locked(
                handle,
                entries,
                {
                    "event": event,
                    "recorded_at": timestamp.isoformat(),
                    "intent_id": intent_id,
                    "intent_fingerprint": intent_fingerprint,
                    "details": details,
                },
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_live_execution_events(
    *, path: Path = LIVE_EXECUTION_JOURNAL_PATH
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return _validate([line.strip() for line in handle if line.strip()])
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
