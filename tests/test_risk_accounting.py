from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.paper_portfolio import PaperPortfolio
from app.risk_accounting import evaluate_portfolio_risk


class RiskAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.state_path = Path(self.temporary_directory.name) / "risk.json"
        self.path_patch = patch(
            "app.risk_accounting.RISK_STATE_PATH",
            self.state_path,
        )
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.day_one = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)

    @staticmethod
    def portfolio(value: str) -> PaperPortfolio:
        return PaperPortfolio(
            usdc_balance=Decimal(value),
            eth_balance=Decimal("0"),
        )

    def evaluate(self, value: str, now: datetime | None = None):
        return evaluate_portfolio_risk(
            self.portfolio(value),
            Decimal("2000"),
            now=now or self.day_one,
        )

    def test_first_run_bootstraps_restart_safe_state(self) -> None:
        first = self.evaluate("10000")
        second = self.evaluate("9900", self.day_one + timedelta(hours=1))

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertEqual(second.high_water_mark, Decimal("10000"))
        self.assertEqual(second.daily_start_value, Decimal("10000"))
        self.assertEqual(second.drawdown_percent, Decimal("1.0000"))
        self.assertEqual(second.daily_loss_percent, Decimal("1.0000"))
        self.assertTrue(self.state_path.exists())

    def test_new_high_water_mark_is_persisted(self) -> None:
        self.evaluate("10000")
        higher = self.evaluate("11000", self.day_one + timedelta(hours=1))
        restarted = self.evaluate("10500", self.day_one + timedelta(hours=2))

        self.assertEqual(higher.high_water_mark, Decimal("11000"))
        self.assertEqual(restarted.high_water_mark, Decimal("11000"))
        self.assertEqual(restarted.drawdown_percent, Decimal("4.5455"))

    def test_daily_loss_limit_triggers_at_five_percent(self) -> None:
        self.evaluate("10000")
        decision = self.evaluate("9500", self.day_one + timedelta(hours=1))

        self.assertTrue(decision.ready)
        self.assertTrue(decision.daily_loss_halt)
        self.assertEqual(decision.daily_loss_percent, Decimal("5.0000"))

    def test_high_water_drawdown_triggers_at_twenty_percent(self) -> None:
        self.evaluate("10000")
        decision = self.evaluate("8000", self.day_one + timedelta(hours=1))

        self.assertTrue(decision.ready)
        self.assertTrue(decision.drawdown_halt)
        self.assertEqual(decision.drawdown_percent, Decimal("20.0000"))

    def test_complete_paper_loss_is_recorded_as_a_halt(self) -> None:
        self.evaluate("10000")
        decision = self.evaluate("0", self.day_one + timedelta(hours=1))

        self.assertTrue(decision.ready)
        self.assertTrue(decision.drawdown_halt)
        self.assertTrue(decision.daily_loss_halt)
        self.assertEqual(decision.drawdown_percent, Decimal("100.0000"))
        self.assertEqual(decision.daily_loss_percent, Decimal("100.0000"))

    def test_eth_is_included_in_mark_to_market_value(self) -> None:
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("5000"),
            eth_balance=Decimal("2.5"),
        )

        decision = evaluate_portfolio_risk(
            portfolio,
            Decimal("2000"),
            now=self.day_one,
        )

        self.assertTrue(decision.ready)
        self.assertEqual(decision.current_value, Decimal("10000.0"))

    def test_utc_day_rollover_resets_daily_start_only(self) -> None:
        self.evaluate("10000")
        self.evaluate("9600", self.day_one + timedelta(hours=1))
        next_day = self.evaluate("9400", self.day_one + timedelta(days=1))

        self.assertEqual(next_day.daily_start_value, Decimal("9600"))
        self.assertEqual(next_day.daily_loss_percent, Decimal("2.0833"))
        self.assertEqual(next_day.high_water_mark, Decimal("10000"))
        self.assertEqual(next_day.drawdown_percent, Decimal("6.0000"))

    def test_corrupted_state_fails_closed_without_overwrite(self) -> None:
        corrupted = "{not valid json"
        self.state_path.write_text(corrupted, encoding="utf-8")

        decision = self.evaluate("10000")

        self.assertFalse(decision.ready)
        self.assertIn("unavailable", decision.reason)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), corrupted)

    def test_clock_rollback_fails_closed(self) -> None:
        self.evaluate("10000")
        decision = self.evaluate("10000", self.day_one - timedelta(seconds=1))

        self.assertFalse(decision.ready)
        self.assertIn("clock moved", decision.reason)

    def test_unsupported_state_version_fails_closed(self) -> None:
        self.state_path.write_text(
            json.dumps({"version": 999}),
            encoding="utf-8",
        )

        decision = self.evaluate("10000")

        self.assertFalse(decision.ready)
        self.assertIn("unsupported", decision.reason)

    def test_state_write_failure_fails_closed(self) -> None:
        with patch(
            "app.risk_accounting._save_state",
            side_effect=OSError("disk unavailable"),
        ):
            decision = self.evaluate("10000")

        self.assertFalse(decision.ready)
        self.assertIn("disk unavailable", decision.reason)


if __name__ == "__main__":
    unittest.main()
