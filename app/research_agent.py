"""Free, observation-only Base market research worker for Railway.

The worker reads public DEX Screener data and stores normalized research
packets.  It deliberately contains no wallet, signing, order, or messaging
integration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.base_asset_universe import AssetUniverseError, load_governed_asset_universe
from app.base_asset_universe_refresh import refresh_governed_asset_universe

DEXSCREENER_ORIGIN = "https://api.dexscreener.com"
ALLOWED_API_HOST = "api.dexscreener.com"
ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
MAX_API_CANDIDATES = 30
MAX_REQUIRED_CONTRACTS = 50
REQUIRED_PACKET_MAX_AGE = timedelta(seconds=90)
MAX_PROVIDER_ATTEMPTS = 3
RESEARCH_SCHEMA_VERSION = 2
BASE_RESEARCH_PATH = "/research/crypto/base/latest"
LEGACY_RESEARCH_PATH = "/research/latest"
EQUITIES_RESEARCH_PATH = "/research/equities/latest"
BITCOIN_NETWORK_RESEARCH_PATH = "/research/bitcoin-network/latest"
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
DEFAULT_WATCHLIST = (
    "0x4200000000000000000000000000000000000006",  # WETH on Base
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC on Base
)
WETH_CONTRACT = DEFAULT_WATCHLIST[0]
USDC_CONTRACT = DEFAULT_WATCHLIST[1]
USDBC_CONTRACT = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
APPROVED_QUOTE_CONTRACTS = {
    WETH_CONTRACT: {USDC_CONTRACT},
    USDC_CONTRACT: {USDBC_CONTRACT},
}
DISCOVERY_QUOTE_CONTRACTS = {WETH_CONTRACT, USDC_CONTRACT, USDBC_CONTRACT}
DISCOVERY_FEEDS = (
    ("/token-profiles/latest/v1", "dexscreener_latest_profile", "profile"),
    ("/token-boosts/latest/v1", "dexscreener_latest_boost", "boost"),
    ("/token-boosts/top/v1", "dexscreener_top_boost", "boost"),
    ("/ads/latest/v1", "dexscreener_latest_ad", "advertisement"),
)

STATE: dict[str, object] = {
    "mode": "observation_only",
    "status": "starting",
    "last_cycle_at": None,
    "packets_stored": 0,
    "last_error": None,
}
RESEARCH_PROVIDER_LOCK = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _plain_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def get_json(path: str) -> object:
    """Fetch JSON only from the fixed DEX Screener HTTPS origin."""
    url = urljoin(f"{DEXSCREENER_ORIGIN}/", path.lstrip("/"))
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_API_HOST:
        raise ValueError("Only the approved DEX Screener HTTPS host is allowed.")
    request = Request(url, headers={"User-Agent": "lumen-railway-research/1"})
    for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=10) as response:  # nosec B310
                return json.load(response)
        except HTTPError as error:
            if error.code not in RETRYABLE_HTTP_STATUS or attempt == MAX_PROVIDER_ATTEMPTS:
                raise
        except URLError:
            if attempt == MAX_PROVIDER_ATTEMPTS:
                raise
        time.sleep(attempt)
    raise RuntimeError("DEX Screener retry loop ended unexpectedly.")


def discover_base_contracts(
    limit: int,
    configured_addresses: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    """Return recent Base token profiles, explicitly retaining source bias."""
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for address in configured_addresses:
        seen.add(address.lower())
        candidates.append(
            {
                "contract_address": address.lower(),
                "profile_url": None,
                "description": None,
                "discovery_source": "configured_watchlist",
                "marketing_influenced": False,
                "promotion_type": None,
            }
        )
        if len(candidates) >= limit:
            return candidates

    for path, source_name, promotion_type in DISCOVERY_FEEDS:
        payload = get_json(path)
        if not isinstance(payload, list):
            raise ValueError("DEX Screener discovery response must be a list.")
        for profile in payload:
            if not isinstance(profile, dict) or profile.get("chainId") != "base":
                continue
            address = str(profile.get("tokenAddress", ""))
            if not ADDRESS_PATTERN.fullmatch(address) or address.lower() in seen:
                continue
            seen.add(address.lower())
            candidates.append(
                {
                    "contract_address": address.lower(),
                    "profile_url": profile.get("url"),
                    "description": profile.get("description"),
                    "discovery_source": source_name,
                    "marketing_influenced": True,
                    "promotion_type": promotion_type,
                }
            )
            if len(candidates) >= limit:
                return candidates
    return candidates


def fetch_pairs(addresses: list[str]) -> list[dict[str, object]]:
    if not addresses:
        return []
    if len(addresses) > MAX_API_CANDIDATES:
        raise ValueError("DEX Screener accepts at most 30 token addresses per batch.")
    if any(not ADDRESS_PATTERN.fullmatch(address) for address in addresses):
        raise ValueError("Every Base token address must be a full hex contract address.")
    pairs: list[dict[str, object]] = []
    for address in addresses:
        payload = get_json(f"/tokens/v1/base/{address}")
        if not isinstance(payload, list):
            raise ValueError("DEX Screener tokens response must be a list.")
        pairs.extend(pair for pair in payload if isinstance(pair, dict))
    return pairs


def _pair_liquidity(pair: dict[str, object]) -> Decimal:
    liquidity = pair.get("liquidity")
    value = liquidity.get("usd") if isinstance(liquidity, dict) else None
    return _decimal(value) or Decimal("0")


def _pair_has_complete_core_metrics(
    pair: dict[str, object],
    contract_address: str,
    *,
    pair_created_at_fallback: datetime | None = None,
) -> bool:
    volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    changes = pair.get("priceChange") if isinstance(pair.get("priceChange"), dict) else {}
    transactions = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    h24_transactions = (
        transactions.get("h24") if isinstance(transactions.get("h24"), dict) else {}
    )
    required = [
        pair.get("priceUsd"),
        _pair_liquidity(pair),
        volume.get("h24"),
        volume.get("h6"),
        h24_transactions.get("buys"),
        h24_transactions.get("sells"),
    ]
    if pair.get("pairCreatedAt") is None and pair_created_at_fallback is None:
        required.append(None)
    if contract_address.lower() != USDC_CONTRACT:
        required.extend((changes.get("h24"), changes.get("h6")))
    return all(value is not None for value in required)


def eligible_base_pairs(
    contract_address: str,
    pairs: list[dict[str, object]],
    approved_quote_addresses: set[str] | None = None,
    *,
    pair_created_at_fallback: datetime | None = None,
) -> list[dict[str, object]]:
    """Return Base pools where the researched contract is the base token."""
    eligible = []
    for pair in pairs:
        base_token = pair.get("baseToken")
        quote_token = pair.get("quoteToken")
        base_address = (
            str(base_token.get("address", ""))
            if isinstance(base_token, dict)
            else ""
        )
        quote_address = (
            str(quote_token.get("address", ""))
            if isinstance(quote_token, dict)
            else ""
        )
        if (
            pair.get("chainId") == "base"
            and base_address.lower() == contract_address.lower()
            and (
                approved_quote_addresses is None
                or quote_address.lower() in approved_quote_addresses
            )
            and _pair_has_complete_core_metrics(
                pair,
                contract_address,
                pair_created_at_fallback=pair_created_at_fallback,
            )
        ):
            eligible.append(pair)
    return eligible


def select_primary_pair(
    contract_address: str,
    pairs: list[dict[str, object]],
) -> dict[str, object] | None:
    """Choose the most liquid Base pair where the researched token is base."""
    eligible = eligible_base_pairs(contract_address, pairs)
    return max(eligible, key=_pair_liquidity, default=None)


def build_packet(
    profile: dict[str, object],
    pair: dict[str, object] | None,
    received_at: datetime,
    minimum_liquidity_usd: Decimal,
    freshness_minutes: int,
    eligible_pair_count: int = 0,
) -> dict[str, object]:
    address = str(profile["contract_address"]).lower()
    warnings = [
        "CONTRACT_SECURITY_NOT_VERIFIED",
        "HOLDER_CONCENTRATION_NOT_VERIFIED",
    ]
    if profile.get("marketing_influenced") is not False:
        warnings.append("DISCOVERY_SOURCE_MAY_REFLECT_TOKEN_MARKETING")
    metrics: dict[str, object] = {}
    token = {}
    pair_address = None
    dex_id = None
    base_contract_address = None
    quote_contract_address = None
    pair_created_at_provider = None

    if pair is None:
        warnings.append("NO_BASE_DENOMINATED_PAIR_FOUND")
    else:
        token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        quote_token = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
        liquidity = _pair_liquidity(pair)
        volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
        changes = (
            pair.get("priceChange")
            if isinstance(pair.get("priceChange"), dict)
            else {}
        )
        transactions = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
        boosts = pair.get("boosts") if isinstance(pair.get("boosts"), dict) else {}
        h24_transactions = (
            transactions.get("h24")
            if isinstance(transactions.get("h24"), dict)
            else {}
        )
        created_ms = pair.get("pairCreatedAt")
        created_at = None
        if isinstance(created_ms, (int, float)):
            created_at = datetime.fromtimestamp(created_ms / 1000, timezone.utc)
            pair_created_at_provider = "dexscreener"
        elif isinstance(profile.get("pair_created_at_fallback"), datetime):
            fallback = profile["pair_created_at_fallback"]
            if fallback.tzinfo is not None and fallback.utcoffset() is not None:
                created_at = fallback.astimezone(timezone.utc)
                pair_created_at_provider = "geckoterminal_governed_universe"
        if created_at is not None and received_at - created_at < timedelta(days=7):
            warnings.append("PAIR_YOUNGER_THAN_7_DAYS")
        if liquidity < minimum_liquidity_usd:
            warnings.append("LIQUIDITY_BELOW_RESEARCH_THRESHOLD")
        metrics = {
            "price_usd": _plain_decimal(_decimal(pair.get("priceUsd"))),
            "liquidity_usd": _plain_decimal(liquidity),
            "volume_h24_usd": _plain_decimal(_decimal(volume.get("h24"))),
            "volume_h6_usd": _plain_decimal(_decimal(volume.get("h6"))),
            "price_change_h24_percent": _plain_decimal(
                _decimal(changes.get("h24"))
            ),
            "price_change_h6_percent": _plain_decimal(
                _decimal(changes.get("h6"))
            ),
            "buys_h24": h24_transactions.get("buys"),
            "sells_h24": h24_transactions.get("sells"),
            "pair_created_at": created_at.isoformat() if created_at else None,
            "market_cap_usd": _plain_decimal(_decimal(pair.get("marketCap"))),
            "fdv_usd": _plain_decimal(_decimal(pair.get("fdv"))),
            "active_boosts": int(boosts.get("active", 0) or 0),
        }
        core_metric_names = {
            "price_usd", "liquidity_usd", "volume_h24_usd", "volume_h6_usd",
            "price_change_h24_percent", "price_change_h6_percent", "buys_h24",
            "sells_h24", "pair_created_at",
        }
        if any(metrics[name] is None for name in core_metric_names):
            warnings.append("MARKET_FIELDS_INCOMPLETE")
        pair_address = pair.get("pairAddress")
        dex_id = pair.get("dexId")
        base_contract_address = str(token.get("address", "")).lower() or None
        quote_contract_address = str(quote_token.get("address", "")).lower() or None

    payload: dict[str, object] = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "network": "base",
        "contract_address": address,
        "symbol": token.get("symbol"),
        "name": token.get("name"),
        "pair_address": pair_address,
        "dex_id": dex_id,
        "received_at": received_at.isoformat(),
        "expires_at": (received_at + timedelta(minutes=freshness_minutes)).isoformat(),
        "source": {
            "provider": "dexscreener",
            "discovery": profile["discovery_source"],
            "profile_url": profile.get("profile_url"),
            "marketing_influenced": profile.get("marketing_influenced") is not False,
            "promotion_type": profile.get("promotion_type"),
            "eligible_pair_count": eligible_pair_count,
            "base_contract_address": base_contract_address,
            "quote_contract_address": quote_contract_address,
            "pair_created_at_provider": pair_created_at_provider,
        },
        "metrics": metrics,
        "warnings": sorted(set(warnings)),
        "data_quality": (
            "partial"
            if pair is None or "MARKET_FIELDS_INCOMPLETE" in warnings
            else "complete"
        ),
        "recommendation": "OBSERVE_ONLY",
        "execution_authorized": False,
    }
    digest_source = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["packet_id"] = hashlib.sha256(digest_source.encode()).hexdigest()
    return payload


def store_packets(database_path: Path, packets: list[dict[str, object]]) -> int:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_packets (
                packet_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL,
                contract_address TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        before = connection.total_changes
        connection.executemany(
            """
            INSERT OR IGNORE INTO research_packets
                (packet_id, received_at, contract_address, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    str(packet["packet_id"]),
                    str(packet["received_at"]),
                    str(packet["contract_address"]),
                    json.dumps(packet, sort_keys=True),
                )
                for packet in packets
            ],
        )
        return connection.total_changes - before


