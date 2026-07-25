import json
import tempfile
import unittest
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
                )

            with journal_path.open("r", encoding="utf-8") as journal:
                record = json.loads(journal.readline())

            self.assertEqual(record["signal"], "SELL")
            self.assertEqual(record["reference_price"], "900.00")
            self.assertEqual(record["maximum_risk"], "25.00")
            self.assertTrue(record["paper_only"])
            self.assertFalse(record["risk_approved"])
            self.assertEqual(record["order_status"], "REJECTED")
            self.assertIn("timestamp", record)


if __name__ == "__main__":
    unittest.main()
