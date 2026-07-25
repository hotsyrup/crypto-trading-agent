from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.strategy import Signal

if TYPE_CHECKING:
    from app.trading_cycle import TradeProposal


JOURNAL_PATH = Path("data/trade_journal.jsonl")


def _ensure_data_dir() -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)


def record_decision(
    signal: Signal,
    reference_price: Decimal,
    maximum_risk: Decimal,
    paper_only: bool,
) -> None:
    _ensure_data_dir()

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal": signal.value,
        "reference_price": str(reference_price),
        "maximum_risk": str(maximum_risk),
        "paper_only": paper_only,
    }

    with JOURNAL_PATH.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(entry) + "\n")


def append_trade_record(proposal: "TradeProposal") -> None:
    record_decision(
        signal=proposal.signal,
        reference_price=proposal.reference_price,
        maximum_risk=proposal.maximum_risk,
        paper_only=proposal.paper_only,
    )


def read_trade_records() -> list[dict[str, Any]]:
    if not JOURNAL_PATH.exists():
        return []

    records: list[dict[str, Any]] = []
    with JOURNAL_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    return records


if __name__ == "__main__":
    from app.trading_cycle import create_trade_proposal

    proposal = create_trade_proposal()
    append_trade_record(proposal)
    print(f"Appended trade record to {JOURNAL_PATH}")
