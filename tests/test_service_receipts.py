import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.service_receipts import (
    ReceiptOutcome,
    ServiceReceipt,
    load_receipts,
    record_receipt,
    score_provider,
    validate_receipt,
)


def receipt(**overrides: object) -> ServiceReceipt:
    values: dict[str, object] = {
        "attempt_id": "attempt-001",
        "provider": "Example Provider",
        "service": "Market data",
        "occurred_at": datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
        "quoted_max_usdc": Decimal("0.01"),
        "settled_usdc": Decimal("0.01"),
        "outcome": ReceiptOutcome.DELIVERED,
        "usefulness_score": 4,
        "result_summary": "Returned a current, structured market record.",
        "transaction_reference": "masked-reference",
        "balance_after_usdc": Decimal("24.90"),
    }
    values.update(overrides)
    return ServiceReceipt(**values)  # type: ignore[arg-type]


class ServiceReceiptTests(unittest.TestCase):
    def test_receipts_are_appended_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "receipts.jsonl"
            record_receipt(receipt(), journal)
            record_receipt(
                receipt(attempt_id="attempt-002", usefulness_score=5), journal
            )

            loaded = load_receipts(journal)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0].settled_usdc, Decimal("0.01"))
            self.assertEqual(loaded[1].attempt_id, "attempt-002")

    def test_missing_journal_loads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_receipts(Path(directory) / "missing.jsonl"), ())

    def test_malformed_journal_fails_closed_with_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "receipts.jsonl"
            journal.write_text('{"not":"a receipt"}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "line 1"):
                load_receipts(journal)

    def test_failed_no_charge_requires_zero_settlement(self) -> None:
        item = receipt(
            outcome=ReceiptOutcome.FAILED_NO_CHARGE,
            settled_usdc=Decimal("0.01"),
            usefulness_score=0,
        )

        with self.assertRaisesRegex(ValueError, "zero settlement"):
            validate_receipt(item)

    def test_charged_without_result_requires_positive_settlement(self) -> None:
        item = receipt(
            outcome=ReceiptOutcome.CHARGED_WITHOUT_RESULT,
            settled_usdc=Decimal("0"),
            usefulness_score=0,
        )

        with self.assertRaisesRegex(ValueError, "require a settlement"):
            validate_receipt(item)

    def test_charged_without_result_blocks_provider_recommendation(self) -> None:
        history = (
            receipt(),
            receipt(
                attempt_id="attempt-002",
                outcome=ReceiptOutcome.CHARGED_WITHOUT_RESULT,
                usefulness_score=0,
                result_summary="Payment settled but the service rejected the input.",
            ),
        )

        scorecard = score_provider(history, "example provider")

        self.assertTrue(scorecard.would_block)
        self.assertEqual(scorecard.recommendation, "manual_review")
        self.assertEqual(scorecard.charged_without_result, 1)
        self.assertEqual(scorecard.total_settled_usdc, Decimal("0.02"))
        self.assertFalse(scorecard.execution_permitted)

    def test_successful_provider_is_eligible_but_not_authorized(self) -> None:
        scorecard = score_provider((receipt(),), "Example Provider")

        self.assertFalse(scorecard.would_block)
        self.assertEqual(scorecard.recommendation, "eligible")
        self.assertEqual(scorecard.paid_delivery_rate, Decimal("1"))
        self.assertEqual(scorecard.average_usefulness, Decimal("4"))
        self.assertFalse(scorecard.execution_permitted)

    def test_repeated_low_usefulness_blocks(self) -> None:
        history = (
            receipt(
                attempt_id="attempt-001",
                outcome=ReceiptOutcome.DELIVERED,
                usefulness_score=1,
            ),
            receipt(
                attempt_id="attempt-002",
                outcome=ReceiptOutcome.DELIVERED,
                usefulness_score=1,
            ),
            receipt(
                attempt_id="attempt-003",
                outcome=ReceiptOutcome.DELIVERED,
                usefulness_score=1,
            ),
        )

        scorecard = score_provider(history, "Example Provider")

        self.assertTrue(scorecard.would_block)
        self.assertEqual(scorecard.paid_delivery_rate, Decimal("1"))
        self.assertLess(scorecard.average_usefulness, Decimal("2.5"))

    def test_new_provider_has_insufficient_history(self) -> None:
        scorecard = score_provider((), "New Provider")

        self.assertFalse(scorecard.would_block)
        self.assertEqual(scorecard.recommendation, "insufficient_history")
        self.assertIsNone(scorecard.paid_delivery_rate)

    def test_json_representation_contains_no_implicit_float_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "receipts.jsonl"
            record_receipt(receipt(), journal)
            data = json.loads(journal.read_text(encoding="utf-8"))

            self.assertEqual(data["quoted_max_usdc"], "0.01")
            self.assertEqual(data["balance_after_usdc"], "24.90")


if __name__ == "__main__":
    unittest.main()
