from dataclasses import dataclass
from decimal import Decimal

from app.strategy import Signal
from app.trading_cycle import TradeProposal


@dataclass(frozen=True)
class PaperOrder:
    side: Signal
    reference_price: Decimal
    amount_usdc: Decimal
    quantity_eth: Decimal
    status: str


def simulate_order(proposal: TradeProposal) -> PaperOrder:
    if proposal.signal == Signal.HOLD:
        return PaperOrder(
            side=Signal.HOLD,
            reference_price=proposal.reference_price,
            amount_usdc=Decimal("0"),
            quantity_eth=Decimal("0"),
            status="SKIPPED",
        )

    quantity = proposal.maximum_risk / proposal.reference_price

    return PaperOrder(
        side=proposal.signal,
        reference_price=proposal.reference_price,
        amount_usdc=proposal.maximum_risk,
        quantity_eth=quantity.quantize(Decimal("0.000001")),
        status="SIMULATED",
    )
