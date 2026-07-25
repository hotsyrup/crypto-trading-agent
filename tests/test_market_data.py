import unittest
from decimal import Decimal
from unittest.mock import patch

from app.market_data import get_eth_usd_price, get_recent_closing_prices


class MarketDataTests(unittest.TestCase):
    @patch("app.market_data.get_json")
    def test_get_eth_usd_price_parses_decimal(self, mock_get_json) -> None:
        mock_get_json.return_value = {"price": "1234.56"}

        self.assertEqual(get_eth_usd_price(), Decimal("1234.56"))

    @patch("app.market_data.get_json")
    def test_recent_prices_are_sorted_and_limited(self, mock_get_json) -> None:
        mock_get_json.return_value = [
            [3, 0, 0, 0, "300", 0],
            [1, 0, 0, 0, "100", 0],
            [5, 0, 0, 0, "500", 0],
            [2, 0, 0, 0, "200", 0],
            [4, 0, 0, 0, "400", 0],
        ]

        self.assertEqual(
            get_recent_closing_prices(limit=3),
            [Decimal("300"), Decimal("400"), Decimal("500")],
        )


if __name__ == "__main__":
    unittest.main()
