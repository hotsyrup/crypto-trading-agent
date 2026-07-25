import unittest
from decimal import Decimal

from app.paper_portfolio import PaperPortfolio
from app.risk_manager import evaluate_risk
from app.strategy import Signal
from app.trading_cycle import TradeProposal


class RiskManagerTests(unittest.TestCase):
    def test_safe_paper_buy_is_approved(self) -> None:
        proposal = TradeProposal(
            signal=Signal.BUY,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
        )
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("10000"),
            eth_balance=Decimal("0"),
        )

        decision = evaluate_risk(proposal, portfolio)

        self.assertTrue(decision.approved)

    def test_trade_is_rejected_after_maximum_drawdown(self) -> None:
        proposal = TradeProposal(
            signal=Signal.BUY,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
        )
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("9700"),
            eth_balance=Decimal("0"),
        )

        decision = evaluate_risk(proposal, portfolio)

        self.assertFalse(decision.approved)
        self.assertEqual(
            decision.reason,
            "Maximum simulated drawdown reached.",
        )

    def test_buy_is_rejected_when_position_is_already_open(self) -> None:
        proposal = TradeProposal(
            signal=Signal.BUY,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
        )
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("9950"),
            eth_balance=Decimal("0.025"),
        )

        decision = evaluate_risk(proposal, portfolio)

        self.assertFalse(decision.approved)
        self.assertEqual(
            decision.reason,
            "A simulated ETH position is already open.",
        )

    def test_sell_is_rejected_without_simulated_eth(self) -> None:
        proposal = TradeProposal(
            signal=Signal.SELL,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
        )
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("10000"),
            eth_balance=Decimal("0"),
        )

        decision = evaluate_risk(proposal, portfolio)

        self.assertFalse(decision.approved)
        self.assertEqual(
            decision.reason,
            "No simulated ETH is available to sell.",
        )


if __name__ == "__main__":
    unittest.main()