def load_latest_packets(
    database_path: Path,
    limit: int = MAX_API_CANDIDATES,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Read recent public packets without creating or modifying the database."""
    if not 1 <= limit <= 50:
        raise ValueError("Research packet read limit must be between 1 and 50.")
    if not database_path.exists():
        return []
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT payload_json
            FROM research_packets
            ORDER BY received_at DESC, packet_id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    current_time = now or _utc_now()
    packets = []
    for (payload_json,) in rows:
        packet = json.loads(payload_json)
        expires_at = datetime.fromisoformat(str(packet["expires_at"]))
        packet["is_stale"] = expires_at <= current_time
        packets.append(packet)
    return packets


def load_latest_packets_for_contracts(
    database_path: Path,
    contracts: tuple[str, ...],
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Return exactly the newest stored packet for each requested contract."""

    if not contracts:
        return []
    if len(contracts) > MAX_REQUIRED_CONTRACTS:
        raise ValueError("Required research coverage exceeds 50 contracts.")
    normalized = tuple(dict.fromkeys(contract.lower() for contract in contracts))
    if any(not ADDRESS_PATTERN.fullmatch(contract) for contract in normalized):
        raise ValueError("Required research coverage contains an invalid contract.")
    if not database_path.exists():
        return []
    placeholders = ",".join("?" for _ in normalized)
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            f"""
            SELECT contract_address, payload_json
            FROM research_packets
            WHERE contract_address IN ({placeholders})
            ORDER BY received_at DESC, packet_id ASC
            """,  # nosec B608 -- placeholders are generated, not user-controlled
            normalized,
        ).fetchall()
    newest: dict[str, dict[str, object]] = {}
    current_time = now or _utc_now()
    for contract_address, payload_json in rows:
        contract = str(contract_address).lower()
        if contract in newest:
            continue
        packet = json.loads(payload_json)
        expires_at = datetime.fromisoformat(str(packet["expires_at"]))
        packet["is_stale"] = expires_at <= current_time
        newest[contract] = packet
    return [newest[contract] for contract in normalized if contract in newest]


