"""Runnable Stage 3 research application; all public routes are read-only."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app.openai_research_v3 import OpenAIResponsesMacroClient, ResearchSynthesis
from app.research_v3_runtime import (
    AssetTarget,
    CoinGeckoContractCollector,
    DefiLlamaCollector,
    DexScreenerCollector,
    DuneCachedCollector,
    FallbackMarketCollector,
    GeckoTerminalCollector,
    GeckoTerminalMultiTokenCollector,
    JsonSourceClient,
    ResearchV3Store,
)
from app.research_v3_service import ResearchV3Service, public_route_response


WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"


@dataclass(frozen=True)
class DuneQuerySpec:
    query_id: int
    columns: tuple[str, ...]


@dataclass(frozen=True)
class AppConfig:
    database_path: Path
    openai_api_key: str = field(repr=False)
    openai_model: str
    dune_api_key: str | None = field(repr=False)
    coingecko_api_key: str | None = field(repr=False)
    dune_queries: tuple[DuneQuerySpec, ...]
    market_targets: tuple[AssetTarget, ...]
    market_interval_seconds: int
    openai_interval_seconds: int
    port: int
    execution: str = "disabled"


def _optional_secret(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _dune_specs(raw: str) -> tuple[DuneQuerySpec, ...]:
    if not raw.strip():
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("DUNE_QUERY_SPECS must be valid JSON.") from error
    if not isinstance(payload, list) or len(payload) > 10:
        raise ValueError("DUNE_QUERY_SPECS must contain at most ten queries.")
    specs = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"query_id", "columns"}:
            raise ValueError("Each Dune query spec requires query_id and columns.")
        query_id = item["query_id"]
        columns = item["columns"]
        if (
            type(query_id) is not int
            or query_id <= 0
            or not isinstance(columns, list)
            or not columns
            or len(columns) > 20
            or any(not isinstance(column, str) or not column.replace("_", "").isalnum() for column in columns)
        ):
            raise ValueError("Dune query spec values are invalid.")
        specs.append(DuneQuerySpec(query_id, tuple(columns)))
    return tuple(specs)


def _market_targets(raw: str) -> tuple[AssetTarget, ...]:
    if not raw.strip():
        return (
            AssetTarget(WETH, "WETH", (USDC,)),
            AssetTarget(USDC, "USDC", (USDBC, WETH)),
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("RESEARCH_V3_MARKET_TARGETS must be valid JSON.") from error
    if not isinstance(payload, list) or not 1 <= len(payload) <= 25:
        raise ValueError("RESEARCH_V3_MARKET_TARGETS must contain one to 25 targets.")
    targets: list[AssetTarget] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {
            "contract_address", "symbol", "approved_quote_addresses"
        }:
            raise ValueError("Each market target has an invalid shape.")
        quotes = item["approved_quote_addresses"]
        if not isinstance(quotes, list):
            raise ValueError("Market target quote addresses must be a list.")
        targets.append(AssetTarget(
            str(item["contract_address"]).lower(),
            str(item["symbol"]).upper(),
            tuple(str(address).lower() for address in quotes),
        ))
    identities = {(item.contract_address, item.symbol) for item in targets}
    if len(identities) != len(targets):
        raise ValueError("RESEARCH_V3_MARKET_TARGETS contains duplicate identities.")
    return tuple(targets)


def load_app_config() -> AppConfig:
    if os.getenv("RESEARCH_MODE", "observation_only").strip().lower() != "observation_only":
        raise ValueError("RESEARCH_MODE must remain observation_only.")
    for flag in ("LIVE_TRADING_ENABLED", "BANKR_ENABLED", "AIXBT_ENABLED"):
        if os.getenv(flag, "false").strip().lower() == "true":
            raise ValueError(f"{flag} must remain false for the research service.")
    openai_key = _optional_secret("OPENAI_API_KEY")
    if openai_key is None:
        raise ValueError("OPENAI_API_KEY is required for Stage 3 synthesis.")
    market_interval = int(os.getenv("RESEARCH_V3_MARKET_INTERVAL_SECONDS", "300"))
    if not 60 <= market_interval <= 3600:
        raise ValueError("Market collection interval must be between 60 and 3600 seconds.")
    configured_openai_interval = int(os.getenv("OPENAI_RESEARCH_INTERVAL_SECONDS", "21600"))
    if configured_openai_interval != 21600:
        raise ValueError("OpenAI research interval must remain 21600 seconds.")
    port = int(os.getenv("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT is invalid.")
    return AppConfig(
        database_path=Path(os.getenv("RESEARCH_V3_DB_PATH", "data/research_v3.sqlite3")),
        openai_api_key=openai_key,
        openai_model=os.getenv("OPENAI_RESEARCH_MODEL", "gpt-5.6-terra").strip(),
        dune_api_key=_optional_secret("DUNE_API_KEY"),
        coingecko_api_key=_optional_secret("COINGECKO_API_KEY"),
        dune_queries=_dune_specs(os.getenv("DUNE_QUERY_SPECS", "")),
        market_targets=_market_targets(os.getenv("RESEARCH_V3_MARKET_TARGETS", "")),
        market_interval_seconds=market_interval,
        openai_interval_seconds=configured_openai_interval,
        port=port,
    )


@dataclass(frozen=True)
class BoundDuneCollector:
    collector: DuneCachedCollector
    spec: DuneQuerySpec

    def collect(self, *, now: datetime) -> dict[str, object]:
        return self.collector.collect(
            query_id=self.spec.query_id, columns=self.spec.columns, now=now
        )


@dataclass(frozen=True)
class BoundCoinGeckoCollector:
    collector: CoinGeckoContractCollector
    target: AssetTarget

    def collect(self, target: AssetTarget, *, now: datetime) -> dict[str, object]:
        return self.collector.collect(target, now=now)


def build_service(config: AppConfig) -> ResearchV3Service:
    client = JsonSourceClient()
    targets = config.market_targets
    macro_collectors: list[object] = [DefiLlamaCollector(client)]
    if config.dune_api_key:
        dune = DuneCachedCollector(client, api_key=config.dune_api_key, row_limit=100)
        macro_collectors.extend(BoundDuneCollector(dune, spec) for spec in config.dune_queries)
    if config.coingecko_api_key:
        coin = CoinGeckoContractCollector(client, api_key=config.coingecko_api_key)
        macro_collectors.extend(BoundCoinGeckoCollector(coin, target) for target in targets)
    store = ResearchV3Store(config.database_path)
    openai = OpenAIResponsesMacroClient(
        api_key=config.openai_api_key,
        model=config.openai_model,
    )
    synthesis = ResearchSynthesis(store, openai, prompt_version="base-macro-v1")
    return ResearchV3Service(
        store,
        FallbackMarketCollector(
            DexScreenerCollector(client),
            GeckoTerminalCollector(client),
        ),
        tuple(macro_collectors),
        synthesis,
        targets,
        GeckoTerminalMultiTokenCollector(client),
    )


def make_handler(store: ResearchV3Store) -> type[BaseHTTPRequestHandler]:
    class ReadOnlyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            result = public_route_response(
                self.path, store, now=datetime.now(timezone.utc)
            )
            if result is None:
                self.send_error(404)
                return
            status, body = result
            raw = json.dumps(body, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ReadOnlyHandler


def main() -> None:
    config = load_app_config()
    service = build_service(config)
    server = HTTPServer(("0.0.0.0", config.port), make_handler(service.store))  # nosec B104
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        try:
            state = service.run_cycle(now=datetime.now(timezone.utc))
        except Exception as error:  # retain liveness, log only the error class
            state = {
                "status": "failed",
                "error_type": type(error).__name__,
                "execution_authorized": False,
            }
        print(json.dumps(state, sort_keys=True), flush=True)
        time.sleep(config.market_interval_seconds)


if __name__ == "__main__":
    main()
