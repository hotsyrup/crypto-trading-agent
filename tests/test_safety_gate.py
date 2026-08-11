from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app.safety_gate import evaluate_safety_gate
from app.strategy import Signal
from app.trading_cycle import TradeProposal


class SafetyGateTests(unittest.TestCase):
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

    def proposal(self, observed_at: datetime | None) -> TradeProposal:
        return TradeProposal(
            signal=Signal.BUY,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
            market_data_observed_at=observed_at,
        )

    def test_gate_defaults_to_halted(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            decision = evaluate_safety_gate(
                self.proposal(self.now),
                now=self.now,
            )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.kill_switch_state, "halted")
        self.assertEqual(decision.reason, "Paper kill switch is halted.")

    def test_fresh_data_passes_when_explicitly_armed(self) -> None:
        with patch.dict(
            os.environ,
            {"PAPER_KILL_SWITCH": "armed"},
            clear=True,
        ):
            decision = evaluate_safety_gate(
                self.proposal(self.now - timedelta(seconds=30)),
                now=self.now,
            )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.market_data_age_seconds, 30)

    def test_stale_data_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PAPER_KILL_SWITCH": "armed",
                "PAPER_MAX_MARKET_DATA_AGE_SECONDS": "300",
            },
            clear=True,
        ):
            decision = evaluate_safety_gate(
                self.proposal(self.now - timedelta(seconds=301)),
                now=self.now,
            )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "Market data is stale.")

    def test_missing_timestamp_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"PAPER_KILL_SWITCH": "armed"},
            clear=True,
        ):
            decision = evaluate_safety_gate(
                self.proposal(None),
                now=self.now,
            )

        self.assertFalse(decision.allowed)
        self.assertIn("unavailable", decision.reason)

    def test_future_dated_data_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"PAPER_KILL_SWITCH": "armed"},
            clear=True,
        ):
            decision = evaluate_safety_gate(
                self.proposal(self.now + timedelta(seconds=61)),
                now=self.now,
            )

        self.assertFalse(decision.allowed)
        self.assertIn("future", decision.reason)

    def test_invalid_configuration_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PAPER_KILL_SWITCH": "armed",
                "PAPER_MAX_MARKET_DATA_AGE_SECONDS": "not-a-number",
            },
            clear=True,
        ):
            decision = evaluate_safety_gate(
                self.proposal(self.now),
                now=self.now,
            )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.kill_switch_state, "halted")
        self.assertIn("configuration invalid", decision.reason)


if __name__ == "__main__":
    unittest.main()
