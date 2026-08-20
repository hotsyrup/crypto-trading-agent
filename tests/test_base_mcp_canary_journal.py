import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.base_mcp_canary_journal import (
    EVENT_AMBIGUOUS,
    EVENT_APPROVAL_REQUESTED,
    EVENT_COMPLETED,
    EVENT_FAILED,
    EVENT_PREPARED,
    CanaryJournalIntegrityError,
    append_canary_event,
    read_canary_events,
)


NOW = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)
CANARY_ID = "base-canary-001"
DIGEST = "a" * 64
REQUEST_ID = "base-request-001"
TX_HASH = "0x" + "b" * 64


class BaseMcpCanaryJournalTests(unittest.TestCase):
    def append(self, path: Path, event: str, **updates: object):
        return append_canary_event(
            canary_id=CANARY_ID,
            request_digest=DIGEST,
            event=event,
            path=path,
            recorded_at=NOW,
            **updates,
        )

    def test_preparation_is_recorded_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary.jsonl"
            first = self.append(path, EVENT_PREPARED)
            duplicate = self.append(path, EVENT_PREPARED)
            events = read_canary_events(path=path)
        self.assertTrue(first.recorded)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(len(events), 1)

    def test_valid_lifecycle_records_request_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary.jsonl"
            self.append(path, EVENT_PREPARED)
            self.append(
                path,
                EVENT_APPROVAL_REQUESTED,
                request_id=REQUEST_ID,
            )
            self.append(
                path,
                EVENT_COMPLETED,
                request_id=REQUEST_ID,
                transaction_hash=TX_HASH,
            )
            events = read_canary_events(path=path)
        self.assertEqual([item["sequence"] for item in events], [1, 2, 3])
        self.assertEqual(events[-1]["transaction_hash"], TX_HASH)

    def test_submission_cannot_skip_human_approval_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary.jsonl"
            self.append(path, EVENT_PREPARED)
            with self.assertRaisesRegex(
                CanaryJournalIntegrityError,
                "Invalid canary transition",
            ):
                self.append(
                    path,
                    EVENT_COMPLETED,
                    request_id=REQUEST_ID,
                    transaction_hash=TX_HASH,
                )

    def test_request_id_must_match_for_final_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary.jsonl"
            self.append(path, EVENT_PREPARED)
            self.append(
                path,
                EVENT_APPROVAL_REQUESTED,
                request_id=REQUEST_ID,
            )
            with self.assertRaisesRegex(
                CanaryJournalIntegrityError,
                "does not match",
            ):
                self.append(
                    path,
                    EVENT_FAILED,
                    request_id="different-request",
                )

    def test_ambiguous_outcome_can_be_reconciled_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary.jsonl"
            self.append(path, EVENT_PREPARED)
            self.append(
                path,
                EVENT_APPROVAL_REQUESTED,
                request_id=REQUEST_ID,
            )
            self.append(path, EVENT_AMBIGUOUS, request_id=REQUEST_ID)
            self.append(
                path,
                EVENT_COMPLETED,
                request_id=REQUEST_ID,
                transaction_hash=TX_HASH,
            )
            events = read_canary_events(path=path)
        self.assertEqual(events[-2]["event"], EVENT_AMBIGUOUS)
        self.assertEqual(events[-1]["event"], EVENT_COMPLETED)

    def test_terminal_state_cannot_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary.jsonl"
            self.append(path, EVENT_PREPARED)
            self.append(
                path,
                EVENT_APPROVAL_REQUESTED,
                request_id=REQUEST_ID,
            )
            self.append(path, EVENT_FAILED, request_id=REQUEST_ID)
            with self.assertRaisesRegex(
                CanaryJournalIntegrityError,
                "Invalid canary transition",
            ):
                self.append(path, EVENT_AMBIGUOUS, request_id=REQUEST_ID)

    def test_corruption_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary.jsonl"
            self.append(path, EVENT_PREPARED)
            entry = json.loads(path.read_text(encoding="utf-8"))
            entry["request_digest"] = "c" * 64
            path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                CanaryJournalIntegrityError,
                "entry hash is invalid",
            ):
                read_canary_events(path=path)

    def test_canary_id_cannot_be_reused_for_different_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary.jsonl"
            self.append(path, EVENT_PREPARED)
            with self.assertRaisesRegex(
                CanaryJournalIntegrityError,
                "different request digest",
            ):
                append_canary_event(
                    canary_id=CANARY_ID,
                    request_digest="c" * 64,
                    event=EVENT_PREPARED,
                    path=path,
                    recorded_at=NOW,
                )

    def test_completed_event_requires_transaction_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary.jsonl"
            with self.assertRaisesRegex(ValueError, "requires a transaction hash"):
                append_canary_event(
                    canary_id=CANARY_ID,
                    request_digest=DIGEST,
                    event=EVENT_COMPLETED,
                    path=path,
                    recorded_at=NOW,
                    request_id=REQUEST_ID,
                )


if __name__ == "__main__":
    unittest.main()
