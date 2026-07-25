import unittest
from decimal import Decimal

from app.backtest import run_backtest


class BacktestTests(unittest.TestCase):
    def test_backtest_completes_simulated_trade(self) -> None:
        prices = [
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("110"),
            Decimal("90"),
            Decimal("80"),
        ]

        result = run_backtest(prices)

        self.assertEqual(result.completed_trades, 1)
        self.assertLess(result.profit_loss, Decimal("0"))
        self.assertEqual(
            result.ending_value,
            result.starting_balance + result.profit_loss,
        )

    def test_backtest_requires_five_prices(self) -> None:
        prices = [Decimal("100"), Decimal("101")]

        with self.assertRaises(ValueError):
            run_backtest(prices)


if __name__ == "__main__":
    unittest.main()
