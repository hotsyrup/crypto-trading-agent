from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.journal_lock import (
    acquire_file_lock,
    establish_file_durability,
    ensure_durable_parent,
)
from app.trading_executor import ExecutionDecision


EXECUTION_JOURNAL_PATH = Path("data/execution_decisions.jsonl")
SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64


class JournalIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class JournalAppendResult:
    recorded: bool
    duplicate: bool
    sequence: int
    entry_hash: str


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _entry_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _validate_entries(lines: list[str]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    previous_hash = GENESIS_HASH
    for expected_sequence, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise JournalIntegrityError("Execution journal contains invalid JSON.") from error
        if not isinstance(entry, dict):
            raise JournalIntegrityError("Execution journal entry must be an object.")
        if entry.get("schema_version") != SCHEMA_VERSION:
            raise JournalIntegrityError("Execution journal schema is unsupported.")
        if entry.get("sequence") != expected_sequence:
            raise JournalIntegrityError("Execution journal sequence is invalid.")
        if entry.get("previous_hash") != previous_hash:
            raise JournalIntegrityError("Execution journal hash chain is broken.")
        stored_hash = entry.get("entry_hash")
        unsigned = dict(entry)
        unsigned.pop("entry_hash", None)
        if stored_hash != _entry_hash(unsigned):
            raise JournalIntegrityError("Execution journal entry hash is invalid.")
        previous_hash = str(stored_hash)
        entries.append(entry)
    return entries


def append_execution_decision(
    decision: ExecutionDecision,
    *,
    path: Path = EXECUTION_JOURNAL_PATH,
    recorded_at: datetime | None = None,
) -> JournalAppendResult:
    """Append one decision under an exclusive lock, rejecting replays.

    The journal contains policy decisions only. It is not a signer, order
    reservation, transaction submission path, or proof that execution occurred.
    """

    timestamp = recorded_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Journal timestamp must include a timezone.")

    ensure_durable_parent(path)
    with path.open("a+", encoding="utf-8") as handle:
        acquire_file_lock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0)
            lines = [line.strip() for line in handle if line.strip()]
            entries = _validate_entries(lines)

            for entry in entries:
                if entry.get("intent_id") != decision.intent_id:
                    continue
                if entry.get("intent_fingerprint") != decision.intent_fingerprint:
                    raise JournalIntegrityError(
                        "Intent ID was reused with different trade content."
                    )
                establish_file_durability(handle, path)
                return JournalAppendResult(
                    recorded=False,
                    duplicate=True,
                    sequence=int(entry["sequence"]),
                    entry_hash=str(entry["entry_hash"]),
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
                "intent_id": decision.intent_id,
                "intent_fingerprint": decision.intent_fingerprint,
                "decision": asdict(decision),
            }
            entry_hash = _entry_hash(payload)
            payload["entry_hash"] = entry_hash

            handle.seek(0, 2)
            handle.write(_canonical(payload) + "\n")
            establish_file_durability(handle, path)
            return JournalAppendResult(
                recorded=True,
                duplicate=False,
                sequence=sequence,
                entry_hash=entry_hash,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_execution_decisions(
    *,
    path: Path = EXECUTION_JOURNAL_PATH,
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        acquire_file_lock(handle, fcntl.LOCK_SH)
        try:
            lines = [line.strip() for line in handle if line.strip()]
            return _validate_entries(lines)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_validated_execution_decision(
    *,
    path: Path,
    sequence: int,
    entry_hash: str,
    intent_id: str,
    intent_fingerprint: str,
) -> dict[str, object]:
    """Return one fully validated journal entry matching an exact binding."""

    entries = read_execution_decisions(path=path)
    if sequence < 1 or sequence > len(entries):
        raise JournalIntegrityError("Execution journal sequence does not exist.")
    entry = entries[sequence - 1]
    if entry.get("sequence") != sequence:
        raise JournalIntegrityError("Execution journal sequence does not match.")
    if entry.get("entry_hash") != entry_hash:
        raise JournalIntegrityError("Execution journal entry hash does not match.")
    if entry.get("intent_id") != intent_id:
        raise JournalIntegrityError("Execution journal intent ID does not match.")
    if entry.get("intent_fingerprint") != intent_fingerprint:
        raise JournalIntegrityError(
            "Execution journal intent fingerprint does not match."
        )
    decision = entry.get("decision")
    if not isinstance(decision, dict):
        raise JournalIntegrityError("Execution journal decision is invalid.")
    if decision.get("intent_id") != intent_id:
        raise JournalIntegrityError("Journal decision intent ID does not match.")
    if decision.get("intent_fingerprint") != intent_fingerprint:
        raise JournalIntegrityError(
            "Journal decision intent fingerprint does not match."
        )
    return entry


@contextmanager
def locked_validated_execution_decision(
    *,
    path: Path,
    sequence: int,
    entry_hash: str,
    intent_id: str,
    intent_fingerprint: str,
):
    """Hold a shared lock around use of an exact execution-journal entry.

    The journal is validated both before yielding and immediately before the
    lock is released, closing validation/use races for cooperative writers and
    detecting non-cooperative truncation before readiness can be returned.
    """

    if not path.exists():
        raise JournalIntegrityError("Execution journal does not exist.")
    with path.open("r", encoding="utf-8") as handle:
        acquire_file_lock(handle, fcntl.LOCK_SH)
        try:
            def validate_locked() -> dict[str, object]:
                handle.seek(0)
                entries = _validate_entries(
                    [line.strip() for line in handle if line.strip()]
                )
                if sequence < 1 or sequence > len(entries):
                    raise JournalIntegrityError(
                        "Execution journal sequence does not exist."
                    )
                entry = entries[sequence - 1]
                if entry.get("entry_hash") != entry_hash:
                    raise JournalIntegrityError(
                        "Execution journal entry hash does not match."
                    )
                if entry.get("intent_id") != intent_id:
                    raise JournalIntegrityError(
                        "Execution journal intent ID does not match."
                    )
                if entry.get("intent_fingerprint") != intent_fingerprint:
                    raise JournalIntegrityError(
                        "Execution journal intent fingerprint does not match."
                    )
                decision = entry.get("decision")
                if not isinstance(decision, dict):
                    raise JournalIntegrityError(
                        "Execution journal decision is invalid."
                    )
                if decision.get("intent_id") != intent_id:
                    raise JournalIntegrityError(
                        "Journal decision intent ID does not match."
                    )
                if decision.get("intent_fingerprint") != intent_fingerprint:
                    raise JournalIntegrityError(
                        "Journal decision intent fingerprint does not match."
                    )
                return entry

            entry = validate_locked()
            yield entry
            validate_locked()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