def required_contracts_from_path(path: str) -> tuple[str, ...]:
    parsed = urlparse(path)
    if parsed.path != BASE_RESEARCH_PATH or parsed.fragment:
        return ()
    if not parsed.query:
        return ()
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) != {"required_contracts"} or len(query["required_contracts"]) != 1:
        raise ValueError("Research coverage query fields are invalid.")
    values = tuple(
        dict.fromkeys(
            contract.strip().lower()
            for contract in query["required_contracts"][0].split(",")
            if contract.strip()
        )
    )
    if not values or len(values) > MAX_REQUIRED_CONTRACTS:
        raise ValueError("Research coverage query must contain 1 through 50 contracts.")
    if any(not ADDRESS_PATTERN.fullmatch(contract) for contract in values):
        raise ValueError("Research coverage query contains an invalid contract.")
    return values


def _build_contract_packets(
    contracts: tuple[str, ...],
    *,
    minimum_liquidity: Decimal,
    freshness: int,
) -> list[dict[str, object]]:
    profiles = discover_base_contracts(len(contracts), contracts)
    pairs: list[dict[str, object]] = []
    with RESEARCH_PROVIDER_LOCK:
        for offset in range(0, len(contracts), MAX_API_CANDIDATES):
            pairs.extend(
                fetch_pairs(list(contracts[offset : offset + MAX_API_CANDIDATES]))
            )
    received_at = _utc_now()
    packets = []
    for profile in profiles:
        contract_address = str(profile["contract_address"])
        approved_quotes = APPROVED_QUOTE_CONTRACTS.get(
            contract_address,
            DISCOVERY_QUOTE_CONTRACTS,
        )
        eligible = eligible_base_pairs(contract_address, pairs, approved_quotes)
        packets.append(
            build_packet(
                profile,
                max(eligible, key=_pair_liquidity, default=None),
                received_at,
                minimum_liquidity,
                freshness,
                len(eligible),
            )
        )
    return packets


