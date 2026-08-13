import os
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
    execution_price: Decimal | None = None
    fee_usdc: Decimal = Decimal("0")


def _cost_bps(name: str, default: str) -> Decimal:
    value = Decimal(os.getenv(name, default))
    if not value.is_finite() or value < 0 or value > 500:
        raise ValueError(f"{name} must be between 0 and 500 basis points.")
    return value


def simulate_order(proposal: TradeProposal) -> PaperOrder:
    if proposal.signal == Signal.HOLD:
        return PaperOrder(
            side=Signal.HOLD,
            reference_price=proposal.reference_price,
            amount_usdc=Decimal("0"),
            quantity_eth=Decimal("0"),
            status="SKIPPED",
            execution_price=proposal.reference_price,
        )

    slippage = _cost_bps("PAPER_SLIPPAGE_BPS", "10") / Decimal("10000")
    fee_rate = _cost_bps("PAPER_FEE_BPS", "5") / Decimal("10000")
    execution_price = proposal.reference_price * (
        Decimal("1") + slippage
        if proposal.signal == Signal.BUY
        else Decimal("1") - slippage
    )
    fee = (proposal.maximum_risk * fee_rate).quantize(Decimal("0.000001"))
    quantity = proposal.maximum_risk / execution_price

    return PaperOrder(
        side=proposal.signal,
        reference_price=proposal.reference_price,
        amount_usdc=proposal.maximum_risk,
        quantity_eth=quantity.quantize(Decimal("0.000001")),
        status="SIMULATED",
        execution_price=execution_price,
        fee_usdc=fee,
    )
