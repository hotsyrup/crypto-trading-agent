import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from app.live_trading_config import load_live_trading_config


class LiveTradingConfigTests(unittest.TestCase):
    def test_defaults_match_adopted_mandate_and_fail_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_live_trading_config()

        self.assertFalse(config.enabled)
        self.assertEqual(config.approved_assets, frozenset({"USDC", "ETH"}))
        self.assertEqual(config.max_position_percent, Decimal("20"))
        self.assertEqual(config.max_new_strategy_percent, Decimal("5"))
        self.assertEqual(config.max_daily_loss_percent, Decimal("5"))
        self.assertEqual(config.max_drawdown_percent, Decimal("20"))

    def test_rejects_asset_outside_initial_mandate(self) -> None:
        with patch.dict(
            os.environ,
            {"LIVE_APPROVED_ASSETS": "USDC,ETH,AOMI"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "require amendment"):
                load_live_trading_config()

    def test_rejects_new_strategy_limit_above_position_limit(self) -> None:
        environment = {
            "LIVE_MAX_POSITION_PERCENT": "10",
            "LIVE_MAX_NEW_STRATEGY_PERCENT": "15",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "cannot exceed"):
                load_live_trading_config()


if __name__ == "__main__":
    unittest.main()