def ensure_required_contract_packets(
    contracts: tuple[str, ...],
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Refresh only missing/aged exact contracts, then prove complete coverage."""

    current_time = now or _utc_now()
    _, _, minimum_liquidity, freshness, database_path, _ = load_config()
    existing = load_latest_packets_for_contracts(
        database_path,
        contracts,
        now=current_time,
    )
    by_contract = {
        str(packet["contract_address"]).lower(): packet for packet in existing
    }
    refresh = []
    for contract in contracts:
        packet = by_contract.get(contract)
        if packet is None or packet["is_stale"] is True:
            refresh.append(contract)
            continue
        received_at = datetime.fromisoformat(str(packet["received_at"]))
        if current_time - received_at > REQUIRED_PACKET_MAX_AGE:
            refresh.append(contract)
    if refresh:
        packets = _build_contract_packets(
            tuple(refresh),
            minimum_liquidity=minimum_liquidity,
            freshness=freshness,
        )
        store_packets(database_path, packets)
    result = load_latest_packets_for_contracts(
        database_path,
        contracts,
        now=current_time,
    )
    covered = {str(packet["contract_address"]).lower() for packet in result}
    if covered != set(contracts):
        raise ValueError("Required exact-contract research coverage is incomplete.")
    return result


def load_config() -> tuple[int, int, Decimal, int, Path, tuple[str, ...]]:
    if os.getenv("RESEARCH_MODE", "observation_only").strip().lower() != "observation_only":
        raise ValueError("RESEARCH_MODE must remain observation_only.")
    forbidden_flags = ("LIVE_TRADING_ENABLED", "BANKR_ENABLED", "AIXBT_ENABLED")
    for flag in forbidden_flags:
        if os.getenv(flag, "false").strip().lower() == "true":
            raise ValueError(f"{flag} must remain false for the free research service.")
    interval = int(os.getenv("RESEARCH_INTERVAL_SECONDS", "3600"))
    limit = int(os.getenv("RESEARCH_MAX_CANDIDATES", "10"))
    freshness = int(os.getenv("RESEARCH_FRESHNESS_MINUTES", "90"))
    minimum_liquidity = Decimal(os.getenv("RESEARCH_MIN_LIQUIDITY_USD", "50000"))
    if not 60 <= interval <= 86400:
        raise ValueError("RESEARCH_INTERVAL_SECONDS must be between 60 and 86400.")
    if not 1 <= limit <= MAX_API_CANDIDATES:
        raise ValueError("RESEARCH_MAX_CANDIDATES must be between 1 and 30.")
    if not 5 <= freshness <= 1440:
        raise ValueError("RESEARCH_FRESHNESS_MINUTES must be between 5 and 1440.")
    if minimum_liquidity < 0:
        raise ValueError("RESEARCH_MIN_LIQUIDITY_USD cannot be negative.")
    database_path = Path(os.getenv("RESEARCH_DB_PATH", "data/research_packets.sqlite3"))
    watchlist = tuple(
        address.strip().lower()
        for address in os.getenv(
            "RESEARCH_WATCHLIST",
            ",".join(DEFAULT_WATCHLIST),
        ).split(",")
        if address.strip()
    )
    universe_path = os.getenv("RESEARCH_ASSET_UNIVERSE_PATH", "").strip()
    if universe_path:
        path = Path(universe_path)
        try:
            universe = load_governed_asset_universe(path)
        except AssetUniverseError:
            if (
                os.getenv("RESEARCH_REFRESH_ASSET_UNIVERSE", "false")
                .strip()
                .lower()
                != "true"
            ):
                raise
            refresh_governed_asset_universe(path)
            universe = load_governed_asset_universe(path)
        governed_watchlist = tuple(
            WETH_CONTRACT if asset.token_address is None else asset.token_address
            for asset in universe.assets
        )
        if len(governed_watchlist) > limit:
            raise ValueError(
                "Governed research universe cannot exceed RESEARCH_MAX_CANDIDATES."
            )
        watchlist = tuple(dict.fromkeys((*governed_watchlist, USDC_CONTRACT)))
    if any(not ADDRESS_PATTERN.fullmatch(address) for address in watchlist):
        raise ValueError("RESEARCH_WATCHLIST contains an invalid contract address.")
    if not universe_path and len(watchlist) > limit:
        raise ValueError("RESEARCH_WATCHLIST cannot exceed RESEARCH_MAX_CANDIDATES.")
    if len(watchlist) > MAX_API_CANDIDATES:
        raise ValueError("Research watchlist cannot exceed the provider batch limit.")
    return interval, limit, minimum_liquidity, freshness, database_path, watchlist


def run_research_cycle() -> int:
    _, limit, minimum_liquidity, freshness, database_path, watchlist = load_config()
    cycle_started_at = _utc_now()
    profiles = discover_base_contracts(len(watchlist), watchlist)
    universe_path = os.getenv("RESEARCH_ASSET_UNIVERSE_PATH", "").strip()
    if universe_path:
        governed = load_governed_asset_universe(Path(universe_path))
        pair_age_by_contract = {
            (asset.token_address or WETH_CONTRACT): asset.oldest_pool_created_at
            for asset in governed.assets
        }
        for profile in profiles:
            profile["pair_created_at_fallback"] = pair_age_by_contract.get(
                str(profile["contract_address"])
            )
    addresses = [str(profile["contract_address"]) for profile in profiles]
    with RESEARCH_PROVIDER_LOCK:
        pairs = fetch_pairs(addresses)
    received_at = _utc_now()
    if received_at < cycle_started_at:
        raise ValueError("Research provider cycle completion precedes its start.")
    packets = []
    for profile in profiles:
        contract_address = str(profile["contract_address"])
        approved_quotes = APPROVED_QUOTE_CONTRACTS.get(
            contract_address,
            DISCOVERY_QUOTE_CONTRACTS,
        )
        eligible = eligible_base_pairs(
            contract_address,
            pairs,
            approved_quotes,
            pair_created_at_fallback=profile.get("pair_created_at_fallback"),
        )
        packets.append(
            build_packet(
                profile,
                max(eligible, key=_pair_liquidity, default=None),
                received_at,
                minimum_liquidity,
                freshness,
                len(eligible),
            )
        )
    inserted = store_packets(database_path, packets)
    STATE.update(
        status="healthy",
        last_cycle_at=received_at.isoformat(),
        packets_stored=inserted,
        last_error=None,
    )
    print(json.dumps(STATE), flush=True)
    return inserted


def public_health_state() -> dict[str, object]:
    return {
        "service": "lumen-base-research-agent",
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "mode": STATE["mode"],
        "status": STATE["status"],
        "last_cycle_at": STATE["last_cycle_at"],
        "packets_stored": STATE["packets_stored"],
        "providers": {"dexscreener": "enabled", "aixbt": "disabled", "bankr": "disabled"},
        "execution": "disabled",
    }


def public_route_response(path: str) -> tuple[int, dict[str, object]] | None:
    """Return one public read-only response without implying unbuilt capability."""
    if path in {"/", "/health"}:
        return (200 if STATE["status"] != "failed" else 503, public_health_state())
    parsed = urlparse(path)
    if parsed.path in {BASE_RESEARCH_PATH, LEGACY_RESEARCH_PATH}:
        if parsed.path == LEGACY_RESEARCH_PATH and parsed.query:
            raise ValueError("Legacy research route does not accept coverage queries.")
        database_path = Path(
            os.getenv("RESEARCH_DB_PATH", "data/research_packets.sqlite3")
        )
        required = required_contracts_from_path(path)
        packets = (
            ensure_required_contract_packets(required)
            if required
            else load_latest_packets(database_path)
        )
        return (
            200 if STATE["status"] != "failed" else 503,
            {
                "service": "lumen-base-research-agent",
                "schema_version": RESEARCH_SCHEMA_VERSION,
                "mode": "observation_only",
                "execution": "disabled",
                "generated_at": _utc_now().isoformat(),
                "packets": packets,
            },
        )
    unavailable_domains = {
        EQUITIES_RESEARCH_PATH: "equities",
        BITCOIN_NETWORK_RESEARCH_PATH: "bitcoin-network",
    }
    domain = unavailable_domains.get(path)
    if domain is not None:
        return (
            501,
            {
                "service": "lumen-research-agent",
                "schema_version": 1,
                "domain": domain,
                "status": "not_configured",
                "mode": "observation_only",
                "execution": "disabled",
                "packets": [],
            },
        )
    return None


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        try:
            result = public_route_response(self.path)
        except (HTTPError, URLError, TimeoutError):
            result = (
                503,
                {
                    "service": "lumen-base-research-agent",
                    "schema_version": RESEARCH_SCHEMA_VERSION,
                    "mode": "observation_only",
                    "execution": "disabled",
                    "status": "provider_unavailable",
                    "packets": [],
                },
            )
        except ValueError:
            result = (
                400,
                {
                    "service": "lumen-base-research-agent",
                    "schema_version": RESEARCH_SCHEMA_VERSION,
                    "mode": "observation_only",
                    "execution": "disabled",
                    "status": "invalid_request",
                    "packets": [],
                },
            )
        if result is None:
            self.send_error(404)
            return
        status_code, response = result
        payload = json.dumps(response).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class TimedHTTPServer(HTTPServer):
    request_timeout_seconds = 5.0

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address


def serve_health() -> TimedHTTPServer:
    server = TimedHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), HealthHandler)  # nosec B104
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    serve_health()
    interval = int(os.getenv("RESEARCH_INTERVAL_SECONDS", "3600"))
    if not 60 <= interval <= 86400:
        raise ValueError("RESEARCH_INTERVAL_SECONDS must be between 60 and 86400.")
    while True:
        try:
            run_research_cycle()
        except Exception as error:  # keep health endpoint available for diagnosis
            error_code = getattr(error, "code", None)
            error_name = type(error).__name__
            STATE.update(
                status="failed",
                last_error=(
                    f"{error_name}:{error_code}"
                    if error_code is not None
                    else error_name
                ),
            )
            print(json.dumps(STATE), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
