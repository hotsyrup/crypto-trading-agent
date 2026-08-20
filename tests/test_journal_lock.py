import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.execution_journal import append_execution_decision
from app.journal_lock import acquire_file_lock, ensure_durable_parent
from app.trading_executor import ExecutionDecision


class JournalLockTests(unittest.TestCase):
    def test_lock_acquisition_times_out_fail_closed(self) -> None:
        with tempfile.TemporaryFile(mode="w+") as handle:
            with patch(
                "app.journal_lock.fcntl.flock",
                side_effect=BlockingIOError("busy"),
            ):
                with self.assertRaisesRegex(TimeoutError, "timed out"):
                    acquire_file_lock(handle, 0, timeout_seconds=0)

    def test_new_journal_parent_is_fsynced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new-journal-dir" / "journal.jsonl"
            with patch("app.journal_lock.os.fsync") as fsync:
                ensure_durable_parent(path)
            self.assertTrue(path.parent.exists())
            self.assertGreaterEqual(fsync.call_count, 2)

    def test_execution_file_directory_fsync_follows_content_fsync(self) -> None:
        events = []
        decision = ExecutionDecision(
            status="SHADOW_APPROVED",
            reasons=("validated",),
            intent_id="intent-001",
            intent_fingerprint="a" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.jsonl"
            with patch(
                "app.journal_lock.os.fsync",
                side_effect=lambda descriptor: events.append("file"),
            ), patch(
                "app.journal_lock.fsync_containing_directory",
                side_effect=lambda selected: events.append("directory"),
            ):
                append_execution_decision(decision, path=path)
        self.assertEqual(events, ["file", "directory"])


if __name__ == "__main__":
    unittest.main()
