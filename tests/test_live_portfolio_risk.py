import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.live_portfolio_risk import (
    LivePortfolioRiskError,
    record_live_portfolio_value,
)


NOW = datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc)


class LivePortfolioRiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "risk.jsonl"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def record(self, value: str, at: datetime = NOW):
        return record_live_portfolio_value(
            Decimal(value),
            authorized_capital_usdc=Decimal("500"),
            path=self.path,
            now=at,
        )

    def test_high_water_daily_loss_and_profit_above_capital_are_tracked(self) -> None:
        self.record("500")
        self.record("525", NOW + timedelta(minutes=1))
        risk = self.record("480", NOW + timedelta(minutes=2))

        self.assertEqual(risk.trading_capital_usdc, Decimal("500"))
        self.assertEqual(risk.portfolio_value_usdc, Decimal("480"))
        self.assertEqual(risk.daily_loss_percent, Decimal("4.0000"))
        self.assertEqual(risk.drawdown_percent, Decimal("8.5714"))

    def test_new_utc_day_uses_previous_mark_as_daily_start(self) -> None:
        self.record("500")
        self.record("480", NOW + timedelta(hours=1))
        risk = self.record("470", NOW + timedelta(days=1))
        self.assertEqual(risk.daily_loss_percent, Decimal("2.0833"))
        self.assertEqual(risk.drawdown_percent, Decimal("6.0000"))

    def test_capital_increase_corruption_and_clock_reversal_fail_closed(self) -> None:
        self.record("500")
        with self.assertRaises(LivePortfolioRiskError):
            record_live_portfolio_value(
                Decimal("500"),
                authorized_capital_usdc=Decimal("500.01"),
                path=self.path,
                now=NOW + timedelta(minutes=1),
            )
        with self.assertRaises(LivePortfolioRiskError):
            self.record("500", NOW - timedelta(seconds=1))
        self.path.write_text("not-json\n", encoding="utf-8")
        with self.assertRaises(LivePortfolioRiskError):
            self.record("500", NOW + timedelta(minutes=2))


if __name__ == "__main__":
    unittest.main()
