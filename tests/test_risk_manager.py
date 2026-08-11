import unittest
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from app.paper_portfolio import PaperPortfolio
from app.risk_accounting import RiskAccountingDecision
from app.risk_manager import evaluate_risk
from app.strategy import Signal
from app.trading_cycle import TradeProposal


class RiskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {"PAPER_KILL_SWITCH": "armed"},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.accounting_patch = patch(
            "app.risk_manager.evaluate_portfolio_risk"
        )
        self.mock_accounting = self.accounting_patch.start()
        self.addCleanup(self.accounting_patch.stop)
        self.mock_accounting.return_value = RiskAccountingDecision(
            ready=True,
            reason="Portfolio risk accounting passed.",
            current_value=Decimal("10000"),
            high_water_mark=Decimal("10000"),
            daily_start_value=Decimal("10000"),
            drawdown_percent=Decimal("0"),
            daily_loss_percent=Decimal("0"),
            daily_date=date(2026, 8, 8),
        )
        self.observed_at = datetime.now(timezone.utc)

    def proposal(self, signal: Signal) -> TradeProposal:
        return TradeProposal(
            signal=signal,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
            market_data_observed_at=self.observed_at,
        )

    def test_safe_paper_buy_is_approved(self) -> None:
        proposal = self.proposal(Signal.BUY)
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("10000"),
            eth_balance=Decimal("0"),
        )

        decision = evaluate_risk(proposal, portfolio)

        self.assertTrue(decision.approved)
        self.assertTrue(decision.safety_gate_allowed)

    def test_trade_is_rejected_after_high_water_drawdown(self) -> None:
        proposal = self.proposal(Signal.BUY)
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("9700"),
            eth_balance=Decimal("0"),
        )

        self.mock_accounting.return_value = RiskAccountingDecision(
            ready=True,
            reason="High-water-mark drawdown limit reached.",
            current_value=Decimal("7900"),
            high_water_mark=Decimal("10000"),
            daily_start_value=Decimal("8000"),
            drawdown_percent=Decimal("21"),
            daily_loss_percent=Decimal("1.25"),
            daily_date=date(2026, 8, 8),
            drawdown_halt=True,
        )

        decision = evaluate_risk(proposal, portfolio)

        self.assertFalse(decision.approved)
        self.assertEqual(
            decision.reason,
            "High-water-mark drawdown limit reached.",
        )

    def test_daily_loss_blocks_a_new_buy(self) -> None:
        proposal = self.proposal(Signal.BUY)
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("9400"),
            eth_balance=Decimal("0"),
        )
        self.mock_accounting.return_value = RiskAccountingDecision(
            ready=True,
            reason="Daily loss limit reached; new positions are blocked.",
            current_value=Decimal("9400"),
            high_water_mark=Decimal("10000"),
            daily_start_value=Decimal("10000"),
            drawdown_percent=Decimal("6"),
            daily_loss_percent=Decimal("6"),
            daily_date=date(2026, 8, 8),
            daily_loss_halt=True,
        )

        decision = evaluate_risk(proposal, portfolio)

        self.assertFalse(decision.approved)
        self.assertIn("Daily loss", decision.reason)

    def test_daily_loss_still_allows_a_risk_reducing_sell(self) -> None:
        proposal = self.proposal(Signal.SELL)
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("9000"),
            eth_balance=Decimal("0.2"),
        )
        self.mock_accounting.return_value = RiskAccountingDecision(
            ready=True,
            reason="Daily loss limit reached; new positions are blocked.",
            current_value=Decimal("9400"),
            high_water_mark=Decimal("10000"),
            daily_start_value=Decimal("10000"),
            drawdown_percent=Decimal("6"),
            daily_loss_percent=Decimal("6"),
            daily_date=date(2026, 8, 8),
            daily_loss_halt=True,
        )

        decision = evaluate_risk(proposal, portfolio)

        self.assertTrue(decision.approved)

    def test_unavailable_accounting_fails_closed(self) -> None:
        proposal = self.proposal(Signal.BUY)
        portfolio = PaperPortfolio(
            usdc_balance=Decimal("10000"),
            eth_balance=Decimal("0"),
        )
        self.mock_accounting.return_value = RiskAccountingDecision(
            ready=False,
            reason="Risk accounting unavailable: corrupted state",
        )

        decision = evaluate_risk(proposal, portfolio)

        self.assertFalse(decision.approved)
        self.assertIn("unavailable", decision.reason)

    def test_buy_is_rejected_when_position_is_already_open(self) -> None:
        proposal = self.proposal(Signal.BUY)
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
        proposal = self.proposal(Signal.SELL)
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
