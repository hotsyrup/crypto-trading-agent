import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.strategy import Signal
from app.trade_journal import record_decision


class TradeJournalTests(unittest.TestCase):
    def test_record_decision_writes_risk_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            journal_path = Path(temporary_directory) / "trade_journal.jsonl"

            with patch("app.trade_journal.JOURNAL_PATH", journal_path):
                record_decision(
                    signal=Signal.SELL,
                    reference_price=Decimal("900.00"),
                    maximum_risk=Decimal("25.00"),
                    paper_only=True,
                    risk_approved=False,
                    risk_reason="No simulated ETH is available to sell.",
                    order_status="REJECTED",
                    market_data_observed_at=datetime(
                        2026, 8, 8, 10, tzinfo=timezone.utc
                    ),
                    safety_gate_allowed=False,
                    safety_gate_reason="Market data is stale.",
                    kill_switch_state="armed",
                    market_data_age_seconds=7201,
                    accounting_ready=True,
                    accounting_reason="Daily loss limit reached.",
                    portfolio_value=Decimal("9400"),
                    high_water_mark=Decimal("10000"),
                    daily_start_value=Decimal("10000"),
                    drawdown_percent=Decimal("6.0000"),
                    daily_loss_percent=Decimal("6.0000"),
                    accounting_date="2026-08-08",
                )

            with journal_path.open("r", encoding="utf-8") as journal:
                record = json.loads(journal.readline())

            self.assertEqual(record["signal"], "SELL")
            self.assertEqual(record["reference_price"], "900.00")
            self.assertEqual(record["maximum_risk"], "25.00")
            self.assertTrue(record["paper_only"])
            self.assertFalse(record["risk_approved"])
            self.assertEqual(record["order_status"], "REJECTED")
            self.assertFalse(record["safety_gate_allowed"])
            self.assertEqual(record["safety_gate_reason"], "Market data is stale.")
            self.assertEqual(record["kill_switch_state"], "armed")
            self.assertEqual(record["market_data_age_seconds"], 7201)
            self.assertTrue(record["accounting_ready"])
            self.assertEqual(record["portfolio_value"], "9400")
            self.assertEqual(record["high_water_mark"], "10000")
            self.assertEqual(record["daily_start_value"], "10000")
            self.assertEqual(record["drawdown_percent"], "6.0000")
            self.assertEqual(record["daily_loss_percent"], "6.0000")
            self.assertEqual(record["accounting_date"], "2026-08-08")
            self.assertEqual(
                record["market_data_observed_at"],
                "2026-08-08T10:00:00+00:00",
            )
            self.assertIn("timestamp", record)


if __name__ == "__main__":
    unittest.main()
