import unittest
from unittest.mock import patch

from app.performance_report import generate_report


class PerformanceReportTests(unittest.TestCase):
    @patch("app.performance_report.load_records")
    def test_generate_report_counts_signals(self, mock_load_records) -> None:
        mock_load_records.return_value = [
            {"signal": "BUY"},
            {"signal": "HOLD"},
            {"signal": "BUY"},
            {"signal": "SELL"},
        ]

        report = generate_report()

        self.assertEqual(report["total_decisions"], 4)
        self.assertEqual(report["buy_signals"], 2)
        self.assertEqual(report["sell_signals"], 1)
        self.assertEqual(report["hold_signals"], 1)
        self.assertEqual(report["latest_signal"], "SELL")


if __name__ == "__main__":
    unittest.main()
