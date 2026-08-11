import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from app.market_data import (
    get_eth_usd_price,
    get_recent_closing_prices,
    get_recent_closing_prices_snapshot,
)


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

    @patch("app.market_data.get_json")
    def test_snapshot_preserves_latest_market_timestamp(
        self,
        mock_get_json,
    ) -> None:
        mock_get_json.return_value = [
            [100, 0, 0, 0, "100", 0],
            [200, 0, 0, 0, "200", 0],
        ]

        snapshot = get_recent_closing_prices_snapshot()

        self.assertEqual(snapshot.closing_prices, (Decimal("100"), Decimal("200")))
        self.assertEqual(
            snapshot.latest_observed_at,
            datetime.fromtimestamp(200, tz=timezone.utc),
        )
        self.assertIsNotNone(snapshot.received_at.tzinfo)


if __name__ == "__main__":
    unittest.main()
