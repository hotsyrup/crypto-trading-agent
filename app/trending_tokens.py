import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.request import Request, urlopen


TRENDING_URL = (
    "https://api.geckoterminal.com/api/v2/networks/base/"
    "trending_pools?include=base_token,quote_token,dex"
)
TOKEN_URL = "https://api.geckoterminal.com/api/v2/networks/base/tokens/{address}"
MINIMUM_LIQUIDITY_USD = Decimal("100000")
MINIMUM_DAILY_VOLUME_USD = Decimal("100000")
MINIMUM_POOL_AGE_DAYS = 7
MAXIMUM_DAILY_CHANGE_PERCENT = Decimal("50")
CORE_ASSETS = {"WETH", "ETH", "USDC", "USDT", "DAI", "CBBTC"}


@dataclass(frozen=True)
class TokenMetadata:
    address: str
    name: str
    symbol: str
    decimals: int
    price_usd: Decimal


@dataclass(frozen=True)
class TrendingPool:
    name: str
    pool_address: str
    dex_id: str
    base_token: TokenMetadata
    quote_token: TokenMetadata
    liquidity_usd: Decimal
    daily_volume_usd: Decimal
    hourly_change_percent: Decimal
    daily_change_percent: Decimal
    created_at: datetime


def to_decimal(value: object) -> Decimal:
    return Decimal("0") if value is None else Decimal(str(value))


def get_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "crypto-trading-agent"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def create_token_metadata(
    included_by_id: dict[str, dict],
    token_id: str,
    price_usd: object,
) -> TokenMetadata:
    attributes = included_by_id[token_id]["attributes"]
    return TokenMetadata(
        address=attributes["address"].lower(),
        name=attributes["name"],
        symbol=attributes["symbol"].upper(),
        decimals=int(attributes.get("decimals") or 0),
        price_usd=to_decimal(price_usd),
    )


def fetch_trending_pools() -> list[TrendingPool]:
    payload = get_json(TRENDING_URL)
    included_by_id = {
        item["id"]: item for item in payload.get("included", [])
    }
    pools = []

    for item in payload["data"]:
        attributes = item["attributes"]
        relationships = item["relationships"]
        base_id = relationships["base_token"]["data"]["id"]
        quote_id = relationships["quote_token"]["data"]["id"]

        pools.append(
            TrendingPool(
                name=attributes["name"],
                pool_address=attributes["address"].lower(),
                dex_id=relationships["dex"]["data"]["id"],
                base_token=create_token_metadata(
                    included_by_id,
                    base_id,
                    attributes.get("base_token_price_usd"),
                ),
                quote_token=create_token_metadata(
                    included_by_id,
                    quote_id,
                    attributes.get("quote_token_price_usd"),
                ),
                liquidity_usd=to_decimal(attributes.get("reserve_in_usd")),
                daily_volume_usd=to_decimal(
                    attributes.get("volume_usd", {}).get("h24")
                ),
                hourly_change_percent=to_decimal(
                    attributes.get("price_change_percentage", {}).get("h1")
                ),
                daily_change_percent=to_decimal(
                    attributes.get("price_change_percentage", {}).get("h24")
                ),
                created_at=datetime.fromisoformat(
                    attributes["pool_created_at"].replace("Z", "+00:00")
                ),
            )
        )

    return pools


def fetch_token_price(address: str) -> Decimal:
    payload = get_json(TOKEN_URL.format(address=address))
    return to_decimal(payload["data"]["attributes"].get("price_usd"))


def get_non_core_token(pool: TrendingPool) -> TokenMetadata | None:
    for token in (pool.base_token, pool.quote_token):
        if token.symbol not in CORE_ASSETS:
            return token
    return None


def contains_non_core_asset(pool: TrendingPool) -> bool:
    return get_non_core_token(pool) is not None


def select_trial_candidates(pools: list[TrendingPool]) -> list[TrendingPool]:
    oldest_allowed_creation = (
        datetime.now(timezone.utc) - timedelta(days=MINIMUM_POOL_AGE_DAYS)
    )
    candidates = [
        pool
        for pool in pools
        if pool.liquidity_usd >= MINIMUM_LIQUIDITY_USD
        and pool.daily_volume_usd >= MINIMUM_DAILY_VOLUME_USD
        and pool.created_at <= oldest_allowed_creation
        and contains_non_core_asset(pool)
        and pool.hourly_change_percent > 0
        and pool.daily_change_percent > 0
        and pool.daily_change_percent <= MAXIMUM_DAILY_CHANGE_PERCENT
    ]
    return sorted(candidates, key=lambda pool: pool.daily_volume_usd, reverse=True)


if __name__ == "__main__":
    trending_pools = fetch_trending_pools()
    candidates = select_trial_candidates(trending_pools)
    print(f"Trending Base pools received: {len(trending_pools)}")
    print(f"Pools passing initial safety filters: {len(candidates)}")

    for pool in candidates[:5]:
        token = get_non_core_token(pool)
        if token is None:
            continue
        print(
            f"{token.symbol} ({token.name}) | Contract: {token.address} | "
            f"Pool: {pool.name} | DEX: {pool.dex_id} | "
            f"Liquidity: ${pool.liquidity_usd:,.0f} | "
            f"24h volume: ${pool.daily_volume_usd:,.0f} | "
            f"1h change: {pool.hourly_change_percent}% | "
            f"24h change: {pool.daily_change_percent}%"
        )
