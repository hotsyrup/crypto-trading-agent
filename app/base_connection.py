import os

from dotenv import load_dotenv
from web3 import Web3


load_dotenv()

rpc_url = os.getenv("BASE_RPC_URL")

if not rpc_url:
    raise ValueError("BASE_RPC_URL is missing from the .env file.")

web3 = Web3(Web3.HTTPProvider(rpc_url))


def check_connection() -> bool:
    return web3.is_connected()


if __name__ == "__main__":
    if check_connection():
        print("Successfully connected to Base.")
        print(f"Chain ID: {web3.eth.chain_id}")
    else:
        print("Could not connect to Base.")
