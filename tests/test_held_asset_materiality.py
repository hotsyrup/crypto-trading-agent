from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app.held_asset_materiality import DexScreenerDustPriceReader
from app.live_trading_config import BASE_USDC_ADDRESS


NOW = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
TOKEN = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"


def pair(address: str, price: str, *, chain: str = "base") -> dict[str, object]:
    return {
        "chainId": chain,
        "baseToken": {"address": address},
        "quoteToken": {"address": BASE_USDC_ADDRESS},
        "priceUsd": price,
    }


class HeldAssetMaterialityTests(unittest.TestCase):
    @patch("app.held_asset_materiality.fetch_pairs")
    def test_reader_uses_highest_exact_base_price_with_safety_margin(self, fetch) -> None:
        fetch.return_value = [
            pair(TOKEN, "0.40"),
            pair(TOKEN, "0.50"),
            pair(TOKEN, "999", chain="ethereum"),
            pair(OTHER, "999"),
        ]
        reader = DexScreenerDustPriceReader()

        result = reader.read_conservative_prices((TOKEN,), now=NOW)[TOKEN]

        self.assertEqual(result.price_usd, Decimal("0.50"))
        self.assertEqual(result.conservative_price_usd, Decimal("0.6250"))

    @patch("app.held_asset_materiality.fetch_pairs", return_value=[])
    def test_reader_batches_and_caches_misses(self, fetch) -> None:
        contracts = tuple(f"0x{number:040x}" for number in range(1, 32))
        reader = DexScreenerDustPriceReader()

        first = reader.read_conservative_prices(contracts, now=NOW)
        second = reader.read_conservative_prices(
            contracts,
            now=NOW + timedelta(minutes=9),
        )

        self.assertEqual(first, {})
        self.assertEqual(second, {})
        self.assertEqual([len(call.args[0]) for call in fetch.call_args_list], [30, 1])


if __name__ == "__main__":
    unittest.main()
