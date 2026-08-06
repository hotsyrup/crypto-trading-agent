import json
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def get_rpc_url() -> str:
    rpc_url = os.getenv("BASE_RPC_URL", "").strip()
    parsed = urlparse(rpc_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("BASE_RPC_URL must be a valid HTTPS endpoint.")
    return rpc_url


def rpc_call(method: str, params: list[object] | None = None) -> object:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    ).encode("utf-8")
    request = Request(
        get_rpc_url(),
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "lumen-monitor"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # nosec B310
        result = json.load(response)
    if "error" in result:
        raise RuntimeError(f"Base RPC error: {result['error']}")
    return result["result"]


def get_chain_id() -> int:
    return int(str(rpc_call("eth_chainId")), 16)


def check_connection() -> bool:
    try:
        return get_chain_id() == 8453
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False


if __name__ == "__main__":
    if check_connection():
        print("Successfully connected to Base.")
        print(f"Chain ID: {get_chain_id()}")
    else:
        print("Could not connect to Base mainnet.")
