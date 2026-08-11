from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from app.service_receipts import ProviderScorecard


POLICY_MODE = "observation_only"
JOURNAL_PATH = Path("data/treasury_policy_journal.jsonl")
PROHIBITED_PURPOSE_TERMS = frozenset(
    {
        "gambling",
        "gift",
        "nft",
        "personal purchase",
        "speculation",
        "token trade",
    }
)


@dataclass(frozen=True)
class PaidServiceProposal:
    service: str
    provider: str
    purpose: str
    expected_benefit: str
    price_usdc: Decimal
    remaining_budget_usdc: Decimal
    evidence: tuple[str, ...] = ()
    cheaper_alternative: str | None = None
    recurring: bool = False


@dataclass(frozen=True)
class TreasuryObservation:
    mode: str
    would_authorize: bool
    reason: str
    price_usdc: Decimal
    remaining_after_usdc: Decimal
    execution_permitted: bool = False


def _clean(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty.")
    return cleaned


def evaluate_paid_service(
    proposal: PaidServiceProposal,
    provider_scorecard: ProviderScorecard | None = None,
) -> TreasuryObservation:
    """Evaluate a paid service without purchasing or authorizing it."""
    _clean(proposal.service, "service")
    _clean(proposal.provider, "provider")
    purpose = _clean(proposal.purpose, "purpose")
    _clean(proposal.expected_benefit, "expected_benefit")

    if proposal.price_usdc < 0:
        raise ValueError("price_usdc must not be negative.")
    if proposal.remaining_budget_usdc < 0:
        raise ValueError("remaining_budget_usdc must not be negative.")
    if (
        provider_scorecard is not None
        and provider_scorecard.provider.strip().casefold()
        != proposal.provider.strip().casefold()
    ):
        raise ValueError("provider_scorecard does not match the proposal provider.")

    remaining_after = proposal.remaining_budget_usdc - proposal.price_usdc
    normalized_purpose = purpose.casefold()

    if proposal.recurring:
        decision = (False, "Recurring commitments require separate approval.")
    elif any(term in normalized_purpose for term in PROHIBITED_PURPOSE_TERMS):
        decision = (False, "The stated purpose is outside the operating-wallet mandate.")
    elif proposal.price_usdc > proposal.remaining_budget_usdc:
        decision = (False, "The proposed price exceeds the remaining budget.")
    elif not proposal.evidence:
        decision = (False, "No supporting evidence was supplied.")
    elif proposal.cheaper_alternative is None:
        decision = (False, "A cheaper or free alternative was not considered.")
    elif provider_scorecard is not None and provider_scorecard.would_block:
        decision = (
            False,
            f"Provider history requires manual review: {provider_scorecard.reason}",
        )
    else:
        decision = (
            True,
            "The proposal fits the recorded operating-wallet policy for review.",
        )

    return TreasuryObservation(
        mode=POLICY_MODE,
        would_authorize=decision[0],
        reason=decision[1],
        price_usdc=proposal.price_usdc,
        remaining_after_usdc=remaining_after,
    )


def _json_safe_pairs(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if isinstance(value, Decimal):
            result[key] = str(value)
        elif isinstance(value, tuple):
            result[key] = list(value)
        else:
            result[key] = value
    return result


def record_observation(
    proposal: PaidServiceProposal,
    observation: TreasuryObservation,
    journal_path: Path = JOURNAL_PATH,
) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "proposal": _json_safe_pairs(asdict(proposal).items()),
        "observation": _json_safe_pairs(asdict(observation).items()),
    }
    with journal_path.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(entry, sort_keys=True) + "\n")
