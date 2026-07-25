import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.request import Request, urlopen


TRENDING_URL = (
    "https://api.geckoterminal.com/api/v2/"
    "networks/base/trending_pools"
)

MINIMUM_LIQUIDITY_USD = Decimal("100000")
MINIMUM_DAILY_VOLUME_USD = Decimal("100000")
MINIMUM_POOL_AGE_DAYS = 7
MAXIMUM_DAILY_CHANGE_PERCENT = Decimal("50")
CORE_ASSETS = {
    "WETH",
    "ETH",
    "USDC",
    "USDT",
    "DAI",
    "cbBTC",
    "CBBTC",
}


@dataclass(frozen=True)
class TrendingPool:
    name: str
    pool_address: str
    price_usd: Decimal
    liquidity_usd: Decimal
    daily_volume_usd: Decimal
    hourly_change_percent: Decimal
    daily_change_percent: Decimal
    created_at: datetime


def fetch_trending_pools() -> list[TrendingPool]:
    request = Request(
        TRENDING_URL,
        headers={"User-Agent": "crypto-trading-agent"},
    )

    with urlopen(request, timeout=10) as response:
        payload = json.load(response)

    pools = []

    for item in payload["data"]:
        attributes = item["attributes"]

        pools.append(
            TrendingPool(
                name=attributes["name"],
                pool_address=attributes["address"],
                price_usd=Decimal(attributes["base_token_price_usd"] or "0"),
                liquidity_usd=Decimal(attributes["reserve_in_usd"] or "0"),
                daily_volume_usd=Decimal(attributes["volume_usd"].get("h24") or "0"),
                hourly_change_percent=Decimal(
                    attributes["price_change_percentage"].get("h1") or "0"
                ),
                daily_change_percent=Decimal(
                    attributes["price_change_percentage"].get("h24") or "0"
                ),
                created_at=datetime.fromisoformat(
                    attributes["pool_created_at"].replace("Z", "+00:00")
                ),
            )
        )

    return pools


def contains_non_core_asset(pool: TrendingPool) -> bool:
    pair = pool.name.split(" / ")

    if len(pair) < 2:
        return False

    first_asset = pair[0].strip()
    second_asset = pair[1].split()[0].strip()

    return first_asset not in CORE_ASSETS or second_asset not in CORE_ASSETS


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
        print(
            f"{pool.name} | "
            f"Liquidity: ${pool.liquidity_usd:,.0f} | "
            f"24h volume: ${pool.daily_volume_usd:,.0f} | "
            f"1h change: {pool.hourly_change_percent}% | "
            f"24h change: {pool.daily_change_percent}%"
        )
