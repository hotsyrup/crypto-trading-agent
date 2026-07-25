from app.paper_execution import simulate_order
from app.trade_journal import record_decision
from app.trading_cycle import create_trade_proposal


def run_paper_bot() -> None:
    proposal = create_trade_proposal()
    order = simulate_order(proposal)

    record_decision(
        signal=proposal.signal,
        reference_price=proposal.reference_price,
        maximum_risk=proposal.maximum_risk,
        paper_only=proposal.paper_only,
    )

    print(f"Signal: {proposal.signal.value}")
    print(f"Reference price: ${proposal.reference_price:,.2f}")
    print(f"Simulated amount: ${order.amount_usdc:,.2f}")
    print(f"Simulated ETH quantity: {order.quantity_eth}")
    print(f"Order status: {order.status}")
    print("No real transaction was submitted.")


if __name__ == "__main__":
    run_paper_bot()
