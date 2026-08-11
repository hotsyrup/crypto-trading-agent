import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.live_trading_config import BASE_USDC_ADDRESS, load_live_trading_config
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    BASE_MAINNET_CHAIN_ID,
    EXECUTOR_MODE_SHADOW_ONLY,
    KILL_SWITCH_ARMED,
    KILL_SWITCH_HALTED,
    STATUS_REJECTED,
    STATUS_SHADOW_APPROVED,
    ExecutorConfig,
    RiskSnapshot,
    TradeIntent,
    evaluate_trade_intent,
    load_executor_config,
    process_shadow_trade_intent,
)


NOW = datetime(2026, 8, 8, 20, 0, tzinfo=timezone.utc)


def executor_config(state: str = KILL_SWITCH_ARMED) -> ExecutorConfig:
    return ExecutorConfig(
        mode=EXECUTOR_MODE_SHADOW_ONLY,
        kill_switch_state=state,
        max_data_age_seconds=120,
        max_future_skew_seconds=30,
    )


def trade_intent(**updates: object) -> TradeIntent:
    base = TradeIntent(
        intent_id="signal-2026-08-08-001",
        strategy_id="eth-usdc-trend",
        strategy_version="1.0.0",
        side="BUY",
        asset_symbol="ETH",
        asset_token_address=None,
        settlement_symbol="USDC",
        settlement_token_address=BASE_USDC_ADDRESS,
        notional_usdc=Decimal("5"),
        current_position_usdc=Decimal("0"),
        treasury_value_usdc=Decimal("105"),
        new_strategy=True,
        treasury_address=AUTHORIZED_TREASURY_ADDRESS,
        recipient_address=AUTHORIZED_TREASURY_ADDRESS,
        chain_id=BASE_MAINNET_CHAIN_ID,
        market_data_observed_at=NOW - timedelta(seconds=10),
        created_at=NOW - timedelta(seconds=5),
        source_refs=("market-snapshot:sample-001",),
    )
    return replace(base, **updates)


def risk_snapshot(**updates: object) -> RiskSnapshot:
    base = RiskSnapshot(
        daily_loss_percent=Decimal("0"),
        drawdown_percent=Decimal("0"),
        observed_at=NOW - timedelta(seconds=5),
    )
    return replace(base, **updates)


class TradingExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.live_config = load_live_trading_config()

    def evaluate(
        self,
        intent: TradeIntent | None = None,
        risk: RiskSnapshot | None = None,
        config: ExecutorConfig | None = None,
    ):
        return evaluate_trade_intent(
            intent or trade_intent(),
            risk or risk_snapshot(),
            now=NOW,
            live_config=self.live_config,
            executor_config=config or executor_config(),
        )

    def test_default_executor_config_is_halted_and_shadow_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_executor_config()
        self.assertEqual(config.mode, EXECUTOR_MODE_SHADOW_ONLY)
        self.assertEqual(config.kill_switch_state, KILL_SWITCH_HALTED)

    def test_valid_intent_is_shadow_approved_but_never_executable(self) -> None:
        decision = self.evaluate()
        self.assertEqual(decision.status, STATUS_SHADOW_APPROVED)
        self.assertTrue(decision.shadow_approved)
        self.assertFalse(decision.executable)
        self.assertEqual(decision.signing_authority, "none")

    def test_halted_switch_rejects(self) -> None:
        decision = self.evaluate(config=executor_config(KILL_SWITCH_HALTED))
        self.assertEqual(decision.status, STATUS_REJECTED)
        self.assertIn("kill switch is halted", " ".join(decision.reasons))

    def test_live_enabled_configuration_is_rejected(self) -> None:
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true"}, clear=True):
            live_config = load_live_trading_config()
        decision = evaluate_trade_intent(
            trade_intent(),
            risk_snapshot(),
            now=NOW,
            live_config=live_config,
            executor_config=executor_config(),
        )
        self.assertEqual(decision.status, STATUS_REJECTED)
        self.assertIn("refuses LIVE_TRADING_ENABLED=true", " ".join(decision.reasons))

    def test_wrong_account_chain_or_recipient_is_rejected(self) -> None:
        wrong_address = "0x" + "1" * 40
        decision = self.evaluate(
            trade_intent(
                treasury_address=wrong_address,
                recipient_address=wrong_address,
                chain_id=1,
            )
        )
        text = " ".join(decision.reasons)
        self.assertIn("outside the adopted mandate", text)
        self.assertIn("Base mainnet", text)

    def test_proceeds_cannot_be_redirected(self) -> None:
        decision = self.evaluate(
            trade_intent(recipient_address="0x" + "2" * 40)
        )
        self.assertIn("return to the authorized treasury", " ".join(decision.reasons))

    def test_spoofed_asset_or_unsolicited_asset_is_rejected(self) -> None:
        spoofed = self.evaluate(
            trade_intent(asset_token_address="0x" + "3" * 40)
        )
        unsolicited = self.evaluate(trade_intent(unsolicited_asset=True))
        self.assertEqual(spoofed.status, STATUS_REJECTED)
        self.assertEqual(unsolicited.status, STATUS_REJECTED)

    def test_asset_and_product_scope_is_eth_usdc_spot_only(self) -> None:
        decision = self.evaluate(
            trade_intent(
                asset_symbol="USDC",
                asset_token_address=BASE_USDC_ADDRESS,
                product="perpetual",
            )
        )
        text = " ".join(decision.reasons)
        self.assertIn("unleveraged spot", text)
        self.assertIn("ETH as the traded asset", text)

    def test_position_limit_is_enforced_on_resulting_position(self) -> None:
        decision = self.evaluate(
            trade_intent(
                notional_usdc=Decimal("10"),
                current_position_usdc=Decimal("12"),
                new_strategy=False,
            )
        )
        self.assertIn("20% limit", " ".join(decision.reasons))

    def test_new_strategy_limit_is_enforced(self) -> None:
        decision = self.evaluate(trade_intent(notional_usdc=Decimal("5.26")))
        self.assertIn("5% limit", " ".join(decision.reasons))

    def test_daily_loss_blocks_buy_but_allows_bounded_sell(self) -> None:
        risk = risk_snapshot(daily_loss_percent=Decimal("5"))
        buy = self.evaluate(risk=risk)
        sell = self.evaluate(
            intent=trade_intent(
                side="SELL",
                notional_usdc=Decimal("5"),
                current_position_usdc=Decimal("10"),
                new_strategy=False,
            ),
            risk=risk,
        )
        self.assertEqual(buy.status, STATUS_REJECTED)
        self.assertEqual(sell.status, STATUS_SHADOW_APPROVED)

    def test_drawdown_halts_buy_and_sell(self) -> None:
        risk = risk_snapshot(drawdown_percent=Decimal("20"))
        buy = self.evaluate(risk=risk)
        sell = self.evaluate(
            intent=trade_intent(
                side="SELL",
                current_position_usdc=Decimal("10"),
                new_strategy=False,
            ),
            risk=risk,
        )
        self.assertEqual(buy.status, STATUS_REJECTED)
        self.assertEqual(sell.status, STATUS_REJECTED)

    def test_missing_contradictory_or_stale_risk_fails_closed(self) -> None:
        incomplete = self.evaluate(risk=risk_snapshot(complete=False))
        contradictory = self.evaluate(risk=risk_snapshot(contradictory=True))
        stale = self.evaluate(
            risk=risk_snapshot(observed_at=NOW - timedelta(seconds=121))
        )
        self.assertEqual(incomplete.status, STATUS_REJECTED)
        self.assertEqual(contradictory.status, STATUS_REJECTED)
        self.assertEqual(stale.status, STATUS_REJECTED)

    def test_stale_or_future_market_data_fails_closed(self) -> None:
        stale = self.evaluate(
            trade_intent(market_data_observed_at=NOW - timedelta(seconds=121))
        )
        future = self.evaluate(
            trade_intent(market_data_observed_at=NOW + timedelta(seconds=31))
        )
        self.assertEqual(stale.status, STATUS_REJECTED)
        self.assertEqual(future.status, STATUS_REJECTED)

    def test_missing_provenance_or_strategy_identity_is_rejected(self) -> None:
        decision = self.evaluate(
            trade_intent(source_refs=(), strategy_version="")
        )
        text = " ".join(decision.reasons)
        self.assertIn("source reference", text)
        self.assertIn("Strategy ID and version", text)

    def test_sell_cannot_exceed_current_position(self) -> None:
        decision = self.evaluate(
            trade_intent(
                side="SELL",
                notional_usdc=Decimal("11"),
                current_position_usdc=Decimal("10"),
                new_strategy=False,
            )
        )
        self.assertIn("exceeds the current position", " ".join(decision.reasons))

    def test_nonfinite_values_fail_closed_without_crashing(self) -> None:
        decision = self.evaluate(
            trade_intent(
                notional_usdc=Decimal("NaN"),
                treasury_value_usdc=Decimal("Infinity"),
            )
        )
        self.assertEqual(decision.status, STATUS_REJECTED)
        self.assertIn("finite and nonnegative", " ".join(decision.reasons))

    def test_composed_executor_records_once_and_blocks_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "execution.jsonl"
            first = process_shadow_trade_intent(
                trade_intent(),
                risk_snapshot(),
                journal_path=path,
                now=NOW,
                live_config=self.live_config,
                executor_config=executor_config(),
            )
            replay = process_shadow_trade_intent(
                trade_intent(),
                risk_snapshot(),
                journal_path=path,
                now=NOW,
                live_config=self.live_config,
                executor_config=executor_config(),
            )
        self.assertTrue(first.journal_recorded)
        self.assertFalse(first.ready_for_submission)
        self.assertTrue(replay.duplicate_blocked)
        self.assertEqual(replay.decision.status, "DUPLICATE_BLOCKED")
        self.assertFalse(replay.ready_for_submission)

    def test_composed_executor_fails_closed_when_journal_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "execution.jsonl"
            path.write_text("not-json\n", encoding="utf-8")
            result = process_shadow_trade_intent(
                trade_intent(),
                risk_snapshot(),
                journal_path=path,
                now=NOW,
                live_config=self.live_config,
                executor_config=executor_config(),
            )
        self.assertEqual(result.decision.status, "JOURNAL_FAILURE")
        self.assertFalse(result.ready_for_submission)


if __name__ == "__main__":
    unittest.main()
