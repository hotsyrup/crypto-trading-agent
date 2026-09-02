import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from app.research_feed import (
    APPROVED_QUOTE_CONTRACTS,
    REQUIRED_CONTRACTS,
    USDC_CONTRACT,
    WETH_CONTRACT,
    _packet_digest,
    evaluate_research_payload,
    get_research_payload,
)


TEST_MARKETS = {
    WETH_CONTRACT: ("uniswap", "0x6c561b446416e1a00e8e93e221854d6ea4171372"),
    USDC_CONTRACT: ("aerodrome", "0x98c7a2338336d2d354663246f64676009c7bda97"),
}


class ResearchFeedTests(unittest.TestCase):
    now = datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc)

    def packet(self, contract):
        dex_id, pair_address = TEST_MARKETS[contract]
        packet = {
            "schema_version": 2,
            "network": "base",
            "contract_address": contract,
            "symbol": "WETH" if contract == WETH_CONTRACT else "USDC",
            "name": "Wrapped Ether" if contract == WETH_CONTRACT else "USD Coin",
            "pair_address": pair_address,
            "dex_id": dex_id,
            "received_at": (self.now - timedelta(seconds=30)).isoformat(),
            "expires_at": (self.now + timedelta(minutes=89)).isoformat(),
            "source": {
                "provider": "dexscreener",
                "discovery": "configured_watchlist",
                "profile_url": None,
                "marketing_influenced": False,
                "promotion_type": None,
                "eligible_pair_count": 2,
                "base_contract_address": contract,
                "quote_contract_address": APPROVED_QUOTE_CONTRACTS[contract],
                "pair_created_at_provider": "dexscreener",
            },
            "metrics": {
                "price_usd": "2000" if contract == WETH_CONTRACT else "1.00",
                "liquidity_usd": "100000",
                "volume_h24_usd": "50000",
                "volume_h6_usd": "10000",
                "price_change_h24_percent": "1.2",
                "price_change_h6_percent": "0.2",
                "pair_created_at": "2025-01-01T00:00:00+00:00",
                "buys_h24": 10,
                "sells_h24": 10,
                "market_cap_usd": "1000000",
                "fdv_usd": "1100000",
                "active_boosts": 0,
            },
            "warnings": [
                "CONTRACT_SECURITY_NOT_VERIFIED",
                "HOLDER_CONCENTRATION_NOT_VERIFIED",
            ],
            "data_quality": "complete",
            "recommendation": "OBSERVE_ONLY",
            "execution_authorized": False,
            "is_stale": False,
        }
        packet["packet_id"] = _packet_digest(packet)
        return packet

    def payload(self):
        return {
            "service": "lumen-base-research-agent",
            "schema_version": 2,
            "mode": "observation_only",
            "execution": "disabled",
            "generated_at": self.now.isoformat(),
            "packets": [self.packet(contract) for contract in sorted(REQUIRED_CONTRACTS)],
        }

    def mutate(self, payload, contract, change):
        packet = next(
            packet for packet in payload["packets"] if packet["contract_address"] == contract
        )
        change(packet)
        packet["packet_id"] = _packet_digest(packet)

    @patch("app.research_feed.urlopen")
    def test_exact_required_contracts_are_sent_without_wallet_context(self, urlopen):
        response = BytesIO(json.dumps(self.payload()).encode())
        response.headers = {}
        urlopen.return_value = response

        get_research_payload((WETH_CONTRACT,))

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://lumen-base-research-agent-production.up.railway.app"
            "/research/crypto/base/latest?required_contracts="
            f"{WETH_CONTRACT}",
        )
        self.assertNotIn("wallet", request.full_url)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 120)

    @patch("app.research_feed.urlopen")
    def test_required_contracts_are_isolated_when_bulk_provider_times_out(self, urlopen):
        contracts = (
            WETH_CONTRACT,
            USDC_CONTRACT,
            "0x00f3c42833c3170159af4e92dbb451fb3f708917",
        )

        def respond(request, **_kwargs):
            requested = parse_qs(urlparse(request.full_url).query)[
                "required_contracts"
            ][0].split(",")
            if len(requested) != 1:
                raise TimeoutError("bulk valuation deadline exceeded")
            response = BytesIO(
                json.dumps(
                    {
                        "service": "lumen-base-research-agent",
                        "schema_version": 2,
                        "mode": "observation_only",
                        "execution": "disabled",
                        "generated_at": self.now.isoformat(),
                        "packets": [{"contract_address": requested[0]}],
                    }
                ).encode()
            )
            response.headers = {}
            return response

        urlopen.side_effect = respond

        payload = get_research_payload(contracts)

        self.assertEqual(
            tuple(packet["contract_address"] for packet in payload["packets"]),
            contracts,
        )
        self.assertEqual(urlopen.call_count, 3)

    @patch("app.research_feed.urlopen")
    def test_one_contract_timeout_preserves_other_valuation_packets(self, urlopen):
        contracts = (
            WETH_CONTRACT,
            USDC_CONTRACT,
            "0x00f3c42833c3170159af4e92dbb451fb3f708917",
        )

        def respond(request, **_kwargs):
            contract = parse_qs(urlparse(request.full_url).query)[
                "required_contracts"
            ][0]
            if contract == USDC_CONTRACT:
                raise TimeoutError("one contract timed out")
            response = BytesIO(
                json.dumps(
                    {
                        "service": "lumen-base-research-agent",
                        "schema_version": 2,
                        "mode": "observation_only",
                        "execution": "disabled",
                        "generated_at": self.now.isoformat(),
                        "packets": [{"contract_address": contract}],
                    }
                ).encode()
            )
            response.headers = {}
            return response

        urlopen.side_effect = respond

        payload = get_research_payload(contracts)

        self.assertEqual(
            tuple(packet["contract_address"] for packet in payload["packets"]),
            (contracts[0], contracts[2]),
        )

    def test_fresh_complete_authenticated_packets_pass(self):
        decision = evaluate_research_payload(self.payload(), now=self.now)
        self.assertTrue(decision.ready, decision.reason)
        self.assertEqual(decision.age_seconds, 30)
        self.assertEqual(decision.qualities, ("complete", "complete"))

    def test_usdc_identity_packet_accepts_explicit_missing_change_metrics(self):
        payload = self.payload()
        self.mutate(
            payload,
            USDC_CONTRACT,
            lambda packet: (
                packet["metrics"].update(
                    price_change_h24_percent=None,
                    price_change_h6_percent=None,
                ),
                packet.update(data_quality="partial"),
                packet["warnings"].append("MARKET_FIELDS_INCOMPLETE"),
                packet["warnings"].sort(),
            ),
        )

        decision = evaluate_research_payload(payload, now=self.now)
        self.assertTrue(decision.ready, decision.reason)
        self.assertEqual(
            decision.qualities,
            ("complete", "stablecoin_identity_only"),
        )

    def test_partial_packet_fails_closed(self):
        payload = self.payload()
        self.mutate(payload, USDC_CONTRACT, lambda packet: packet.update(data_quality="partial"))
        decision = evaluate_research_payload(payload, now=self.now)
        self.assertFalse(decision.ready)
        self.assertIn("data_quality", decision.reason)

    def test_incomplete_warning_fails_closed(self):
        payload = self.payload()
        self.mutate(
            payload,
            USDC_CONTRACT,
            lambda packet: packet["warnings"].append("MARKET_FIELDS_INCOMPLETE"),
        )
        self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_missing_or_reordered_advisory_warnings_fail_closed(self):
        changes = (
            lambda packet: packet["warnings"].pop(),
            lambda packet: packet["warnings"].reverse(),
        )
        for change in changes:
            with self.subTest(change=change):
                payload = self.payload()
                self.mutate(payload, WETH_CONTRACT, change)
                self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_forged_packet_id_fails_closed(self):
        payload = self.payload()
        payload["packets"][0]["packet_id"] = "f" * 64
        decision = evaluate_research_payload(payload, now=self.now)
        self.assertFalse(decision.ready)
        self.assertIn("digest", decision.reason)

    def test_wrong_provider_pair_and_dex_fail_closed(self):
        mutations = (
            lambda packet: packet["source"].update(provider="unknown"),
            lambda packet: packet.update(pair_address="not-a-pair"),
            lambda packet: packet.update(dex_id=""),
            lambda packet: packet.update(name="Copied Ether"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = self.payload()
                self.mutate(payload, WETH_CONTRACT, mutation)
                self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_unapproved_pool_asset_identity_fails_closed(self):
        payload = self.payload()
        self.mutate(
            payload,
            WETH_CONTRACT,
            lambda packet: packet["source"].update(
                quote_contract_address="0x" + "1" * 40
            ),
        )
        self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_promotional_or_poolless_watchlist_source_fails_closed(self):
        mutations = (
            lambda packet: packet["source"].update(marketing_influenced=True),
            lambda packet: packet["source"].update(promotion_type="boost"),
            lambda packet: packet["source"].update(eligible_pair_count=0),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = self.payload()
                self.mutate(payload, WETH_CONTRACT, mutation)
                self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_nonfinite_and_inconsistent_metrics_fail_closed(self):
        for field, value in (
            ("price_usd", "NaN"),
            ("liquidity_usd", "-1"),
            ("volume_h6_usd", "999999"),
        ):
            with self.subTest(field=field):
                payload = self.payload()
                self.mutate(
                    payload,
                    WETH_CONTRACT,
                    lambda packet, field=field, value=value: packet["metrics"].update(
                        {field: value}
                    ),
                )
                self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_invalid_enrichment_metrics_fail_closed(self):
        for field, value in (
            ("market_cap_usd", "-1"),
            ("fdv_usd", "NaN"),
            ("active_boosts", -1),
        ):
            with self.subTest(field=field):
                payload = self.payload()
                self.mutate(
                    payload,
                    WETH_CONTRACT,
                    lambda packet, field=field, value=value: packet["metrics"].update(
                        {field: value}
                    ),
                )
                self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_unexpected_packet_or_envelope_fields_fail_closed(self):
        payload = self.payload()
        payload["trade_now"] = True
        self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

        payload = self.payload()
        self.mutate(payload, WETH_CONTRACT, lambda packet: packet.update(order_size="all"))
        self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_stale_future_and_overlong_packets_fail_closed(self):
        changes = (
            lambda packet: packet.update(expires_at=(self.now - timedelta(seconds=1)).isoformat()),
            lambda packet: packet.update(received_at=(self.now + timedelta(minutes=2)).isoformat()),
            lambda packet: packet.update(expires_at=(self.now + timedelta(hours=3)).isoformat()),
            lambda packet: packet.update(expires_at=(self.now + timedelta(minutes=1)).isoformat()),
        )
        for change in changes:
            with self.subTest(change=change):
                payload = self.payload()
                self.mutate(payload, WETH_CONTRACT, change)
                self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_stale_response_generation_fails_closed(self):
        payload = self.payload()
        payload["generated_at"] = (self.now - timedelta(minutes=11)).isoformat()
        self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_execution_authorization_is_rejected(self):
        payload = self.payload()
        self.mutate(
            payload,
            WETH_CONTRACT,
            lambda packet: packet.update(execution_authorized=True),
        )
        self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_missing_staleness_state_and_boolean_schema_fail_closed(self):
        payload = self.payload()
        self.mutate(payload, WETH_CONTRACT, lambda packet: packet.pop("is_stale"))
        self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

        payload = self.payload()
        payload["schema_version"] = True
        self.assertFalse(evaluate_research_payload(payload, now=self.now).ready)

    def test_ambiguous_latest_packet_fails_closed(self):
        payload = self.payload()
        duplicate = deepcopy(payload["packets"][0])
        duplicate["metrics"]["price_usd"] = "2100"
        duplicate["packet_id"] = _packet_digest(duplicate)
        payload["packets"].append(duplicate)
        decision = evaluate_research_payload(payload, now=self.now)
        self.assertFalse(decision.ready)
        self.assertIn("ambiguous", decision.reason)

    def test_reported_age_uses_oldest_required_packet(self):
        payload = self.payload()
        self.mutate(
            payload,
            WETH_CONTRACT,
            lambda packet: packet.update(
                received_at=(self.now - timedelta(seconds=90)).isoformat()
            ),
        )
        decision = evaluate_research_payload(payload, now=self.now)
        self.assertTrue(decision.ready, decision.reason)
        self.assertEqual(decision.age_seconds, 90)

    def test_input_is_not_mutated(self):
        payload = self.payload()
        original = deepcopy(payload)
        evaluate_research_payload(payload, now=self.now)
        self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
