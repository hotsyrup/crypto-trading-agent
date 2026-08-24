import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.controlled_live_execution import (
    CDP_NETWORK_ID,
    NATIVE_ETH_ADDRESS,
    ROUTE_ID,
    STATUS_BACKEND_FAILED,
    STATUS_CONFIRMED,
    STATUS_DAILY_LIMIT_BLOCKED,
    STATUS_DUPLICATE_BLOCKED,
    STATUS_POLICY_REJECTED,
    STATUS_RECEIPT_REJECTED,
    ApprovedSwap,
    CdpAgentKitBackend,
    SwapReceipt,
    execute_controlled_live_trade,
)
from app.live_execution_journal import read_live_execution_events
from app.live_trading_config import BASE_USDC_ADDRESS, load_live_trading_config
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    BASE_MAINNET_CHAIN_ID,
    EXECUTOR_MODE_CONTROLLED_LIVE,
    KILL_SWITCH_ARMED,
    KILL_SWITCH_HALTED,
    ExecutorConfig,
    RiskSnapshot,
    TradeIntent,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def controlled_config(state: str = KILL_SWITCH_ARMED) -> ExecutorConfig:
    return ExecutorConfig(
        mode=EXECUTOR_MODE_CONTROLLED_LIVE,
        kill_switch_state=state,
        max_data_age_seconds=120,
        max_future_skew_seconds=30,
    )


def intent(**updates: object) -> TradeIntent:
    value = TradeIntent(
        intent_id="controlled-001",
        strategy_id="eth-usdc-risk-reduction",
        strategy_version="1.0.0",
        side="SELL",
        asset_symbol="ETH",
        asset_token_address=None,
        settlement_symbol="USDC",
        settlement_token_address=BASE_USDC_ADDRESS,
        notional_usdc=Decimal("20"),
        current_position_usdc=Decimal("100"),
        treasury_value_usdc=Decimal("500"),
        new_strategy=False,
        treasury_address=AUTHORIZED_TREASURY_ADDRESS,
        recipient_address=AUTHORIZED_TREASURY_ADDRESS,
        chain_id=BASE_MAINNET_CHAIN_ID,
        market_data_observed_at=NOW - timedelta(seconds=10),
        created_at=NOW - timedelta(seconds=5),
        source_refs=("quote:controlled-001",),
    )
    return replace(value, **updates)


def risk() -> RiskSnapshot:
    return RiskSnapshot(
        daily_loss_percent=Decimal("0"),
        drawdown_percent=Decimal("0"),
        observed_at=NOW - timedelta(seconds=5),
        trading_capital_usdc=Decimal("500"),
    )


def swap(**updates: object) -> ApprovedSwap:
    value = ApprovedSwap(
        quote_id="cdp-quote-001",
        quote_observed_at=NOW - timedelta(seconds=2),
        route_id=ROUTE_ID,
        wallet_address=AUTHORIZED_TREASURY_ADDRESS,
        chain_id=BASE_MAINNET_CHAIN_ID,
        from_token=NATIVE_ETH_ADDRESS,
        to_token=BASE_USDC_ADDRESS,
        from_amount=Decimal("0.01"),
        notional_usdc=Decimal("20"),
        slippage_bps=50,
    )
    return replace(value, **updates)


def receipt(**updates: object) -> SwapReceipt:
    value = SwapReceipt(
        success=True,
        transaction_hash="0x" + "a" * 64,
        quote_id="cdp-executed-quote-001",
        wallet_address=AUTHORIZED_TREASURY_ADDRESS,
        network_id=CDP_NETWORK_ID,
        from_token=NATIVE_ETH_ADDRESS,
        to_token=BASE_USDC_ADDRESS,
        from_amount=Decimal("0.01"),
        to_amount=Decimal("20"),
        min_to_amount=Decimal("19.90"),
        slippage_bps=50,
    )
    return replace(value, **updates)


class Backend:
    def __init__(self, response: SwapReceipt | None = None, error: Exception | None = None):
        self.response = response or receipt()
        self.error = error
        self.calls = 0

    def submit_swap(self, request: ApprovedSwap) -> SwapReceipt:
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


class ControlledLiveExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.decisions = root / "decisions.jsonl"
        self.audit = root / "live.jsonl"
        self.live_config = replace(load_live_trading_config(), enabled=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def execute(
        self,
        *,
        trade: TradeIntent | None = None,
        order: ApprovedSwap | None = None,
        backend: Backend | None = None,
        config: ExecutorConfig | None = None,
        risk_snapshot: RiskSnapshot | None = None,
    ):
        return execute_controlled_live_trade(
            trade or intent(),
            risk_snapshot or risk(),
            order or swap(),
            backend or Backend(),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            now=NOW,
            live_config=self.live_config,
            executor_config=config or controlled_config(),
        )

    def test_confirmed_swap_records_reservation_and_receipt(self) -> None:
        result = self.execute()
        events = read_live_execution_events(path=self.audit)
        self.assertEqual(result.status, STATUS_CONFIRMED)
        self.assertEqual(result.transaction_hash, "0x" + "a" * 64)
        self.assertEqual([item["event"] for item in events], ["RESERVED", "CONFIRMED"])

    def test_per_trade_and_capital_limits_cannot_be_bypassed(self) -> None:
        too_large = self.execute(
            trade=intent(notional_usdc=Decimal("20.0000001")),
            order=swap(notional_usdc=Decimal("20.0000001")),
        )
        self.decisions.unlink(missing_ok=True)
        overfunded = self.execute(trade=intent(treasury_value_usdc=Decimal("500.01")))
        self.assertEqual(too_large.status, STATUS_POLICY_REJECTED)
        self.assertEqual(overfunded.status, STATUS_POLICY_REJECTED)
        self.assertIn("$20", " ".join(too_large.reasons))
        self.assertIn("$500", " ".join(overfunded.reasons))

    def test_strategy_cannot_spoof_verified_trading_capital(self) -> None:
        missing = self.execute(
            risk_snapshot=replace(risk(), trading_capital_usdc=None)
        )
        self.decisions.unlink(missing_ok=True)
        mismatch = self.execute(
            trade=intent(treasury_value_usdc=Decimal("400")),
            risk_snapshot=risk(),
        )
        self.assertEqual(missing.status, STATUS_POLICY_REJECTED)
        self.assertEqual(mismatch.status, STATUS_POLICY_REJECTED)
        self.assertIn("verified trading capital", " ".join(missing.reasons))
        self.assertIn("does not match", " ".join(mismatch.reasons))

    def test_daily_reservations_enforce_absolute_100_limit(self) -> None:
        for number in range(5):
            trade = intent(intent_id=f"controlled-{number}")
            order = swap(quote_id=f"quote-{number}")
            result = self.execute(trade=trade, order=order)
            self.assertEqual(result.status, STATUS_CONFIRMED)
        blocked = self.execute(
            trade=intent(intent_id="controlled-sixth", notional_usdc=Decimal("0.01")),
            order=swap(quote_id="quote-sixth", notional_usdc=Decimal("0.01")),
        )
        self.assertEqual(blocked.status, STATUS_DAILY_LIMIT_BLOCKED)

    def test_wrong_wallet_chain_route_and_contract_fail_before_backend(self) -> None:
        cases = (
            swap(wallet_address="0x" + "1" * 40),
            swap(chain_id=1),
            swap(route_id="unreviewed-route"),
            swap(to_token="0x" + "2" * 40),
        )
        for number, order in enumerate(cases):
            with self.subTest(number=number):
                backend = Backend()
                result = self.execute(
                    trade=intent(intent_id=f"invalid-{number}"),
                    order=order,
                    backend=backend,
                )
                self.assertEqual(result.status, STATUS_POLICY_REJECTED)
                self.assertEqual(backend.calls, 0)

    def test_stale_quote_and_excess_slippage_fail_closed(self) -> None:
        stale = self.execute(
            order=swap(quote_observed_at=NOW - timedelta(seconds=121))
        )
        self.decisions.unlink(missing_ok=True)
        excessive = self.execute(order=swap(slippage_bps=101))
        self.assertEqual(stale.status, STATUS_POLICY_REJECTED)
        self.assertEqual(excessive.status, STATUS_POLICY_REJECTED)

    def test_halted_kill_switch_and_buy_route_are_rejected(self) -> None:
        halted = self.execute(config=controlled_config(KILL_SWITCH_HALTED))
        self.decisions.unlink(missing_ok=True)
        buy = self.execute(trade=intent(side="BUY"))
        self.assertEqual(halted.status, STATUS_POLICY_REJECTED)
        self.assertEqual(buy.status, STATUS_POLICY_REJECTED)

    def test_duplicate_intent_never_calls_backend_twice(self) -> None:
        backend = Backend()
        first = self.execute(backend=backend)
        replay = self.execute(backend=backend)
        self.assertEqual(first.status, STATUS_CONFIRMED)
        self.assertEqual(replay.status, STATUS_DUPLICATE_BLOCKED)
        self.assertEqual(backend.calls, 1)

    def test_backend_exception_is_audited_and_reservation_remains_counted(self) -> None:
        result = self.execute(backend=Backend(error=TimeoutError("provider timeout")))
        events = read_live_execution_events(path=self.audit)
        self.assertEqual(result.status, STATUS_BACKEND_FAILED)
        self.assertEqual([item["event"] for item in events], ["RESERVED", "BACKEND_FAILED"])

    def test_corrupt_live_audit_blocks_backend(self) -> None:
        self.audit.write_text("not-json\n", encoding="utf-8")
        backend = Backend()
        result = self.execute(backend=backend)
        self.assertEqual(result.status, "AUDIT_FAILURE")
        self.assertEqual(backend.calls, 0)

    def test_mismatched_backend_receipt_fails_closed_and_is_audited(self) -> None:
        result = self.execute(
            backend=Backend(receipt(wallet_address="0x" + "3" * 40))
        )
        events = read_live_execution_events(path=self.audit)
        self.assertEqual(result.status, STATUS_RECEIPT_REJECTED)
        self.assertEqual(events[-1]["event"], "RECEIPT_REJECTED")

    def test_backend_cannot_weaken_slippage_or_expand_notional(self) -> None:
        excessive_slippage = self.execute(
            backend=Backend(receipt(min_to_amount=Decimal("19.89")))
        )
        self.decisions.unlink(missing_ok=True)
        self.audit.unlink(missing_ok=True)
        excessive_output = self.execute(
            backend=Backend(
                receipt(to_amount=Decimal("20.01"), min_to_amount=Decimal("20"))
            )
        )
        self.assertEqual(excessive_slippage.status, STATUS_RECEIPT_REJECTED)
        self.assertEqual(excessive_output.status, STATUS_RECEIPT_REJECTED)


class CdpAgentKitBackendTests(unittest.TestCase):
    def test_adapter_binds_slippage_idempotency_wallet_and_base_receipt(self) -> None:
        calls: dict[str, object] = {}

        class Config:
            def __init__(self, **values: object):
                calls["config"] = values

        class Quote:
            liquidity_available = True
            to_amount = "20000000"
            min_to_amount = "19900000"
            quote_id = "executed-cdp-quote"

            async def execute(self, **values: object):
                calls["execute"] = values
                return types.SimpleNamespace(transaction_hash="0x" + "b" * 64)

        class Account:
            async def quote_swap(self, **values: object):
                calls["quote"] = values
                return Quote()

        class Evm:
            async def get_account(self, **values: object):
                calls["account"] = values
                return Account()

        class Client:
            evm = Evm()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args: object):
                return None

        class Wallet:
            def __init__(self, config: Config):
                self.config = config

            def get_address(self) -> str:
                return AUTHORIZED_TREASURY_ADDRESS

            def get_network(self):
                return types.SimpleNamespace(
                    chain_id=BASE_MAINNET_CHAIN_ID,
                    network_id=CDP_NETWORK_ID,
                )

            def get_client(self) -> Client:
                return Client()

            def wait_for_transaction_receipt(self, tx_hash: str):
                calls["receipt"] = tx_hash
                return types.SimpleNamespace(status=1)

        module = types.SimpleNamespace(
            CdpEvmWalletProvider=Wallet,
            CdpEvmWalletProviderConfig=Config,
        )
        with patch.dict("sys.modules", {"coinbase_agentkit": module}):
            result = CdpAgentKitBackend().submit_swap(swap())

        self.assertTrue(result.success)
        self.assertEqual(result.quote_id, "executed-cdp-quote")
        self.assertEqual(calls["config"], {
            "address": AUTHORIZED_TREASURY_ADDRESS,
            "network_id": CDP_NETWORK_ID,
        })
        quote_call = calls["quote"]
        self.assertEqual(quote_call["network"], "base")
        self.assertEqual(quote_call["slippage_bps"], 50)
        self.assertEqual(quote_call["from_amount"], "10000000000000000")
        self.assertIn("idempotency_key", quote_call)
        self.assertIn("idempotency_key", calls["execute"])
        self.assertEqual(calls["receipt"], "0x" + "b" * 64)

    def test_adapter_rejects_quote_over_notional_before_execute(self) -> None:
        executed = False

        class Config:
            def __init__(self, **values: object):
                pass

        class Quote:
            liquidity_available = True
            to_amount = "20000001"
            min_to_amount = "19900000"
            quote_id = "too-large"

            async def execute(self, **values: object):
                nonlocal executed
                executed = True
                return types.SimpleNamespace(transaction_hash="0x" + "c" * 64)

        class Account:
            async def quote_swap(self, **values: object):
                return Quote()

        class Evm:
            async def get_account(self, **values: object):
                return Account()

        class Client:
            evm = Evm()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args: object):
                return None

        class Wallet:
            def __init__(self, config: Config):
                pass

            def get_address(self) -> str:
                return AUTHORIZED_TREASURY_ADDRESS

            def get_network(self):
                return types.SimpleNamespace(
                    chain_id=BASE_MAINNET_CHAIN_ID,
                    network_id=CDP_NETWORK_ID,
                )

            def get_client(self) -> Client:
                return Client()

        module = types.SimpleNamespace(
            CdpEvmWalletProvider=Wallet,
            CdpEvmWalletProviderConfig=Config,
        )
        with patch.dict("sys.modules", {"coinbase_agentkit": module}):
            backend = CdpAgentKitBackend()
            with self.assertRaisesRegex(RuntimeError, "notional boundary"):
                backend.submit_swap(swap())
        self.assertFalse(executed)


if __name__ == "__main__":
    unittest.main()
