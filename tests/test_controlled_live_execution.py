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
    PERMIT2_ADDRESS,
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
from app.base_asset_universe import GovernedAsset, GovernedAssetUniverse
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
AERO_ADDRESS = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"


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
        from_decimals=18,
        to_decimals=6,
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


def governed_universe() -> GovernedAssetUniverse:
    return GovernedAssetUniverse(
        observed_at=NOW - timedelta(minutes=5),
        source="reviewed-test-snapshot",
        snapshot_sha256="a" * 64,
        assets=(
            GovernedAsset(
                rank=1,
                symbol="ETH",
                name="Ether",
                token_address=None,
                decimals=18,
                market_cap_usd=Decimal("1000000000"),
                liquidity_usd=Decimal("10000000"),
                daily_volume_usd=Decimal("10000000"),
                oldest_pool_created_at=NOW - timedelta(days=365),
            ),
            GovernedAsset(
                rank=2,
                symbol="AERO",
                name="Aerodrome",
                token_address=AERO_ADDRESS,
                decimals=18,
                market_cap_usd=Decimal("500000000"),
                liquidity_usd=Decimal("5000000"),
                daily_volume_usd=Decimal("1000000"),
                oldest_pool_created_at=NOW - timedelta(days=365),
            ),
        ),
    )


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
        universe: GovernedAssetUniverse | None = None,
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
            asset_universe=universe,
        )

    def test_confirmed_swap_records_reservation_and_receipt(self) -> None:
        result = self.execute()
        events = read_live_execution_events(path=self.audit)
        self.assertEqual(result.status, STATUS_CONFIRMED)
        self.assertEqual(result.transaction_hash, "0x" + "a" * 64)
        self.assertEqual([item["event"] for item in events], ["RESERVED", "CONFIRMED"])

    def test_governed_asset_can_be_bought_with_usdc_and_sold_back_to_usdc(self) -> None:
        universe = governed_universe()
        buy_intent = intent(
            intent_id="buy-aero",
            strategy_id="research-ranked-base-portfolio",
            side="BUY",
            asset_symbol="AERO",
            asset_token_address=AERO_ADDRESS,
            current_position_usdc=Decimal("0"),
            new_strategy=True,
            source_refs=("research:packet-aero", universe.snapshot_sha256),
        )
        buy_swap = swap(
            quote_id="buy-aero-quote",
            from_token=BASE_USDC_ADDRESS,
            to_token=AERO_ADDRESS,
            from_amount=Decimal("20"),
            from_decimals=6,
            to_decimals=18,
        )
        buy_receipt = receipt(
            quote_id="buy-aero-provider-quote",
            from_token=BASE_USDC_ADDRESS,
            to_token=AERO_ADDRESS,
            from_amount=Decimal("20"),
            to_amount=Decimal("42"),
            min_to_amount=Decimal("41.79"),
            approval_transaction_hash="0x" + "b" * 64,
            approval_token=BASE_USDC_ADDRESS,
            approval_spender=PERMIT2_ADDRESS,
            approval_amount=Decimal("20"),
        )

        bought = self.execute(
            trade=buy_intent,
            order=buy_swap,
            backend=Backend(buy_receipt),
            universe=universe,
        )

        sell_intent = intent(
            intent_id="sell-aero",
            strategy_id="research-ranked-base-portfolio",
            side="SELL",
            asset_symbol="AERO",
            asset_token_address=AERO_ADDRESS,
            current_position_usdc=Decimal("20"),
            source_refs=("research:packet-aero-exit", universe.snapshot_sha256),
        )
        sell_swap = swap(
            quote_id="sell-aero-quote",
            from_token=AERO_ADDRESS,
            to_token=BASE_USDC_ADDRESS,
            from_amount=Decimal("42"),
            from_decimals=18,
            to_decimals=6,
        )
        sell_receipt = receipt(
            quote_id="sell-aero-provider-quote",
            from_token=AERO_ADDRESS,
            to_token=BASE_USDC_ADDRESS,
            from_amount=Decimal("42"),
            to_amount=Decimal("19.95"),
            min_to_amount=Decimal("19.85025"),
            approval_transaction_hash="0x" + "c" * 64,
            approval_token=AERO_ADDRESS,
            approval_spender=PERMIT2_ADDRESS,
            approval_amount=Decimal("42"),
        )
        sold = self.execute(
            trade=sell_intent,
            order=sell_swap,
            backend=Backend(sell_receipt),
            universe=universe,
        )

        self.assertEqual(bought.status, STATUS_CONFIRMED)
        self.assertEqual(sold.status, STATUS_CONFIRMED)

    def test_research_cannot_substitute_unknown_contract_for_ranked_symbol(self) -> None:
        backend = Backend()
        result = self.execute(
            trade=intent(
                side="BUY",
                asset_symbol="AERO",
                asset_token_address="0x" + "9" * 40,
                current_position_usdc=Decimal("0"),
                new_strategy=True,
            ),
            order=swap(
                from_token=BASE_USDC_ADDRESS,
                to_token="0x" + "9" * 40,
                from_amount=Decimal("20"),
                from_decimals=6,
                to_decimals=18,
            ),
            backend=backend,
            universe=governed_universe(),
        )
        self.assertEqual(result.status, STATUS_POLICY_REJECTED)
        self.assertEqual(backend.calls, 0)

    def test_erc20_approval_or_decimal_bypass_is_rejected(self) -> None:
        universe = governed_universe()
        trade = intent(
            side="BUY",
            asset_symbol="AERO",
            asset_token_address=AERO_ADDRESS,
            current_position_usdc=Decimal("0"),
            new_strategy=True,
            source_refs=("research:aero", universe.snapshot_sha256),
        )
        order = swap(
            from_token=BASE_USDC_ADDRESS,
            to_token=AERO_ADDRESS,
            from_amount=Decimal("20"),
            from_decimals=6,
            to_decimals=18,
        )
        base_receipt = receipt(
            from_token=BASE_USDC_ADDRESS,
            to_token=AERO_ADDRESS,
            from_amount=Decimal("20"),
            to_amount=Decimal("40"),
            min_to_amount=Decimal("39.8"),
            approval_transaction_hash="0x" + "b" * 64,
            approval_token=BASE_USDC_ADDRESS,
            approval_spender=PERMIT2_ADDRESS,
            approval_amount=Decimal("20"),
        )
        receipt_cases = (
            replace(base_receipt, approval_amount=Decimal("21")),
            replace(base_receipt, approval_spender="0x" + "9" * 40),
            replace(base_receipt, approval_token=AERO_ADDRESS),
        )
        for number, backend_receipt in enumerate(receipt_cases):
            with self.subTest(number=number):
                result = self.execute(
                    trade=replace(trade, intent_id=f"approval-{number}"),
                    order=replace(order, quote_id=f"approval-quote-{number}"),
                    backend=Backend(backend_receipt),
                    universe=universe,
                )
                self.assertEqual(result.status, STATUS_RECEIPT_REJECTED)

        backend = Backend(base_receipt)
        invalid_decimals = self.execute(
            trade=replace(trade, intent_id="invalid-decimals"),
            order=replace(order, quote_id="invalid-decimals", from_decimals=18),
            backend=backend,
            universe=universe,
        )
        self.assertEqual(invalid_decimals.status, STATUS_POLICY_REJECTED)
        self.assertEqual(backend.calls, 0)

    def test_per_trade_and_capital_limits_cannot_be_bypassed(self) -> None:
        too_large = self.execute(
            trade=intent(notional_usdc=Decimal("20.0000001")),
            order=swap(notional_usdc=Decimal("20.0000001")),
        )
        self.decisions.unlink(missing_ok=True)
        overfunded = self.execute(
            trade=intent(treasury_value_usdc=Decimal("500.01")),
            risk_snapshot=replace(
                risk(),
                trading_capital_usdc=Decimal("500.01"),
                portfolio_value_usdc=Decimal("500.01"),
            ),
        )
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

    def test_portfolio_profit_above_500_does_not_block_risk_reducing_sell(self) -> None:
        profitable_risk = RiskSnapshot(
            daily_loss_percent=Decimal("0"),
            drawdown_percent=Decimal("0"),
            observed_at=NOW - timedelta(seconds=5),
            trading_capital_usdc=Decimal("500"),
            portfolio_value_usdc=Decimal("550"),
        )
        result = self.execute(
            trade=intent(
                intent_id="profitable-exit",
                treasury_value_usdc=Decimal("550"),
                current_position_usdc=Decimal("20"),
            ),
            risk_snapshot=profitable_risk,
        )
        self.assertEqual(result.status, STATUS_CONFIRMED)

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
    def test_adapter_paginates_and_normalizes_exact_base_balances(self) -> None:
        calls: list[object] = []

        class Config:
            def __init__(self, **values: object):
                pass

        class Account:
            async def list_token_balances(self, **values: object):
                calls.append(values.get("page_token"))
                if values.get("page_token") is None:
                    return types.SimpleNamespace(
                        balances=[
                            types.SimpleNamespace(
                                token=types.SimpleNamespace(
                                    contract_address=BASE_USDC_ADDRESS.upper()
                                ),
                                amount=types.SimpleNamespace(
                                    amount=25000000,
                                    decimals=6,
                                ),
                            )
                        ],
                        next_page_token="page-2",
                    )
                return types.SimpleNamespace(
                    balances=[
                        types.SimpleNamespace(
                            token=types.SimpleNamespace(
                                contract_address=NATIVE_ETH_ADDRESS
                            ),
                            amount=types.SimpleNamespace(
                                amount=10**16,
                                decimals=18,
                            ),
                        )
                    ],
                    next_page_token=None,
                )

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
                    chain_id=str(BASE_MAINNET_CHAIN_ID),
                    network_id=CDP_NETWORK_ID,
                )

            def get_client(self) -> Client:
                return Client()

        module = types.SimpleNamespace(
            CdpEvmWalletProvider=Wallet,
            CdpEvmWalletProviderConfig=Config,
        )
        with patch.dict("sys.modules", {"coinbase_agentkit": module}):
            balances = CdpAgentKitBackend().list_token_balances()

        self.assertEqual(calls, [None, "page-2"])
        self.assertEqual(
            balances,
            (
                (BASE_USDC_ADDRESS, Decimal("25"), 6),
                (NATIVE_ETH_ADDRESS, Decimal("0.01"), 18),
            ),
        )

    def test_adapter_replaces_oversized_allowance_with_exact_permit2_amount(self) -> None:
        calls: dict[str, object] = {}

        class Config:
            def __init__(self, **values: object):
                calls["config"] = values

        class Quote:
            liquidity_available = True
            to_amount = str(40 * 10**18)
            min_to_amount = str(398 * 10**17)
            quote_id = "aero-buy-quote"

            async def execute(self, **values: object):
                calls["execute"] = values
                return types.SimpleNamespace(transaction_hash="0x" + "e" * 64)

        class Account:
            async def quote_swap(self, **values: object):
                calls["quote"] = values
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

            def read_contract(self, **values: object) -> int:
                calls["allowance"] = values
                return 2**256 - 1

            def send_transaction(self, values: dict[str, object]) -> str:
                calls["approval"] = values
                return "0x" + "d" * 64

            def wait_for_transaction_receipt(self, tx_hash: str):
                return types.SimpleNamespace(status=1)

        module = types.SimpleNamespace(
            CdpEvmWalletProvider=Wallet,
            CdpEvmWalletProviderConfig=Config,
        )
        order = swap(
            from_token=BASE_USDC_ADDRESS,
            to_token=AERO_ADDRESS,
            from_amount=Decimal("20"),
            from_decimals=6,
            to_decimals=18,
        )
        with patch.dict("sys.modules", {"coinbase_agentkit": module}):
            result = CdpAgentKitBackend().submit_swap(order)

        approval = calls["approval"]
        self.assertEqual(approval["to"], BASE_USDC_ADDRESS)
        self.assertTrue(str(approval["data"]).startswith("0x095ea7b3"))
        self.assertTrue(str(approval["data"]).endswith(f"{20 * 10**6:064x}"))
        self.assertEqual(result.approval_spender, PERMIT2_ADDRESS)
        self.assertEqual(result.approval_amount, Decimal("20"))
        self.assertEqual(calls["quote"]["from_amount"], str(20 * 10**6))
        self.assertEqual(result.to_amount, Decimal("40"))

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
                    chain_id=str(BASE_MAINNET_CHAIN_ID),
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
            "address": "0x716B5D6Bf67A4C01103B52365C8fB5fdFEf0ff06",
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
