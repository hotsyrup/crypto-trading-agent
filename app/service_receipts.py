from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Iterable


RECEIPT_JOURNAL_PATH = Path("data/x402_service_receipts.jsonl")


class ReceiptOutcome(str, Enum):
    DELIVERED = "delivered"
    FAILED_NO_CHARGE = "failed_no_charge"
    CHARGED_WITHOUT_RESULT = "charged_without_result"


@dataclass(frozen=True)
class ServiceReceipt:
    attempt_id: str
    provider: str
    service: str
    occurred_at: datetime
    quoted_max_usdc: Decimal
    settled_usdc: Decimal
    outcome: ReceiptOutcome
    usefulness_score: int
    result_summary: str
    transaction_reference: str | None = None
    balance_after_usdc: Decimal | None = None


@dataclass(frozen=True)
class ProviderScorecard:
    provider: str
    total_attempts: int
    paid_attempts: int
    delivered_results: int
    failed_no_charge: int
    charged_without_result: int
    total_settled_usdc: Decimal
    paid_delivery_rate: Decimal | None
    average_usefulness: Decimal | None
    recommendation: str
    would_block: bool
    reason: str
    execution_permitted: bool = False


def _clean(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty.")
    return cleaned


def validate_receipt(receipt: ServiceReceipt) -> None:
    _clean(receipt.attempt_id, "attempt_id")
    _clean(receipt.provider, "provider")
    _clean(receipt.service, "service")
    _clean(receipt.result_summary, "result_summary")

    if receipt.occurred_at.tzinfo is None or receipt.occurred_at.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware.")
    if receipt.quoted_max_usdc < 0:
        raise ValueError("quoted_max_usdc must not be negative.")
    if receipt.settled_usdc < 0:
        raise ValueError("settled_usdc must not be negative.")
    if not isinstance(receipt.usefulness_score, int) or not 0 <= receipt.usefulness_score <= 5:
        raise ValueError("usefulness_score must be an integer from 0 through 5.")
    if (
        receipt.outcome is ReceiptOutcome.FAILED_NO_CHARGE
        and receipt.settled_usdc != 0
    ):
        raise ValueError("failed_no_charge receipts must have zero settlement.")
    if (
        receipt.outcome is ReceiptOutcome.CHARGED_WITHOUT_RESULT
        and receipt.settled_usdc <= 0
    ):
        raise ValueError("charged_without_result receipts require a settlement.")
    if receipt.balance_after_usdc is not None and receipt.balance_after_usdc < 0:
        raise ValueError("balance_after_usdc must not be negative.")


def _receipt_to_dict(receipt: ServiceReceipt) -> dict[str, object]:
    validate_receipt(receipt)
    return {
        "attempt_id": receipt.attempt_id,
        "provider": receipt.provider,
        "service": receipt.service,
        "occurred_at": receipt.occurred_at.isoformat(),
        "quoted_max_usdc": str(receipt.quoted_max_usdc),
        "settled_usdc": str(receipt.settled_usdc),
        "outcome": receipt.outcome.value,
        "usefulness_score": receipt.usefulness_score,
        "result_summary": receipt.result_summary,
        "transaction_reference": receipt.transaction_reference,
        "balance_after_usdc": (
            str(receipt.balance_after_usdc)
            if receipt.balance_after_usdc is not None
            else None
        ),
    }


def _receipt_from_dict(value: object) -> ServiceReceipt:
    if not isinstance(value, dict):
        raise ValueError("Receipt entry must be a JSON object.")
    try:
        receipt = ServiceReceipt(
            attempt_id=str(value["attempt_id"]),
            provider=str(value["provider"]),
            service=str(value["service"]),
            occurred_at=datetime.fromisoformat(str(value["occurred_at"])),
            quoted_max_usdc=Decimal(str(value["quoted_max_usdc"])),
            settled_usdc=Decimal(str(value["settled_usdc"])),
            outcome=ReceiptOutcome(str(value["outcome"])),
            usefulness_score=int(value["usefulness_score"]),
            result_summary=str(value["result_summary"]),
            transaction_reference=(
                str(value["transaction_reference"])
                if value.get("transaction_reference") is not None
                else None
            ),
            balance_after_usdc=(
                Decimal(str(value["balance_after_usdc"]))
                if value.get("balance_after_usdc") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid receipt entry: {exc}") from exc
    validate_receipt(receipt)
    return receipt


def record_receipt(
    receipt: ServiceReceipt,
    journal_path: Path = RECEIPT_JOURNAL_PATH,
) -> None:
    """Append one privacy-minimized receipt to the durable local journal."""
    entry = _receipt_to_dict(receipt)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(entry, sort_keys=True) + "\n")
        journal.flush()
        os.fsync(journal.fileno())


def load_receipts(
    journal_path: Path = RECEIPT_JOURNAL_PATH,
) -> tuple[ServiceReceipt, ...]:
    """Load the journal, failing closed if any nonblank line is malformed."""
    if not journal_path.exists():
        return ()

    receipts: list[ServiceReceipt] = []
    for line_number, line in enumerate(
        journal_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            receipts.append(_receipt_from_dict(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                f"Malformed service-receipt journal at line {line_number}: {exc}"
            ) from exc
    return tuple(receipts)


def score_provider(
    receipts: Iterable[ServiceReceipt],
    provider: str,
    *,
    minimum_attempts: int = 3,
    minimum_paid_delivery_rate: Decimal = Decimal("0.80"),
    minimum_average_usefulness: Decimal = Decimal("2.5"),
) -> ProviderScorecard:
    """Summarize observed value and recommend, but never authorize, future use."""
    provider_name = _clean(provider, "provider")
    if minimum_attempts <= 0:
        raise ValueError("minimum_attempts must be positive.")
    if not Decimal("0") <= minimum_paid_delivery_rate <= Decimal("1"):
        raise ValueError("minimum_paid_delivery_rate must be between 0 and 1.")
    if not Decimal("0") <= minimum_average_usefulness <= Decimal("5"):
        raise ValueError("minimum_average_usefulness must be between 0 and 5.")

    matches = tuple(
        receipt
        for receipt in receipts
        if receipt.provider.strip().casefold() == provider_name.casefold()
    )
    for receipt in matches:
        validate_receipt(receipt)

    paid = tuple(receipt for receipt in matches if receipt.settled_usdc > 0)
    delivered = tuple(
        receipt for receipt in matches if receipt.outcome is ReceiptOutcome.DELIVERED
    )
    paid_delivered = tuple(
        receipt for receipt in delivered if receipt.settled_usdc > 0
    )
    failed_no_charge = sum(
        receipt.outcome is ReceiptOutcome.FAILED_NO_CHARGE for receipt in matches
    )
    charged_without_result = sum(
        receipt.outcome is ReceiptOutcome.CHARGED_WITHOUT_RESULT
        for receipt in matches
    )
    total_settled = sum(
        (receipt.settled_usdc for receipt in matches), start=Decimal("0")
    )
    paid_delivery_rate = (
        Decimal(len(paid_delivered)) / Decimal(len(paid)) if paid else None
    )
    average_usefulness = (
        Decimal(sum(receipt.usefulness_score for receipt in matches))
        / Decimal(len(matches))
        if matches
        else None
    )

    if charged_without_result:
        recommendation = "manual_review"
        would_block = True
        reason = "Provider has charged at least once without delivering a result."
    elif (
        len(paid) >= minimum_attempts
        and paid_delivery_rate is not None
        and paid_delivery_rate < minimum_paid_delivery_rate
    ):
        recommendation = "manual_review"
        would_block = True
        reason = "Provider paid-delivery history is below the required threshold."
    elif (
        len(matches) >= minimum_attempts
        and average_usefulness is not None
        and average_usefulness < minimum_average_usefulness
    ):
        recommendation = "manual_review"
        would_block = True
        reason = "Provider result usefulness is below the required threshold."
    elif len(matches) >= minimum_attempts and not delivered:
        recommendation = "manual_review"
        would_block = True
        reason = "Provider has repeated attempts without a delivered result."
    elif delivered:
        recommendation = "eligible"
        would_block = False
        reason = "Provider has delivered a result and has no charged failure on record."
    else:
        recommendation = "insufficient_history"
        would_block = False
        reason = "There is not enough provider history for a reliability judgment."

    return ProviderScorecard(
        provider=provider_name,
        total_attempts=len(matches),
        paid_attempts=len(paid),
        delivered_results=len(delivered),
        failed_no_charge=failed_no_charge,
        charged_without_result=charged_without_result,
        total_settled_usdc=total_settled,
        paid_delivery_rate=paid_delivery_rate,
        average_usefulness=average_usefulness,
        recommendation=recommendation,
        would_block=would_block,
        reason=reason,
    )
