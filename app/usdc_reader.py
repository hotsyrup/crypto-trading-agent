import os

from dotenv import load_dotenv
from web3 import Web3

from app.base_connection import web3


load_dotenv()

USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


def get_usdc_balance() -> float:
    wallet_address = os.getenv("WALLET_ADDRESS")
    usdc_address = os.getenv("USDC_CONTRACT_ADDRESS")

    if not wallet_address:
        raise ValueError("WALLET_ADDRESS is missing.")

    if not usdc_address:
        raise ValueError("USDC_CONTRACT_ADDRESS is missing.")

    wallet = Web3.to_checksum_address(wallet_address)
    contract_address = Web3.to_checksum_address(usdc_address)

    usdc = web3.eth.contract(address=contract_address, abi=USDC_ABI)

    raw_balance = usdc.functions.balanceOf(wallet).call()
    decimals = usdc.functions.decimals().call()

    return raw_balance / (10 ** decimals)


if __name__ == "__main__":
    print(f"USDC balance: ${get_usdc_balance():,.2f}")
