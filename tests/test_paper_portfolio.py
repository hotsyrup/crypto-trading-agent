import unittest
from decimal import Decimal

from app.paper_execution import PaperOrder
from app.paper_portfolio import PaperPortfolio, apply_order
from app.strategy import Signal


class PaperPortfolioTests(unittest.TestCase):
    def test_buy_order_updates_simulated_balances(self) -> None:
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("10000"),
            eth_balance=Decimal("0"),
        )
        order = PaperOrder(
            side=Signal.BUY,
            reference_price=Decimal("2000"),
            amount_usdc=Decimal("50"),
            quantity_eth=Decimal("0.025"),
            status="SIMULATED",
        )

        updated = apply_order(portfolio, order)

        self.assertEqual(updated.usdc_balance, Decimal("9950"))
        self.assertEqual(updated.eth_balance, Decimal("0.025"))

    def test_sell_cannot_exceed_simulated_eth_balance(self) -> None:
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("10000"),
            eth_balance=Decimal("0"),
        )
        order = PaperOrder(
            side=Signal.SELL,
            reference_price=Decimal("2000"),
            amount_usdc=Decimal("50"),
            quantity_eth=Decimal("0.025"),
            status="SIMULATED",
        )

        with self.assertRaises(ValueError):
            apply_order(portfolio, order)

    def test_hold_does_not_change_portfolio(self) -> None:
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("10000"),
            eth_balance=Decimal("0"),
        )
        order = PaperOrder(
            side=Signal.HOLD,
            reference_price=Decimal("2000"),
            amount_usdc=Decimal("0"),
            quantity_eth=Decimal("0"),
            status="SKIPPED",
        )

        self.assertEqual(apply_order(portfolio, order), portfolio)


if __name__ == "__main__":
    unittest.main()
