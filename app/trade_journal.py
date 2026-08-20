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
    risk_approved: bool | None = None,
    risk_reason: str = "Not evaluated.",
    order_status: str = "NOT_EVALUATED",
    market_data_observed_at: datetime | None = None,
    market_data_received_at: datetime | None = None,
    safety_gate_allowed: bool | None = None,
    safety_gate_reason: str = "Not evaluated.",
    kill_switch_state: str = "unknown",
    market_data_age_seconds: int | None = None,
    accounting_ready: bool | None = None,
    accounting_reason: str = "Not evaluated.",
    portfolio_value: Decimal | None = None,
    high_water_mark: Decimal | None = None,
    daily_start_value: Decimal | None = None,
    drawdown_percent: Decimal | None = None,
    daily_loss_percent: Decimal | None = None,
    accounting_date: str | None = None,
) -> None:
    _ensure_data_dir()

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal": signal.value,
        "reference_price": str(reference_price),
        "maximum_risk": str(maximum_risk),
        "paper_only": paper_only,
        "risk_approved": risk_approved,
        "risk_reason": risk_reason,
        "order_status": order_status,
        "market_data_observed_at": (
            market_data_observed_at.isoformat()
            if market_data_observed_at is not None
            else None
        ),
        "market_data_received_at": (
            market_data_received_at.isoformat()
            if market_data_received_at is not None
            else None
        ),
        "safety_gate_allowed": safety_gate_allowed,
        "safety_gate_reason": safety_gate_reason,
        "kill_switch_state": kill_switch_state,
        "market_data_age_seconds": market_data_age_seconds,
        "accounting_ready": accounting_ready,
        "accounting_reason": accounting_reason,
        "portfolio_value": (
            str(portfolio_value) if portfolio_value is not None else None
        ),
        "high_water_mark": (
            str(high_water_mark) if high_water_mark is not None else None
        ),
        "daily_start_value": (
            str(daily_start_value) if daily_start_value is not None else None
        ),
        "drawdown_percent": (
            str(drawdown_percent) if drawdown_percent is not None else None
        ),
        "daily_loss_percent": (
            str(daily_loss_percent) if daily_loss_percent is not None else None
        ),
        "accounting_date": accounting_date,
    }

    with JOURNAL_PATH.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(entry) + "\n")


def append_trade_record(proposal: "TradeProposal") -> None:
    record_decision(
        signal=proposal.signal,
        reference_price=proposal.reference_price,
        maximum_risk=proposal.maximum_risk,
        paper_only=proposal.paper_only,
        market_data_observed_at=proposal.market_data_observed_at,
        market_data_received_at=proposal.market_data_received_at,
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
