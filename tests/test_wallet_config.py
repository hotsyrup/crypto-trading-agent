import os
import unittest
from unittest.mock import patch

from app.wallet_config import load_wallet_config


class WalletConfigTests(unittest.TestCase):
    def test_loads_separate_trading_and_agentic_wallets(self) -> None:
        environment = {
            "BASE_TRADING_WALLET_NAME": "ihaveonefriend.base.eth",
            "BASE_TRADING_WALLET_ADDRESS": (
                "0x3c981ec319107be8b8bb614da0742fc5b28e8d9c"
            ),
            "LUMEN_AGENTIC_WALLET_ADDRESS": (
                "0xfDfaDd01eDcBaBE025931e45cdc8532B00218500"
            ),
        }

        with patch.dict(os.environ, environment, clear=True):
            config = load_wallet_config()

        self.assertEqual(config.trading_wallet_name, "ihaveonefriend.base.eth")
        self.assertEqual(
            config.trading_wallet_address,
            "0x3c981ec319107be8b8bb614da0742fc5b28e8d9c",
        )
        self.assertEqual(
            config.agentic_wallet_address,
            "0xfdfadd01edcbabe025931e45cdc8532b00218500",
        )

    def test_rejects_same_address_for_both_wallet_roles(self) -> None:
        address = "0x3c981ec319107be8b8bb614da0742fc5b28e8d9c"
        environment = {
            "BASE_TRADING_WALLET_NAME": "ihaveonefriend.base.eth",
            "BASE_TRADING_WALLET_ADDRESS": address,
            "LUMEN_AGENTIC_WALLET_ADDRESS": address,
        }

        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "must remain separate"):
                load_wallet_config()


if __name__ == "__main__":
    unittest.main()
