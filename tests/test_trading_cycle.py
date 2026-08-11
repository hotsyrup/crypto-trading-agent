import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from app.market_data import MarketDataSnapshot
from app.strategy import Signal
from app.trading_cycle import TradeProposal, create_trade_proposal


class TradingCycleTests(unittest.TestCase):
    @patch("app.trading_cycle.generate_signal")
    @patch("app.trading_cycle.calculate_max_risk")
    @patch("app.trading_cycle.get_recent_closing_prices_snapshot")
    def test_create_trade_proposal(
        self,
        mock_get_recent_closing_prices_snapshot,
        mock_calculate_max_risk,
        mock_generate_signal,
    ) -> None:
        observed_at = datetime(2026, 8, 8, 10, tzinfo=timezone.utc)
        received_at = datetime(2026, 8, 8, 10, 1, tzinfo=timezone.utc)
        mock_get_recent_closing_prices_snapshot.return_value = MarketDataSnapshot(
            closing_prices=(
                Decimal("100"),
                Decimal("110"),
                Decimal("120"),
                Decimal("130"),
                Decimal("140"),
            ),
            latest_observed_at=observed_at,
            received_at=received_at,
        )
        mock_calculate_max_risk.return_value = Decimal("50.00")
        mock_generate_signal.return_value = Signal.BUY

        proposal = create_trade_proposal()

        self.assertIsInstance(proposal, TradeProposal)
        self.assertEqual(proposal.signal, Signal.BUY)
        self.assertEqual(proposal.reference_price, Decimal("140"))
        self.assertEqual(proposal.maximum_risk, Decimal("50.00"))
        self.assertTrue(proposal.paper_only)
        self.assertEqual(proposal.market_data_observed_at, observed_at)
        self.assertEqual(proposal.market_data_received_at, received_at)


if __name__ == "__main__":
    unittest.main()
