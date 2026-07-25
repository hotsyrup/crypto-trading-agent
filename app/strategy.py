from decimal import Decimal
from enum import Enum


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


def generate_signal(prices: list[Decimal]) -> Signal:
    if len(prices) < 5:
        return Signal.HOLD

    short_average = sum(prices[-3:]) / Decimal("3")
    long_average = sum(prices[-5:]) / Decimal("5")

    if short_average > long_average:
        return Signal.BUY

    if short_average < long_average:
        return Signal.SELL

    return Signal.HOLD


if __name__ == "__main__":
    sample_prices = [
        Decimal("1800"),
        Decimal("1810"),
        Decimal("1820"),
        Decimal("1840"),
        Decimal("1870"),
    ]

    print(f"Sample paper-trading signal: {generate_signal(sample_prices).value}")
