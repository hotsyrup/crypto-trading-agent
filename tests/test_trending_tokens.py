import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.trending_tokens import TrendingPool, select_trial_candidates


class TrendingTokenTests(unittest.TestCase):
    def create_pool(
        self,
        name: str,
        liquidity: str = "500000",
        volume: str = "300000",
        hourly_change: str = "2",
        daily_change: str = "10",
    ) -> TrendingPool:
        return TrendingPool(
            name=name,
            pool_address="0x0000000000000000000000000000000000000001",
            price_usd=Decimal("1"),
            liquidity_usd=Decimal(liquidity),
            daily_volume_usd=Decimal(volume),
            hourly_change_percent=Decimal(hourly_change),
            daily_change_percent=Decimal(daily_change),
            created_at=(datetime.now(timezone.utc) - timedelta(days=30)),
        )

    def test_non_core_momentum_pool_is_selected(self) -> None:
        candidate = self.create_pool("VIRTUAL / WETH 0.3%")

        selected = select_trial_candidates([candidate])

        self.assertEqual(selected, [candidate])

    def test_core_only_pool_is_rejected(self) -> None:
        core_pool = self.create_pool("WETH / USDC 0.05%")

        selected = select_trial_candidates([core_pool])

        self.assertEqual(selected, [])

    def test_low_liquidity_pool_is_rejected(self) -> None:
        low_liquidity = self.create_pool(
            "TOKEN / WETH",
            liquidity="5000",
        )

        selected = select_trial_candidates([low_liquidity])

        self.assertEqual(selected, [])

    def test_extreme_daily_move_is_rejected(self) -> None:
        extreme_move = self.create_pool(
            "TOKEN / WETH",
            daily_change="200",
        )

        selected = select_trial_candidates([extreme_move])

        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
