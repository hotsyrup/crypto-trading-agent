from dataclasses import dataclass
from decimal import Decimal

from app.market_data import get_recent_closing_prices
from app.paper_trader import calculate_max_risk
from app.strategy import Signal, generate_signal


@dataclass(frozen=True)
class TradeProposal:
    signal: Signal
    reference_price: Decimal
    maximum_risk: Decimal
    paper_only: bool = True


def create_trade_proposal() -> TradeProposal:
    prices = get_recent_closing_prices()

    return TradeProposal(
        signal=generate_signal(prices),
        reference_price=prices[-1],
        maximum_risk=calculate_max_risk(),
    )


if __name__ == "__main__":
    proposal = create_trade_proposal()

    print(f"Signal: {proposal.signal.value}")
    print(f"Reference price: ${proposal.reference_price:,.2f}")
    print(f"Maximum paper risk: ${proposal.maximum_risk:,.2f}")
    print(f"Paper trading only: {proposal.paper_only}")
