from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


LIVE_EXECUTION_JOURNAL_PATH = Path("data/live_execution_audit.jsonl")
SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
MAX_DAILY_EXECUTED_USDC = Decimal("100")
TRANSACTION_HASH_PATTERN = re.compile(r"0x[0-9a-fA-F]{64}")
ROUNDING_REJECTION_REASON = (
    "CDP receipt minimum output exceeds approved slippage."
)


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


@dataclass(frozen=True)
class LiveReconciliation:
    recorded: bool
    duplicate: bool
    sequence: int


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
    strategy_profile: str | None = None,
    entry_score: int | None = None,
    exit_reason: str | None = None,
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
                    "strategy_profile": strategy_profile,
                    "entry_score": entry_score,
                    "exit_reason": exit_reason,
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


def reconcile_rejected_receipt_as_confirmed(
    *,
    source_sequence: int,
    receipt_status: str,
    verification_source: str,
    path: Path = LIVE_EXECUTION_JOURNAL_PATH,
    recorded_at: datetime | None = None,
) -> LiveReconciliation:
    """Append a bounded confirmation correction without rewriting history."""

    if type(source_sequence) is not int or source_sequence <= 0:
        raise ValueError("Reconciliation source sequence must be positive.")
    if receipt_status != "0x1":
        raise ValueError("Reconciliation requires a successful Base receipt status.")
    if verification_source != "independent_base_rpc":
        raise ValueError("Reconciliation verification source is unsupported.")
    timestamp = _aware_utc(recorded_at or datetime.now(timezone.utc))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            entries = _validate([line.strip() for line in handle if line.strip()])
            if source_sequence > len(entries):
                raise LiveExecutionJournalError(
                    "Reconciliation source sequence does not exist."
                )
            source = entries[source_sequence - 1]
            if source.get("event") != "RECEIPT_REJECTED":
                raise LiveExecutionJournalError(
                    "Reconciliation source must be a rejected receipt."
                )
            details = source.get("details")
            if not isinstance(details, dict):
                raise LiveExecutionJournalError(
                    "Rejected receipt details are unavailable."
                )
            transaction_hash = details.get("transaction_hash")
            if (
                details.get("success") is not True
                or not isinstance(transaction_hash, str)
                or not TRANSACTION_HASH_PATTERN.fullmatch(transaction_hash)
                or details.get("validation_reasons") != [ROUNDING_REJECTION_REASON]
            ):
                raise LiveExecutionJournalError(
                    "Rejected receipt is outside the bounded rounding reconciliation."
                )
            for entry in entries:
                if entry.get("event") != "RECONCILED_CONFIRMED":
                    continue
                reconciliation = entry.get("details")
                if (
                    isinstance(reconciliation, dict)
                    and reconciliation.get("source_sequence") == source_sequence
                ):
                    return LiveReconciliation(
                        recorded=False,
                        duplicate=True,
                        sequence=int(entry["sequence"]),
                    )
            sequence = _append_locked(
                handle,
                entries,
                {
                    "event": "RECONCILED_CONFIRMED",
                    "recorded_at": timestamp.isoformat(),
                    "intent_id": source["intent_id"],
                    "intent_fingerprint": source["intent_fingerprint"],
                    "details": {
                        "source_sequence": source_sequence,
                        "source_entry_hash": source["entry_hash"],
                        "source_event": "RECEIPT_REJECTED",
                        "receipt_status": receipt_status,
                        "verification_source": verification_source,
                        "reason": (
                            "On-chain-successful receipt accepted by the corrected "
                            "bounded rounding rule."
                        ),
                    },
                },
            )
            return LiveReconciliation(
                recorded=True,
                duplicate=False,
                sequence=sequence,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_reconciled_transfer_accounting(
    *,
    reconciliation_sequence: int,
    transaction_hash: str,
    block_number: int,
    from_atomic_amount: int,
    to_atomic_amount: int,
    verification_source: str,
    path: Path = LIVE_EXECUTION_JOURNAL_PATH,
    recorded_at: datetime | None = None,
) -> LiveReconciliation:
    """Append exact Base transfer evidence for one reconciled receipt."""

    if type(reconciliation_sequence) is not int or reconciliation_sequence <= 0:
        raise ValueError("Accounting reconciliation sequence must be positive.")
    if not TRANSACTION_HASH_PATTERN.fullmatch(transaction_hash):
        raise ValueError("Accounting transaction hash is invalid.")
    if type(block_number) is not int or block_number <= 0:
        raise ValueError("Accounting block number must be positive.")
    if type(from_atomic_amount) is not int or from_atomic_amount <= 0:
        raise ValueError("Accounting input transfer must be positive atomic units.")
    if type(to_atomic_amount) is not int or to_atomic_amount <= 0:
        raise ValueError("Accounting output transfer must be positive atomic units.")
    if verification_source != "public_base_rpc":
        raise ValueError("Accounting verification source is unsupported.")
    timestamp = _aware_utc(recorded_at or datetime.now(timezone.utc))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            entries = _validate([line.strip() for line in handle if line.strip()])
            if reconciliation_sequence > len(entries):
                raise LiveExecutionJournalError(
                    "Accounting reconciliation sequence does not exist."
                )
            reconciliation = entries[reconciliation_sequence - 1]
            if reconciliation.get("event") != "RECONCILED_CONFIRMED":
                raise LiveExecutionJournalError(
                    "Accounting source must be a confirmed reconciliation."
                )
            reconciliation_details = reconciliation.get("details")
            if not isinstance(reconciliation_details, dict):
                raise LiveExecutionJournalError(
                    "Confirmed reconciliation details are unavailable."
                )
            source_sequence = reconciliation_details.get("source_sequence")
            if type(source_sequence) is not int or not 0 < source_sequence < reconciliation_sequence:
                raise LiveExecutionJournalError(
                    "Confirmed reconciliation source sequence is invalid."
                )
            source = entries[source_sequence - 1]
            source_details = source.get("details")
            if (
                source.get("event") != "RECEIPT_REJECTED"
                or source.get("intent_id") != reconciliation.get("intent_id")
                or source.get("entry_hash")
                != reconciliation_details.get("source_entry_hash")
                or not isinstance(source_details, dict)
                or source_details.get("transaction_hash") != transaction_hash
            ):
                raise LiveExecutionJournalError(
                    "Accounting source does not match its rejected receipt."
                )
            reservations = [
                entry
                for entry in entries
                if entry.get("event") == "RESERVED"
                and entry.get("intent_id") == source.get("intent_id")
            ]
            if len(reservations) != 1:
                raise LiveExecutionJournalError(
                    "Accounting requires exactly one source reservation."
                )
            reservation = reservations[0]
            from_decimals = reservation.get("from_decimals")
            to_decimals = reservation.get("to_decimals")
            if (
                type(from_decimals) is not int
                or type(to_decimals) is not int
                or not 0 <= from_decimals <= 36
                or not 0 <= to_decimals <= 36
            ):
                raise LiveExecutionJournalError(
                    "Accounting token decimals are invalid."
                )
            exact_from = Decimal(from_atomic_amount).scaleb(-from_decimals)
            exact_to = Decimal(to_atomic_amount).scaleb(-to_decimals)
            if (
                exact_from
                != _decimal(source_details.get("from_amount"), "Receipt input")
                or exact_from
                != _decimal(reservation.get("from_amount"), "Reservation input")
                or exact_to
                < _decimal(source_details.get("min_to_amount"), "Receipt minimum")
            ):
                raise LiveExecutionJournalError(
                    "Exact Base transfers do not match the reconciled swap bounds."
                )
            for entry in entries:
                if entry.get("event") != "RECONCILIATION_ACCOUNTED":
                    continue
                details = entry.get("details")
                if (
                    isinstance(details, dict)
                    and details.get("source_reconciliation_sequence")
                    == reconciliation_sequence
                ):
                    if (
                        details.get("transaction_hash") != transaction_hash
                        or details.get("block_number") != block_number
                        or details.get("from_atomic_amount")
                        != str(from_atomic_amount)
                        or details.get("to_atomic_amount") != str(to_atomic_amount)
                    ):
                        raise LiveExecutionJournalError(
                            "Accounting reconciliation was reused with different evidence."
                        )
                    return LiveReconciliation(
                        recorded=False,
                        duplicate=True,
                        sequence=int(entry["sequence"]),
                    )
            details: dict[str, object] = {
                "source_reconciliation_sequence": reconciliation_sequence,
                "source_receipt_sequence": source_sequence,
                "transaction_hash": transaction_hash,
                "block_number": block_number,
                "from_token": str(reservation["from_token"]).lower(),
                "to_token": str(reservation["to_token"]).lower(),
                "from_decimals": from_decimals,
                "to_decimals": to_decimals,
                "from_atomic_amount": str(from_atomic_amount),
                "to_atomic_amount": str(to_atomic_amount),
                "from_amount": str(exact_from),
                "to_amount": str(exact_to),
                "min_to_amount": str(source_details["min_to_amount"]),
                "executed_at": str(source["recorded_at"]),
                "verification_source": verification_source,
            }
            if source_details.get("gas_cost_eth") is not None:
                details["gas_cost_eth"] = str(source_details["gas_cost_eth"])
            sequence = _append_locked(
                handle,
                entries,
                {
                    "event": "RECONCILIATION_ACCOUNTED",
                    "recorded_at": timestamp.isoformat(),
                    "intent_id": source["intent_id"],
                    "intent_fingerprint": source["intent_fingerprint"],
                    "details": details,
                },
            )
            return LiveReconciliation(
                recorded=True,
                duplicate=False,
                sequence=sequence,
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
