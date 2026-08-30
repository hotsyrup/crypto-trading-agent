from __future__ import annotations

import json
import os
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from app.base_asset_universe import (
    ADDRESS_PATTERN,
    BASE_MAINNET_CHAIN_ID,
    BASE_MAINNET_NETWORK,
    MAX_TRADABLE_ASSETS,
    MINIMUM_DAILY_VOLUME_USD,
    MINIMUM_LIQUIDITY_USD,
    MINIMUM_POOL_AGE,
)
from app.live_trading_config import BASE_USDC_ADDRESS


COINGECKO_HOST = "api.coingecko.com"
GECKOTERMINAL_HOST = "api.geckoterminal.com"
ALLOWED_HOSTS = {COINGECKO_HOST, GECKOTERMINAL_HOST}
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
MAX_CANDIDATES = 90
TOKEN_BATCH_SIZE = 30
GECKOTERMINAL_REQUEST_INTERVAL_SECONDS = 6.1


@dataclass(frozen=True)
class UniverseRefreshAssessment:
    snapshot: dict[str, object]
    diagnostics: dict[str, object]


class UniverseRefreshError(ValueError):
    def __init__(self, message: str, diagnostics: dict[str, object]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return parsed if parsed.is_finite() and parsed >= 0 else Decimal("0")


def _pool_created_at(pool: object) -> datetime | None:
    if not isinstance(pool, dict):
        return None
    attributes = pool.get("attributes")
    value = (
        attributes.get("pool_created_at")
        if isinstance(attributes, dict)
        else pool.get("pool_created_at")
    )
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def assess_cross_verified_snapshot(
    *,
    markets: list[object],
    coins: list[object],
    token_details: dict[str, object],
    pools_by_address: dict[str, list[object]],
    observed_at: datetime,
) -> UniverseRefreshAssessment:
    """Return a strictly qualified universe plus attributable rejection evidence."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Universe observation time must include a timezone.")
    observed_at = observed_at.astimezone(timezone.utc)
    base_addresses: dict[str, str] = {}
    for coin in coins:
        if not isinstance(coin, dict) or not isinstance(coin.get("platforms"), dict):
            continue
        address = str(coin["platforms"].get("base", "")).lower()
        if ADDRESS_PATTERN.fullmatch(address):
            base_addresses[str(coin.get("id", ""))] = address

    normalized_details = {
        address.lower(): detail
        for address, detail in token_details.items()
        if isinstance(address, str)
    }
    normalized_pools = {
        address.lower(): pools
        for address, pools in pools_by_address.items()
        if isinstance(address, str)
    }
    selected: list[dict[str, object]] = []
    selected_symbols: set[str] = set()
    selected_addresses: set[str] = set()
    rejected_assets: list[dict[str, object]] = []
    rejection_counts: Counter[str] = Counter()
    candidates_seen = 0
    eligible_beyond_capacity = 0
    for market in markets:
        if not isinstance(market, dict):
            rejection_counts["invalid_market_record"] += 1
            rejected_assets.append(
                {
                    "coin_id": None,
                    "symbol": None,
                    "token_address": None,
                    "reasons": ["invalid_market_record"],
                }
            )
            continue
        candidates_seen += 1
        coin_id = str(market.get("id", ""))
        market_symbol = str(market.get("symbol", "")).upper() or None
        address = base_addresses.get(str(market.get("id", "")))
        reasons: list[str] = []
        if not address:
            reasons.append("missing_base_contract")
        elif address == BASE_USDC_ADDRESS:
            reasons.append("official_usdc_excluded")
        if reasons:
            rejection_counts.update(reasons)
            rejected_assets.append(
                {
                    "coin_id": coin_id or None,
                    "symbol": market_symbol,
                    "token_address": address,
                    "reasons": reasons,
                }
            )
            continue
        details = normalized_details.get(address)
        if not isinstance(details, dict):
            reasons.append("missing_token_details")
            rejection_counts.update(reasons)
            rejected_assets.append(
                {
                    "coin_id": coin_id or None,
                    "symbol": market_symbol,
                    "token_address": address,
                    "reasons": reasons,
                }
            )
            continue
        if str(details.get("address", "")).lower() != address:
            reasons.append("token_address_mismatch")
        liquidity = _decimal(details.get("total_reserve_in_usd"))
        volume_data = details.get("volume_usd")
        volume = _decimal(
            volume_data.get("h24") if isinstance(volume_data, dict) else None
        )
        market_cap = _decimal(market.get("market_cap"))
        if market_cap <= 0:
            reasons.append("missing_market_cap")
        if liquidity < MINIMUM_LIQUIDITY_USD:
            reasons.append("liquidity_below_minimum")
        if volume < MINIMUM_DAILY_VOLUME_USD:
            reasons.append("volume_below_minimum")
        pool_times = [
            created
            for pool in normalized_pools.get(address, [])
            if (created := _pool_created_at(pool)) is not None
        ]
        if (
            not pool_times
            and liquidity >= MINIMUM_LIQUIDITY_USD
            and volume >= MINIMUM_DAILY_VOLUME_USD
        ):
            reasons.append("missing_pool_age")
            oldest_pool = None
        elif pool_times:
            oldest_pool = min(pool_times)
            if observed_at - oldest_pool < MINIMUM_POOL_AGE:
                reasons.append("pool_too_young")
        else:
            oldest_pool = None
        decimals = details.get("decimals")
        if type(decimals) is not int or not 0 <= decimals <= 36:
            reasons.append("invalid_decimals")
        native_eth = address == WETH_ADDRESS
        symbol = "ETH" if native_eth else str(details.get("symbol", "")).upper()
        name = "Ether" if native_eth else str(details.get("name", "")).strip()
        governed_address = None if native_eth else address
        if not symbol:
            reasons.append("missing_symbol")
        if not name:
            reasons.append("missing_name")
        if symbol in selected_symbols:
            reasons.append("duplicate_symbol")
        if address in selected_addresses:
            reasons.append("duplicate_contract")
        if reasons:
            rejection_counts.update(reasons)
            rejected_assets.append(
                {
                    "coin_id": coin_id or None,
                    "symbol": symbol or market_symbol,
                    "token_address": address,
                    "reasons": reasons,
                }
            )
            continue
        if len(selected) >= MAX_TRADABLE_ASSETS:
            eligible_beyond_capacity += 1
            continue
        selected_symbols.add(symbol)
        selected_addresses.add(address)
        selected.append(
            {
                "rank": len(selected) + 1,
                "symbol": symbol,
                "name": name,
                "token_address": governed_address,
                "decimals": decimals,
                "market_cap_usd": str(market_cap),
                "liquidity_usd": str(liquidity),
                "daily_volume_usd": str(volume),
                "oldest_pool_created_at": oldest_pool.isoformat(),
            }
        )
    diagnostics: dict[str, object] = {
        "schema_version": 1,
        "observed_at": observed_at.isoformat(),
        "candidates_seen": candidates_seen,
        "accepted_count": len(selected),
        "maximum_asset_count": MAX_TRADABLE_ASSETS,
        "eligible_beyond_capacity": eligible_beyond_capacity,
        "strict_thresholds": {
            "minimum_liquidity_usd": str(MINIMUM_LIQUIDITY_USD),
            "minimum_daily_volume_usd": str(MINIMUM_DAILY_VOLUME_USD),
            "minimum_pool_age_days": MINIMUM_POOL_AGE.days,
        },
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "rejected_assets": rejected_assets,
    }
    if not selected:
        raise UniverseRefreshError(
            "No cross-verified Base assets passed the strict universe policy.",
            diagnostics,
        )
    snapshot = {
        "schema_version": 1,
        "network": BASE_MAINNET_NETWORK,
        "chain_id": BASE_MAINNET_CHAIN_ID,
        "observed_at": observed_at.isoformat(),
        "source": (
            "coingecko-base-ecosystem-market-cap+"
            "geckoterminal-contract-liquidity-volume-pool-age"
        ),
        "assets": selected,
    }
    return UniverseRefreshAssessment(snapshot=snapshot, diagnostics=diagnostics)


def build_cross_verified_snapshot(
    *,
    markets: list[object],
    coins: list[object],
    token_details: dict[str, object],
    pools_by_address: dict[str, list[object]],
    observed_at: datetime,
) -> dict[str, object]:
    """Compatibility interface returning only the strictly qualified snapshot."""

    return assess_cross_verified_snapshot(
        markets=markets,
        coins=coins,
        token_details=token_details,
        pools_by_address=pools_by_address,
        observed_at=observed_at,
    ).snapshot


def _get_json(url: str) -> object:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("Only approved HTTPS market-data hosts are allowed.")
    headers = {"User-Agent": "lumen-base-universe/1"}
    if parsed.hostname == GECKOTERMINAL_HOST:
        headers["Accept"] = "application/json;version=20230203"
    request = Request(url, headers=headers)
    for attempt in range(5):
        try:
            with urlopen(request, timeout=20) as response:  # nosec B310
                return json.load(response)
        except HTTPError as error:
            if error.code != 429 or attempt == 4:
                raise
            time.sleep(GECKOTERMINAL_REQUEST_INTERVAL_SECONDS * (attempt + 1))
    raise RuntimeError("Market-data request retry loop ended unexpectedly.")


def _data_list(payload: object, label: str) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    raise ValueError(f"{label} response must contain a list.")


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            temporary_path = Path(handle.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _diagnostics_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}_diagnostics.json")


def _log_assessment(diagnostics: dict[str, object], *, status: str) -> None:
    rejected = diagnostics.get("rejected_assets", [])
    print(
        json.dumps(
            {
                "event": "asset_universe_refresh_assessed",
                "status": status,
                "observed_at": diagnostics.get("observed_at"),
                "accepted_count": diagnostics.get("accepted_count"),
                "maximum_asset_count": diagnostics.get("maximum_asset_count"),
                "candidates_seen": diagnostics.get("candidates_seen"),
                "rejection_counts": diagnostics.get("rejection_counts"),
                "rejected_assets": rejected[:25] if isinstance(rejected, list) else [],
                "rejected_assets_truncated": (
                    isinstance(rejected, list) and len(rejected) > 25
                ),
            }
        ),
        flush=True,
    )


def refresh_governed_asset_universe(path: Path) -> dict[str, object]:
    """Refresh up to 25 strictly qualified assets without execution access."""

    markets_query = urlencode(
        {
            "vs_currency": "usd",
            "category": "base-ecosystem",
            "order": "market_cap_desc",
            "per_page": "100",
            "page": "1",
            "sparkline": "false",
        }
    )
    markets = _data_list(
        _get_json(f"https://{COINGECKO_HOST}/api/v3/coins/markets?{markets_query}"),
        "CoinGecko markets",
    )
    coins = _data_list(
        _get_json(
            f"https://{COINGECKO_HOST}/api/v3/coins/list?include_platform=true"
        ),
        "CoinGecko coins",
    )
    base_by_id = {
        str(coin.get("id", "")): str(coin.get("platforms", {}).get("base", "")).lower()
        for coin in coins
        if isinstance(coin, dict) and isinstance(coin.get("platforms"), dict)
    }
    ordered_addresses = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        address = base_by_id.get(str(market.get("id", "")), "")
        if (
            ADDRESS_PATTERN.fullmatch(address)
            and address != BASE_USDC_ADDRESS
            and address not in ordered_addresses
        ):
            ordered_addresses.append(address)
        if len(ordered_addresses) >= MAX_CANDIDATES:
            break

    token_details: dict[str, object] = {}
    for offset in range(0, len(ordered_addresses), TOKEN_BATCH_SIZE):
        batch = ordered_addresses[offset : offset + TOKEN_BATCH_SIZE]
        payload = _get_json(
            f"https://{GECKOTERMINAL_HOST}/api/v2/networks/base/tokens/multi/"
            + ",".join(batch)
        )
        for item in _data_list(payload, "GeckoTerminal tokens"):
            if not isinstance(item, dict) or not isinstance(item.get("attributes"), dict):
                continue
            attributes = item["attributes"]
            address = str(attributes.get("address", "")).lower()
            if ADDRESS_PATTERN.fullmatch(address):
                token_details[address] = attributes

    pool_candidates = [
        address
        for address in ordered_addresses
        if isinstance(token_details.get(address), dict)
        and _decimal(token_details[address].get("total_reserve_in_usd"))
        >= MINIMUM_LIQUIDITY_USD
        and _decimal(
            token_details[address].get("volume_usd", {}).get("h24")
            if isinstance(token_details[address].get("volume_usd"), dict)
            else None
        )
        >= MINIMUM_DAILY_VOLUME_USD
    ]
    pools_by_address: dict[str, list[object]] = {}
    for number, address in enumerate(pool_candidates):
        if number:
            time.sleep(GECKOTERMINAL_REQUEST_INTERVAL_SECONDS)
        payload = _get_json(
            f"https://{GECKOTERMINAL_HOST}/api/v2/networks/base/tokens/"
            f"{address}/pools?page=1"
        )
        pools_by_address[address] = _data_list(payload, "GeckoTerminal pools")

    try:
        assessment = assess_cross_verified_snapshot(
            markets=markets,
            coins=coins,
            token_details=token_details,
            pools_by_address=pools_by_address,
            observed_at=datetime.now(timezone.utc),
        )
    except UniverseRefreshError as error:
        _write_json_atomically(_diagnostics_path(path), error.diagnostics)
        _log_assessment(error.diagnostics, status="rejected")
        raise
    _write_json_atomically(path, assessment.snapshot)
    _write_json_atomically(_diagnostics_path(path), assessment.diagnostics)
    _log_assessment(assessment.diagnostics, status="refreshed")
    return assessment.snapshot


def main() -> None:
    path = Path(
        os.getenv("LIVE_ASSET_UNIVERSE_PATH", "data/base_top25_universe.json")
    )
    snapshot = refresh_governed_asset_universe(path)
    print(
        json.dumps(
            {
                "status": "refreshed",
                "path": str(path),
                "observed_at": snapshot["observed_at"],
                "asset_count": len(snapshot["assets"]),
            }
        )
    )


if __name__ == "__main__":
    main()
