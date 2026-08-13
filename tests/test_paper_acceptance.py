import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.paper_acceptance import acceptance_credit_enabled, legacy_progress_status
from app.paper_cycle_ledger import (
    LedgerConflictError,
    commit_cycle,
    current_portfolio,
    read_ledger,
)


class PaperAcceptanceTests(unittest.TestCase):
    def paths(self, directory):
        return (
            patch("app.paper_cycle_ledger.LEDGER_PATH", Path(directory) / "ledger.jsonl"),
            patch("app.paper_cycle_ledger.LOCK_PATH", Path(directory) / "ledger.lock"),
        )

    def payload(
        self,
        number,
        *,
        recorded_at=None,
        healthy=True,
        eligible=True,
        credit=True,
        before=("10000.00", "0"),
        after=("10000.00", "0"),
    ):
        cycle_id = hashlib.sha256(f"cycle-{number}".encode()).hexdigest()
        recorded_at = recorded_at or datetime.now(timezone.utc)
        return {
            "recorded_at": recorded_at.isoformat(),
            "cycle_id": cycle_id,
            "signal_id": cycle_id,
            "strategy_id": "eth_usd_sma_3_5",
            "strategy_version": "1.0.0",
            "acceptance_policy_version": "2.0.0",
            "acceptance_credit_enabled": credit,
            "system_healthy": healthy,
            "paper_eligible": eligible,
            "simulated": False,
            "blocked_reason": "" if healthy else "blocked",
            "order": {"status": "BLOCKED"},
            "portfolio_before": {"usdc_balance": before[0], "eth_balance": before[1]},
            "portfolio_after": {"usdc_balance": after[0], "eth_balance": after[1]},
        }

    def test_credit_is_frozen_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(acceptance_credit_enabled())

    def test_duplicate_cycle_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_patch, lock_patch = self.paths(directory)
            with ledger_patch, lock_patch:
                payload = self.payload(1)
                first, _, first_duplicate = commit_cycle(payload)
                second, summary, second_duplicate = commit_cycle(payload)
                records = read_ledger()
        self.assertFalse(first_duplicate)
        self.assertTrue(second_duplicate)
        self.assertEqual(first["entry_hash"], second["entry_hash"])
        self.assertEqual(len(records), 1)
        self.assertEqual(summary["unique_eligible_signals"], 1)

    def test_duplicate_signal_with_changed_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_patch, lock_patch = self.paths(directory)
            with ledger_patch, lock_patch:
                first = self.payload(1)
                commit_cycle(first)
                changed = self.payload(1)
                changed["blocked_reason"] = "different research evidence"
                with self.assertRaisesRegex(LedgerConflictError, "different cycle evidence"):
                    commit_cycle(changed)

    def test_legacy_progress_is_hashed_and_never_read_for_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper_acceptance.json"
            path.write_text(
                '{"cycles":12,"eligible_cycles":8,"simulated_orders":7}',
                encoding="utf-8",
            )
            with patch("app.paper_acceptance.LEGACY_ACCEPTANCE_PATH", path):
                status = legacy_progress_status()

        self.assertTrue(status["present"])
        self.assertFalse(status["read_for_credit"])
        self.assertEqual(status["reported_eligible_cycles"], 8)
        self.assertEqual(len(status["sha256"]), 64)

    def test_portfolio_continuity_rejects_stale_concurrent_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_patch, lock_patch = self.paths(directory)
            with ledger_patch, lock_patch:
                commit_cycle(self.payload(1, after=("9950", "0.025")))
                with self.assertRaises(LedgerConflictError):
                    commit_cycle(self.payload(2))

    def test_tampering_fails_closed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            ledger_patch, lock_patch = self.paths(directory)
            with ledger_patch, lock_patch:
                commit_cycle(self.payload(1))
                content = ledger_path.read_text(encoding="utf-8")
                ledger_path.write_text(content.replace('"paper_eligible": true', '"paper_eligible": false'), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "hash"):
                    read_ledger()

    def test_fifty_unique_eligible_signals_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_patch, lock_patch = self.paths(directory)
            with ledger_patch, lock_patch:
                summary = None
                for number in range(50):
                    _, summary, _ = commit_cycle(self.payload(number))
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["completion_reason"], "50_UNIQUE_ELIGIBLE_SIGNALS")

    def test_seven_completed_qualifying_utc_days_complete(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            ledger_patch, lock_patch = self.paths(directory)
            with ledger_patch, lock_patch:
                number = 0
                summary = None
                for days_ago in range(7, 0, -1):
                    day = (now - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0)
                    for cycle in range(20):
                        _, summary, _ = commit_cycle(
                            self.payload(
                                number,
                                recorded_at=day + timedelta(minutes=cycle),
                                eligible=False,
                            )
                        )
                        number += 1
        self.assertTrue(summary["complete"])
        self.assertEqual(
            summary["completion_reason"],
            "7_CONSECUTIVE_QUALIFYING_UTC_DAYS",
        )

    def test_blocked_cycle_disqualifies_day(self):
        now = datetime.now(timezone.utc)
        day = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0)
        with tempfile.TemporaryDirectory() as directory:
            ledger_patch, lock_patch = self.paths(directory)
            with ledger_patch, lock_patch:
                for number in range(20):
                    _, summary, _ = commit_cycle(
                        self.payload(
                            number,
                            recorded_at=day + timedelta(minutes=number),
                            healthy=number != 19,
                            eligible=False,
                        )
                    )
        self.assertEqual(summary["qualifying_utc_days"], 0)
        self.assertEqual(summary["consecutive_qualifying_utc_days"], 0)

    def test_restart_reconstructs_portfolio_from_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_patch, lock_patch = self.paths(directory)
            with ledger_patch, lock_patch:
                commit_cycle(self.payload(1, after=("9950", "0.025")))
                self.assertEqual(current_portfolio(), (Decimal("9950"), Decimal("0.025")))


if __name__ == "__main__":
    unittest.main()
