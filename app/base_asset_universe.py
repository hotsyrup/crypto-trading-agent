from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


BASE_MAINNET_CHAIN_ID = 8453
BASE_MAINNET_NETWORK = "base-mainnet"
MAX_TRADABLE_ASSETS = 25
MAX_SNAPSHOT_AGE = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)
MINIMUM_LIQUIDITY_USD = Decimal("100000")
MINIMUM_DAILY_VOLUME_USD = Decimal("100000")
MINIMUM_POOL_AGE = timedelta(days=30)
ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}")
SYMBOL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,19}")
ZERO_ADDRESS = "0x" + "0" * 40


class AssetUniverseError(ValueError):
    """Raised when a governed Base asset snapshot is unsafe to use."""


@dataclass(frozen=True)
class GovernedAsset:
    rank: int
    symbol: str
    name: str
    token_address: str | None
    decimals: int
    market_cap_usd: Decimal
    liquidity_usd: Decimal
    daily_volume_usd: Decimal
    oldest_pool_created_at: datetime


@dataclass(frozen=True)
class GovernedAssetUniverse:
    observed_at: datetime
    source: str
    snapshot_sha256: str
    assets: tuple[GovernedAsset, ...]

    def contains(self, symbol: str, token_address: str | None) -> bool:
        try:
            self.require(symbol, token_address)
        except AssetUniverseError:
            return False
        return True

    def require(
        self,
        symbol: str,
        token_address: str | None,
    ) -> GovernedAsset:
        normalized_symbol = symbol.strip().upper()
        normalized_address = (
            token_address.strip().lower() if token_address is not None else None
        )
        for asset in self.assets:
            if (
                asset.symbol == normalized_symbol
                and asset.token_address == normalized_address
            ):
                return asset
        raise AssetUniverseError(
            "Asset symbol and exact Base contract are outside the governed universe."
        )


