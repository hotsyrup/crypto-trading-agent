import unittest
from decimal import Decimal
from unittest.mock import patch

from app.run_paper_bot import run_paper_bot
from app.strategy import Signal


class RunPaperBotTests(unittest.TestCase):
    @patch("app.run_paper_bot.record_decision")
    @patch("app.run_paper_bot.simulate_order")
    @patch("app.run_paper_bot.create_trade_proposal")
    def test_run_paper_bot_records_and_prints_summary(
        self,
        mock_create_trade_proposal,
        mock_simulate_order,
        mock_record_decision,
    ) -> None:
        mock_create_trade_proposal.return_value = type(
            "Proposal",
            (),
            {
                "signal": Signal.BUY,
                "reference_price": Decimal("2000"),
                "maximum_risk": Decimal("50"),
                "paper_only": True,
            },
        )()
        mock_simulate_order.return_value = type(
            "Order",
            (),
            {
                "amount_usdc": Decimal("50"),
                "quantity_eth": Decimal("0.025000"),
                "status": "SIMULATED",
            },
        )()

        with patch("builtins.print") as mock_print:
            run_paper_bot()

        mock_record_decision.assert_called_once()
        self.assertTrue(mock_print.called)


if __name__ == "__main__":
    unittest.main()
