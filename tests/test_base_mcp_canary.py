from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.base_mcp_canary import (
    CANARY_KILL_SWITCH_ARMED, CANARY_KILL_SWITCH_HALTED,
    CANARY_MODE_PREPARE_ONLY, STATUS_BLOCKED, STATUS_CANDIDATE, STATUS_READY,
    BaseMcpCanaryConfig, load_base_mcp_canary_config,
    _build_base_mcp_canary_candidate,
    prepare_base_mcp_canary,
)
from app.base_mcp_canary_journal import (
    EVENT_AMBIGUOUS, EVENT_APPROVAL_REQUESTED, EVENT_COMPLETED,
    EVENT_EXPIRED, EVENT_FAILED, EVENT_REJECTED, append_canary_event,
    read_canary_events,
)
from app.execution_journal import (
    append_execution_decision,
    read_execution_decisions,
)
from app.journal_lock import fsync_containing_directory
from app.live_trading_config import BASE_USDC_ADDRESS, load_live_trading_config
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS, BASE_MAINNET_CHAIN_ID,
    EXECUTOR_MODE_SHADOW_ONLY, KILL_SWITCH_ARMED,
    ExecutorConfig, ExecutorRunResult, ExecutionDecision, RiskSnapshot,
    TradeIntent, evaluate_trade_intent,
)

NOW = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)


def canary_config(kill_switch: str = CANARY_KILL_SWITCH_ARMED):
    return BaseMcpCanaryConfig(
        mode=CANARY_MODE_PREPARE_ONLY, kill_switch_state=kill_switch,
        maximum_notional_usdc=Decimal("1.00"), approval_ttl_seconds=300,
        maximum_intent_age_seconds=120,
    )


def executor_config():
    return ExecutorConfig(
        mode=EXECUTOR_MODE_SHADOW_ONLY, kill_switch_state=KILL_SWITCH_ARMED,
        max_data_age_seconds=120, max_future_skew_seconds=30,
    )


def intent(**updates: object) -> TradeIntent:
    value = TradeIntent(
        intent_id="base-canary-signal-001", strategy_id="eth-usdc-trend",
        strategy_version="2.0.0", side="BUY", asset_symbol="ETH",
        asset_token_address=None, settlement_symbol="USDC",
        settlement_token_address=BASE_USDC_ADDRESS,
        notional_usdc=Decimal("1.00"), current_position_usdc=Decimal("0"),
        treasury_value_usdc=Decimal("105"), new_strategy=False,
        treasury_address=AUTHORIZED_TREASURY_ADDRESS,
        recipient_address=AUTHORIZED_TREASURY_ADDRESS,
        chain_id=BASE_MAINNET_CHAIN_ID,
        market_data_observed_at=NOW - timedelta(seconds=10),
        created_at=NOW - timedelta(seconds=5),
        source_refs=("research-packet:sample",),
    )
    return replace(value, **updates)


def risk():
    return RiskSnapshot(
        daily_loss_percent=Decimal("0"), drawdown_percent=Decimal("0"),
        observed_at=NOW - timedelta(seconds=5),
    )


def _restart_prepare(execution_path: str, canary_path: str):
    with patch.dict(os.environ, {}, clear=True):
        live_config = load_live_trading_config()
    with patch(
        "app.base_mcp_canary._trusted_utc_now",
        return_value=NOW + timedelta(seconds=10),
    ):
        result = prepare_base_mcp_canary(
            intent(),
            risk(),
            execution_journal_path=Path(execution_path),
            canary_journal_path=Path(canary_path),
            live_config=live_config,
            executor_config=executor_config(),
            canary_config=canary_config(),
        )
    return (
        result.status,
        result.canary_id,
        result.request_digest,
        len(read_canary_events(path=Path(canary_path))),
    )


class BaseMcpCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.execution_path = root / "execution.jsonl"
        self.canary_path = root / "canary.jsonl"
        with patch.dict(os.environ, {}, clear=True):
            self.live_config = load_live_trading_config()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepare(self, selected: TradeIntent | None = None, **updates: object):
        trusted_now = updates.pop("trusted_now", NOW)
        arguments = {
            "execution_journal_path": self.execution_path,
            "canary_journal_path": self.canary_path,
            "live_config": self.live_config,
            "executor_config": executor_config(),
            "canary_config": canary_config(), **updates,
        }
        with patch(
            "app.base_mcp_canary._trusted_utc_now",
            return_value=trusted_now,
        ):
            return prepare_base_mcp_canary(
                selected or intent(), risk(), **arguments
            )

    def decision(self, selected: TradeIntent) -> ExecutionDecision:
        return evaluate_trade_intent(
            selected, risk(), now=NOW, live_config=self.live_config,
            executor_config=executor_config(),
        )

    def test_default_configuration_is_halted_and_prepare_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_base_mcp_canary_config()
        self.assertEqual(config.mode, CANARY_MODE_PREPARE_ONLY)
        self.assertEqual(config.kill_switch_state, CANARY_KILL_SWITCH_HALTED)
        self.assertEqual(config.maximum_notional_usdc, Decimal("1.00"))

    def test_ready_is_bound_and_prepared_is_durable(self) -> None:
        result = self.prepare()
        self.assertEqual(result.status, STATUS_READY)
        self.assertTrue(result.ready_to_request_human_approval)
        self.assertFalse(result.approval_requested)
        self.assertFalse(result.executable)
        self.assertEqual(result.signing_authority, "base_account_human_only")
        self.assertEqual(result.request.tool_arguments(), {
            "amount": "1.00", "chain": "base",
            "fromAsset": BASE_USDC_ADDRESS, "fromDecimals": 6, "toAsset": "ETH",
        })
        events = read_canary_events(path=self.canary_path)
        self.assertEqual([event["event"] for event in events], ["PREPARED"])
        self.assertEqual(events[0]["request_digest"], result.request_digest)
        self.assertEqual(events[0]["intent_id"], result.intent_id)
        self.assertEqual(
            events[0]["execution_journal_entry_hash"],
            result.journal_entry_hash,
        )

    def test_lower_level_builder_cannot_return_ready(self) -> None:
        selected = intent()
        candidate = _build_base_mcp_canary_candidate(
            selected,
            self.decision(selected),
            journal_sequence=1,
            journal_entry_hash="a" * 64,
            live_config=self.live_config,
            canary_config=canary_config(),
            prepared_at=NOW,
        )
        self.assertEqual(candidate.status, STATUS_CANDIDATE)
        self.assertFalse(candidate.ready_to_request_human_approval)

    def test_safe_retry_and_restart_are_idempotent(self) -> None:
        first = self.prepare()
        second = self.prepare(trusted_now=NOW + timedelta(seconds=10))
        self.assertEqual(second.status, STATUS_READY)
        self.assertEqual(second.canary_id, first.canary_id)
        self.assertEqual(second.request_digest, first.request_digest)
        self.assertEqual(len(read_canary_events(path=self.canary_path)), 1)

    def test_fresh_process_restart_reopens_journals_idempotently(self) -> None:
        first = self.prepare()
        code = (
            "import json,sys; "
            "from tests.test_base_mcp_canary import _restart_prepare; "
            "print(json.dumps(_restart_prepare(sys.argv[1], sys.argv[2])))"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(self.execution_path),
                str(self.canary_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        status, canary_id, request_digest, event_count = json.loads(
            completed.stdout
        )
        self.assertEqual(status, STATUS_READY)
        self.assertEqual(canary_id, first.canary_id)
        self.assertEqual(request_digest, first.request_digest)
        self.assertEqual(event_count, 1)

    def test_fabricated_hash_and_mismatched_sequence_fail_closed(self) -> None:
        selected = intent()
        decision = self.decision(selected)
        append = append_execution_decision(
            decision, path=self.execution_path, recorded_at=NOW
        )
        bindings = ((append.sequence, "f" * 64), (99, append.entry_hash))
        for sequence, entry_hash in bindings:
            fabricated = ExecutorRunResult(
                decision=decision, journal_recorded=True, duplicate_blocked=False,
                journal_sequence=sequence, journal_entry_hash=entry_hash,
            )
            with patch(
                "app.base_mcp_canary.process_shadow_trade_intent",
                return_value=fabricated,
            ):
                result = self.prepare(selected)
            self.assertEqual(result.status, STATUS_BLOCKED)
            self.assertFalse(result.ready_to_request_human_approval)

    def test_missing_and_corrupt_execution_journals_fail_closed(self) -> None:
        selected = intent()
        decision = self.decision(selected)
        fabricated = ExecutorRunResult(
            decision=decision, journal_recorded=True, duplicate_blocked=False,
            journal_sequence=1, journal_entry_hash="a" * 64,
        )
        with patch("app.base_mcp_canary.process_shadow_trade_intent", return_value=fabricated):
            missing = self.prepare(selected)
        self.execution_path.write_text("not-json\n", encoding="utf-8")
        with patch("app.base_mcp_canary.process_shadow_trade_intent", return_value=fabricated):
            corrupt = self.prepare(selected)
        self.assertEqual(missing.status, STATUS_BLOCKED)
        self.assertEqual(corrupt.status, STATUS_BLOCKED)

    def test_execution_journal_lock_or_read_failure_fails_closed(self) -> None:
        with patch(
            "app.execution_journal.read_validated_execution_decision",
            side_effect=OSError("lock unavailable"),
        ):
            result = self.prepare()
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertFalse(result.ready_to_request_human_approval)

    def test_mismatched_intent_fingerprint_and_status_fail_closed(self) -> None:
        cases = (
            replace(self.decision(intent()), intent_id="other-intent"),
            replace(self.decision(intent()), intent_fingerprint="b" * 64),
            replace(self.decision(intent()), status="REJECTED"),
        )
        for index, stored in enumerate(cases):
            self.execution_path = Path(self.temp.name) / f"execution-{index}.jsonl"
            appended = append_execution_decision(
                stored, path=self.execution_path, recorded_at=NOW
            )
            fabricated = ExecutorRunResult(
                decision=stored, journal_recorded=True, duplicate_blocked=False,
                journal_sequence=appended.sequence,
                journal_entry_hash=appended.entry_hash,
            )
            with patch(
                "app.base_mcp_canary.process_shadow_trade_intent",
                return_value=fabricated,
            ):
                self.assertEqual(self.prepare().status, STATUS_BLOCKED)

    def test_reused_intent_id_with_changed_content_is_blocked(self) -> None:
        self.assertEqual(self.prepare().status, STATUS_READY)
        replay = self.prepare(intent(strategy_version="2.0.1"))
        self.assertEqual(replay.status, STATUS_BLOCKED)

    def test_corrupt_canary_journal_and_prepared_write_failure_block(self) -> None:
        self.canary_path.write_text("not-json\n", encoding="utf-8")
        self.assertEqual(self.prepare().status, STATUS_BLOCKED)
        self.canary_path.unlink()
        with patch(
            "app.base_mcp_canary_journal.append_canary_event",
            side_effect=OSError("disk full"),
        ):
            failed = self.prepare()
        self.assertEqual(failed.status, STATUS_BLOCKED)
        self.assertIn("PREPARED journal write failed", " ".join(failed.reasons))

        recovered = self.prepare(trusted_now=NOW + timedelta(seconds=10))
        self.assertEqual(recovered.status, STATUS_READY)
        self.assertEqual(len(read_canary_events(path=self.canary_path)), 1)

    def test_execution_residual_requires_full_durability_recovery(self) -> None:
        for index, failure_stage in enumerate(("file", "directory")):
            with self.subTest(failure_stage=failure_stage):
                root = Path(self.temp.name) / f"execution-fsync-{index}"
                root.mkdir()
                self.execution_path = root / "execution.jsonl"
                self.canary_path = root / "canary.jsonl"

                def fail_durability(handle, path):
                    handle.flush()
                    if failure_stage == "file":
                        raise OSError("execution file fsync failed")
                    os.fsync(handle.fileno())
                    raise OSError("execution directory fsync failed")

                for attempt in range(2):
                    with patch(
                        "app.execution_journal.establish_file_durability",
                        side_effect=fail_durability,
                    ):
                        blocked = self.prepare(
                            trusted_now=NOW + timedelta(seconds=attempt)
                        )
                    self.assertEqual(blocked.status, STATUS_BLOCKED)
                    self.assertFalse(blocked.ready_to_request_human_approval)
                    self.assertEqual(
                        len(read_execution_decisions(path=self.execution_path)),
                        1,
                    )

                with patch(
                    "app.journal_lock.os.fsync",
                    wraps=os.fsync,
                ) as file_fsync, patch(
                    "app.journal_lock.fsync_containing_directory",
                    wraps=fsync_containing_directory,
                ) as directory_fsync:
                    recovered = self.prepare(
                        trusted_now=NOW + timedelta(seconds=3)
                    )
                self.assertEqual(recovered.status, STATUS_READY)
                self.assertGreaterEqual(file_fsync.call_count, 2)
                self.assertGreaterEqual(directory_fsync.call_count, 2)

    def test_canary_residual_requires_full_durability_recovery(self) -> None:
        for index, failure_stage in enumerate(("file", "directory")):
            with self.subTest(failure_stage=failure_stage):
                root = Path(self.temp.name) / f"canary-fsync-{index}"
                root.mkdir()
                self.execution_path = root / "execution.jsonl"
                self.canary_path = root / "canary.jsonl"

                def fail_durability(handle, path):
                    handle.flush()
                    if failure_stage == "file":
                        raise OSError("canary file fsync failed")
                    os.fsync(handle.fileno())
                    raise OSError("canary directory fsync failed")

                for attempt in range(2):
                    with patch(
                        "app.base_mcp_canary_journal.establish_file_durability",
                        side_effect=fail_durability,
                    ):
                        blocked = self.prepare(
                            trusted_now=NOW + timedelta(seconds=attempt)
                        )
                    self.assertEqual(blocked.status, STATUS_BLOCKED)
                    self.assertFalse(blocked.ready_to_request_human_approval)
                    self.assertEqual(
                        len(read_canary_events(path=self.canary_path)),
                        1,
                    )

                with patch(
                    "app.journal_lock.os.fsync",
                    wraps=os.fsync,
                ) as file_fsync, patch(
                    "app.journal_lock.fsync_containing_directory",
                    wraps=fsync_containing_directory,
                ) as directory_fsync:
                    recovered = self.prepare(
                        trusted_now=NOW + timedelta(seconds=3)
                    )
                self.assertEqual(recovered.status, STATUS_READY)
                self.assertGreaterEqual(file_fsync.call_count, 2)
                self.assertGreaterEqual(directory_fsync.call_count, 2)

    def test_partial_trailing_records_fail_closed(self) -> None:
        for journal_name in ("execution", "canary"):
            with self.subTest(journal=journal_name):
                root = Path(self.temp.name) / journal_name
                root.mkdir()
                self.execution_path = root / "execution.jsonl"
                self.canary_path = root / "canary.jsonl"
                self.assertEqual(self.prepare().status, STATUS_READY)
                selected_path = (
                    self.execution_path
                    if journal_name == "execution"
                    else self.canary_path
                )
                with selected_path.open("a", encoding="utf-8") as handle:
                    handle.write('{"interrupted":')
                    handle.flush()
                replay = self.prepare(trusted_now=NOW + timedelta(seconds=10))
                self.assertEqual(replay.status, STATUS_BLOCKED)
                self.assertFalse(replay.ready_to_request_human_approval)

    def test_halted_canary_switch_blocks(self) -> None:
        halted = self.prepare(canary_config=canary_config(CANARY_KILL_SWITCH_HALTED))
        self.assertEqual(halted.status, STATUS_BLOCKED)
        self.assertIn("kill switch is halted", " ".join(halted.reasons))

    def test_live_enabled_configuration_blocks_preparation(self) -> None:
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true"}, clear=True):
            live = load_live_trading_config()
        live_enabled = self.prepare(live_config=live)
        self.assertEqual(live_enabled.status, STATUS_BLOCKED)

    def test_amount_side_account_asset_chain_and_recipient_are_bounded(self) -> None:
        wrong = intent(
            side="SELL", notional_usdc=Decimal("1.01"),
            treasury_address="0x" + "1" * 40,
            recipient_address="0x" + "2" * 40,
            asset_symbol="USDC", asset_token_address=BASE_USDC_ADDRESS,
            chain_id=1,
        )
        result = self.prepare(wrong)
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertFalse(result.ready_to_request_human_approval)

    def test_stale_intent_is_blocked(self) -> None:
        result = self.prepare(intent(created_at=NOW - timedelta(seconds=121)))
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertFalse(result.ready_to_request_human_approval)

    def test_production_api_does_not_accept_a_caller_clock(self) -> None:
        arguments = {
            "execution_journal_path": self.execution_path,
            "canary_journal_path": self.canary_path,
            "live_config": self.live_config,
            "executor_config": executor_config(),
            "canary_config": canary_config(),
        }
        with self.assertRaises(TypeError):
            prepare_base_mcp_canary(intent(), risk(), now=NOW, **arguments)
        stale = prepare_base_mcp_canary(intent(), risk(), **arguments)
        self.assertEqual(stale.status, STATUS_BLOCKED)

    def test_execution_journal_truncation_before_prepared_blocks_ready(self) -> None:
        from app import base_mcp_canary_journal

        original = base_mcp_canary_journal._append_canary_event

        def truncate_then_append(**arguments):
            self.execution_path.write_text("", encoding="utf-8")
            return original(**arguments)

        with patch(
            "app.base_mcp_canary_journal._append_canary_event",
            side_effect=truncate_then_append,
        ):
            result = self.prepare()
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertFalse(result.ready_to_request_human_approval)

    def test_terminal_canary_cannot_be_reprepared(self) -> None:
        prepared = self.prepare()
        append_canary_event(
            canary_id=prepared.canary_id,
            request_digest=prepared.request_digest,
            event=EVENT_APPROVAL_REQUESTED,
            path=self.canary_path,
            recorded_at=NOW + timedelta(seconds=1),
            request_id="request-001",
        )
        append_canary_event(
            canary_id=prepared.canary_id,
            request_digest=prepared.request_digest,
            event=EVENT_COMPLETED,
            path=self.canary_path,
            recorded_at=NOW + timedelta(seconds=2),
            request_id="request-001",
            transaction_hash="0x" + "b" * 64,
        )
        replay = self.prepare(trusted_now=NOW + timedelta(seconds=3))
        self.assertEqual(replay.status, STATUS_BLOCKED)
        self.assertFalse(replay.ready_to_request_human_approval)

    def test_each_advanced_lifecycle_state_blocks_repreparation(self) -> None:
        states = (
            EVENT_APPROVAL_REQUESTED,
            EVENT_COMPLETED,
            EVENT_FAILED,
            EVENT_REJECTED,
            EVENT_EXPIRED,
            EVENT_AMBIGUOUS,
        )
        for index, state in enumerate(states):
            with self.subTest(state=state):
                root = Path(self.temp.name) / f"state-{index}"
                root.mkdir()
                self.execution_path = root / "execution.jsonl"
                self.canary_path = root / "canary.jsonl"
                prepared = self.prepare()
                append_canary_event(
                    canary_id=prepared.canary_id,
                    request_digest=prepared.request_digest,
                    event=EVENT_APPROVAL_REQUESTED,
                    path=self.canary_path,
                    recorded_at=NOW + timedelta(seconds=1),
                    request_id="request-001",
                )
                if state != EVENT_APPROVAL_REQUESTED:
                    arguments = {
                        "canary_id": prepared.canary_id,
                        "request_digest": prepared.request_digest,
                        "event": state,
                        "path": self.canary_path,
                        "recorded_at": NOW + timedelta(seconds=2),
                        "request_id": "request-001",
                    }
                    if state == EVENT_COMPLETED:
                        arguments["transaction_hash"] = "0x" + "c" * 64
                    append_canary_event(**arguments)
                replay = self.prepare(trusted_now=NOW + timedelta(seconds=3))
                self.assertEqual(replay.status, STATUS_BLOCKED)
                self.assertFalse(replay.ready_to_request_human_approval)

    def test_environment_cannot_raise_canary_above_one_usdc(self) -> None:
        with patch.dict(
            os.environ, {"BASE_MCP_CANARY_MAX_NOTIONAL_USDC": "1.01"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "cannot exceed 1.00"):
                load_base_mcp_canary_config()


if __name__ == "__main__":
    unittest.main()
