from dataclasses import dataclass
from decimal import Decimal

from app.market_data import get_recent_closing_prices
from app.paper_trader import STARTING_BALANCE
from app.strategy import Signal, generate_signal


TRADE_AMOUNT = Decimal("50.00")


@dataclass(frozen=True)
class BacktestResult:
    starting_balance: Decimal
    ending_value: Decimal
    profit_loss: Decimal
    completed_trades: int


def run_backtest(prices: list[Decimal]) -> BacktestResult:
    if len(prices) < 5:
        raise ValueError("At least five prices are required.")

    usdc_balance = STARTING_BALANCE
    eth_balance = Decimal("0")
    completed_trades = 0

    for index in range(4, len(prices)):
        current_price = prices[index]
        signal = generate_signal(prices[index - 4:index + 1])

        if signal == Signal.BUY and eth_balance == 0:
            eth_balance = TRADE_AMOUNT / current_price
            usdc_balance -= TRADE_AMOUNT

        elif signal == Signal.SELL and eth_balance > 0:
            usdc_balance += eth_balance * current_price
            eth_balance = Decimal("0")
            completed_trades += 1

    ending_value = usdc_balance + eth_balance * prices[-1]

    return BacktestResult(
        starting_balance=STARTING_BALANCE,
        ending_value=ending_value,
        profit_loss=ending_value - STARTING_BALANCE,
        completed_trades=completed_trades,
    )


if __name__ == "__main__":
    historical_prices = get_recent_closing_prices(limit=100)
    result = run_backtest(historical_prices)

    print("Historical Paper Backtest")
    print(f"Hourly prices tested: {len(historical_prices)}")
    print(f"Completed trades: {result.completed_trades}")
    print(f"Starting balance: ${result.starting_balance:,.2f}")
    print(f"Ending value: ${result.ending_value:,.2f}")
    print(f"Profit/loss: ${result.profit_loss:,.2f}")
