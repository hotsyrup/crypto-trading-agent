from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.market_data import get_recent_closing_prices_snapshot
from app.paper_trader import calculate_max_risk
from app.strategy import Signal, generate_signal
from app.trade_journal import record_decision


@dataclass(frozen=True)
class TradeProposal:
    signal: Signal
    reference_price: Decimal
    maximum_risk: Decimal
    paper_only: bool = True
    market_data_observed_at: datetime | None = None
    market_data_received_at: datetime | None = None


def create_trade_proposal() -> TradeProposal:
    snapshot = get_recent_closing_prices_snapshot()
    prices = list(snapshot.closing_prices)

    return TradeProposal(
        signal=generate_signal(prices),
        reference_price=prices[-1],
        maximum_risk=calculate_max_risk(),
        market_data_observed_at=snapshot.latest_observed_at,
        market_data_received_at=snapshot.received_at,
    )


if __name__ == "__main__":
    proposal = create_trade_proposal()

    record_decision(
        signal=proposal.signal,
        reference_price=proposal.reference_price,
        maximum_risk=proposal.maximum_risk,
        paper_only=proposal.paper_only,
    )

    print(f"Signal: {proposal.signal.value}")
    print(f"Reference price: ${proposal.reference_price:,.2f}")
    print(f"Maximum paper risk: ${proposal.maximum_risk:,.2f}")
    print(f"Paper trading only: {proposal.paper_only}")
    print("Decision recorded in private local journal.")
