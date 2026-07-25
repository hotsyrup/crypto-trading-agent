import json
from collections import Counter
from decimal import Decimal

from app.market_data import get_eth_usd_price
from app.paper_portfolio import load_portfolio
from app.paper_trader import STARTING_BALANCE
from app.trade_journal import JOURNAL_PATH


def load_records() -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []

    with JOURNAL_PATH.open("r", encoding="utf-8") as journal:
        return [json.loads(line) for line in journal if line.strip()]


def generate_report() -> dict:
    records = load_records()
    portfolio = load_portfolio()
    current_eth_price = get_eth_usd_price()
    signal_counts = Counter(record.get("signal", "NONE") for record in records)

    approved_orders = sum(record.get("risk_approved") is True for record in records)
    rejected_orders = sum(record.get("risk_approved") is False for record in records)

    total_value = portfolio.usdc_balance + portfolio.eth_balance * current_eth_price
    simulated_profit_loss = total_value - STARTING_BALANCE

    return {
        "total_decisions": len(records),
        "buy_signals": signal_counts["BUY"],
        "sell_signals": signal_counts["SELL"],
        "hold_signals": signal_counts["HOLD"],
        "approved_orders": approved_orders,
        "rejected_orders": rejected_orders,
        "latest_signal": records[-1].get("signal", "NONE") if records else "NONE",
        "usdc_balance": portfolio.usdc_balance,
        "eth_balance": portfolio.eth_balance,
        "eth_price": current_eth_price,
        "total_value": total_value,
        "profit_loss": simulated_profit_loss,
    }


if __name__ == "__main__":
    report = generate_report()

    print("Paper-Trading Performance Report")
    print(f"Total decisions: {report['total_decisions']}")
    print(f"BUY signals: {report['buy_signals']}")
    print(f"SELL signals: {report['sell_signals']}")
    print(f"HOLD signals: {report['hold_signals']}")
    print(f"Approved orders: {report['approved_orders']}")
    print(f"Rejected orders: {report['rejected_orders']}")
    print(f"Latest signal: {report['latest_signal']}")
    print(f"Simulated USDC: ${report['usdc_balance']:,.2f}")
    print(f"Simulated ETH: {report['eth_balance']}")
    print(f"Current ETH price: ${report['eth_price']:,.2f}")
    print(f"Total simulated value: ${report['total_value']:,.2f}")
    print(f"Simulated profit/loss: ${report['profit_loss']:,.2f}")
