from dataclasses import dataclass
from decimal import Decimal

from app.paper_portfolio import PaperPortfolio
from app.paper_trader import STARTING_BALANCE
from app.strategy import Signal
from app.trading_cycle import TradeProposal


MAX_DRAWDOWN_PERCENT = Decimal("2.0")


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


def evaluate_risk(
    proposal: TradeProposal,
    portfolio: PaperPortfolio,
) -> RiskDecision:
    portfolio_value = (
        portfolio.usdc_balance
        + portfolio.eth_balance * proposal.reference_price
    )
    minimum_allowed_value = STARTING_BALANCE * (
        Decimal("1") - MAX_DRAWDOWN_PERCENT / Decimal("100")
    )

    if not proposal.paper_only:
        return RiskDecision(False, "Only paper-trading proposals are allowed.")

    if portfolio_value < minimum_allowed_value:
        return RiskDecision(False, "Maximum simulated drawdown reached.")

    if proposal.signal == Signal.HOLD:
        return RiskDecision(False, "No trade is needed for a HOLD signal.")

    if proposal.signal == Signal.BUY and portfolio.eth_balance > 0:
        return RiskDecision(
            False,
            "A simulated ETH position is already open.",
        )

    if (
        proposal.signal == Signal.BUY
        and proposal.maximum_risk > portfolio.usdc_balance
    ):
        return RiskDecision(False, "Insufficient simulated USDC balance.")

    if proposal.signal == Signal.SELL and portfolio.eth_balance <= 0:
        return RiskDecision(False, "No simulated ETH is available to sell.")

    return RiskDecision(True, "Paper trade approved.")
