import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.service_receipts import ProviderScorecard
from app.treasury_policy import (
    PaidServiceProposal,
    evaluate_paid_service,
    record_observation,
)


def proposal(**overrides: object) -> PaidServiceProposal:
    values: dict[str, object] = {
        "service": "Market data API",
        "provider": "Example Provider",
        "purpose": "Improve a read-only research report",
        "expected_benefit": "Fresher evidence for the next recommendation",
        "price_usdc": Decimal("0.01"),
        "remaining_budget_usdc": Decimal("24.984"),
        "evidence": ("Provider documentation checked on 2026-08-08",),
        "cheaper_alternative": "Use the free source, with lower freshness",
        "recurring": False,
    }
    values.update(overrides)
    return PaidServiceProposal(**values)  # type: ignore[arg-type]


class TreasuryPolicyTests(unittest.TestCase):
    def test_complete_bounded_proposal_is_observed_but_not_executed(self) -> None:
        observation = evaluate_paid_service(proposal())

        self.assertTrue(observation.would_authorize)
        self.assertEqual(observation.mode, "observation_only")
        self.assertFalse(observation.execution_permitted)
        self.assertEqual(observation.remaining_after_usdc, Decimal("24.974"))

    def test_missing_evidence_fails_closed(self) -> None:
        observation = evaluate_paid_service(proposal(evidence=()))

        self.assertFalse(observation.would_authorize)
        self.assertIn("evidence", observation.reason)

    def test_recurring_commitment_fails_closed(self) -> None:
        observation = evaluate_paid_service(proposal(recurring=True))

        self.assertFalse(observation.would_authorize)
        self.assertIn("Recurring", observation.reason)

    def test_over_budget_proposal_fails_closed(self) -> None:
        observation = evaluate_paid_service(
            proposal(price_usdc=Decimal("25"), remaining_budget_usdc=Decimal("1"))
        )

        self.assertFalse(observation.would_authorize)
        self.assertIn("exceeds", observation.reason)

    def test_prohibited_purpose_fails_closed(self) -> None:
        observation = evaluate_paid_service(proposal(purpose="NFT speculation"))

        self.assertFalse(observation.would_authorize)
        self.assertIn("outside", observation.reason)

    def test_observation_is_append_only_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "treasury.jsonl"
            item = proposal()
            result = evaluate_paid_service(item)

            record_observation(item, result, journal)
            record_observation(item, result, journal)

            lines = journal.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            record = json.loads(lines[0])
            self.assertEqual(record["observation"]["mode"], "observation_only")
            self.assertFalse(record["observation"]["execution_permitted"])
            self.assertEqual(record["proposal"]["price_usdc"], "0.01")

    def test_charged_provider_history_fails_closed(self) -> None:
        scorecard = ProviderScorecard(
            provider="Example Provider",
            total_attempts=2,
            paid_attempts=2,
            delivered_results=1,
            failed_no_charge=0,
            charged_without_result=1,
            total_settled_usdc=Decimal("0.02"),
            paid_delivery_rate=Decimal("0.5"),
            average_usefulness=Decimal("2"),
            recommendation="manual_review",
            would_block=True,
            reason="Provider charged without delivering a result.",
        )

        observation = evaluate_paid_service(proposal(), scorecard)

        self.assertFalse(observation.would_authorize)
        self.assertIn("Provider history", observation.reason)
        self.assertFalse(observation.execution_permitted)

    def test_mismatched_provider_scorecard_is_rejected(self) -> None:
        scorecard = ProviderScorecard(
            provider="Different Provider",
            total_attempts=0,
            paid_attempts=0,
            delivered_results=0,
            failed_no_charge=0,
            charged_without_result=0,
            total_settled_usdc=Decimal("0"),
            paid_delivery_rate=None,
            average_usefulness=None,
            recommendation="insufficient_history",
            would_block=False,
            reason="No history.",
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            evaluate_paid_service(proposal(), scorecard)


if __name__ == "__main__":
    unittest.main()
