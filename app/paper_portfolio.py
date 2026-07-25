import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from app.paper_execution import PaperOrder
from app.paper_trader import STARTING_BALANCE
from app.strategy import Signal


PORTFOLIO_PATH = Path("data/paper_portfolio.json")


@dataclass(frozen=True)
class PaperPortfolio:
    usdc_balance: Decimal
    eth_balance: Decimal


def load_portfolio() -> PaperPortfolio:
    if not PORTFOLIO_PATH.exists():
        return PaperPortfolio(
            usdc_balance=STARTING_BALANCE,
            eth_balance=Decimal("0"),
        )

    with PORTFOLIO_PATH.open("r", encoding="utf-8") as portfolio_file:
        data = json.load(portfolio_file)

    return PaperPortfolio(
        usdc_balance=Decimal(data["usdc_balance"]),
        eth_balance=Decimal(data["eth_balance"]),
    )


def apply_order(
    portfolio: PaperPortfolio,
    order: PaperOrder,
) -> PaperPortfolio:
    if order.status != "SIMULATED" or order.side == Signal.HOLD:
        return portfolio

    if order.side == Signal.BUY:
        if order.amount_usdc > portfolio.usdc_balance:
            raise ValueError("Insufficient simulated USDC balance.")

        return PaperPortfolio(
            usdc_balance=portfolio.usdc_balance - order.amount_usdc,
            eth_balance=portfolio.eth_balance + order.quantity_eth,
        )

    if order.quantity_eth > portfolio.eth_balance:
        raise ValueError("Insufficient simulated ETH balance.")

    proceeds = order.quantity_eth * order.reference_price

    return PaperPortfolio(
        usdc_balance=portfolio.usdc_balance + proceeds,
        eth_balance=portfolio.eth_balance - order.quantity_eth,
    )


def save_portfolio(portfolio: PaperPortfolio) -> None:
    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)

    data = {key: str(value) for key, value in asdict(portfolio).items()}

    with PORTFOLIO_PATH.open("w", encoding="utf-8") as portfolio_file:
        json.dump(data, portfolio_file, indent=2)
