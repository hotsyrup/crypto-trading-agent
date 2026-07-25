from decimal import Decimal


STARTING_BALANCE = Decimal("10000.00")
MAX_RISK_PERCENT = Decimal("0.5")


def calculate_max_risk() -> Decimal:
    return STARTING_BALANCE * (MAX_RISK_PERCENT / Decimal("100"))


if __name__ == "__main__":
    print(f"Simulated balance: ${STARTING_BALANCE:,.2f}")
    print(f"Maximum risk per trade: ${calculate_max_risk():,.2f}")
