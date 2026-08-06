import os
import re

from app.base_connection import rpc_call
from app.wallet_config import load_wallet_config


ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}")
BALANCE_OF_SELECTOR = "70a08231"
DECIMALS_SELECTOR = "313ce567"


def _eth_call(contract_address: str, data: str) -> int:
    if not ADDRESS_PATTERN.fullmatch(contract_address):
        raise ValueError("USDC_CONTRACT_ADDRESS is invalid.")
    result = rpc_call(
        "eth_call",
        [{"to": contract_address, "data": data}, "latest"],
    )
    return int(str(result), 16)


def get_usdc_balance() -> float:
    wallet_address = load_wallet_config().trading_wallet_address
    contract_address = os.getenv("USDC_CONTRACT_ADDRESS", "").strip()
    if not contract_address:
        raise ValueError("USDC_CONTRACT_ADDRESS is missing.")

    padded_wallet = wallet_address.removeprefix("0x").lower().rjust(64, "0")
    raw_balance = _eth_call(contract_address, f"0x{BALANCE_OF_SELECTOR}{padded_wallet}")
    decimals = _eth_call(contract_address, f"0x{DECIMALS_SELECTOR}")
    return raw_balance / (10**decimals)


if __name__ == "__main__":
    print(f"USDC balance: ${get_usdc_balance():,.2f}")
