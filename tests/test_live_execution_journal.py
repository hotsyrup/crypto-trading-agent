import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.live_execution_journal import (
    LiveExecutionJournalError,
    append_live_execution_event,
    record_reconciled_transfer_accounting,
    read_live_execution_events,
    reconcile_rejected_receipt_as_confirmed,
    reserve_live_execution,
)


NOW = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
WALLET = "0x716b5d6bf67a4c01103b52365c8fb5fdfef0ff06"
ASSET = "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


class LiveExecutionReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "live.jsonl"
        reserve_live_execution(
            intent_id="production-sell-137",
            intent_fingerprint="a" * 64,
            notional_usdc=Decimal("7.003110"),
            route_id="cdp_agentkit_base_governed_asset_usdc_v2",
            wallet_address=WALLET,
            chain_id=8453,
            quote_id="production-sell-137-quote",
            quote_observed_at=NOW,
            from_token=ASSET,
            to_token=USDC,
            from_amount=Decimal("0.002515683710638053"),
            from_decimals=18,
            to_decimals=6,
            slippage_bps=50,
            path=self.path,
            recorded_at=NOW,
        )
        append_live_execution_event(
            event="RECEIPT_REJECTED",
            intent_id="production-sell-137",
            intent_fingerprint="a" * 64,
            details={
                "success": True,
                "transaction_hash": "0x" + "b" * 64,
                "from_amount": "0.002515683710638053",
                "to_amount": "6.9958",
                "min_to_amount": "6.9608",
                "validation_reasons": [
                    "CDP receipt minimum output exceeds approved slippage."
                ],
            },
            path=self.path,
            recorded_at=NOW,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_confirmed_reconciliation_appends_without_rewriting_source(self) -> None:
        before = read_live_execution_events(path=self.path)

        result = reconcile_rejected_receipt_as_confirmed(
            source_sequence=2,
            receipt_status="0x1",
            verification_source="independent_base_rpc",
            path=self.path,
            recorded_at=NOW,
        )
        after = read_live_execution_events(path=self.path)

        self.assertEqual(
            {
                "recorded": result.recorded,
                "duplicate": result.duplicate,
                "sequence": result.sequence,
                "source_unchanged": after[:2] == before,
                "event": after[-1]["event"],
                "source_sequence": after[-1]["details"]["source_sequence"],
                "receipt_status": after[-1]["details"]["receipt_status"],
            },
            {
                "recorded": True,
                "duplicate": False,
                "sequence": 3,
                "source_unchanged": True,
                "event": "RECONCILED_CONFIRMED",
                "source_sequence": 2,
                "receipt_status": "0x1",
            },
        )

    def test_reconciliation_retry_is_idempotent(self) -> None:
        first = reconcile_rejected_receipt_as_confirmed(
            source_sequence=2,
            receipt_status="0x1",
            verification_source="independent_base_rpc",
            path=self.path,
            recorded_at=NOW,
        )
        second = reconcile_rejected_receipt_as_confirmed(
            source_sequence=2,
            receipt_status="0x1",
            verification_source="independent_base_rpc",
            path=self.path,
            recorded_at=NOW,
        )

        self.assertEqual(
            (first.recorded, second.duplicate, first.sequence, second.sequence),
            (True, True, 3, 3),
        )

    def test_exact_transfer_accounting_appends_once_after_reconciliation(self) -> None:
        reconciliation = reconcile_rejected_receipt_as_confirmed(
            source_sequence=2,
            receipt_status="0x1",
            verification_source="independent_base_rpc",
            path=self.path,
            recorded_at=NOW,
        )

        first = record_reconciled_transfer_accounting(
            reconciliation_sequence=reconciliation.sequence,
            transaction_hash="0x" + "b" * 64,
            block_number=50731970,
            from_atomic_amount=2515683710638053,
            to_atomic_amount=6995866,
            verification_source="public_base_rpc",
            path=self.path,
            recorded_at=NOW,
        )
        second = record_reconciled_transfer_accounting(
            reconciliation_sequence=reconciliation.sequence,
            transaction_hash="0x" + "b" * 64,
            block_number=50731970,
            from_atomic_amount=2515683710638053,
            to_atomic_amount=6995866,
            verification_source="public_base_rpc",
            path=self.path,
            recorded_at=NOW,
        )
        entry = read_live_execution_events(path=self.path)[-1]

        self.assertEqual((first.recorded, second.duplicate), (True, True))
        self.assertEqual(entry["event"], "RECONCILIATION_ACCOUNTED")
        self.assertEqual(
            entry["details"],
            {
                "source_reconciliation_sequence": 3,
                "source_receipt_sequence": 2,
                "transaction_hash": "0x" + "b" * 64,
                "block_number": 50731970,
                "from_token": ASSET,
                "to_token": USDC,
                "from_decimals": 18,
                "to_decimals": 6,
                "from_atomic_amount": "2515683710638053",
                "to_atomic_amount": "6995866",
                "from_amount": "0.002515683710638053",
                "to_amount": "6.995866",
                "min_to_amount": "6.9608",
                "executed_at": NOW.isoformat(),
                "verification_source": "public_base_rpc",
            },
        )

    def test_unrelated_receipt_rejection_cannot_be_reconciled(self) -> None:
        reserve_live_execution(
            intent_id="unrelated-rejection",
            intent_fingerprint="c" * 64,
            notional_usdc=Decimal("1"),
            route_id="cdp_agentkit_base_governed_asset_usdc_v2",
            wallet_address=WALLET,
            chain_id=8453,
            quote_id="unrelated-rejection-quote",
            quote_observed_at=NOW,
            from_token=ASSET,
            to_token=USDC,
            from_amount=Decimal("0.1"),
            from_decimals=18,
            to_decimals=6,
            slippage_bps=50,
            path=self.path,
            recorded_at=NOW,
        )
        append_live_execution_event(
            event="RECEIPT_REJECTED",
            intent_id="unrelated-rejection",
            intent_fingerprint="c" * 64,
            details={
                "success": True,
                "transaction_hash": "0x" + "d" * 64,
                "validation_reasons": ["Wrong wallet."],
            },
            path=self.path,
            recorded_at=NOW,
        )

        with self.assertRaisesRegex(
            LiveExecutionJournalError,
            "outside the bounded rounding reconciliation",
        ):
            reconcile_rejected_receipt_as_confirmed(
                source_sequence=4,
                receipt_status="0x1",
                verification_source="independent_base_rpc",
                path=self.path,
                recorded_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
