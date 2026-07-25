import json
from decimal import Decimal
from urllib.request import Request, urlopen


PRICE_URL = "https://api.exchange.coinbase.com/products/ETH-USD/ticker"


def get_eth_usdc_price() -> Decimal:
    request = Request(PRICE_URL, headers={"User-Agent": "crypto-trading-agent"})

    with urlopen(request, timeout=10) as response:
        data = json.load(response)

    return Decimal(data["price"])


if __name__ == "__main__":
    price = get_eth_usdc_price()
    print(f"ETH/USD reference price: ${price:,.2f}")
