import unittest
from decimal import Decimal
from unittest.mock import patch

from app.paper_portfolio import PaperPortfolio
from app.performance_report import generate_report


class PerformanceReportTests(unittest.TestCase):
    @patch("app.performance_report.get_eth_usd_price")
    @patch("app.performance_report.load_portfolio")
    @patch("app.performance_report.load_records")
    def test_generate_report_calculates_profit_and_loss(
        self,
        mock_load_records,
        mock_load_portfolio,
        mock_get_eth_usd_price,
    ) -> None:
        mock_load_records.return_value = [
            {"signal": "BUY"},
            {"signal": "HOLD"},
            {"signal": "SELL"},
        ]
        mock_load_portfolio.return_value = PaperPortfolio(
            usdc_balance=Decimal("5000"),
            eth_balance=Decimal("2"),
        )
        mock_get_eth_usd_price.return_value = Decimal("2000")

        report = generate_report()

        self.assertEqual(report["total_decisions"], 3)
        self.assertEqual(report["buy_signals"], 1)
        self.assertEqual(report["sell_signals"], 1)
        self.assertEqual(report["hold_signals"], 1)
        self.assertEqual(report["total_value"], Decimal("9000"))
        self.assertEqual(report["profit_loss"], Decimal("-1000"))


if __name__ == "__main__":
    unittest.main()
