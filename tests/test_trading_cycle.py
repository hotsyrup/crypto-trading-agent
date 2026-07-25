import unittest
from decimal import Decimal
from unittest.mock import patch

from app.strategy import Signal
from app.trading_cycle import TradeProposal, create_trade_proposal


class TradingCycleTests(unittest.TestCase):
    @patch("app.trading_cycle.generate_signal")
    @patch("app.trading_cycle.calculate_max_risk")
    @patch("app.trading_cycle.get_recent_closing_prices")
    def test_create_trade_proposal(
        self,
        mock_get_recent_closing_prices,
        mock_calculate_max_risk,
        mock_generate_signal,
    ) -> None:
        mock_get_recent_closing_prices.return_value = [
            Decimal("100"),
            Decimal("110"),
            Decimal("120"),
            Decimal("130"),
            Decimal("140"),
        ]
        mock_calculate_max_risk.return_value = Decimal("50.00")
        mock_generate_signal.return_value = Signal.BUY

        proposal = create_trade_proposal()

        self.assertIsInstance(proposal, TradeProposal)
        self.assertEqual(proposal.signal, Signal.BUY)
        self.assertEqual(proposal.reference_price, Decimal("140"))
        self.assertEqual(proposal.maximum_risk, Decimal("50.00"))
        self.assertTrue(proposal.paper_only)


if __name__ == "__main__":
    unittest.main()
