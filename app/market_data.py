import json
from decimal import Decimal
from urllib.parse import urlparse
from urllib.request import Request, urlopen


TICKER_URL = "https://api.exchange.coinbase.com/products/ETH-USD/ticker"
CANDLES_URL = (
    "https://api.exchange.coinbase.com/products/ETH-USD/candles"
    "?granularity=3600"
)
ALLOWED_API_HOSTS = {"api.exchange.coinbase.com"}


def get_json(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_API_HOSTS:
        raise ValueError("Only approved HTTPS market-data endpoints are allowed")
    request = Request(url, headers={"User-Agent": "crypto-trading-agent"})

    with urlopen(request, timeout=10) as response:  # nosec B310
        return json.load(response)


def get_eth_usd_price() -> Decimal:
    data = get_json(TICKER_URL)
    return Decimal(data["price"])


def get_eth_usdc_price() -> Decimal:
    return get_eth_usd_price()


def get_recent_closing_prices(limit: int = 5) -> list[Decimal]:
    candles = get_json(CANDLES_URL)
    candles.sort(key=lambda candle: candle[0])

    return [Decimal(str(candle[4])) for candle in candles[-limit:]]


if __name__ == "__main__":
    price = get_eth_usdc_price()
    recent_prices = get_recent_closing_prices()

    print(f"ETH/USD reference price: ${price:,.2f}")
    print(f"Recent hourly closing prices collected: {len(recent_prices)}")
