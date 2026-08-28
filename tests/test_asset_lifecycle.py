import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.asset_lifecycle import AssetLifecycle, AssetLifecycleState
from app.base_asset_universe import GovernedAsset, GovernedAssetUniverse
from app.live_portfolio_worker import OnchainTokenBalance


NOW = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
MAG7_ADDRESS = "0x9e6a46f294bb67c20f1d1e7afb0bbef614403b55"
AERO_ADDRESS = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"


def universe(symbol: str, address: str) -> GovernedAssetUniverse:
    return GovernedAssetUniverse(
        observed_at=NOW - timedelta(minutes=5),
        source="cross-verified-test",
        snapshot_sha256="d" * 64,
        assets=(
            GovernedAsset(
                rank=1,
                symbol=symbol,
                name=symbol,
                token_address=address,
                decimals=8 if symbol == "MAG7.SSI" else 18,
                market_cap_usd=Decimal("1000000"),
                liquidity_usd=Decimal("2000000"),
                daily_volume_usd=Decimal("300000"),
                oldest_pool_created_at=NOW - timedelta(days=365),
            ),
        ),
    )


class AssetLifecycleTests(unittest.TestCase):
    def test_held_governed_asset_survives_candidate_refresh_by_exact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = AssetLifecycle(Path(directory) / "asset_lifecycle.json")
            balances = (
                OnchainTokenBalance(MAG7_ADDRESS, Decimal("0.00000106"), 8),
            )

            first = lifecycle.evaluate(
                universe("MAG7.SSI", MAG7_ADDRESS),
                balances,
                now=NOW,
            )
            restarted = AssetLifecycle(Path(directory) / "asset_lifecycle.json")
            second = restarted.evaluate(
                universe("AERO", AERO_ADDRESS),
                balances,
                now=NOW + timedelta(minutes=1),
            )

            self.assertEqual(first.held_governed[0].state, AssetLifecycleState.CANDIDATE)
            self.assertEqual(second.held_governed[0].state, AssetLifecycleState.RETAINED)
            self.assertEqual(second.held_governed[0].token_address, MAG7_ADDRESS)
            self.assertIn(MAG7_ADDRESS, second.required_research_contracts)
            self.assertNotIn(MAG7_ADDRESS, second.candidate_contracts)


if __name__ == "__main__":
    unittest.main()
