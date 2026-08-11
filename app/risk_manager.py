from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.paper_portfolio import PaperPortfolio
from app.risk_accounting import RiskAccountingDecision, evaluate_portfolio_risk
from app.safety_gate import SafetyGateDecision, evaluate_safety_gate
from app.strategy import Signal
from app.trading_cycle import TradeProposal


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    safety_gate_allowed: bool = False
    safety_gate_reason: str = "Not evaluated."
    kill_switch_state: str = "unknown"
    market_data_age_seconds: int | None = None
    accounting_ready: bool = False
    accounting_reason: str = "Not evaluated."
    portfolio_value: Decimal | None = None
    high_water_mark: Decimal | None = None
    daily_start_value: Decimal | None = None
    drawdown_percent: Decimal | None = None
    daily_loss_percent: Decimal | None = None
    accounting_date: str | None = None


def _risk_decision(
    approved: bool,
    reason: str,
    safety_gate: SafetyGateDecision,
    accounting: RiskAccountingDecision | None = None,
) -> RiskDecision:
    return RiskDecision(
        approved=approved,
        reason=reason,
        safety_gate_allowed=safety_gate.allowed,
        safety_gate_reason=safety_gate.reason,
        kill_switch_state=safety_gate.kill_switch_state,
        market_data_age_seconds=safety_gate.market_data_age_seconds,
        accounting_ready=accounting.ready if accounting is not None else False,
        accounting_reason=(
            accounting.reason if accounting is not None else "Not evaluated."
        ),
        portfolio_value=(
            accounting.current_value if accounting is not None else None
        ),
        high_water_mark=(
            accounting.high_water_mark if accounting is not None else None
        ),
        daily_start_value=(
            accounting.daily_start_value if accounting is not None else None
        ),
        drawdown_percent=(
            accounting.drawdown_percent if accounting is not None else None
        ),
        daily_loss_percent=(
            accounting.daily_loss_percent if accounting is not None else None
        ),
        accounting_date=(
            accounting.daily_date.isoformat()
            if accounting is not None and accounting.daily_date is not None
            else None
        ),
    )


def evaluate_risk(
    proposal: TradeProposal,
    portfolio: PaperPortfolio,
) -> RiskDecision:
    if not proposal.paper_only:
        return RiskDecision(False, "Only paper-trading proposals are allowed.")

    safety_gate = evaluate_safety_gate(proposal)
    if not safety_gate.allowed:
        return _risk_decision(False, safety_gate.reason, safety_gate)

    accounting = evaluate_portfolio_risk(portfolio, proposal.reference_price)
    if not accounting.ready:
        return _risk_decision(
            False,
            accounting.reason,
            safety_gate,
            accounting,
        )
    if accounting.drawdown_halt:
        return _risk_decision(False, accounting.reason, safety_gate, accounting)
    if accounting.daily_loss_halt and proposal.signal == Signal.BUY:
        return _risk_decision(False, accounting.reason, safety_gate, accounting)

    if proposal.signal == Signal.HOLD:
        return _risk_decision(
            False,
            "No trade is needed for a HOLD signal.",
            safety_gate,
            accounting,
        )

    if proposal.signal == Signal.BUY and portfolio.eth_balance > 0:
        return _risk_decision(
            False,
            "A simulated ETH position is already open.",
            safety_gate,
            accounting,
        )

    if (
        proposal.signal == Signal.BUY
        and proposal.maximum_risk > portfolio.usdc_balance
    ):
        return _risk_decision(
            False,
            "Insufficient simulated USDC balance.",
            safety_gate,
            accounting,
        )

    if proposal.signal == Signal.SELL and portfolio.eth_balance <= 0:
        return _risk_decision(
            False,
            "No simulated ETH is available to sell.",
            safety_gate,
            accounting,
        )

    return _risk_decision(
        True,
        "Paper trade approved.",
        safety_gate,
        accounting,
    )
