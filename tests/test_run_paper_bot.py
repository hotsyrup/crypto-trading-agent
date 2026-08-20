import unittest
from decimal import Decimal
from unittest.mock import patch

from app.paper_execution import PaperOrder
from app.paper_portfolio import PaperPortfolio
from app.risk_manager import RiskDecision
from app.run_paper_bot import run_paper_bot
from app.strategy import Signal
from app.trading_cycle import TradeProposal


class RunPaperBotTests(unittest.TestCase):
    @patch("app.run_paper_bot.record_decision")
    @patch("app.run_paper_bot.save_portfolio")
    @patch("app.run_paper_bot.apply_order")
    @patch("app.run_paper_bot.evaluate_risk")
    @patch("app.run_paper_bot.load_portfolio")
    @patch("app.run_paper_bot.simulate_order")
    @patch("app.run_paper_bot.create_trade_proposal")
    def test_approved_trade_updates_simulated_portfolio(
        self,
        mock_create_trade_proposal,
        mock_simulate_order,
        mock_load_portfolio,
        mock_evaluate_risk,
        mock_apply_order,
        mock_save_portfolio,
        mock_record_decision,
    ) -> None:
        proposal = TradeProposal(
            signal=Signal.BUY,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
        )
        order = PaperOrder(
            side=Signal.BUY,
            reference_price=Decimal("2000"),
            amount_usdc=Decimal("50"),
            quantity_eth=Decimal("0.025"),
            status="SIMULATED",
        )
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("10000"),
            eth_balance=Decimal("0"),
        )
        updated_portfolio = PaperPortfolio(
            usdc_balance=Decimal("9950"),
            eth_balance=Decimal("0.025"),
        )

        mock_create_trade_proposal.return_value = proposal
        mock_simulate_order.return_value = order
        mock_load_portfolio.return_value = portfolio
        mock_evaluate_risk.return_value = RiskDecision(
            approved=True,
            reason="Paper trade approved.",
        )
        mock_apply_order.return_value = updated_portfolio

        with patch("builtins.print"):
            run_paper_bot()

        mock_evaluate_risk.assert_called_once_with(proposal, portfolio)
        mock_apply_order.assert_called_once_with(portfolio, order)
        mock_save_portfolio.assert_called_once_with(updated_portfolio)
        mock_record_decision.assert_called_once()

    @patch("app.run_paper_bot.record_decision")
    @patch("app.run_paper_bot.save_portfolio")
    @patch("app.run_paper_bot.apply_order")
    @patch("app.run_paper_bot.evaluate_risk")
    @patch("app.run_paper_bot.load_portfolio")
    @patch("app.run_paper_bot.simulate_order")
    @patch("app.run_paper_bot.create_trade_proposal")
    def test_blocked_safety_gate_never_simulates_or_saves(
        self,
        mock_create_trade_proposal,
        mock_simulate_order,
        mock_load_portfolio,
        mock_evaluate_risk,
        mock_apply_order,
        mock_save_portfolio,
        mock_record_decision,
    ) -> None:
        proposal = TradeProposal(
            signal=Signal.BUY,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
        )
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("10000"),
            eth_balance=Decimal("0"),
        )
        mock_create_trade_proposal.return_value = proposal
        mock_load_portfolio.return_value = portfolio
        mock_evaluate_risk.return_value = RiskDecision(
            approved=False,
            reason="Market data is stale.",
            safety_gate_allowed=False,
            safety_gate_reason="Market data is stale.",
            kill_switch_state="armed",
            market_data_age_seconds=7201,
        )

        with patch("builtins.print"):
            run_paper_bot()

        mock_simulate_order.assert_not_called()
        mock_apply_order.assert_not_called()
        mock_save_portfolio.assert_not_called()
        mock_record_decision.assert_called_once()


if __name__ == "__main__":
    unittest.main()
