import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.execution_journal import (
    JournalIntegrityError,
    append_execution_decision,
    read_execution_decisions,
)
from app.trading_executor import ExecutionDecision


def decision(
    intent_id: str = "intent-001",
    fingerprint: str = "a" * 64,
) -> ExecutionDecision:
    return ExecutionDecision(
        status="SHADOW_APPROVED",
        reasons=("Intent passed the shadow-only deterministic policy.",),
        intent_id=intent_id,
        intent_fingerprint=fingerprint,
    )


class ExecutionJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "execution.jsonl"
        self.recorded_at = datetime(2026, 8, 8, 20, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_appends_hash_chained_decisions(self) -> None:
        first = append_execution_decision(
            decision(), path=self.path, recorded_at=self.recorded_at
        )
        second = append_execution_decision(
            decision("intent-002", "b" * 64),
            path=self.path,
            recorded_at=self.recorded_at,
        )
        records = read_execution_decisions(path=self.path)
        self.assertTrue(first.recorded)
        self.assertTrue(second.recorded)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["previous_hash"], records[0]["entry_hash"])

    def test_restart_replay_is_blocked_without_second_append(self) -> None:
        append_execution_decision(
            decision(), path=self.path, recorded_at=self.recorded_at
        )
        duplicate = append_execution_decision(
            decision(), path=self.path, recorded_at=self.recorded_at
        )
        self.assertFalse(duplicate.recorded)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(len(read_execution_decisions(path=self.path)), 1)

    def test_reused_intent_id_with_different_content_fails_closed(self) -> None:
        append_execution_decision(
            decision(), path=self.path, recorded_at=self.recorded_at
        )
        with self.assertRaisesRegex(JournalIntegrityError, "different trade"):
            append_execution_decision(
                decision(fingerprint="b" * 64),
                path=self.path,
                recorded_at=self.recorded_at,
            )

    def test_tampered_entry_fails_integrity_validation(self) -> None:
        append_execution_decision(
            decision(), path=self.path, recorded_at=self.recorded_at
        )
        entry = json.loads(self.path.read_text(encoding="utf-8"))
        entry["decision"]["status"] = "REJECTED"
        self.path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(JournalIntegrityError, "hash is invalid"):
            read_execution_decisions(path=self.path)

    def test_corrupt_journal_blocks_new_append(self) -> None:
        self.path.write_text("not-json\n", encoding="utf-8")
        with self.assertRaisesRegex(JournalIntegrityError, "invalid JSON"):
            append_execution_decision(
                decision(), path=self.path, recorded_at=self.recorded_at
            )

    def test_naive_journal_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            append_execution_decision(
                decision(),
                path=self.path,
                recorded_at=datetime(2026, 8, 8, 20, 0),
            )


if __name__ == "__main__":
    unittest.main()
