import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.trending_tokens import TokenMetadata, TrendingPool
from app.trending_trial import (
    TrialPosition,
    close_position,
    new_state,
    open_candidate,
    total_value,
)


class TrendingTrialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 25, tzinfo=timezone.utc)
        self.state = new_state(self.now)
        token = TokenMetadata(
            "0x0000000000000000000000000000000000000001",
            "Example Token",
            "EXAMPLE",
            18,
            Decimal("2"),
        )
        usdc = TokenMetadata(
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "USD Coin",
            "USDC",
            6,
            Decimal("1"),
        )
        self.pool = TrendingPool(
            "EXAMPLE / USDC",
            "0x0000000000000000000000000000000000000002",
            "test-dex",
            token,
            usdc,
            Decimal("500000"),
            Decimal("300000"),
            Decimal("2"),
            Decimal("10"),
            self.now,
        )

    def test_open_candidate_uses_four_dollar_limit_and_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.jsonl"
            with patch("app.trending_trial.JOURNAL_PATH", journal):
                open_candidate(self.state, self.pool, self.now)
        position = next(iter(self.state.positions.values()))
        self.assertEqual(self.state.cash_usdc, Decimal("36.00"))
        self.assertEqual(position.quantity, Decimal("1.98"))
        self.assertEqual(total_value(self.state), Decimal("39.96"))

    def test_close_position_records_cost_and_profit(self) -> None:
        self.state.positions["0x1"] = TrialPosition(
            "0x1", "EXAMPLE", "Example", Decimal("2"), Decimal("2"),
            Decimal("2"), self.now.isoformat()
        )
        self.state.cash_usdc = Decimal("36")
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.jsonl"
            with patch("app.trending_trial.JOURNAL_PATH", journal):
                close_position(self.state, "0x1", Decimal("3"), "Test")
        self.assertEqual(self.state.cash_usdc, Decimal("41.94"))
        self.assertEqual(self.state.realized_pnl, Decimal("1.94"))


if __name__ == "__main__":
    unittest.main()