def _aware_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise AssetUniverseError(f"{label} must be an ISO-8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AssetUniverseError(
            f"{label} must be an ISO-8601 timestamp."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssetUniverseError(f"{label} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise AssetUniverseError(f"{label} must be a decimal value.") from error
    if not parsed.is_finite() or parsed < 0:
        raise AssetUniverseError(f"{label} must be finite and nonnegative.")
    return parsed


def _asset(item: object, *, now: datetime) -> GovernedAsset:
    if not isinstance(item, dict):
        raise AssetUniverseError("Each governed asset must be an object.")
    expected = {
        "rank",
        "symbol",
        "name",
        "token_address",
        "decimals",
        "market_cap_usd",
        "liquidity_usd",
        "daily_volume_usd",
        "oldest_pool_created_at",
    }
    if set(item) != expected:
        raise AssetUniverseError("Governed asset fields do not match schema 1.")

    rank = item["rank"]
    decimals = item["decimals"]
    if type(rank) is not int or not 1 <= rank <= MAX_TRADABLE_ASSETS:
        raise AssetUniverseError("Asset rank must be an integer from 1 through 25.")
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise AssetUniverseError("Token decimals must be an integer from 0 through 36.")

    symbol_value = item["symbol"]
    name_value = item["name"]
    if not isinstance(symbol_value, str) or not SYMBOL_PATTERN.fullmatch(symbol_value):
        raise AssetUniverseError("Asset symbol is invalid.")
    if not isinstance(name_value, str) or not name_value.strip():
        raise AssetUniverseError("Asset name is required.")
    symbol = symbol_value.upper()

    address_value = item["token_address"]
    if symbol == "ETH":
        if address_value is not None:
            raise AssetUniverseError("Native ETH must not have a token contract.")
        address = None
        if decimals != 18:
            raise AssetUniverseError("Native ETH must use 18 decimals.")
    else:
        if not isinstance(address_value, str) or not ADDRESS_PATTERN.fullmatch(
            address_value
        ):
            raise AssetUniverseError("ERC-20 assets require an exact Base contract.")
        address = address_value.lower()
        if address == ZERO_ADDRESS:
            raise AssetUniverseError("The zero address is not an ERC-20 asset.")

    pool_created = _aware_timestamp(
        item["oldest_pool_created_at"],
        "Oldest pool creation",
    )
    if pool_created > now + MAX_FUTURE_SKEW:
        raise AssetUniverseError("Pool creation timestamp is in the future.")

    return GovernedAsset(
        rank=rank,
        symbol=symbol,
        name=name_value.strip(),
        token_address=address,
        decimals=decimals,
        market_cap_usd=_decimal(item["market_cap_usd"], "Market cap"),
        liquidity_usd=_decimal(item["liquidity_usd"], "Liquidity"),
        daily_volume_usd=_decimal(item["daily_volume_usd"], "Daily volume"),
        oldest_pool_created_at=pool_created,
    )


def load_governed_asset_universe(
    path: Path,
    *,
    now: datetime | None = None,
) -> GovernedAssetUniverse:
    """Load one reviewed, strictly qualified top-25 snapshot.

    Ranking data proposes the universe; exact identity, freshness, liquidity,
    volume, and pool-age rules determine which entries can cross this interface.
    """

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise AssetUniverseError("Universe evaluation time must include a timezone.")
    current_time = current_time.astimezone(timezone.utc)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssetUniverseError("Governed asset snapshot is unavailable.") from error
    if not isinstance(payload, dict):
        raise AssetUniverseError("Governed asset snapshot must be an object.")
    expected = {
        "schema_version",
        "network",
        "chain_id",
        "observed_at",
        "source",
        "assets",
    }
    if set(payload) != expected or payload.get("schema_version") != 1:
        raise AssetUniverseError("Governed asset snapshot schema is invalid.")
    if (
        payload.get("network") != BASE_MAINNET_NETWORK
        or payload.get("chain_id") != BASE_MAINNET_CHAIN_ID
    ):
        raise AssetUniverseError("Governed asset snapshot is not Base mainnet.")
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip():
        raise AssetUniverseError("Governed asset snapshot source is required.")
    observed_at = _aware_timestamp(payload.get("observed_at"), "Snapshot observation")
    age = current_time - observed_at
    if age > MAX_SNAPSHOT_AGE:
        raise AssetUniverseError("Governed asset snapshot is stale.")
    if age < -MAX_FUTURE_SKEW:
        raise AssetUniverseError("Governed asset snapshot is from the future.")

    raw_assets = payload.get("assets")
    if (
        not isinstance(raw_assets, list)
        or not 1 <= len(raw_assets) <= MAX_TRADABLE_ASSETS
    ):
        raise AssetUniverseError(
            "Governed asset snapshot must contain between 1 and 25 assets."
        )
    parsed = tuple(_asset(item, now=current_time) for item in raw_assets)
    ranks = [item.rank for item in parsed]
    symbols = [item.symbol for item in parsed]
    addresses = [item.token_address for item in parsed if item.token_address is not None]
    if len(set(ranks)) != len(ranks):
        raise AssetUniverseError("Governed asset ranks must be unique.")
    if ranks != list(range(1, len(parsed) + 1)):
        raise AssetUniverseError("Governed asset ranks must be contiguous from 1.")
    if len(set(symbols)) != len(symbols):
        raise AssetUniverseError("Governed asset symbols must be unique.")
    if len(set(addresses)) != len(addresses):
        raise AssetUniverseError("Governed asset contracts must be unique.")
    if sum(item.symbol == "ETH" for item in parsed) > 1:
        raise AssetUniverseError("Native ETH may appear only once.")

    eligible = tuple(
        sorted(
            (
                item
                for item in parsed
                if item.liquidity_usd >= MINIMUM_LIQUIDITY_USD
                and item.daily_volume_usd >= MINIMUM_DAILY_VOLUME_USD
                and current_time - item.oldest_pool_created_at >= MINIMUM_POOL_AGE
            ),
            key=lambda item: item.rank,
        )
    )
    if len(eligible) != len(parsed):
        raise AssetUniverseError(
            "Every governed asset in the snapshot must remain live-eligible."
        )
    return GovernedAssetUniverse(
        observed_at=observed_at,
        source=source.strip(),
        snapshot_sha256=hashlib.sha256(raw).hexdigest(),
        assets=eligible,
    )
