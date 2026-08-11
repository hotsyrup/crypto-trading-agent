import os
import unittest
from unittest.mock import patch

from app.live_asset_policy import evaluate_asset_identity
from app.live_trading_config import BASE_USDC_ADDRESS, load_live_trading_config


class LiveAssetPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.config = load_live_trading_config()

    def test_accepts_exact_base_usdc_contract(self) -> None:
        decision = evaluate_asset_identity(
            symbol="USDC",
            token_address=BASE_USDC_ADDRESS,
            unsolicited=False,
            config=self.config,
        )
        self.assertTrue(decision.allowed)

    def test_rejects_spoofed_usdc_symbol(self) -> None:
        decision = evaluate_asset_identity(
            symbol="USDC",
            token_address="0x" + "1" * 40,
            unsolicited=False,
            config=self.config,
        )
        self.assertFalse(decision.allowed)

    def test_rejects_unsolicited_asset_even_with_approved_symbol(self) -> None:
        decision = evaluate_asset_identity(
            symbol="USDC",
            token_address=BASE_USDC_ADDRESS,
            unsolicited=True,
            config=self.config,
        )
        self.assertFalse(decision.allowed)

    def test_accepts_native_eth_without_contract(self) -> None:
        decision = evaluate_asset_identity(
            symbol="ETH",
            token_address=None,
            unsolicited=False,
            config=self.config,
        )
        self.assertTrue(decision.allowed)

    def test_rejects_wrapped_or_spoofed_eth_as_native_eth(self) -> None:
        decision = evaluate_asset_identity(
            symbol="ETH",
            token_address="0x4200000000000000000000000000000000000006",
            unsolicited=False,
            config=self.config,
        )
        self.assertFalse(decision.allowed)


if __name__ == "__main__":
    unittest.main()
