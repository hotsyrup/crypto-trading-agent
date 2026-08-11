import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.research_agent import (
    build_packet,
    discover_base_contracts,
    load_latest_packets,
    load_config,
    public_health_state,
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
    }


class ResearchAgentTests(unittest.TestCase):
    def test_default_config_is_free_and_observation_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            interval, limit, minimum, freshness, path, watchlist = load_config()
        self.assertEqual(interval, 3600)
        self.assertEqual(limit, 10)
        self.assertEqual(minimum, Decimal("50000"))
        self.assertEqual(freshness, 90)
        self.assertEqual(path, Path("data/research_packets.sqlite3"))
        self.assertEqual(len(watchlist), 2)

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
                }
            ],
        )

    def test_most_liquid_base_pair_is_selected(self) -> None:
        low = sample_pair("100")
        high = sample_pair("1000")
        high["pairAddress"] = "0x0000000000000000000000000000000000000004"
        wrong_chain = {**sample_pair("999999"), "chainId": "ethereum"}
        self.assertIs(select_primary_pair(ADDRESS, [low, high, wrong_chain]), high)

    def test_packet_is_expiring_observation_not_execution(self) -> None:
        now = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
        packet = build_packet(
            {
                "contract_address": ADDRESS,
                "profile_url": "https://dexscreener.com/base/example",
                "discovery_source": "dexscreener_latest_profile",
            },
            sample_pair(),
            now,
            Decimal("50000"),
            90,
        )
        self.assertEqual(packet["recommendation"], "OBSERVE_ONLY")
        self.assertFalse(packet["execution_authorized"])
        self.assertEqual(packet["metrics"]["liquidity_usd"], "100000")
        self.assertEqual(packet["expires_at"], "2026-08-10T21:30:00+00:00")
        self.assertIn("CONTRACT_SECURITY_NOT_VERIFIED", packet["warnings"])
        self.assertEqual(len(packet["packet_id"]), 64)

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


if __name__ == "__main__":
    unittest.main()
