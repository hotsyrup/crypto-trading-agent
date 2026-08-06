import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WalletConfig:
    trading_wallet_name: str
    trading_wallet_address: str
    agentic_wallet_address: str | None


def load_wallet_config() -> WalletConfig:
    trading_name = os.getenv("BASE_TRADING_WALLET_NAME", "").strip()
    trading_address = os.getenv("BASE_TRADING_WALLET_ADDRESS", "").strip()
    agentic_address = os.getenv("LUMEN_AGENTIC_WALLET_ADDRESS", "").strip()

    if not trading_name:
        raise ValueError("BASE_TRADING_WALLET_NAME is missing.")
    if not trading_address:
        raise ValueError("BASE_TRADING_WALLET_ADDRESS is missing.")
    address_pattern = re.compile(r"0x[0-9a-fA-F]{40}")

    if not address_pattern.fullmatch(trading_address):
        raise ValueError("BASE_TRADING_WALLET_ADDRESS is invalid.")
    if agentic_address and not address_pattern.fullmatch(agentic_address):
        raise ValueError("LUMEN_AGENTIC_WALLET_ADDRESS is invalid.")

    trading_normalized = trading_address.lower()
    agentic_normalized = agentic_address.lower() if agentic_address else None

    if agentic_normalized == trading_normalized:
        raise ValueError("Trading and agentic wallets must remain separate.")

    return WalletConfig(
        trading_wallet_name=trading_name,
        trading_wallet_address=trading_normalized,
        agentic_wallet_address=agentic_normalized,
    )
