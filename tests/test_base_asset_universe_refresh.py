import unittest
from datetime import datetime, timedelta, timezone

from app.base_asset_universe_refresh import build_cross_verified_snapshot


NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
WETH = "0x4200000000000000000000000000000000000006"


class AssetUniverseRefreshTests(unittest.TestCase):
    def test_market_cap_rank_is_cross_checked_against_onchain_liquidity_and_age(self) -> None:
        markets = []
        coins = []
        token_details = {}
        pools_by_address = {}
        for number in range(1, 29):
            coin_id = f"coin-{number}"
            address = WETH if number == 1 else f"0x{number:040x}"
            markets.append(
                {
                    "id": coin_id,
                    "name": "Wrapped Ether" if number == 1 else f"Token {number}",
                    "symbol": "weth" if number == 1 else f"token{number}",
                    "market_cap": 1_000_000_000 - number,
                    "total_volume": 1_000_000,
                }
            )
            coins.append({"id": coin_id, "platforms": {"base": address}})
            token_details[address] = {
                "address": address,
                "name": "Wrapped Ether" if number == 1 else f"Token {number}",
                "symbol": "WETH" if number == 1 else f"TOKEN{number}",
                "decimals": 18,
                "total_reserve_in_usd": "1000000" if number != 2 else "99999",
                "volume_usd": {"h24": "500000"},
            }
            pools_by_address[address] = [
                {
                    "pool_created_at": (
                        NOW - (timedelta(days=10) if number == 3 else timedelta(days=365))
                    ).isoformat()
                }
            ]

        snapshot = build_cross_verified_snapshot(
            markets=markets,
            coins=coins,
            token_details=token_details,
            pools_by_address=pools_by_address,
            observed_at=NOW,
        )

        self.assertEqual(len(snapshot["assets"]), 25)
        self.assertEqual(snapshot["assets"][0]["symbol"], "ETH")
        self.assertIsNone(snapshot["assets"][0]["token_address"])
        addresses = {item["token_address"] for item in snapshot["assets"]}
        self.assertNotIn(f"0x{2:040x}", addresses)
        self.assertNotIn(f"0x{3:040x}", addresses)
        self.assertEqual(
            [item["rank"] for item in snapshot["assets"]],
            list(range(1, 26)),
        )


if __name__ == "__main__":
    unittest.main()
