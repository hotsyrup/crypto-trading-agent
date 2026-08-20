import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app.base_mcp_canary import (
    CANARY_KILL_SWITCH_ARMED,
    CANARY_KILL_SWITCH_HALTED,
    CANARY_MODE_PREPARE_ONLY,
    STATUS_BLOCKED,
    STATUS_READY,
    BaseMcpCanaryConfig,
    load_base_mcp_canary_config,
    prepare_base_mcp_canary,
)
from app.live_trading_config import BASE_USDC_ADDRESS, load_live_trading_config
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    BASE_MAINNET_CHAIN_ID,
    EXECUTOR_MODE_SHADOW_ONLY,
    KILL_SWITCH_ARMED,
    ExecutorConfig,
    RiskSnapshot,
    TradeIntent,
    evaluate_trade_intent,
)


NOW = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)
JOURNAL_HASH = "a" * 64


def canary_config(
    *,
    kill_switch: str = CANARY_KILL_SWITCH_ARMED,
    maximum_notional: Decimal = Decimal("1.00"),
) -> BaseMcpCanaryConfig:
    return BaseMcpCanaryConfig(
        mode=CANARY_MODE_PREPARE_ONLY,
        kill_switch_state=kill_switch,
        maximum_notional_usdc=maximum_notional,
        approval_ttl_seconds=300,
        maximum_intent_age_seconds=120,
    )


def intent(**updates: object) -> TradeIntent:
    value = TradeIntent(
        intent_id="base-canary-signal-001",
        strategy_id="eth-usdc-trend",
        strategy_version="2.0.0",
        side="BUY",
        asset_symbol="ETH",
        asset_token_address=None,
        settlement_symbol="USDC",
        settlement_token_address=BASE_USDC_ADDRESS,
        notional_usdc=Decimal("1.00"),
        current_position_usdc=Decimal("0"),
        treasury_value_usdc=Decimal("105"),
        new_strategy=False,
        treasury_address=AUTHORIZED_TREASURY_ADDRESS,
        recipient_address=AUTHORIZED_TREASURY_ADDRESS,
        chain_id=BASE_MAINNET_CHAIN_ID,
        market_data_observed_at=NOW - timedelta(seconds=10),
        created_at=NOW - timedelta(seconds=5),
        source_refs=("research-packet:sample",),
    )
    return replace(value, **updates)


def decision_for(value: TradeIntent):
    with patch.dict(os.environ, {}, clear=True):
        live = load_live_trading_config()
    return evaluate_trade_intent(
        value,
        RiskSnapshot(
            daily_loss_percent=Decimal("0"),
            drawdown_percent=Decimal("0"),
            observed_at=NOW - timedelta(seconds=5),
        ),
        now=NOW,
        live_config=live,
        executor_config=ExecutorConfig(
            mode=EXECUTOR_MODE_SHADOW_ONLY,
            kill_switch_state=KILL_SWITCH_ARMED,
            max_data_age_seconds=120,
            max_future_skew_seconds=30,
        ),
    )


class BaseMcpCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.live_config = load_live_trading_config()

    def prepare(self, value: TradeIntent | None = None, **updates: object):
        selected = value or intent()
        arguments = {
            "journal_sequence": 1,
            "journal_entry_hash": JOURNAL_HASH,
            "live_config": self.live_config,
            "canary_config": canary_config(),
            "now": NOW,
            **updates,
        }
        return prepare_base_mcp_canary(
            selected,
            decision_for(selected),
            **arguments,
        )

    def test_default_configuration_is_halted_and_prepare_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_base_mcp_canary_config()
        self.assertEqual(config.mode, CANARY_MODE_PREPARE_ONLY)
        self.assertEqual(config.kill_switch_state, CANARY_KILL_SWITCH_HALTED)
        self.assertEqual(config.maximum_notional_usdc, Decimal("1.00"))

    def test_valid_canary_is_exact_and_never_executable(self) -> None:
        result = self.prepare()
        self.assertEqual(result.status, STATUS_READY)
        self.assertTrue(result.ready_to_request_human_approval)
        self.assertFalse(result.approval_requested)
        self.assertFalse(result.executable)
        self.assertEqual(result.signing_authority, "base_account_human_only")
        self.assertEqual(
            result.request.tool_arguments(),
            {
                "amount": "1.00",
                "chain": "base",
                "fromAsset": BASE_USDC_ADDRESS,
                "fromDecimals": 6,
                "toAsset": "ETH",
            },
        )

    def test_digest_is_stable_and_bound_to_journal(self) -> None:
        first = self.prepare()
        repeated = self.prepare()
        changed = self.prepare(journal_entry_hash="b" * 64)
        self.assertEqual(first.request_digest, repeated.request_digest)
        self.assertNotEqual(first.request_digest, changed.request_digest)

    def test_halted_canary_switch_blocks(self) -> None:
        result = self.prepare(
            canary_config=canary_config(
                kill_switch=CANARY_KILL_SWITCH_HALTED
            )
        )
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn("kill switch is halted", " ".join(result.reasons))

    def test_live_enabled_configuration_blocks_preparation(self) -> None:
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true"}, clear=True):
            live = load_live_trading_config()
        result = self.prepare(live_config=live)
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn("must remain false", " ".join(result.reasons))

    def test_wrong_amount_side_account_or_asset_blocks(self) -> None:
        wrong = intent(
            side="SELL",
            notional_usdc=Decimal("1.01"),
            treasury_address="0x" + "1" * 40,
            recipient_address="0x" + "1" * 40,
            asset_symbol="USDC",
            asset_token_address=BASE_USDC_ADDRESS,
        )
        result = self.prepare(wrong)
        text = " ".join(result.reasons)
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn("buy ETH", text)
        self.assertIn("exactly 1.00", text)
        self.assertIn("outside the adopted mandate", text)
        self.assertIn("native ETH", text)

    def test_unrecorded_or_stale_intent_blocks(self) -> None:
        stale = intent(created_at=NOW - timedelta(seconds=121))
        result = self.prepare(
            stale,
            journal_sequence=0,
            journal_entry_hash="invalid",
        )
        text = " ".join(result.reasons)
        self.assertIn("stale or future-dated", text)
        self.assertIn("journal sequence", text)
        self.assertIn("journal entry hash", text)

    def test_mismatched_decision_blocks(self) -> None:
        selected = intent()
        wrong_decision = replace(
            decision_for(selected),
            intent_fingerprint="b" * 64,
        )
        result = prepare_base_mcp_canary(
            selected,
            wrong_decision,
            journal_sequence=1,
            journal_entry_hash=JOURNAL_HASH,
            live_config=self.live_config,
            canary_config=canary_config(),
            now=NOW,
        )
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn("fingerprint", " ".join(result.reasons))

    def test_environment_cannot_raise_canary_above_one_usdc(self) -> None:
        with patch.dict(
            os.environ,
            {"BASE_MCP_CANARY_MAX_NOTIONAL_USDC": "1.01"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "cannot exceed 1.00"):
                load_base_mcp_canary_config()


if __name__ == "__main__":
    unittest.main()
