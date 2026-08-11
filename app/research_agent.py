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
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


DEXSCREENER_ORIGIN = "https://api.dexscreener.com"
ALLOWED_API_HOST = "api.dexscreener.com"
ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
MAX_API_CANDIDATES = 30
DEFAULT_WATCHLIST = (
    "0x4200000000000000000000000000000000000006",  # WETH on Base
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC on Base
)
DISCOVERY_FEEDS = (
    ("/token-profiles/latest/v1", "dexscreener_latest_profile"),
    ("/token-boosts/latest/v1", "dexscreener_latest_boost"),
    ("/token-boosts/top/v1", "dexscreener_top_boost"),
)

STATE: dict[str, object] = {
    "mode": "observation_only",
    "status": "starting",
    "last_cycle_at": None,
    "packets_stored": 0,
    "last_error": None,
}


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
    with urlopen(request, timeout=10) as response:  # nosec B310
        return json.load(response)


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
            }
        )
        if len(candidates) >= limit:
            return candidates

    for path, source_name in DISCOVERY_FEEDS:
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
    payload = get_json(f"/tokens/v1/base/{','.join(addresses)}")
    if not isinstance(payload, list):
        raise ValueError("DEX Screener pairs response must be a list.")
    return [pair for pair in payload if isinstance(pair, dict)]


def _pair_liquidity(pair: dict[str, object]) -> Decimal:
    liquidity = pair.get("liquidity")
    value = liquidity.get("usd") if isinstance(liquidity, dict) else None
    return _decimal(value) or Decimal("0")


def select_primary_pair(
    contract_address: str,
    pairs: list[dict[str, object]],
) -> dict[str, object] | None:
    """Choose the most liquid Base pair where the researched token is base."""
    eligible = []
    for pair in pairs:
        base_token = pair.get("baseToken")
        base_address = (
            str(base_token.get("address", ""))
            if isinstance(base_token, dict)
            else ""
        )
        if (
            pair.get("chainId") == "base"
            and base_address.lower() == contract_address.lower()
        ):
            eligible.append(pair)
    return max(eligible, key=_pair_liquidity, default=None)


def build_packet(
    profile: dict[str, object],
    pair: dict[str, object] | None,
    received_at: datetime,
    minimum_liquidity_usd: Decimal,
    freshness_minutes: int,
) -> dict[str, object]:
    address = str(profile["contract_address"]).lower()
    warnings = [
        "DISCOVERY_SOURCE_MAY_REFLECT_TOKEN_MARKETING",
        "CONTRACT_SECURITY_NOT_VERIFIED",
        "HOLDER_CONCENTRATION_NOT_VERIFIED",
    ]
    metrics: dict[str, object] = {}
    token = {}
    pair_address = None
    dex_id = None

    if pair is None:
        warnings.append("NO_BASE_DENOMINATED_PAIR_FOUND")
    else:
        token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        liquidity = _pair_liquidity(pair)
        volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
        changes = (
            pair.get("priceChange")
            if isinstance(pair.get("priceChange"), dict)
            else {}
        )
        transactions = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
        h24_transactions = (
            transactions.get("h24")
            if isinstance(transactions.get("h24"), dict)
            else {}
        )
        created_ms = pair.get("pairCreatedAt")
        created_at = None
        if isinstance(created_ms, (int, float)):
            created_at = datetime.fromtimestamp(created_ms / 1000, timezone.utc)
            if received_at - created_at < timedelta(days=7):
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
        }
        if any(value is None for value in metrics.values()):
            warnings.append("MARKET_FIELDS_INCOMPLETE")
        pair_address = pair.get("pairAddress")
        dex_id = pair.get("dexId")

    payload: dict[str, object] = {
        "schema_version": 1,
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
    limit: int = 20,
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
    if not 300 <= interval <= 86400:
        raise ValueError("RESEARCH_INTERVAL_SECONDS must be between 300 and 86400.")
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
    if any(not ADDRESS_PATTERN.fullmatch(address) for address in watchlist):
        raise ValueError("RESEARCH_WATCHLIST contains an invalid contract address.")
    if len(watchlist) > limit:
        raise ValueError("RESEARCH_WATCHLIST cannot exceed RESEARCH_MAX_CANDIDATES.")
    return interval, limit, minimum_liquidity, freshness, database_path, watchlist


def run_research_cycle() -> int:
    _, limit, minimum_liquidity, freshness, database_path, watchlist = load_config()
    received_at = _utc_now()
    profiles = discover_base_contracts(limit, watchlist)
    addresses = [str(profile["contract_address"]) for profile in profiles]
    pairs = fetch_pairs(addresses)
    packets = [
        build_packet(
            profile,
            select_primary_pair(str(profile["contract_address"]), pairs),
            received_at,
            minimum_liquidity,
            freshness,
        )
        for profile in profiles
    ]
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
        "schema_version": 1,
        "mode": STATE["mode"],
        "status": STATE["status"],
        "last_cycle_at": STATE["last_cycle_at"],
        "packets_stored": STATE["packets_stored"],
        "providers": {"dexscreener": "enabled", "aixbt": "disabled", "bankr": "disabled"},
        "execution": "disabled",
    }


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/health"}:
            response = public_health_state()
        elif self.path == "/research/latest":
            *_, database_path, _ = load_config()
            response = {
                "service": "lumen-base-research-agent",
                "schema_version": 1,
                "mode": "observation_only",
                "execution": "disabled",
                "generated_at": _utc_now().isoformat(),
                "packets": load_latest_packets(database_path),
            }
        else:
            self.send_error(404)
            return
        payload = json.dumps(response).encode()
        self.send_response(200 if STATE["status"] != "failed" else 503)
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
    interval, *_ = load_config()
    serve_health()
    while True:
        try:
            run_research_cycle()
        except Exception as error:  # keep health endpoint available for diagnosis
            STATE.update(status="failed", last_error=type(error).__name__)
            print(json.dumps(STATE), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
