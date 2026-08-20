from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


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
        if event == EVENT_APPROVAL_REQUESTED:
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


def append_canary_event(
    *,
    canary_id: str,
    request_digest: str,
    event: str,
    path: Path = CANARY_JOURNAL_PATH,
    recorded_at: datetime | None = None,
    request_id: str | None = None,
    transaction_hash: str | None = None,
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

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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
            for item in matching:
                if (
                    item.get("event") == event
                    and item.get("request_id") == request_id
                    and item.get("transaction_hash") == transaction_hash
                ):
                    return CanaryJournalAppendResult(
                        recorded=False,
                        duplicate=True,
                        sequence=int(item["sequence"]),
                        entry_hash=str(item["entry_hash"]),
                    )

            previous_event = str(matching[-1]["event"]) if matching else None
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
            }
            entry_hash = _hash(payload)
            payload["entry_hash"] = entry_hash
            handle.seek(0, 2)
            handle.write(_canonical(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return CanaryJournalAppendResult(
                recorded=True,
                duplicate=False,
                sequence=sequence,
                entry_hash=entry_hash,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_canary_events(
    *,
    path: Path = CANARY_JOURNAL_PATH,
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return _validate([line.strip() for line in handle if line.strip()])
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
