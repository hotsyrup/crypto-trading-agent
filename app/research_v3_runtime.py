"""Deterministic collection and persistence for research contract version 3."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


MAX_SOURCE_BYTES = 2_000_000
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-f]{40}$")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def seal(resource: dict[str, object]) -> dict[str, object]:
    sealed = dict(resource)
    sealed["digest"] = _sha256(resource)
    return sealed


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return value.astimezone(timezone.utc)


def _timestamp(value: object, field_name: str) -> datetime:
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")), field_name)
    except ValueError as error:
        raise ValueError(f"{field_name} is invalid.") from error


@dataclass(frozen=True)
class JsonResult:
    payload: object
    raw_sha256: str


@dataclass(frozen=True)
class JsonSourceClient:
    """Bounded HTTPS GET client whose errors never include credential headers."""

    timeout_seconds: float = 10
    max_attempts: int = 3
    opener: Callable[..., object] = field(default=urlopen, repr=False, compare=False)
    sleeper: Callable[[float], None] = field(default=time.sleep, repr=False, compare=False)

    def get(
        self,
        url: str,
        *,
        allowed_host: str,
        headers: dict[str, str] | None = None,
    ) -> JsonResult:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != allowed_host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("Research source must use its approved HTTPS host.")
        request_headers = {"Accept": "application/json", "User-Agent": "lumen-research/3"}
        request_headers.update(headers or {})
        request = Request(url, headers=request_headers, method="GET")
        for attempt in range(1, self.max_attempts + 1):
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    raw = response.read(MAX_SOURCE_BYTES + 1)
                if len(raw) > MAX_SOURCE_BYTES:
                    raise ValueError("Research source response exceeds the size limit.")
                return JsonResult(
                    payload=json.loads(raw),
                    raw_sha256=hashlib.sha256(raw).hexdigest(),
                )
            except HTTPError as error:
                if error.code not in RETRYABLE_HTTP_STATUSES or attempt == self.max_attempts:
                    raise RuntimeError(f"Research source HTTP status {error.code}.") from error
            except (TimeoutError, URLError, OSError) as error:
                if attempt == self.max_attempts:
                    raise RuntimeError("Research source is unavailable.") from error
            self.sleeper(min(2 ** (attempt - 1), 2))
        raise RuntimeError("Research source attempts exhausted.")


@dataclass(frozen=True)
class AssetTarget:
    contract_address: str
    symbol: str
    approved_quote_addresses: tuple[str, ...]

    def __post_init__(self) -> None:
        if not ADDRESS_PATTERN.fullmatch(self.contract_address):
            raise ValueError("Asset target contract address is invalid.")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("Asset target symbol must be uppercase.")
        if not self.approved_quote_addresses or any(
            not ADDRESS_PATTERN.fullmatch(address) for address in self.approved_quote_addresses
        ):
            raise ValueError("Asset target quote contracts are invalid.")


def _metric(mapping: object, key: str) -> object:
    return mapping.get(key) if isinstance(mapping, dict) else None


def _decimal_string(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return format(number, "f") if number.is_finite() else None


def build_dex_snapshot(
    target: AssetTarget,
    pairs: object,
    *,
    collected_at: datetime,
    freshness: timedelta,
    raw_response_sha256: str | None = None,
) -> dict[str, object]:
    """Select the most liquid exact-contract Base pair and seal a v3 snapshot."""

    collected = _utc(collected_at, "collected_at")
    if freshness <= timedelta(0) or freshness > timedelta(hours=24):
        raise ValueError("Market snapshot freshness is invalid.")
    if not isinstance(pairs, list):
        raise ValueError("DEX Screener response must be an array.")
    eligible: list[dict[str, object]] = []
    quotes = set(target.approved_quote_addresses)
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "base":
            continue
        base = pair.get("baseToken")
        quote_token = pair.get("quoteToken")
        if not isinstance(base, dict) or not isinstance(quote_token, dict):
            continue
        if (
            str(base.get("address", "")).lower() == target.contract_address
            and str(base.get("symbol", "")).upper() == target.symbol
            and str(quote_token.get("address", "")).lower() in quotes
        ):
            eligible.append(pair)
    if not eligible:
        raise ValueError("No exact-contract Base DEX pair was found.")
    selected = max(
        eligible,
        key=lambda pair: float(_metric(pair.get("liquidity"), "usd") or 0),
    )
    volume = selected.get("volume")
    changes = selected.get("priceChange")
    h24_txns = _metric(selected.get("txns"), "h24")
    metrics = {
        "price_usd": _decimal_string(selected.get("priceUsd")),
        "liquidity_usd": _decimal_string(_metric(selected.get("liquidity"), "usd")),
        "volume_h24_usd": _decimal_string(_metric(volume, "h24")),
        "volume_h6_usd": _decimal_string(_metric(volume, "h6")),
        "price_change_h24_percent": _decimal_string(_metric(changes, "h24")),
        "price_change_h6_percent": _decimal_string(_metric(changes, "h6")),
        "buys_h24": _metric(h24_txns, "buys"),
        "sells_h24": _metric(h24_txns, "sells"),
    }
    missing_warnings = [
        f"MISSING_{name.upper()}" for name, value in metrics.items() if value is None
    ]
    warnings = list(missing_warnings)
    warnings.append("PROVIDER_OBSERVATION_TIME_UNAVAILABLE")
    source_ref = (
        "https://api.dexscreener.com/tokens/v1/base/"
        f"{target.contract_address}#{selected.get('pairAddress', '')}"
    )
    resource = {
        "resource_type": "market_snapshot",
        "schema_version": 3,
        "network": "eip155:8453",
        "contract_address": target.contract_address,
        "symbol": target.symbol,
        "observed_at": collected.isoformat(),
        "collected_at": collected.isoformat(),
        "expires_at": (collected + freshness).isoformat(),
        "source": {
            "provider": "dexscreener",
            "source_ref": source_ref,
            "raw_response_sha256": raw_response_sha256 or _sha256(pairs),
        },
        "quality": "partial" if missing_warnings else "complete",
        "metrics": metrics,
        "warnings": warnings,
        "execution_authorized": False,
    }
    sealed = seal(resource)
    from app.research_contract_v3 import validate_research_bundle

    validate_research_bundle(
        {
            "service": "lumen-base-research-agent",
            "schema_version": 3,
            "mode": "observation_only",
            "execution": "disabled",
            "generated_at": collected.isoformat(),
            "market_snapshots": [sealed],
            "asset_failures": [],
            "macro_report": None,
        },
        now=collected,
    )
    return sealed


def build_macro_source(
    *,
    provider: str,
    source_ref: str,
    observed_at: datetime,
    retrieved_at: datetime,
    raw_sha256: str,
    max_age: timedelta,
) -> dict[str, object]:
    observed = _utc(observed_at, "observed_at")
    retrieved = _utc(retrieved_at, "retrieved_at")
    if observed > retrieved or retrieved - observed > max_age:
        raise ValueError(f"{provider} source data is stale.")
    if not re.fullmatch(r"[0-9a-f]{64}", raw_sha256):
        raise ValueError("Research source digest is invalid.")
    return {
        "provider": provider,
        "source_ref": source_ref,
        "observed_at": observed.isoformat(),
        "retrieved_at": retrieved.isoformat(),
        "raw_response_sha256": raw_sha256,
    }


@dataclass(frozen=True)
class DuneCachedCollector:
    client: JsonSourceClient
    api_key: str = field(repr=False)
    row_limit: int = 100

    def collect(
        self,
        *,
        query_id: int,
        columns: tuple[str, ...],
        now: datetime,
    ) -> dict[str, object]:
        if query_id <= 0 or not 1 <= self.row_limit <= 1000 or not columns:
            raise ValueError("Dune cached-result configuration is invalid.")
        encoded_columns = quote(",".join(columns), safe=",")
        url = (
            f"https://api.dune.com/api/v1/query/{query_id}/results"
            f"?limit={self.row_limit}&columns={encoded_columns}"
        )
        result = self.client.get(
            url,
            allowed_host="api.dune.com",
            headers={"X-Dune-API-Key": self.api_key},
        )
        if not isinstance(result.payload, dict) or result.payload.get("state") != "QUERY_STATE_COMPLETED":
            raise ValueError("Dune cached result is incomplete.")
        observed = _timestamp(result.payload.get("execution_ended_at"), "Dune execution time")
        source = build_macro_source(
            provider="dune",
            source_ref=url,
            observed_at=observed,
            retrieved_at=now,
            raw_sha256=result.raw_sha256,
            max_age=timedelta(hours=6),
        )
        rows = result.payload.get("result", {}).get("rows")
        if not isinstance(rows, list) or len(rows) > self.row_limit:
            raise ValueError("Dune cached result rows are invalid.")
        return {"source": source, "facts": {"rows": rows}}


@dataclass(frozen=True)
class DexScreenerCollector:
    client: JsonSourceClient
    freshness: timedelta = timedelta(minutes=10)

    def collect(self, target: AssetTarget, *, now: datetime) -> dict[str, object]:
        url = f"https://api.dexscreener.com/tokens/v1/base/{target.contract_address}"
        result = self.client.get(url, allowed_host="api.dexscreener.com")
        return build_dex_snapshot(
            target,
            result.payload,
            collected_at=now,
            freshness=self.freshness,
            raw_response_sha256=result.raw_sha256,
        )


def build_geckoterminal_snapshot(
    target: AssetTarget,
    payload: object,
    *,
    collected_at: datetime,
    freshness: timedelta,
    raw_response_sha256: str,
) -> dict[str, object]:
    """Normalize the most liquid exact-contract GeckoTerminal pool into v3."""

    collected = _utc(collected_at, "collected_at")
    if freshness <= timedelta(0) or freshness > timedelta(hours=24):
        raise ValueError("Market snapshot freshness is invalid.")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("GeckoTerminal response must contain a pool array.")
    included = payload.get("included")
    if not isinstance(included, list):
        raise ValueError("GeckoTerminal token identities are unavailable.")
    tokens = {
        str(item.get("id", "")).lower(): item.get("attributes", {})
        for item in included
        if isinstance(item, dict) and item.get("type") == "token"
    }
    eligible: list[tuple[dict[str, object], bool]] = []
    target_id = f"base_{target.contract_address}"
    quote_ids = {f"base_{address}" for address in target.approved_quote_addresses}
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        relationships = item.get("relationships")
        attributes = item.get("attributes")
        if not isinstance(relationships, dict) or not isinstance(attributes, dict):
            continue
        base_id = str(_metric(_metric(relationships.get("base_token"), "data"), "id")).lower()
        quote_id = str(_metric(_metric(relationships.get("quote_token"), "data"), "id")).lower()
        target_is_base = base_id == target_id and quote_id in quote_ids
        target_is_quote = quote_id == target_id and base_id in quote_ids
        identity = tokens.get(target_id)
        if (
            (target_is_base or target_is_quote)
            and isinstance(identity, dict)
            and str(identity.get("symbol", "")).upper() == target.symbol
        ):
            eligible.append((item, target_is_base))
    if not eligible:
        raise ValueError("No exact-contract Base GeckoTerminal pool was found.")
    selected, target_is_base = max(
        eligible,
        key=lambda pair: float(_decimal_string(pair[0]["attributes"].get("reserve_in_usd")) or 0),
    )
    attributes = selected["attributes"]
    changes = attributes.get("price_change_percentage")
    volumes = attributes.get("volume_usd")
    h24_txns = _metric(attributes.get("transactions"), "h24")
    buys = _metric(h24_txns, "buys")
    sells = _metric(h24_txns, "sells")
    metrics = {
        "price_usd": _decimal_string(
            attributes.get("base_token_price_usd" if target_is_base else "quote_token_price_usd")
        ),
        "liquidity_usd": _decimal_string(attributes.get("reserve_in_usd")),
        "volume_h24_usd": _decimal_string(_metric(volumes, "h24")),
        "volume_h6_usd": _decimal_string(_metric(volumes, "h6")),
        "price_change_h24_percent": _decimal_string(_metric(changes, "h24")),
        "price_change_h6_percent": _decimal_string(_metric(changes, "h6")),
        "buys_h24": buys if target_is_base else sells,
        "sells_h24": sells if target_is_base else buys,
    }
    missing_warnings = [
        f"MISSING_{name.upper()}" for name, value in metrics.items() if value is None
    ]
    pool_id = str(selected.get("id", ""))
    pool_address = pool_id.removeprefix("base_")
    resource = {
        "resource_type": "market_snapshot",
        "schema_version": 3,
        "network": "eip155:8453",
        "contract_address": target.contract_address,
        "symbol": target.symbol,
        "observed_at": collected.isoformat(),
        "collected_at": collected.isoformat(),
        "expires_at": (collected + freshness).isoformat(),
        "source": {
            "provider": "geckoterminal",
            "source_ref": f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_address}",
            "raw_response_sha256": raw_response_sha256,
        },
        "quality": "partial" if missing_warnings else "complete",
        "metrics": metrics,
        "warnings": [*missing_warnings, "PROVIDER_OBSERVATION_TIME_UNAVAILABLE"],
        "execution_authorized": False,
    }
    sealed = seal(resource)
    from app.research_contract_v3 import validate_research_bundle

    validate_research_bundle({
        "service": "lumen-base-research-agent",
        "schema_version": 3,
        "mode": "observation_only",
        "execution": "disabled",
        "generated_at": collected.isoformat(),
        "market_snapshots": [sealed],
        "asset_failures": [],
        "macro_report": None,
    }, now=collected)
    return sealed


@dataclass(frozen=True)
class GeckoTerminalCollector:
    client: JsonSourceClient
    freshness: timedelta = timedelta(minutes=10)

    def collect(self, target: AssetTarget, *, now: datetime) -> dict[str, object]:
        url = (
            "https://api.geckoterminal.com/api/v2/networks/base/tokens/"
            f"{target.contract_address}/pools?page=1&include=base_token%2Cquote_token"
        )
        result = self.client.get(
            url,
            allowed_host="api.geckoterminal.com",
            headers={"x-cg-pro-api-version": "2024-04-04"},
        )
        return build_geckoterminal_snapshot(
            target,
            result.payload,
            collected_at=now,
            freshness=self.freshness,
            raw_response_sha256=result.raw_sha256,
        )


@dataclass(frozen=True)
class GeckoTerminalMultiTokenCollector:
    """Recover valuation-only snapshots for several exact contracts in one call."""

    client: JsonSourceClient
    freshness: timedelta = timedelta(minutes=10)

    def collect_many(
        self,
        targets: tuple[AssetTarget, ...],
        *,
        now: datetime,
    ) -> dict[tuple[str, str], dict[str, object]]:
        if not 1 <= len(targets) <= 30:
            raise ValueError("GeckoTerminal multi-token collection requires one to 30 targets.")
        addresses = ",".join(target.contract_address for target in targets)
        url = f"https://api.geckoterminal.com/api/v2/networks/base/tokens/multi/{addresses}"
        result = self.client.get(
            url,
            allowed_host="api.geckoterminal.com",
            headers={"Accept": "application/json;version=20230203"},
        )
        payload = result.payload
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("GeckoTerminal multi-token response is invalid.")
        expected = {
            target.contract_address: target
            for target in targets
        }
        recovered: dict[tuple[str, str], dict[str, object]] = {}
        collected = _utc(now, "collected_at")
        for row in rows:
            attributes = row.get("attributes") if isinstance(row, dict) else None
            if not isinstance(attributes, dict):
                continue
            address = str(attributes.get("address", "")).lower()
            target = expected.get(address)
            if target is None or str(attributes.get("symbol", "")).upper() != target.symbol:
                continue
            metrics = {
                "price_usd": _decimal_string(attributes.get("price_usd")),
                "liquidity_usd": _decimal_string(attributes.get("total_reserve_in_usd")),
                "volume_h24_usd": _decimal_string(_metric(attributes.get("volume_usd"), "h24")),
                "volume_h6_usd": None,
                "price_change_h24_percent": None,
                "price_change_h6_percent": None,
                "buys_h24": None,
                "sells_h24": None,
            }
            if metrics["price_usd"] is None:
                continue
            resource = seal({
                "resource_type": "market_snapshot",
                "schema_version": 3,
                "network": "eip155:8453",
                "contract_address": address,
                "symbol": target.symbol,
                "observed_at": collected.isoformat(),
                "collected_at": collected.isoformat(),
                "expires_at": (collected + self.freshness).isoformat(),
                "source": {
                    "provider": "geckoterminal",
                    "source_ref": f"{url}#{address}",
                    "raw_response_sha256": result.raw_sha256,
                },
                "quality": "partial",
                "metrics": metrics,
                "warnings": [
                    "VALUATION_ONLY_BATCH_FALLBACK",
                    "PROVIDER_OBSERVATION_TIME_UNAVAILABLE",
                ],
                "execution_authorized": False,
            })
            from app.research_contract_v3 import validate_research_bundle
            validate_research_bundle({
                "service": "lumen-base-research-agent",
                "schema_version": 3,
                "mode": "observation_only",
                "execution": "disabled",
                "generated_at": collected.isoformat(),
                "market_snapshots": [resource],
                "asset_failures": [],
                "macro_report": None,
            }, now=collected)
            recovered[(address, target.symbol)] = resource
        return recovered


@dataclass(frozen=True)
class FallbackMarketCollector:
    primary: object
    fallback: object

    def collect(self, target: AssetTarget, *, now: datetime) -> dict[str, object]:
        try:
            return self.primary.collect(target, now=now)
        except (TimeoutError, ConnectionError, OSError, RuntimeError, TypeError, ValueError, KeyError):
            return self.fallback.collect(target, now=now)


@dataclass(frozen=True)
class CoinGeckoContractCollector:
    """Optional exact-contract corroboration through CoinGecko's onchain API."""

    client: JsonSourceClient
    api_key: str = field(repr=False)
    max_age: timedelta = timedelta(minutes=15)

    def collect(self, target: AssetTarget, *, now: datetime) -> dict[str, object]:
        url = (
            "https://pro-api.coingecko.com/api/v3/onchain/networks/base/tokens/"
            f"{target.contract_address}"
        )
        result = self.client.get(
            url,
            allowed_host="pro-api.coingecko.com",
            headers={"x-cg-pro-api-key": self.api_key},
        )
        data = result.payload.get("data") if isinstance(result.payload, dict) else None
        attributes = data.get("attributes") if isinstance(data, dict) else None
        if not isinstance(attributes, dict):
            raise ValueError("CoinGecko contract payload is invalid.")
        address = str(attributes.get("address", "")).lower()
        symbol = str(attributes.get("symbol", "")).upper()
        if address != target.contract_address or symbol != target.symbol:
            raise ValueError("CoinGecko returned a different asset identity.")
        last_trade = attributes.get("last_trade_timestamp")
        if not isinstance(last_trade, (int, float)):
            raise ValueError("CoinGecko observation time is unavailable.")
        observed = datetime.fromtimestamp(last_trade, timezone.utc)
        source = build_macro_source(
            provider="coingecko",
            source_ref=url,
            observed_at=observed,
            retrieved_at=now,
            raw_sha256=result.raw_sha256,
            max_age=self.max_age,
        )
        return {
            "source": source,
            "facts": {
                "contract_address": address,
                "symbol": symbol,
                "price_usd": _decimal_string(attributes.get("price_usd")),
                "volume_h24_usd": _decimal_string(_metric(attributes.get("volume_usd"), "h24")),
            },
        }


