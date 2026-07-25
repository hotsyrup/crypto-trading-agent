import unittest
from decimal import Decimal

from app.strategy import Signal, generate_signal


class StrategyTests(unittest.TestCase):
    def test_buy_signal(self) -> None:
        prices = [Decimal(value) for value in ["1800", "1810", "1820", "1840", "1870"]]
        self.assertEqual(generate_signal(prices), Signal.BUY)

    def test_sell_signal(self) -> None:
        prices = [Decimal(value) for value in ["1870", "1840", "1820", "1810", "1800"]]
        self.assertEqual(generate_signal(prices), Signal.SELL)

    def test_hold_when_history_is_too_short(self) -> None:
        prices = [Decimal(value) for value in ["1800", "1810"]]
        self.assertEqual(generate_signal(prices), Signal.HOLD)


if __name__ == "__main__":
    unittest.main()
