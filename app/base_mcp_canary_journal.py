from __future__ import annotations

import fcntl
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.journal_lock import (
    acquire_file_lock,
    establish_file_durability,
    ensure_durable_parent,
)


CANARY_JOURNAL_PATH = Path("data/base_mcp_canary_events.jsonl")
SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
EVENT_PREPARED = "PREPARED"
EVENT_APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
EVENT_COMPLETED = "COMPLETED"
EVENT_FAILED = "FAILED"
EVENT_REJECTED = "REJECTED"
EVENT_EXPIRED = "EXPIRED"
EVENT_AMBIGUOUS = "AMBIGUOUS"
HEX_64_PATTERN = re.compile(r"[0-9a-f]{64}")
TX_HASH_PATTERN = re.compile(r"0x[0-9a-fA-F]{64}")
_VERIFIED_PREPARED_BINDING = object()


class CanaryJournalIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class CanaryJournalAppendResult:
    recorded: bool
    duplicate: bool
    sequence: int
    entry_hash: str


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _validate(lines: list[str]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    previous_hash = GENESIS_HASH
    last_event_by_canary: dict[str, str] = {}
    request_id_by_canary: dict[str, str] = {}
    for sequence, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise CanaryJournalIntegrityError("Canary journal contains invalid JSON.") from error
        if not isinstance(entry, dict):
            raise CanaryJournalIntegrityError("Canary journal entry must be an object.")
        if entry.get("schema_version") != SCHEMA_VERSION:
            raise CanaryJournalIntegrityError("Canary journal schema is unsupported.")
        if entry.get("sequence") != sequence:
            raise CanaryJournalIntegrityError("Canary journal sequence is invalid.")
        if entry.get("previous_hash") != previous_hash:
            raise CanaryJournalIntegrityError("Canary journal hash chain is broken.")
        stored_hash = entry.get("entry_hash")
        unsigned = dict(entry)
        unsigned.pop("entry_hash", None)
        if stored_hash != _hash(unsigned):
            raise CanaryJournalIntegrityError("Canary journal entry hash is invalid.")
        canary_id = str(entry.get("canary_id", ""))
        event = str(entry.get("event", ""))
        _validate_transition(last_event_by_canary.get(canary_id), event)
        request_id = entry.get("request_id")
        if event == EVENT_PREPARED:
            if not isinstance(entry.get("intent_id"), str) or not str(
                entry["intent_id"]
            ).strip():
                raise CanaryJournalIntegrityError(
                    "Prepared event requires an intent ID."
                )
            for field in (
                "intent_fingerprint",
                "execution_journal_entry_hash",
                "execution_decision_digest",
            ):
                if not isinstance(entry.get(field), str) or not HEX_64_PATTERN.fullmatch(
                    str(entry[field])
                ):
                    raise CanaryJournalIntegrityError(
                        f"Prepared event {field} is invalid."
                    )
            if not isinstance(entry.get("execution_journal_sequence"), int) or int(
                entry["execution_journal_sequence"]
            ) < 1:
                raise CanaryJournalIntegrityError(
                    "Prepared event execution journal sequence is invalid."
                )
        elif event == EVENT_APPROVAL_REQUESTED:
            if not isinstance(request_id, str) or not request_id.strip():
                raise CanaryJournalIntegrityError(
                    "Approval-requested event requires a request ID."
                )
            request_id_by_canary[canary_id] = request_id
        elif event not in {EVENT_PREPARED}:
            if request_id != request_id_by_canary.get(canary_id):
                raise CanaryJournalIntegrityError(
                    "Canary event request ID does not match the approval request."
                )
        previous_hash = str(stored_hash)
        last_event_by_canary[canary_id] = event
        entries.append(entry)
    return entries


def _validate_transition(previous: str | None, event: str) -> None:
    allowed = {
        None: {EVENT_PREPARED},
        EVENT_PREPARED: {EVENT_APPROVAL_REQUESTED},
        EVENT_APPROVAL_REQUESTED: {
            EVENT_COMPLETED,
            EVENT_FAILED,
            EVENT_REJECTED,
            EVENT_EXPIRED,
            EVENT_AMBIGUOUS,
        },
        EVENT_AMBIGUOUS: {EVENT_COMPLETED, EVENT_FAILED},
    }
    if event not in allowed.get(previous, set()):
        raise CanaryJournalIntegrityError(
            f"Invalid canary transition from {previous or 'none'} to {event}."
        )


def _append_canary_event(
    *,
    canary_id: str,
    request_digest: str,
    event: str,
    path: Path = CANARY_JOURNAL_PATH,
    recorded_at: datetime | None = None,
    request_id: str | None = None,
    transaction_hash: str | None = None,
    intent_id: str | None = None,
    intent_fingerprint: str | None = None,
    execution_journal_sequence: int | None = None,
    execution_journal_entry_hash: str | None = None,
    execution_decision_digest: str | None = None,
    _verified_binding: object | None = None,
) -> CanaryJournalAppendResult:
    timestamp = recorded_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Canary journal timestamp must include a timezone.")
    if not canary_id.strip():
        raise ValueError("Canary ID is required.")
    if not HEX_64_PATTERN.fullmatch(request_digest):
        raise ValueError("Request digest must be a lowercase SHA-256 hash.")
    if transaction_hash is not None and not TX_HASH_PATTERN.fullmatch(
        transaction_hash
    ):
        raise ValueError("Transaction hash is invalid.")
    if event == EVENT_COMPLETED and transaction_hash is None:
        raise ValueError("Completed canary requires a transaction hash.")
    if event != EVENT_COMPLETED and transaction_hash is not None:
        raise ValueError("Only a completed canary may record a transaction hash.")
    binding_values = (
        intent_id,
        intent_fingerprint,
        execution_journal_sequence,
        execution_journal_entry_hash,
        execution_decision_digest,
    )
    if event == EVENT_PREPARED:
        if _verified_binding is not _VERIFIED_PREPARED_BINDING:
            raise ValueError(
                "Prepared event requires a verified execution journal binding."
            )
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ValueError("Prepared event requires an intent ID.")
        for value, label in (
            (intent_fingerprint, "intent fingerprint"),
            (execution_journal_entry_hash, "execution journal entry hash"),
            (execution_decision_digest, "execution decision digest"),
        ):
            if not isinstance(value, str) or not HEX_64_PATTERN.fullmatch(value):
                raise ValueError(f"Prepared event {label} is invalid.")
        if not isinstance(execution_journal_sequence, int) or (
            execution_journal_sequence < 1
        ):
            raise ValueError(
                "Prepared event execution journal sequence is invalid."
            )
    elif any(value is not None for value in binding_values):
        raise ValueError("Execution binding is allowed only on PREPARED.")

    ensure_durable_parent(path)
    with path.open("a+", encoding="utf-8") as handle:
        acquire_file_lock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0)
            entries = _validate(
                [line.strip() for line in handle if line.strip()]
            )
            matching = [
                item for item in entries if item.get("canary_id") == canary_id
            ]
            if matching and matching[0].get("request_digest") != request_digest:
                raise CanaryJournalIntegrityError(
                    "Canary ID was reused with a different request digest."
                )
            previous_event = str(matching[-1]["event"]) if matching else None
            for index, item in enumerate(matching):
                if (
                    item.get("event") == event
                    and item.get("request_id") == request_id
                    and item.get("transaction_hash") == transaction_hash
                ):
                    if index != len(matching) - 1:
                        _validate_transition(previous_event, event)
                    if event == EVENT_PREPARED and any(
                        item.get(field) != value
                        for field, value in (
                            ("intent_id", intent_id),
                            ("intent_fingerprint", intent_fingerprint),
                            (
                                "execution_journal_sequence",
                                execution_journal_sequence,
                            ),
                            (
                                "execution_journal_entry_hash",
                                execution_journal_entry_hash,
                            ),
                            ("execution_decision_digest", execution_decision_digest),
                        )
                    ):
                        raise CanaryJournalIntegrityError(
                            "Prepared retry execution binding does not match."
                        )
                    establish_file_durability(handle, path)
                    return CanaryJournalAppendResult(
                        recorded=False,
                        duplicate=True,
                        sequence=int(item["sequence"]),
                        entry_hash=str(item["entry_hash"]),
                    )
            _validate_transition(previous_event, event)
            expected_request_id = next(
                (
                    str(item["request_id"])
                    for item in matching
                    if item.get("event") == EVENT_APPROVAL_REQUESTED
                ),
                None,
            )
            if event == EVENT_APPROVAL_REQUESTED:
                if not isinstance(request_id, str) or not request_id.strip():
                    raise ValueError("Approval-requested event requires a request ID.")
            elif event != EVENT_PREPARED and request_id != expected_request_id:
                raise CanaryJournalIntegrityError(
                    "Canary event request ID does not match the approval request."
                )

            sequence = len(entries) + 1
            previous_hash = (
                str(entries[-1]["entry_hash"]) if entries else GENESIS_HASH
            )
            payload: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "sequence": sequence,
                "previous_hash": previous_hash,
                "recorded_at": timestamp.astimezone(timezone.utc).isoformat(),
                "canary_id": canary_id,
                "request_digest": request_digest,
                "event": event,
                "request_id": request_id,
                "transaction_hash": transaction_hash,
                "intent_id": intent_id,
                "intent_fingerprint": intent_fingerprint,
                "execution_journal_sequence": execution_journal_sequence,
                "execution_journal_entry_hash": execution_journal_entry_hash,
                "execution_decision_digest": execution_decision_digest,
            }
            entry_hash = _hash(payload)
            payload["entry_hash"] = entry_hash
            handle.seek(0, 2)
            handle.write(_canonical(payload) + "\n")
            establish_file_durability(handle, path)
            return CanaryJournalAppendResult(
                recorded=True,
                duplicate=False,
                sequence=sequence,
                entry_hash=entry_hash,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_canary_event(
    *,
    canary_id: str,
    request_digest: str,
    event: str,
    path: Path = CANARY_JOURNAL_PATH,
    recorded_at: datetime | None = None,
    request_id: str | None = None,
    transaction_hash: str | None = None,
    intent_id: str | None = None,
    intent_fingerprint: str | None = None,
    execution_journal_sequence: int | None = None,
    execution_journal_entry_hash: str | None = None,
    execution_decision_digest: str | None = None,
    execution_journal_path: Path | None = None,
) -> CanaryJournalAppendResult:
    """Append an event, requiring a locked journal proof for PREPARED."""

    arguments = {
        "canary_id": canary_id,
        "request_digest": request_digest,
        "event": event,
        "path": path,
        "recorded_at": recorded_at,
        "request_id": request_id,
        "transaction_hash": transaction_hash,
        "intent_id": intent_id,
        "intent_fingerprint": intent_fingerprint,
        "execution_journal_sequence": execution_journal_sequence,
        "execution_journal_entry_hash": execution_journal_entry_hash,
        "execution_decision_digest": execution_decision_digest,
    }
    if event != EVENT_PREPARED:
        if execution_journal_path is not None:
            raise ValueError(
                "Execution journal path is allowed only on PREPARED."
            )
        return _append_canary_event(**arguments)

    if execution_journal_path is None:
        raise ValueError(
            "Prepared event requires a verified execution journal binding."
        )
    if not isinstance(execution_journal_sequence, int):
        raise ValueError("Prepared event execution journal sequence is invalid.")
    if not isinstance(execution_journal_entry_hash, str):
        raise ValueError("Prepared event execution journal entry hash is invalid.")
    if not isinstance(intent_id, str) or not isinstance(intent_fingerprint, str):
        raise ValueError("Prepared event intent binding is invalid.")

    from app.execution_journal import (
        JournalIntegrityError,
        locked_validated_execution_decision,
    )
    from app.trading_executor import STATUS_SHADOW_APPROVED

    with locked_validated_execution_decision(
        path=execution_journal_path,
        sequence=execution_journal_sequence,
        entry_hash=execution_journal_entry_hash,
        intent_id=intent_id,
        intent_fingerprint=intent_fingerprint,
    ) as entry:
        decision = entry.get("decision")
        if not isinstance(decision, dict):
            raise JournalIntegrityError("Execution journal decision is invalid.")
        if decision.get("status") != STATUS_SHADOW_APPROVED:
            raise JournalIntegrityError(
                "Execution journal decision is not SHADOW_APPROVED."
            )
        actual_digest = _hash(decision)
        if execution_decision_digest != actual_digest:
            raise JournalIntegrityError(
                "Execution decision digest does not match."
            )
        return _append_canary_event(
            **arguments,
            _verified_binding=_VERIFIED_PREPARED_BINDING,
        )


def read_canary_events(
    *,
    path: Path = CANARY_JOURNAL_PATH,
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        acquire_file_lock(handle, fcntl.LOCK_SH)
        try:
            return _validate([line.strip() for line in handle if line.strip()])
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
