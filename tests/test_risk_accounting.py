from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.paper_cycle_ledger import current_portfolio, read_ledger
from app.paper_portfolio import PaperPortfolio
from app.risk_accounting import evaluate_portfolio_risk, portfolio_risk_transaction


class RiskAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        directory = Path(self.temporary_directory.name)
        self.state_path = directory / "risk.json"
        patches = (
            patch("app.risk_accounting.RISK_STATE_PATH", self.state_path),
            patch("app.risk_accounting.RISK_LOCK_PATH", directory / "risk.lock"),
            patch("app.paper_cycle_ledger.LEDGER_PATH", directory / "ledger.jsonl"),
            patch("app.paper_cycle_ledger.LOCK_PATH", directory / "ledger.lock"),
        )
        for path_patch in patches:
            path_patch.start()
            self.addCleanup(path_patch.stop)
        self.day_one = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
        self.cycle_number = 0

    @staticmethod
    def portfolio(value: str) -> PaperPortfolio:
        return PaperPortfolio(Decimal(value), Decimal("0"))

    def payload(self, decision, now, portfolio, reference_price):
        self.cycle_number += 1
        cycle_id = hashlib.sha256(f"risk-{self.cycle_number}".encode()).hexdigest()
        return {
            "recorded_at": now.isoformat(),
            "cycle_id": cycle_id,
            "signal_id": cycle_id,
            "reference_price": str(reference_price),
            "accounting_date": decision.daily_date.isoformat(),
            "portfolio_value": str(decision.current_value),
            "high_water_mark": str(decision.high_water_mark),
            "daily_start_value": str(decision.daily_start_value),
            "system_healthy": True,
            "paper_eligible": False,
            "simulated": False,
            "blocked_reason": "test",
            "order": {"status": "BLOCKED"},
            "portfolio_before": {
                "usdc_balance": str(portfolio.usdc_balance),
                "eth_balance": str(portfolio.eth_balance),
            },
            "portfolio_after": {
                "usdc_balance": "0" if portfolio.eth_balance == 0 else str(portfolio.usdc_balance),
                "eth_balance": "5" if portfolio.eth_balance == 0 else str(portfolio.eth_balance),
            },
        }

    def commit(self, value: str, now: datetime):
        usdc, eth = current_portfolio()
        portfolio = PaperPortfolio(usdc, eth)
        reference_price = (
            Decimal("2000")
            if eth == 0
            else (Decimal(value) - usdc) / eth
        )
        with portfolio_risk_transaction(
            portfolio, reference_price, now=now
        ) as transaction:
            self.assertTrue(transaction.decision.ready, transaction.decision.reason)
            result = transaction.commit_cycle(
                self.payload(transaction.decision, now, portfolio, reference_price)
            )
            return transaction.decision, result

    def evaluate(self, value: str, now: datetime):
        return evaluate_portfolio_risk(
            self.portfolio(value), Decimal("2000"), now=now
        )

    def test_committed_state_survives_restart_style_reconciliation(self) -> None:
        self.commit("10000", self.day_one)
        higher, _ = self.commit("11000", self.day_one + timedelta(hours=1))
        self.state_path.unlink()

        restarted = self.evaluate("10500", self.day_one + timedelta(hours=2))

        self.assertEqual(higher.high_water_mark, Decimal("11000"))
        self.assertEqual(restarted.high_water_mark, Decimal("11000"))
        self.assertEqual(restarted.drawdown_percent, Decimal("4.5455"))
        cache = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(cache["ledger_sequence"], 2)
        self.assertEqual(cache["ledger_head"], read_ledger()[-1]["entry_hash"])

    def test_post_append_cache_failure_recovers_committed_high_water_mark(self) -> None:
        now = self.day_one
        self.commit("10000", now)
        portfolio = PaperPortfolio(Decimal("0"), Decimal("5"))
        reference_price = Decimal("2200")
        with portfolio_risk_transaction(
            portfolio, reference_price, now=now + timedelta(hours=1)
        ) as transaction:
            payload = self.payload(
                transaction.decision,
                now + timedelta(hours=1),
                portfolio,
                reference_price,
            )
            with patch(
                "app.risk_accounting._save_state",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    transaction.commit_cycle(payload)

        self.assertEqual(len(read_ledger()), 2)
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8"))["ledger_sequence"],
            1,
        )

        restarted = self.evaluate("10000", now + timedelta(hours=2))

        self.assertTrue(restarted.ready)
        self.assertEqual(restarted.high_water_mark, Decimal("11000"))
        self.assertEqual(restarted.drawdown_percent, Decimal("9.0909"))
        cache = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(cache["ledger_sequence"], 2)
        self.assertEqual(cache["ledger_head"], read_ledger()[1]["entry_hash"])

    def test_stale_or_corrupt_cache_is_rebuilt_from_ledger(self) -> None:
        self.commit("10000", self.day_one)
        self.state_path.write_text("{not json", encoding="utf-8")

        decision = self.evaluate("9900", self.day_one + timedelta(hours=1))

        self.assertTrue(decision.ready)
        self.assertEqual(decision.daily_start_value, Decimal("10000"))
        self.assertEqual(json.loads(self.state_path.read_text())["ledger_sequence"], 1)

    def test_rebuild_failure_fails_closed(self) -> None:
        self.commit("10000", self.day_one)
        self.state_path.unlink()
        with patch(
            "app.risk_accounting._save_state",
            side_effect=OSError("cache unavailable"),
        ):
            decision = self.evaluate("9900", self.day_one + timedelta(hours=1))
        self.assertFalse(decision.ready)
        self.assertIn("cache unavailable", decision.reason)

    def test_concurrent_commits_preserve_high_water_mark(self) -> None:
        self.commit("10000", self.day_one)
        errors = []

        def commit(value: str) -> None:
            try:
                self.commit(value, self.day_one + timedelta(hours=1))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        higher = threading.Thread(target=commit, args=("11000",))
        lower = threading.Thread(target=commit, args=("9000",))
        higher.start()
        lower.start()
        higher.join(timeout=2)
        lower.join(timeout=2)

        self.assertFalse(higher.is_alive())
        self.assertFalse(lower.is_alive())
        self.assertEqual(errors, [])
        restarted = self.evaluate("10500", self.day_one + timedelta(hours=2))
        self.assertEqual(restarted.high_water_mark, Decimal("11000"))

    def test_daily_and_drawdown_limits_use_committed_baseline(self) -> None:
        self.commit("10000", self.day_one)
        daily = self.evaluate("9500", self.day_one + timedelta(hours=1))
        drawdown = self.evaluate("8000", self.day_one + timedelta(hours=1))
        total = self.evaluate("0", self.day_one + timedelta(hours=1))

        self.assertTrue(daily.daily_loss_halt)
        self.assertEqual(daily.daily_loss_percent, Decimal("5.0000"))
        self.assertTrue(drawdown.drawdown_halt)
        self.assertEqual(drawdown.drawdown_percent, Decimal("20.0000"))
        self.assertTrue(total.drawdown_halt)
        self.assertTrue(total.daily_loss_halt)

    def test_utc_day_rollover_uses_last_committed_value(self) -> None:
        self.commit("10000", self.day_one)
        self.commit("9600", self.day_one + timedelta(hours=1))
        next_day = self.evaluate("9400", self.day_one + timedelta(days=1))

        self.assertEqual(next_day.daily_start_value, Decimal("9600"))
        self.assertEqual(next_day.daily_loss_percent, Decimal("2.0833"))
        self.assertEqual(next_day.high_water_mark, Decimal("10000"))

    def test_clock_rollback_fails_closed_against_committed_state(self) -> None:
        self.commit("10000", self.day_one)
        decision = self.evaluate("10000", self.day_one - timedelta(seconds=1))
        self.assertFalse(decision.ready)
        self.assertIn("clock moved", decision.reason)

    def test_invalid_ledger_risk_fields_fail_closed(self) -> None:
        self.commit("10000", self.day_one)
        ledger_path = Path(self.temporary_directory.name) / "ledger.jsonl"
        entry = json.loads(ledger_path.read_text(encoding="utf-8"))
        entry["high_water_mark"] = "bad"
        ledger_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        decision = self.evaluate("10000", self.day_one + timedelta(hours=1))

        self.assertFalse(decision.ready)
        self.assertIn("unavailable", decision.reason)

    def test_append_rejects_risk_fields_that_weaken_transition(self) -> None:
        usdc, eth = current_portfolio()
        portfolio = PaperPortfolio(usdc, eth)
        with portfolio_risk_transaction(
            portfolio, Decimal("2000"), now=self.day_one
        ) as transaction:
            payload = self.payload(
                transaction.decision,
                self.day_one,
                portfolio,
                Decimal("2000"),
            )
            payload["daily_start_value"] = "1"
            with self.assertRaisesRegex(ValueError, "daily_start_value"):
                transaction.commit_cycle(payload)
        self.assertEqual(read_ledger(), [])

    def test_orphaned_or_ahead_cache_fails_closed_without_reset(self) -> None:
        self.commit("10000", self.day_one)
        ledger_path = Path(self.temporary_directory.name) / "ledger.jsonl"
        orphaned_cache = self.state_path.read_bytes()
        ledger_path.unlink()

        orphaned = self.evaluate("10000", self.day_one + timedelta(hours=1))

        self.assertFalse(orphaned.ready)
        self.assertIn("without an authoritative ledger", orphaned.reason)
        self.assertEqual(self.state_path.read_bytes(), orphaned_cache)

        self.state_path.unlink()
        self.commit("10000", self.day_one)
        self.commit("11000", self.day_one + timedelta(hours=1))
        ahead_cache = self.state_path.read_bytes()
        first_line = ledger_path.read_text(encoding="utf-8").splitlines(keepends=True)[0]
        ledger_path.write_text(first_line, encoding="utf-8")

        ahead = self.evaluate("10000", self.day_one + timedelta(hours=2))

        self.assertFalse(ahead.ready)
        self.assertIn("ahead of the authoritative ledger", ahead.reason)
        self.assertEqual(self.state_path.read_bytes(), ahead_cache)

    def test_cross_process_locks_serialize_cycle_commits(self) -> None:
        directory = Path(self.temporary_directory.name)
        worker = Path(__file__).with_name("subprocess_risk_worker.py")
        base = self.day_one.isoformat()
        subprocess.run(
            [sys.executable, str(worker), "commit", str(directory), "seed", "10000", base],
            check=True,
            capture_output=True,
            text=True,
        )
        holder = subprocess.Popen(
            [
                sys.executable,
                str(worker),
                "hold",
                str(directory),
                "holder",
                "11000",
                (self.day_one + timedelta(hours=1)).isoformat(),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        deadline = time.monotonic() + 5
        while not (directory / "holder-ready").exists():
            if holder.poll() is not None:
                self.fail(holder.communicate()[1])
            if time.monotonic() >= deadline:
                self.fail("holder subprocess did not acquire the locks")
            time.sleep(0.01)
        contender = subprocess.Popen(
            [
                sys.executable,
                str(worker),
                "commit",
                str(directory),
                "contender",
                "9000",
                (self.day_one + timedelta(hours=2)).isoformat(),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: contender.poll() is None and contender.kill())
        deadline = time.monotonic() + 5
        while not (directory / "contender-started").exists():
            if contender.poll() is not None:
                self.fail(contender.communicate()[1])
            if time.monotonic() >= deadline:
                self.fail("contender subprocess did not start")
            time.sleep(0.01)
        time.sleep(0.1)
        self.assertIsNone(contender.poll(), "contender bypassed the held process lock")
        (directory / "holder-release").touch()
        holder_output = holder.communicate(timeout=5)
        contender_output = contender.communicate(timeout=5)
        self.assertEqual(holder.returncode, 0, holder_output[1])
        self.assertEqual(contender.returncode, 0, contender_output[1])
        records = read_ledger()
        self.assertEqual(len(records), 3)
        self.assertEqual(records[-1]["high_water_mark"], "11000")

    def test_abrupt_exit_after_append_reconstructs_and_halts_on_restart(self) -> None:
        directory = Path(self.temporary_directory.name)
        worker = Path(__file__).with_name("subprocess_risk_worker.py")
        subprocess.run(
            [
                sys.executable,
                str(worker),
                "commit",
                str(directory),
                "seed",
                "10000",
                self.day_one.isoformat(),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        crashed = subprocess.run(
            [
                sys.executable,
                str(worker),
                "crash_after_append",
                str(directory),
                "crash-window",
                "11000",
                (self.day_one + timedelta(hours=1)).isoformat(),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        self.assertEqual(len(read_ledger()), 2)
        self.assertEqual(json.loads(self.state_path.read_text())["ledger_sequence"], 1)
        restarted = subprocess.run(
            [
                sys.executable,
                str(worker),
                "evaluate",
                str(directory),
                "8000",
                (self.day_one + timedelta(hours=2)).isoformat(),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        decision = json.loads(restarted.stdout)
        self.assertTrue(decision["ready"])
        self.assertEqual(decision["high_water_mark"], "11000")
        self.assertTrue(decision["drawdown_halt"])
        self.assertTrue(decision["daily_loss_halt"])
        self.assertEqual(json.loads(self.state_path.read_text())["ledger_sequence"], 2)

    def test_eth_is_included_in_mark_to_market_value(self) -> None:
        portfolio = PaperPortfolio(Decimal("5000"), Decimal("2.5"))
        decision = evaluate_portfolio_risk(portfolio, Decimal("2000"), now=self.day_one)
        self.assertTrue(decision.ready)
        self.assertEqual(decision.current_value, Decimal("10000.0"))


if __name__ == "__main__":
    unittest.main()
