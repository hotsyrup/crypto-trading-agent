import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.base_asset_universe import (
    AssetUniverseError,
    load_governed_asset_universe,
)


NOW = datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)


def asset(
    rank: int,
    *,
    symbol: str | None = None,
    address: str | None = None,
    liquidity: str = "1000000",
    volume: str = "500000",
) -> dict[str, object]:
    native = rank == 1
    return {
        "rank": rank,
        "symbol": symbol or ("ETH" if native else f"TOKEN{rank}"),
        "name": "Ether" if native else f"Token {rank}",
        "token_address": (
            None
            if native
            else address or f"0x{rank:040x}"
        ),
        "decimals": 18,
        "market_cap_usd": "100000000",
        "liquidity_usd": liquidity,
        "daily_volume_usd": volume,
        "oldest_pool_created_at": "2025-01-01T00:00:00+00:00",
    }


def snapshot(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "network": "base-mainnet",
        "chain_id": 8453,
        "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
        "source": "coingecko-market-cap+geckoterminal-liquidity",
        "assets": [asset(rank) for rank in range(1, 26)],
    }
    value.update(updates)
    return value


class GovernedAssetUniverseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "universe.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, value: dict[str, object]) -> None:
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_current_snapshot_exposes_exactly_25_exact_contract_assets(self) -> None:
        self.write(snapshot())

        universe = load_governed_asset_universe(self.path, now=NOW)

        self.assertEqual(len(universe.assets), 25)
        self.assertEqual(universe.assets[0].symbol, "ETH")
        self.assertIsNone(universe.assets[0].token_address)
        self.assertEqual(
            universe.require("TOKEN2", "0x0000000000000000000000000000000000000002").rank,
            2,
        )

    def test_stale_wrong_chain_and_more_than_25_fail_closed(self) -> None:
        cases = (
            snapshot(observed_at=(NOW - timedelta(hours=25)).isoformat()),
            snapshot(chain_id=1),
            snapshot(assets=[asset(rank) for rank in range(1, 27)]),
        )
        for number, value in enumerate(cases):
            with self.subTest(number=number):
                self.write(value)
                with self.assertRaises(AssetUniverseError):
                    load_governed_asset_universe(self.path, now=NOW)

    def test_thin_asset_in_a_persisted_snapshot_fails_closed(self) -> None:
        assets = [asset(rank) for rank in range(1, 26)]
        assets[3] = asset(4, liquidity="99999")
        assets[4] = asset(5, volume="99999")
        self.write(snapshot(assets=assets))

        with self.assertRaisesRegex(AssetUniverseError, "live-eligible"):
            load_governed_asset_universe(self.path, now=NOW)

    def test_strictly_qualified_universe_may_contain_fewer_than_25_assets(self) -> None:
        self.write(snapshot(assets=[asset(rank) for rank in range(1, 25)]))

        universe = load_governed_asset_universe(self.path, now=NOW)

        self.assertEqual(len(universe.assets), 24)

    def test_empty_universe_fails_closed(self) -> None:
        self.write(snapshot(assets=[]))

        with self.assertRaisesRegex(AssetUniverseError, "between 1 and 25"):
            load_governed_asset_universe(self.path, now=NOW)

    def test_spoofed_symbol_duplicate_contract_and_native_alias_fail_closed(self) -> None:
        duplicate_symbol = [asset(rank) for rank in range(1, 26)]
        duplicate_symbol[2] = asset(3, symbol="TOKEN2")
        duplicate_contract = [asset(rank) for rank in range(1, 26)]
        duplicate_contract[2] = asset(3, address=f"0x{2:040x}")
        fake_eth = [asset(rank) for rank in range(1, 26)]
        fake_eth[2] = asset(3, symbol="ETH")
        for number, assets in enumerate(
            (duplicate_symbol, duplicate_contract, fake_eth)
        ):
            with self.subTest(number=number):
                self.write(snapshot(assets=assets))
                with self.assertRaises(AssetUniverseError):
                    load_governed_asset_universe(self.path, now=NOW)

    def test_symbol_alone_never_matches_an_erc20_asset(self) -> None:
        self.write(snapshot())
        universe = load_governed_asset_universe(self.path, now=NOW)

        self.assertFalse(universe.contains("TOKEN2", None))
        with self.assertRaises(AssetUniverseError):
            universe.require("TOKEN2", f"0x{3:040x}")


if __name__ == "__main__":
    unittest.main()