@dataclass(frozen=True)
class DefiLlamaCollector:
    """Read Base chain TVL from the documented public chains dataset."""

    client: JsonSourceClient

    def collect(self, *, now: datetime) -> dict[str, object]:
        url = "https://api.llama.fi/v2/chains"
        result = self.client.get(url, allowed_host="api.llama.fi")
        if not isinstance(result.payload, list):
            raise ValueError("DeFiLlama chains payload is invalid.")
        base = next(
            (
                row for row in result.payload
                if isinstance(row, dict) and str(row.get("name", "")).lower() == "base"
            ),
            None,
        )
        tvl = _decimal_string(base.get("tvl")) if isinstance(base, dict) else None
        if tvl is None:
            raise ValueError("DeFiLlama Base TVL is unavailable.")
        current = _utc(now, "DeFiLlama retrieval time")
        return {
            "source": build_macro_source(
                provider="defillama",
                source_ref=url,
                observed_at=current,
                retrieved_at=current,
                raw_sha256=result.raw_sha256,
                max_age=timedelta(minutes=1),
            ),
            "facts": {
                "base_tvl_usd": tvl,
                "timestamp_semantics": "retrieval_time_provider_timestamp_unavailable",
            },
        }


class ResearchV3Store:
    """SQLite store with digest deduplication and bounded resource history."""

    def __init__(self, path: Path, history_limit: int = 100):
        if history_limit < 1:
            raise ValueError("Research history limit must be positive.")
        self.path = path
        self.history_limit = history_limit
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS resources (
                    digest TEXT PRIMARY KEY,
                    resource_type TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS synthesis_slots (
                    slot_id INTEGER PRIMARY KEY,
                    report_digest TEXT NOT NULL,
                    response_id TEXT NOT NULL,
                    usage_json TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS synthesis_claims (
                    slot_id INTEGER PRIMARY KEY,
                    attempted_at TEXT NOT NULL
                )"""
            )

    def _put(self, resource: dict[str, object], identity: str, recorded_at: str) -> bool:
        digest = str(resource["digest"])
        kind = str(resource["resource_type"])
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO resources VALUES (?, ?, ?, ?, ?)",
                (digest, kind, identity, recorded_at, json.dumps(resource, sort_keys=True)),
            )
            if cursor.rowcount:
                connection.execute(
                    """DELETE FROM resources WHERE digest IN (
                        SELECT digest FROM resources WHERE resource_type = ?
                        ORDER BY recorded_at DESC, digest DESC LIMIT -1 OFFSET ?
                    )""",
                    (kind, self.history_limit),
                )
            return cursor.rowcount == 1

    def put_market(self, resource: dict[str, object]) -> bool:
        return self._put(resource, str(resource["contract_address"]), str(resource["collected_at"]))

    def put_macro(self, resource: dict[str, object]) -> bool:
        return self._put(resource, str(resource["report_id"]), str(resource["generated_at"]))

    def put_failure(self, resource: dict[str, object]) -> bool:
        return self._put(resource, str(resource["contract_address"]), str(resource["failed_at"]))

    def latest_market(self, contract_address: str) -> dict[str, object] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """SELECT payload_json FROM resources
                   WHERE resource_type = 'market_snapshot' AND identity = ?
                   ORDER BY recorded_at DESC, digest DESC LIMIT 1""",
                (contract_address,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def latest_macro(self) -> dict[str, object] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """SELECT payload_json FROM resources WHERE resource_type = 'macro_report'
                   ORDER BY recorded_at DESC, digest DESC LIMIT 1"""
            ).fetchone()
        return json.loads(row[0]) if row else None

    def history_count(self, resource_type: str) -> int:
        with sqlite3.connect(self.path) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM resources WHERE resource_type = ?", (resource_type,)
            ).fetchone()[0])

    def latest_by_identity(self, resource_type: str) -> list[dict[str, object]]:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                """SELECT payload_json FROM resources WHERE resource_type = ?
                   ORDER BY recorded_at DESC, digest DESC""",
                (resource_type,),
            ).fetchall()
        result = []
        seen = set()
        for (payload_json,) in rows:
            payload = json.loads(payload_json)
            identity = (
                payload.get("contract_address")
                if resource_type != "macro_report"
                else payload.get("report_id")
            )
            if identity not in seen:
                seen.add(identity)
                result.append(payload)
        return result

    def slot_report(self, slot_id: int) -> dict[str, object] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """SELECT r.payload_json FROM synthesis_slots s
                   JOIN resources r ON r.digest = s.report_digest WHERE s.slot_id = ?""",
                (slot_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def claim_slot(self, slot_id: int, attempted_at: str) -> bool:
        """Atomically reserve one six-hour model attempt across processes/restarts."""
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO synthesis_claims VALUES (?, ?)",
                (slot_id, attempted_at),
            )
            return cursor.rowcount == 1

    def slot_claimed(self, slot_id: int) -> bool:
        with sqlite3.connect(self.path) as connection:
            return connection.execute(
                "SELECT 1 FROM synthesis_claims WHERE slot_id = ?", (slot_id,)
            ).fetchone() is not None

    def commit_slot(
        self,
        slot_id: int,
        report: dict[str, object],
        response_id: str,
        usage: dict[str, object],
    ) -> bool:
        with sqlite3.connect(self.path) as connection:
            if connection.execute(
                "SELECT 1 FROM synthesis_slots WHERE slot_id = ?", (slot_id,)
            ).fetchone():
                return False
            connection.execute(
                "INSERT OR IGNORE INTO resources VALUES (?, ?, ?, ?, ?)",
                (
                    report["digest"], "macro_report", report["report_id"],
                    report["generated_at"], json.dumps(report, sort_keys=True),
                ),
            )
            connection.execute(
                "INSERT INTO synthesis_slots VALUES (?, ?, ?, ?)",
                (slot_id, report["digest"], response_id, json.dumps(usage, sort_keys=True)),
            )
            return True
