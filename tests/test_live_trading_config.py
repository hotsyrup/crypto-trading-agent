import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from app.live_trading_config import BASE_USDC_ADDRESS, load_live_trading_config


class LiveTradingConfigTests(unittest.TestCase):
    def test_defaults_match_adopted_mandate_and_fail_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_live_trading_config()

        self.assertFalse(config.enabled)
        self.assertEqual(config.approved_assets, frozenset({"USDC", "ETH"}))
        self.assertEqual(
            config.approved_erc20_contracts,
            frozenset({BASE_USDC_ADDRESS}),
        )
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

    def test_rejects_nonfinite_percentage_limits_as_value_errors(self) -> None:
        names = (
            "LIVE_MAX_POSITION_PERCENT",
            "LIVE_MAX_NEW_STRATEGY_PERCENT",
            "LIVE_MAX_DAILY_LOSS_PERCENT",
            "LIVE_MAX_DRAWDOWN_PERCENT",
        )
        for name in names:
            for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
                with self.subTest(name=name, value=value):
                    with patch.dict(os.environ, {name: value}, clear=True):
                        with self.assertRaisesRegex(
                            ValueError,
                            f"{name} must be a finite decimal percentage",
                        ):
                            load_live_trading_config()

    def test_rejects_spoofed_or_unreviewed_token_contract(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LIVE_APPROVED_ERC20_CONTRACTS": (
                    BASE_USDC_ADDRESS + ",0x" + "1" * 40
                )
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "require amendment"):
                load_live_trading_config()

    def test_rejects_usdc_without_exact_base_contract(self) -> None:
        with patch.dict(
            os.environ,
            {"LIVE_APPROVED_ERC20_CONTRACTS": ""},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "exact approved contract"):
                load_live_trading_config()


if __name__ == "__main__":
    unittest.main()
