import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.agent_commerce_research import (
    AgentCommerceResearchGate,
    AmbiguousResearchPayment,
    CdpX402ResearchProvider,
    PreparedResearchPayment,
    PurchasedResearch,
    RESEARCH_ENDPOINT,
    RESEARCH_NETWORK,
    RESEARCH_PAY_TO,
    RESEARCH_USDC_ADDRESS,
    ResearchUnavailable,
    _CdpX402Signer,
    _TypedDataDomain,
    _TypedDataField,
    candidate_for_trade,
    load_research_mode,
    read_research_journal,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
AERO = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"


def candidate(*, address: str = AERO, decision: str = "a" * 64, now=NOW):
    return candidate_for_trade(
        symbol="AERO",
        token_address=address,
        side="BUY",
        trading_decision_id=decision,
        requested_at=now,
    )


def report(
    *,
    now=NOW,
    report_id="report-1",
    verdict="consider",
    thesis_status="supported",
    confidence="medium",
    red_flags=None,
):
    return {
        "report_id": report_id,
        "model": "test-model",
        "report": {
            "as_of": now.date().isoformat(),
            "verdict": verdict,
            "thesis_status": thesis_status,
            "confidence": confidence,
            "red_flags": [] if red_flags is None else red_flags,
        },
    }


class Provider:
    def __init__(self, factory=None, *, prepare_error=None, pay_error=None, delay=0):
        self.factory = factory or (lambda now, count: report(now=now, report_id=f"r-{count}"))
        self.prepare_error = prepare_error
        self.pay_error = pay_error
        self.delay = delay
        self.prepare_calls = 0
        self.pay_calls = 0
        self._lock = threading.Lock()

    def prepare(self, trade_candidate):
        with self._lock:
            self.prepare_calls += 1
        if self.prepare_error:
            raise self.prepare_error
        return PreparedResearchPayment({"subject": trade_candidate.symbol}, object())

    def pay(self, prepared, *, attempt_id, now):
        with self._lock:
            self.pay_calls += 1
            count = self.pay_calls
        if self.delay:
            time.sleep(self.delay)
        if self.pay_error:
            raise self.pay_error
        return PurchasedResearch(self.factory(now, count), "0x" + f"{count:064x}")


class AgentCommerceResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal = Path(self.temp_dir.name) / "research.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

    def gate(self, provider, mode="enforced"):
        return AgentCommerceResearchGate(
            mode=mode,
            provider=provider,
            journal_path=self.journal,
        )

    def test_feature_flag_defaults_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_research_mode(), "disabled")

    def test_disabled_mode_never_contacts_or_audits_provider(self):
        provider = Provider()
        decision = self.gate(provider, "disabled").evaluate(candidate())

        self.assertTrue(decision.allowed)
        self.assertEqual(provider.prepare_calls, 0)
        self.assertEqual(provider.pay_calls, 0)
        self.assertFalse(self.journal.exists())

    def test_shadow_mode_validates_challenge_without_signing_or_paying(self):
        provider = Provider()
        decision = self.gate(provider, "shadow").evaluate(candidate())

        self.assertFalse(decision.allowed)
        self.assertEqual(provider.prepare_calls, 1)
        self.assertEqual(provider.pay_calls, 0)
        details = read_research_journal(self.journal)[0]["details"]
        self.assertEqual(details["amount_usdc"], "0.00")
        self.assertEqual(details["payment_status"], "shadow_not_paid")

    def test_success_is_cached_per_asset_for_rolling_24_hours(self):
        provider = Provider()
        gate = self.gate(provider)

        first = gate.evaluate(candidate())
        second = gate.evaluate(
            candidate(decision="b" * 64, now=NOW + timedelta(hours=23, minutes=59))
        )

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(provider.prepare_calls, 1)
        self.assertEqual(provider.pay_calls, 1)
        evaluations = [
            item["details"]
            for item in read_research_journal(self.journal)
            if item["event"] == "EVALUATED"
        ]
        self.assertEqual(evaluations[-1]["amount_usdc"], "0.00")
        self.assertEqual(evaluations[-1]["payment_status"], "cache_reused")

    def test_expired_asset_cache_allows_one_new_purchase(self):
        provider = Provider()
        gate = self.gate(provider)
        gate.evaluate(candidate())

        decision = gate.evaluate(
            candidate(decision="b" * 64, now=NOW + timedelta(hours=24, seconds=1))
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(provider.pay_calls, 2)

    def test_five_dollar_rolling_ceiling_blocks_sixth_asset(self):
        provider = Provider()
        gate = self.gate(provider)
        for number in range(5):
            address = "0x" + f"{number + 1:040x}"
            decision = gate.evaluate(
                candidate(address=address, decision=f"{number + 1:064x}")
            )
            self.assertTrue(decision.allowed)

        blocked = gate.evaluate(
            candidate(address="0x" + "f" * 40, decision="f" * 64)
        )

        self.assertFalse(blocked.allowed)
        self.assertIn("budget", blocked.reason.lower())
        self.assertEqual(provider.pay_calls, 5)
        reservations = [
            item
            for item in read_research_journal(self.journal)
            if item["event"] == "RESERVED"
        ]
        self.assertEqual(len(reservations), 5)
        self.assertTrue(all(item["details"]["amount_usdc"] == "1.00" for item in reservations))

    def test_concurrent_same_asset_requests_make_one_purchase(self):
        provider = Provider(delay=0.1)
        gate = self.gate(provider)
        results = []
        barrier = threading.Barrier(3)

        def run(number):
            barrier.wait()
            results.append(
                gate.evaluate(candidate(decision=f"{number:064x}"))
            )

        threads = [threading.Thread(target=run, args=(number,)) for number in (1, 2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(item.allowed for item in results))
        self.assertEqual(provider.pay_calls, 1)
        self.assertEqual(sorted(item.cache_hit for item in results), [False, True])

    def test_all_configured_veto_rules_block(self):
        cases = (
            {"verdict": "avoid"},
            {"thesis_status": "contradicted"},
            {"confidence": "high", "red_flags": ["material risk"]},
        )
        for number, values in enumerate(cases, start=1):
            with self.subTest(values=values):
                journal = Path(self.temp_dir.name) / f"case-{number}.jsonl"
                provider = Provider(
                    lambda now, count, values=values: report(now=now, **values)
                )
                decision = AgentCommerceResearchGate(
                    mode="enforced", provider=provider, journal_path=journal
                ).evaluate(candidate(decision=f"{number:064x}"))
                self.assertFalse(decision.allowed)
                self.assertEqual(
                    read_research_journal(journal)[-1]["details"]["final_result"],
                    "veto",
                )

    def test_favorable_report_only_passes_existing_candidate(self):
        provider = Provider()
        decision = self.gate(provider).evaluate(candidate())

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "Paid research raised no configured veto.")
        self.assertEqual(provider.pay_calls, 1)

    def test_malformed_invalid_and_stale_reports_fail_closed(self):
        cases = (
            lambda now, count: {"report_id": "bad", "report": []},
            lambda now, count: report(now=now, verdict="buy"),
            lambda now, count: report(now=now - timedelta(days=2)),
        )
        for number, factory in enumerate(cases, start=1):
            with self.subTest(number=number):
                journal = Path(self.temp_dir.name) / f"invalid-{number}.jsonl"
                decision = AgentCommerceResearchGate(
                    mode="enforced",
                    provider=Provider(factory),
                    journal_path=journal,
                ).evaluate(candidate(decision=f"{number:064x}"))
                self.assertFalse(decision.allowed)
                details = read_research_journal(journal)[-1]["details"]
                self.assertEqual(details["settlement_status"], "confirmed")
                self.assertEqual(details["final_result"], "veto")

    def test_timeout_or_unavailable_before_signing_spends_nothing(self):
        provider = Provider(prepare_error=ResearchUnavailable("timeout"))
        decision = self.gate(provider).evaluate(candidate())

        self.assertFalse(decision.allowed)
        self.assertEqual(provider.pay_calls, 0)
        details = read_research_journal(self.journal)[0]["details"]
        self.assertEqual(details["amount_usdc"], "0.00")
        self.assertEqual(details["payment_status"], "not_started")

    def test_ambiguous_settlement_is_charged_and_never_retried(self):
        provider = Provider(
            pay_error=AmbiguousResearchPayment(
                "timeout", transaction_hash="0x" + "1" * 64
            )
        )
        gate = self.gate(provider)
        first = gate.evaluate(candidate())
        second = gate.evaluate(candidate(decision="b" * 64, now=NOW + timedelta(minutes=1)))

        self.assertFalse(first.allowed)
        self.assertFalse(second.allowed)
        self.assertEqual(provider.pay_calls, 1)
        events = read_research_journal(self.journal)
        self.assertEqual(sum(item["event"] == "RESERVED" for item in events), 1)
        self.assertEqual(events[-1]["details"]["payment_status"], "not_retried")

    def test_audit_journal_persists_required_candidate_fields(self):
        self.gate(Provider()).evaluate(candidate())
        details = read_research_journal(self.journal)[-1]["details"]
        required = {
            "normalized_asset",
            "research_request_time",
            "amount",
            "amount_usdc",
            "payment_status",
            "settlement_status",
            "report_id",
            "report_as_of",
            "verdict",
            "thesis_status",
            "confidence",
            "red_flags",
            "cache_hit",
            "report_source",
            "final_result",
            "trading_decision_id",
        }
        self.assertTrue(required <= set(details))
        text = self.journal.read_text()
        self.assertNotIn("PAYMENT-SIGNATURE", text)
        self.assertNotIn("authorization", text.lower())

    def test_journal_hash_chain_detects_tampering(self):
        self.gate(Provider()).evaluate(candidate())
        entries = [json.loads(line) for line in self.journal.read_text().splitlines()]
        entries[-1]["details"]["final_result"] = "pass" if entries[-1]["details"]["final_result"] == "veto" else "veto"
        self.journal.write_text("\n".join(json.dumps(item) for item in entries) + "\n")

        with self.assertRaisesRegex(RuntimeError, "hash"):
            read_research_journal(self.journal)

    def test_x402_challenge_enforces_exact_one_dollar_ceiling(self):
        provider = CdpX402ResearchProvider(wallet_address="0x" + "1" * 40)

        def challenge(amount):
            return {
                "x402Version": 2,
                "resource": {"url": RESEARCH_ENDPOINT},
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": RESEARCH_NETWORK,
                        "asset": RESEARCH_USDC_ADDRESS,
                        "amount": str(amount),
                        "payTo": RESEARCH_PAY_TO,
                        "maxTimeoutSeconds": 300,
                        "extra": {"name": "USD Coin", "version": "2"},
                    }
                ],
            }

        provider._validate_challenge(challenge(1_000_000))
        with self.assertRaises(ResearchUnavailable):
            provider._validate_challenge(challenge(1_000_001))

    def test_cdp_signer_boundary_rejects_amount_recipient_and_chain_changes(self):
        wallet = SimpleWallet()
        signer = _CdpX402Signer(wallet, attempt_id="a" * 64, now=NOW)
        domain = _TypedDataDomain(
            "USD Coin", "2", 8453, RESEARCH_USDC_ADDRESS
        )
        fields = {
            "TransferWithAuthorization": [
                _TypedDataField("from", "address"),
                _TypedDataField("to", "address"),
                _TypedDataField("value", "uint256"),
                _TypedDataField("validAfter", "uint256"),
                _TypedDataField("validBefore", "uint256"),
                _TypedDataField("nonce", "bytes32"),
            ]
        }
        message = {
            "from": wallet.get_address(),
            "to": RESEARCH_PAY_TO,
            "value": "1000000",
            "validAfter": "0",
            "validBefore": str(int((NOW + timedelta(seconds=300)).timestamp())),
            "nonce": "0x" + "1" * 64,
        }
        signer._validate(domain, fields, "TransferWithAuthorization", message)

        mutations = (
            (domain.__class__("USD Coin", "2", 1, RESEARCH_USDC_ADDRESS), message),
            (domain, {**message, "value": "1000001"}),
            (domain, {**message, "to": "0x" + "2" * 40}),
        )
        for changed_domain, changed_message in mutations:
            with self.subTest(domain=changed_domain, message=changed_message):
                with self.assertRaises(RuntimeError):
                    signer._validate(
                        changed_domain,
                        fields,
                        "TransferWithAuthorization",
                        changed_message,
                    )


class SimpleWallet:
    @staticmethod
    def get_address():
        return "0x" + "1" * 40


if __name__ == "__main__":
    unittest.main()
