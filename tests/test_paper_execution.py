import unittest
from decimal import Decimal

from app.paper_execution import simulate_order
from app.strategy import Signal
from app.trading_cycle import TradeProposal


class PaperExecutionTests(unittest.TestCase):
    def test_buy_signal_creates_simulated_order(self) -> None:
        proposal = TradeProposal(
            signal=Signal.BUY,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
        )

        order = simulate_order(proposal)

        self.assertEqual(order.side, Signal.BUY)
        self.assertEqual(order.amount_usdc, Decimal("50"))
        self.assertEqual(order.quantity_eth, Decimal("0.025000"))
        self.assertEqual(order.status, "SIMULATED")

    def test_hold_signal_skips_order(self) -> None:
        proposal = TradeProposal(
            signal=Signal.HOLD,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
        )

        order = simulate_order(proposal)

        self.assertEqual(order.amount_usdc, Decimal("0"))
        self.assertEqual(order.quantity_eth, Decimal("0"))
        self.assertEqual(order.status, "SKIPPED")


if __name__ == "__main__":
    unittest.main()
