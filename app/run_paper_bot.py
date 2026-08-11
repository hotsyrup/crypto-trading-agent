from decimal import Decimal

from app.paper_execution import simulate_order
from app.paper_portfolio import apply_order, load_portfolio, save_portfolio
from app.risk_manager import evaluate_risk
from app.trade_journal import record_decision
from app.trading_cycle import create_trade_proposal


def run_paper_bot() -> None:
    proposal = create_trade_proposal()
    portfolio = load_portfolio()
    risk_decision = evaluate_risk(proposal, portfolio)

    simulated_amount = Decimal("0")
    simulated_quantity = Decimal("0")
    updated_portfolio = portfolio

    if risk_decision.approved:
        order = simulate_order(proposal)

        try:
            updated_portfolio = apply_order(portfolio, order)
            save_portfolio(updated_portfolio)
            order_result = order.status
            simulated_amount = order.amount_usdc
            simulated_quantity = order.quantity_eth
        except ValueError as error:
            order_result = f"REJECTED: {error}"
    else:
        order_result = f"REJECTED: {risk_decision.reason}"

    record_decision(
        signal=proposal.signal,
        reference_price=proposal.reference_price,
        maximum_risk=proposal.maximum_risk,
        paper_only=proposal.paper_only,
        risk_approved=risk_decision.approved,
        risk_reason=risk_decision.reason,
        order_status=order_result,
        market_data_observed_at=proposal.market_data_observed_at,
        market_data_received_at=proposal.market_data_received_at,
        safety_gate_allowed=risk_decision.safety_gate_allowed,
        safety_gate_reason=risk_decision.safety_gate_reason,
        kill_switch_state=risk_decision.kill_switch_state,
        market_data_age_seconds=risk_decision.market_data_age_seconds,
        accounting_ready=risk_decision.accounting_ready,
        accounting_reason=risk_decision.accounting_reason,
        portfolio_value=risk_decision.portfolio_value,
        high_water_mark=risk_decision.high_water_mark,
        daily_start_value=risk_decision.daily_start_value,
        drawdown_percent=risk_decision.drawdown_percent,
        daily_loss_percent=risk_decision.daily_loss_percent,
        accounting_date=risk_decision.accounting_date,
    )

    print(f"Signal: {proposal.signal.value}")
    print(f"Reference price: ${proposal.reference_price:,.2f}")
    print(f"Paper kill switch: {risk_decision.kill_switch_state}")
    print(f"Safety gate: {risk_decision.safety_gate_reason}")
    print(f"Risk accounting: {risk_decision.accounting_reason}")
    print(f"Drawdown: {risk_decision.drawdown_percent}%")
    print(f"Daily loss: {risk_decision.daily_loss_percent}%")
    print(f"Risk decision: {risk_decision.reason}")
    print(f"Simulated amount: ${simulated_amount:,.2f}")
    print(f"Simulated ETH quantity: {simulated_quantity}")
    print(f"Order result: {order_result}")
    print(f"Simulated USDC balance: ${updated_portfolio.usdc_balance:,.2f}")
    print(f"Simulated ETH balance: {updated_portfolio.eth_balance}")
    print("No real transaction was submitted.")


if __name__ == "__main__":
    run_paper_bot()
