import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from app.research_agent import (
    build_packet,
    discover_base_contracts,
    eligible_base_pairs,
    fetch_pairs,
    get_json,
    load_latest_packets,
    load_config,
    public_health_state,
    public_route_response,
    select_primary_pair,
    store_packets,
)


ADDRESS = "0x0000000000000000000000000000000000000001"


def sample_pair(liquidity: str = "100000") -> dict[str, object]:
    return {
        "chainId": "base",
        "dexId": "test-dex",
        "pairAddress": "0x0000000000000000000000000000000000000002",
        "baseToken": {"address": ADDRESS, "name": "Example", "symbol": "TEST"},
        "quoteToken": {"address": "0x0000000000000000000000000000000000000003"},
        "priceUsd": "1.25",
        "liquidity": {"usd": liquidity},
        "volume": {"h24": "250000", "h6": "50000"},
        "priceChange": {"h24": "4", "h6": "1"},
        "txns": {"h24": {"buys": 120, "sells": 90}},
        "pairCreatedAt": 1750000000000,
        "marketCap": "1250000",
        "fdv": "1500000",
        "boosts": {"active": 3},
    }


class ResearchAgentTests(unittest.TestCase):
    @patch("app.research_agent.time.sleep")
    @patch("app.research_agent.urlopen")
    def test_provider_transient_failure_retries_then_recovers(self, urlopen, sleep) -> None:
        response = BytesIO(b'{"ok": true}')
        response.headers = {}
        urlopen.side_effect = [
            HTTPError("https://api.dexscreener.com/test", 503, "busy", {}, None),
            URLError("temporary DNS failure"),
            response,
        ]
        self.assertEqual(get_json("/test"), {"ok": True})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    @patch("app.research_agent.time.sleep")
    @patch("app.research_agent.urlopen")
    def test_provider_permanent_client_error_does_not_retry(self, urlopen, sleep) -> None:
        urlopen.side_effect = HTTPError(
            "https://api.dexscreener.com/test", 404, "missing", {}, None
        )
        with self.assertRaises(HTTPError):
            get_json("/test")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_default_config_is_free_and_observation_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            interval, limit, minimum, freshness, path, watchlist = load_config()
        self.assertEqual(interval, 3600)
        self.assertEqual(limit, 10)
        self.assertEqual(minimum, Decimal("50000"))
        self.assertEqual(freshness, 90)
        self.assertEqual(path, Path("data/research_packets.sqlite3"))
        self.assertEqual(len(watchlist), 2)

    def test_governed_universe_becomes_exact_research_watchlist(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.json"
            assets = [
                {
                    "rank": 1,
                    "symbol": "ETH",
                    "name": "Ether",
                    "token_address": None,
                    "decimals": 18,
                    "market_cap_usd": "1000000000",
                    "liquidity_usd": "1000000",
                    "daily_volume_usd": "1000000",
                    "oldest_pool_created_at": "2025-01-01T00:00:00+00:00",
                },
                {
                    "rank": 2,
                    "symbol": "AERO",
                    "name": "Aerodrome",
                    "token_address": "0x940181a94a35a4569e4529a3cdfb74e38fd98631",
                    "decimals": 18,
                    "market_cap_usd": "450000000",
                    "liquidity_usd": "25000000",
                    "daily_volume_usd": "15000000",
                    "oldest_pool_created_at": "2024-01-01T00:00:00+00:00",
                },
            ]
            assets.extend(
                {
                    "rank": rank,
                    "symbol": f"TOKEN{rank}",
                    "name": f"Token {rank}",
                    "token_address": f"0x{rank:040x}",
                    "decimals": 18,
                    "market_cap_usd": "100000000",
                    "liquidity_usd": "1000000",
                    "daily_volume_usd": "1000000",
                    "oldest_pool_created_at": "2025-01-01T00:00:00+00:00",
                }
                for rank in range(3, 26)
            )
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "network": "base-mainnet",
                        "chain_id": 8453,
                        "observed_at": now.isoformat(),
                        "source": "reviewed-test-snapshot",
                        "assets": assets,
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "RESEARCH_ASSET_UNIVERSE_PATH": str(path),
                    "RESEARCH_MAX_CANDIDATES": "25",
                },
                clear=True,
            ):
                *_, watchlist = load_config()
        self.assertEqual(len(watchlist), 25)
        self.assertEqual(watchlist[0], "0x4200000000000000000000000000000000000006")
        self.assertEqual(watchlist[1], "0x940181a94a35a4569e4529a3cdfb74e38fd98631")

    @patch("app.research_agent.get_json")
    def test_fetch_pairs_uses_all_pools_endpoint_for_each_candidate(self, get_json) -> None:
        get_json.side_effect = [[sample_pair("100")], [sample_pair("200")]]
        second = "0x0000000000000000000000000000000000000002"
        pairs = fetch_pairs([ADDRESS, second])
        self.assertEqual(len(pairs), 2)
        self.assertEqual(
            [call.args[0] for call in get_json.call_args_list],
            [
                f"/token-pairs/v1/base/{ADDRESS}",
                f"/token-pairs/v1/base/{second}",
            ],
        )

    def test_paid_and_execution_flags_fail_closed(self) -> None:
        for flag in ("LIVE_TRADING_ENABLED", "BANKR_ENABLED", "AIXBT_ENABLED"):
            with self.subTest(flag=flag):
                with patch.dict(os.environ, {flag: "true"}, clear=True):
                    with self.assertRaisesRegex(ValueError, flag):
                        load_config()

    @patch("app.research_agent.get_json")
    def test_discovery_keeps_only_unique_valid_base_contracts(self, get_json) -> None:
        get_json.return_value = [
            {"chainId": "base", "tokenAddress": ADDRESS, "url": "https://example.test"},
            {"chainId": "base", "tokenAddress": ADDRESS.upper()},
            {"chainId": "ethereum", "tokenAddress": ADDRESS},
            {"chainId": "base", "tokenAddress": "not-an-address"},
        ]
        self.assertEqual(
            discover_base_contracts(10),
            [
                {
                    "contract_address": ADDRESS,
                    "profile_url": "https://example.test",
                    "description": None,
                    "discovery_source": "dexscreener_latest_profile",
                    "marketing_influenced": True,
                    "promotion_type": "profile",
                }
            ],
        )

    def test_most_liquid_base_pair_is_selected(self) -> None:
        low = sample_pair("100")
        high = sample_pair("1000")
        high["pairAddress"] = "0x0000000000000000000000000000000000000004"
        wrong_chain = {**sample_pair("999999"), "chainId": "ethereum"}
        self.assertIs(select_primary_pair(ADDRESS, [low, high, wrong_chain]), high)
        self.assertEqual(eligible_base_pairs(ADDRESS, [low, high, wrong_chain]), [low, high])

    def test_incomplete_pool_is_not_eligible_even_when_more_liquid(self) -> None:
        complete = sample_pair("100")
        incomplete = sample_pair("999999")
        incomplete["pairCreatedAt"] = None
        self.assertEqual(eligible_base_pairs(ADDRESS, [complete, incomplete]), [complete])

    def test_packet_is_expiring_observation_not_execution(self) -> None:
        now = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
        packet = build_packet(
            {
                "contract_address": ADDRESS,
                "profile_url": "https://dexscreener.com/base/example",
                "discovery_source": "dexscreener_latest_profile",
                "marketing_influenced": True,
                "promotion_type": "profile",
            },
            sample_pair(),
            now,
            Decimal("50000"),
            90,
            2,
        )
        self.assertEqual(packet["schema_version"], 2)
        self.assertEqual(packet["recommendation"], "OBSERVE_ONLY")
        self.assertFalse(packet["execution_authorized"])
        self.assertEqual(packet["metrics"]["liquidity_usd"], "100000")
        self.assertEqual(packet["expires_at"], "2026-08-10T21:30:00+00:00")
        self.assertIn("CONTRACT_SECURITY_NOT_VERIFIED", packet["warnings"])
        self.assertTrue(packet["source"]["marketing_influenced"])
        self.assertEqual(packet["source"]["promotion_type"], "profile")
        self.assertEqual(packet["source"]["eligible_pair_count"], 2)
        self.assertEqual(packet["metrics"]["market_cap_usd"], "1250000")
        self.assertEqual(packet["metrics"]["fdv_usd"], "1500000")
        self.assertEqual(packet["metrics"]["active_boosts"], 3)
        self.assertEqual(len(packet["packet_id"]), 64)

    def test_watchlist_packet_is_not_labeled_as_promotional_discovery(self) -> None:
        packet = build_packet(
            {
                "contract_address": ADDRESS,
                "profile_url": None,
                "discovery_source": "configured_watchlist",
                "marketing_influenced": False,
                "promotion_type": None,
            },
            sample_pair(),
            datetime(2026, 8, 10, tzinfo=timezone.utc),
            Decimal("50000"),
            90,
            1,
        )
        self.assertFalse(packet["source"]["marketing_influenced"])
        self.assertNotIn("DISCOVERY_SOURCE_MAY_REFLECT_TOKEN_MARKETING", packet["warnings"])

    def test_missing_pair_is_partial_and_not_invented(self) -> None:
        packet = build_packet(
            {
                "contract_address": ADDRESS,
                "profile_url": None,
                "discovery_source": "dexscreener_latest_profile",
            },
            None,
            datetime(2026, 8, 10, tzinfo=timezone.utc),
            Decimal("50000"),
            90,
        )
        self.assertEqual(packet["data_quality"], "partial")
        self.assertEqual(packet["metrics"], {})
        self.assertIn("NO_BASE_DENOMINATED_PAIR_FOUND", packet["warnings"])

    def test_sqlite_store_is_deduplicated_and_queryable(self) -> None:
        packet = build_packet(
            {
                "contract_address": ADDRESS,
                "profile_url": None,
                "discovery_source": "dexscreener_latest_profile",
            },
            sample_pair(),
            datetime(2026, 8, 10, tzinfo=timezone.utc),
            Decimal("50000"),
            90,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.sqlite3"
            self.assertEqual(store_packets(path, [packet]), 1)
            self.assertEqual(store_packets(path, [packet]), 0)
            with sqlite3.connect(path) as connection:
                stored = connection.execute(
                    "SELECT payload_json FROM research_packets"
                ).fetchone()[0]
        self.assertEqual(json.loads(stored)["packet_id"], packet["packet_id"])

    def test_latest_packet_reader_marks_expired_data_stale(self) -> None:
        packet = build_packet(
            {
                "contract_address": ADDRESS,
                "profile_url": None,
                "discovery_source": "configured_watchlist",
            },
            sample_pair(),
            datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc),
            Decimal("50000"),
            90,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.sqlite3"
            store_packets(path, [packet])
            latest = load_latest_packets(
                path,
                now=datetime(2026, 8, 10, 22, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(len(latest), 1)
        self.assertTrue(latest[0]["is_stale"])
        self.assertFalse(latest[0]["execution_authorized"])

    def test_latest_packet_reader_does_not_create_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            self.assertEqual(load_latest_packets(path), [])
            self.assertFalse(path.exists())

    def test_public_health_exposes_provider_and_execution_boundaries(self) -> None:
        health = public_health_state()
        self.assertEqual(health["mode"], "observation_only")
        self.assertEqual(health["providers"]["dexscreener"], "enabled")
        self.assertEqual(health["providers"]["bankr"], "disabled")
        self.assertEqual(health["execution"], "disabled")
        self.assertNotIn("last_error", health)

    def test_unbuilt_domain_routes_are_explicitly_unavailable(self) -> None:
        for path, domain in (
            ("/research/equities/latest", "equities"),
            ("/research/bitcoin-network/latest", "bitcoin-network"),
        ):
            with self.subTest(path=path):
                status, response = public_route_response(path)
                self.assertEqual(status, 501)
                self.assertEqual(response["domain"], domain)
                self.assertEqual(response["status"], "not_configured")
                self.assertEqual(response["mode"], "observation_only")
                self.assertEqual(response["execution"], "disabled")
                self.assertEqual(response["packets"], [])

    def test_unknown_research_route_is_not_silently_accepted(self) -> None:
        self.assertIsNone(public_route_response("/research/options/latest"))


if __name__ == "__main__":
    unittest.main()
