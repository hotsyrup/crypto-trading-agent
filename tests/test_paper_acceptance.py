import hashlib
import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import app.paper_cycle_ledger as paper_cycle_ledger
from app.paper_acceptance import acceptance_credit_enabled, legacy_progress_status
from app.paper_cycle_ledger import (
    LedgerConflictError,
    current_portfolio,
    read_ledger,
)
from app.paper_portfolio import PaperPortfolio
from app.risk_accounting import RiskAccountingTransaction, portfolio_risk_transaction


class PaperAcceptanceTests(unittest.TestCase):
    def paths(self, directory):
        return (
            patch("app.paper_cycle_ledger.LEDGER_PATH", Path(directory) / "ledger.jsonl"),
            patch("app.paper_cycle_ledger.LOCK_PATH", Path(directory) / "ledger.lock"),
            patch("app.risk_accounting.RISK_STATE_PATH", Path(directory) / "risk.json"),
            patch("app.risk_accounting.RISK_LOCK_PATH", Path(directory) / "risk.lock"),
        )

    def commit_cycle(self, payload):
        before = payload["portfolio_before"]
        portfolio = PaperPortfolio(
            Decimal(before["usdc_balance"]),
            Decimal(before["eth_balance"]),
        )
        recorded_at = datetime.fromisoformat(payload["recorded_at"])
        with portfolio_risk_transaction(
            portfolio,
            Decimal(payload["reference_price"]),
            now=recorded_at,
        ) as transaction:
            self.assertTrue(transaction.decision.ready, transaction.decision.reason)
            payload.setdefault("accounting_date", transaction.decision.daily_date.isoformat())
            payload.setdefault("portfolio_value", str(transaction.decision.current_value))
            payload.setdefault("high_water_mark", str(transaction.decision.high_water_mark))
            payload.setdefault("daily_start_value", str(transaction.decision.daily_start_value))
            return transaction.commit_cycle(payload)

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
            "reference_price": "2000",
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

    def test_ledger_exposes_no_public_append_or_coordinator_route(self):
        self.assertFalse(hasattr(paper_cycle_ledger, "commit_cycle"))
        self.assertFalse(hasattr(paper_cycle_ledger, "coordinated_ledger"))
        self.assertFalse(hasattr(paper_cycle_ledger, "LedgerCoordinator"))

    def test_no_importable_callable_can_bypass_coordinated_risk_append(self):
        append_routes = {
            name
            for name, value in vars(paper_cycle_ledger).items()
            if callable(value)
            and (
                "append" in name.lower()
                or "commit" in name.lower()
                or name.lower().startswith("_locked")
            )
        }
        self.assertEqual(append_routes, set())
        module_functions = [
            value
            for value in vars(paper_cycle_ledger).values()
            if inspect.isfunction(value) and value.__module__ == paper_cycle_ledger.__name__
        ]
        self.assertFalse(
            any("fsync" in function.__code__.co_names for function in module_functions)
        )
        with tempfile.TemporaryDirectory() as directory:
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    RiskAccountingTransaction().commit_cycle(self.payload(1))
                self.assertEqual(read_ledger(), [])

    def test_duplicate_cycle_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                payload = self.payload(1)
                first, _, first_duplicate = self.commit_cycle(payload)
                second, summary, second_duplicate = self.commit_cycle(payload)
                records = read_ledger()
        self.assertFalse(first_duplicate)
        self.assertTrue(second_duplicate)
        self.assertEqual(first["entry_hash"], second["entry_hash"])
        self.assertEqual(len(records), 1)
        self.assertEqual(summary["unique_eligible_signals"], 1)

    def test_true_retry_with_new_recorded_at_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                first_recorded_at = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)
                first_payload = self.payload(1, recorded_at=first_recorded_at)
                retry_payload = dict(first_payload)
                retry_payload["recorded_at"] = (
                    first_recorded_at + timedelta(seconds=30)
                ).isoformat()
                first, _, _ = self.commit_cycle(first_payload)
                retry, _, duplicate = self.commit_cycle(retry_payload)
                records = read_ledger()

        self.assertTrue(duplicate)
        self.assertEqual(retry["entry_hash"], first["entry_hash"])
        self.assertEqual(len(records), 1)

    def test_duplicate_signal_with_changed_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                first = self.payload(1)
                self.commit_cycle(first)
                changed = self.payload(1)
                changed["blocked_reason"] = "different research evidence"
                with self.assertRaisesRegex(LedgerConflictError, "different cycle evidence"):
                    self.commit_cycle(changed)

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
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                self.commit_cycle(self.payload(1, after=("9950", "0.025")))
                with self.assertRaises(LedgerConflictError):
                    self.commit_cycle(self.payload(2))

    def test_tampering_fails_closed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                self.commit_cycle(self.payload(1))
                content = ledger_path.read_text(encoding="utf-8")
                ledger_path.write_text(content.replace('"paper_eligible": true', '"paper_eligible": false'), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "hash"):
                    read_ledger()

    def test_fifty_unique_eligible_signals_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                summary = None
                for number in range(50):
                    _, summary, _ = self.commit_cycle(self.payload(number))
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["completion_reason"], "50_UNIQUE_ELIGIBLE_SIGNALS")

    def test_seven_completed_qualifying_utc_days_complete(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                number = 0
                summary = None
                for days_ago in range(7, 0, -1):
                    day = (now - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0)
                    for cycle in range(20):
                        _, summary, _ = self.commit_cycle(
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
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                for number in range(20):
                    _, summary, _ = self.commit_cycle(
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
            patches = self.paths(directory)
            with patches[0], patches[1], patches[2], patches[3]:
                self.commit_cycle(self.payload(1, after=("9950", "0.025")))
                self.assertEqual(current_portfolio(), (Decimal("9950"), Decimal("0.025")))


if __name__ == "__main__":
    unittest.main()
