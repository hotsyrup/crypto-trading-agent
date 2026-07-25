import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.trending_tokens import (
    TokenMetadata,
    TrendingPool,
    get_non_core_token,
    select_trial_candidates,
)


class TrendingTokenTests(unittest.TestCase):
    def create_pool(
        self,
        symbol: str = "VIRTUAL",
        liquidity: str = "500000",
        volume: str = "300000",
        hourly_change: str = "2",
        daily_change: str = "10",
    ) -> TrendingPool:
        token = TokenMetadata(
            address="0x0000000000000000000000000000000000000001",
            name=symbol,
            symbol=symbol,
            decimals=18,
            price_usd=Decimal("1"),
        )
        weth = TokenMetadata(
            address="0x4200000000000000000000000000000000000006",
            name="Wrapped Ether",
            symbol="WETH",
            decimals=18,
            price_usd=Decimal("2000"),
        )
        return TrendingPool(
            name=f"{symbol} / WETH",
            pool_address="0x0000000000000000000000000000000000000002",
            dex_id="test-dex",
            base_token=token,
            quote_token=weth,
            liquidity_usd=Decimal(liquidity),
            daily_volume_usd=Decimal(volume),
            hourly_change_percent=Decimal(hourly_change),
            daily_change_percent=Decimal(daily_change),
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )

    def test_non_core_momentum_pool_is_selected(self) -> None:
        candidate = self.create_pool()
        self.assertEqual(select_trial_candidates([candidate]), [candidate])
        self.assertEqual(get_non_core_token(candidate).symbol, "VIRTUAL")

    def test_core_only_pool_is_rejected(self) -> None:
        core_pool = self.create_pool(symbol="USDC")
        self.assertEqual(select_trial_candidates([core_pool]), [])

    def test_low_liquidity_pool_is_rejected(self) -> None:
        self.assertEqual(
            select_trial_candidates([self.create_pool(liquidity="5000")]),
            [],
        )

    def test_extreme_daily_move_is_rejected(self) -> None:
        self.assertEqual(
            select_trial_candidates([self.create_pool(daily_change="200")]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
