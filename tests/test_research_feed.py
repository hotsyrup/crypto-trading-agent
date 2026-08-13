import unittest
from datetime import datetime, timedelta, timezone

from app.research_feed import REQUIRED_CONTRACTS, evaluate_research_payload


class ResearchFeedTests(unittest.TestCase):
    now = datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc)

    def payload(self):
        packets = []
        for index, contract in enumerate(sorted(REQUIRED_CONTRACTS)):
            packets.append(
                {
                    "packet_id": str(index + 1) * 64,
                    "network": "base",
                    "contract_address": contract,
                    "received_at": (self.now - timedelta(seconds=30)).isoformat(),
                    "expires_at": (self.now + timedelta(minutes=30)).isoformat(),
                    "data_quality": "complete",
                    "metrics": {
                        "price_usd": "1",
                        "liquidity_usd": "100000",
                        "volume_h24_usd": "50000",
                        "pair_created_at": "2025-01-01T00:00:00+00:00",
                        "buys_h24": 10,
                        "sells_h24": 10,
                    },
                    "recommendation": "OBSERVE_ONLY",
                    "execution_authorized": False,
                    "is_stale": False,
                }
            )
        return {
            "service": "lumen-base-research-agent",
            "schema_version": 1,
            "mode": "observation_only",
            "execution": "disabled",
            "packets": packets,
        }

    def test_fresh_complete_required_packets_pass(self):
        decision = evaluate_research_payload(self.payload(), now=self.now)
        self.assertTrue(decision.ready)
        self.assertEqual(decision.age_seconds, 30)
        self.assertEqual(len(decision.packet_ids), 2)

    def test_partial_packet_fails_closed(self):
        payload = self.payload()
        payload["packets"][0]["metrics"]["liquidity_usd"] = None
        decision = evaluate_research_payload(payload, now=self.now)
        self.assertFalse(decision.ready)

    def test_stablecoin_missing_change_percent_can_pass(self):
        payload = self.payload()
        payload["packets"][0]["data_quality"] = "partial"
        payload["packets"][0]["metrics"]["price_change_h24_percent"] = None
        decision = evaluate_research_payload(payload, now=self.now)
        self.assertTrue(decision.ready)

    def test_stale_packet_fails_closed(self):
        payload = self.payload()
        payload["packets"][0]["expires_at"] = (self.now - timedelta(seconds=1)).isoformat()
        decision = evaluate_research_payload(payload, now=self.now)
        self.assertFalse(decision.ready)

    def test_execution_authorization_is_rejected(self):
        payload = self.payload()
        payload["packets"][0]["execution_authorized"] = True
        decision = evaluate_research_payload(payload, now=self.now)
        self.assertFalse(decision.ready)


if __name__ == "__main__":
    unittest.main()
