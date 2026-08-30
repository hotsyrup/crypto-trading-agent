import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from http.server import HTTPServer
from io import StringIO
from urllib.request import urlopen

from app.base_asset_universe import GovernedAsset, GovernedAssetUniverse
from app.controlled_live_execution import (
    CDP_NETWORK_ID,
    NATIVE_ETH_ADDRESS,
    PERMIT2_ADDRESS,
    STATUS_CONFIRMED,
    SwapReceipt,
)
from app.live_portfolio_worker import (
    CYCLE_NO_FUNDS,
    CYCLE_NO_SIGNAL,
    CYCLE_POLICY_BLOCKED,
    CYCLE_VALUATION_BLOCKED,
    HealthHandler,
    LiveCycleResult,
    OnchainTokenBalance,
    STATE,
    _record_cycle_failure,
    _record_cycle_result,
    _cycle_sleep_seconds,
    _verified_portfolio,
    run_live_cycle,
)
from app.live_execution_journal import reserve_live_execution
from app.live_trading_config import BASE_USDC_ADDRESS, load_live_trading_config
from app.research_agent import build_packet
from app.portfolio_trading import ResearchSignal
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    EXECUTOR_MODE_CONTROLLED_LIVE,
    EXECUTOR_MODE_SHADOW_ONLY,
    KILL_SWITCH_ARMED,
    KILL_SWITCH_HALTED,
    STATUS_DUPLICATE_BLOCKED,
    ExecutorConfig,
)


NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
AERO_ADDRESS = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
THIN_ADDRESS = "0x2222222222222222222222222222222222222222"
MAG7_ADDRESS = "0x9e6a46f294bb67c20f1d1e7afb0bbef614403b55"
CHIP_ADDRESS = "0x0c1c6a5b1c6f5f8e98d2f9f4b1fbb0e61fef1f6e"


def universe() -> GovernedAssetUniverse:
    return GovernedAssetUniverse(
        observed_at=NOW - timedelta(minutes=5),
        source="cross-verified-test",
        snapshot_sha256="d" * 64,
        assets=(
            GovernedAsset(
                rank=1,
                symbol="AERO",
                name="Aerodrome",
                token_address=AERO_ADDRESS,
                decimals=18,
                market_cap_usd=Decimal("450000000"),
                liquidity_usd=Decimal("25000000"),
                daily_volume_usd=Decimal("15000000"),
                oldest_pool_created_at=NOW - timedelta(days=900),
            ),
        ),
    )


def research_payload() -> dict[str, object]:
    pair = {
        "chainId": "base",
        "dexId": "aerodrome",
        "pairAddress": "0x" + "1" * 40,
        "baseToken": {"address": AERO_ADDRESS, "name": "Aerodrome", "symbol": "AERO"},
        "quoteToken": {"address": BASE_USDC_ADDRESS, "name": "USD Coin", "symbol": "USDC"},
        "priceUsd": "0.50",
        "liquidity": {"usd": "25000000"},
        "volume": {"h24": "15000000", "h6": "4000000"},
        "priceChange": {"h24": "8", "h6": "3"},
        "txns": {"h24": {"buys": 1200, "sells": 900}},
        "pairCreatedAt": 1704067200000,
        "marketCap": "450000000",
        "fdv": "500000000",
        "boosts": {"active": 0},
    }
    packet = build_packet(
        {
            "contract_address": AERO_ADDRESS,
            "discovery_source": "configured_watchlist",
            "profile_url": None,
            "marketing_influenced": False,
            "promotion_type": None,
        },
        pair,
        NOW - timedelta(seconds=10),
        Decimal("100000"),
        90,
        1,
    )
    packet["is_stale"] = False
    return {
        "service": "lumen-base-research-agent",
        "schema_version": 2,
        "mode": "observation_only",
        "execution": "disabled",
        "generated_at": NOW.isoformat(),
        "packets": [packet],
    }


class Runtime:
    wallet_address = AUTHORIZED_TREASURY_ADDRESS
    network_id = CDP_NETWORK_ID

    def __init__(self, balances: tuple[OnchainTokenBalance, ...]) -> None:
        self.balances = balances
        self.requests = []

    def list_token_balances(self) -> tuple[OnchainTokenBalance, ...]:
        return self.balances

    def submit_swap(self, request):
        self.requests.append(request)
        return SwapReceipt(
            success=True,
            transaction_hash="0x" + "a" * 64,
            quote_id="provider-quote",
            wallet_address=self.wallet_address,
            network_id=self.network_id,
            from_token=request.from_token,
            to_token=request.to_token,
            from_amount=request.from_amount,
            to_amount=Decimal("40"),
            min_to_amount=Decimal("39.8"),
            slippage_bps=request.slippage_bps,
            approval_transaction_hash="0x" + "b" * 64,
            approval_token=request.from_token,
            approval_spender=PERMIT2_ADDRESS,
            approval_amount=request.from_amount,
        )


class LivePortfolioWorkerTests(unittest.TestCase):
    def test_valuation_outage_uses_provider_cooldown_without_delaying_normal_cycles(self) -> None:
        blocked = LiveCycleResult(
            CYCLE_VALUATION_BLOCKED,
            AUTHORIZED_TREASURY_ADDRESS,
            CDP_NETWORK_ID,
            Decimal("0"),
            "research unavailable",
        )
        ready = replace(blocked, status=CYCLE_NO_SIGNAL)

        self.assertEqual(_cycle_sleep_seconds(60, blocked), 120)
        self.assertEqual(_cycle_sleep_seconds(60, ready), 60)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.decisions = root / "decisions.jsonl"
        self.audit = root / "audit.jsonl"
        self.risk = root / "risk.jsonl"
        self.original_state = STATE.copy()

    def tearDown(self) -> None:
        STATE.clear()
        STATE.update(self.original_state)
        self.temp_dir.cleanup()

    def cycle(self, runtime: Runtime, config: ExecutorConfig):
        return run_live_cycle(
            runtime=runtime,
            research_payload=research_payload(),
            universe=universe(),
            authorized_capital_usdc=Decimal("500"),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            risk_journal_path=self.risk,
            now=NOW,
            live_config=replace(
                load_live_trading_config(),
                enabled=config.mode == EXECUTOR_MODE_CONTROLLED_LIVE,
            ),
            executor_config=config,
        )

    def test_no_funds_startup_verifies_wallet_without_submitting(self) -> None:
        runtime = Runtime(())
        loaded = []

        result = run_live_cycle(
            runtime=runtime,
            research_payload=lambda: loaded.append("research"),
            universe=lambda: loaded.append("universe"),
            authorized_capital_usdc=Decimal("500"),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            risk_journal_path=self.risk,
            now=NOW,
            live_config=load_live_trading_config(),
            executor_config=ExecutorConfig(
                mode=EXECUTOR_MODE_SHADOW_ONLY,
                kill_switch_state=KILL_SWITCH_HALTED,
                max_data_age_seconds=120,
                max_future_skew_seconds=30,
            ),
        )

        self.assertEqual(result.status, CYCLE_NO_FUNDS)
        self.assertEqual(result.wallet_address, AUTHORIZED_TREASURY_ADDRESS)
        self.assertEqual(result.network_id, CDP_NETWORK_ID)
        self.assertEqual(runtime.requests, [])
        self.assertEqual(loaded, [])

    def test_halted_funded_cycle_records_risk_but_never_submits(self) -> None:
        runtime = Runtime(
            (OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("25"), 6),)
        )

        result = self.cycle(
            runtime,
            ExecutorConfig(
                mode=EXECUTOR_MODE_SHADOW_ONLY,
                kill_switch_state=KILL_SWITCH_HALTED,
                max_data_age_seconds=120,
                max_future_skew_seconds=30,
            ),
        )

        self.assertEqual(result.status, CYCLE_POLICY_BLOCKED)
        self.assertEqual(result.portfolio_value_usdc, Decimal("25"))
        self.assertTrue(self.risk.exists())
        self.assertEqual(runtime.requests, [])

    def test_native_eth_gas_reserve_is_not_tradable_portfolio_value(self) -> None:
        governed = GovernedAssetUniverse(
            observed_at=NOW - timedelta(minutes=5),
            source="cross-verified-test",
            snapshot_sha256="f" * 64,
            assets=(
                GovernedAsset(
                    rank=1,
                    symbol="ETH",
                    name="Ether",
                    token_address=None,
                    decimals=18,
                    market_cap_usd=Decimal("500000000000"),
                    liquidity_usd=Decimal("100000000"),
                    daily_volume_usd=Decimal("100000000"),
                    oldest_pool_created_at=NOW - timedelta(days=1000),
                ),
            ),
        )
        signal = ResearchSignal(
            packet_id="a" * 64,
            observed_at=NOW - timedelta(seconds=10),
            symbol="ETH",
            token_address=None,
            price_usd=Decimal("2462.10"),
            liquidity_usd=Decimal("100000000"),
            daily_volume_usd=Decimal("100000000"),
            change_h6_percent=Decimal("1"),
            change_h24_percent=Decimal("2"),
            buys_h24=100,
            sells_h24=90,
        )

        portfolio = _verified_portfolio(
            (
                OnchainTokenBalance(
                    NATIVE_ETH_ADDRESS,
                    Decimal("0.0020228"),
                    18,
                ),
                OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("25"), 6),
            ),
            (signal,),
            governed,
            wallet_address=AUTHORIZED_TREASURY_ADDRESS,
            native_gas_reserve_eth=Decimal("0.0020228"),
            now=NOW,
        )

        self.assertEqual(portfolio.total_value_usdc, Decimal("25"))
        self.assertEqual(portfolio.usdc_balance, Decimal("25"))
        self.assertEqual(portfolio.positions, ())

    def test_retained_governed_dust_gets_exact_valuation_after_candidate_refresh(self) -> None:
        def governed(symbol: str, address: str, decimals: int) -> GovernedAssetUniverse:
            return GovernedAssetUniverse(
                observed_at=NOW - timedelta(minutes=5),
                source="cross-verified-test",
                snapshot_sha256=symbol[0].lower() * 64,
                assets=(
                    GovernedAsset(
                        rank=1,
                        symbol=symbol,
                        name=symbol,
                        token_address=address,
                        decimals=decimals,
                        market_cap_usd=Decimal("1000000"),
                        liquidity_usd=Decimal("2000000"),
                        daily_volume_usd=Decimal("300000"),
                        oldest_pool_created_at=NOW - timedelta(days=365),
                    ),
                ),
            )

        def packet(address: str, symbol: str) -> dict[str, object]:
            pair = {
                "chainId": "base",
                "dexId": "uniswap",
                "pairAddress": "0x" + "4" * 40,
                "baseToken": {"address": address, "name": symbol, "symbol": symbol},
                "quoteToken": {
                    "address": BASE_USDC_ADDRESS,
                    "name": "USD Coin",
                    "symbol": "USDC",
                },
                "priceUsd": "0.50",
                "liquidity": {"usd": "2000000"},
                "volume": {"h24": "300000", "h6": "100000"},
                "priceChange": {"h24": "-1", "h6": "-1"},
                "txns": {"h24": {"buys": 100, "sells": 101}},
                "pairCreatedAt": 1704067200000,
                "marketCap": "1000000",
                "fdv": "1000000",
                "boosts": {"active": 0},
            }
            result = build_packet(
                {
                    "contract_address": address,
                    "discovery_source": "configured_watchlist",
                    "profile_url": None,
                    "marketing_influenced": False,
                    "promotion_type": None,
                },
                pair,
                NOW - timedelta(seconds=10),
                Decimal("100000"),
                5,
                1,
            )
            result["is_stale"] = False
            return result

        payloads = {
            MAG7_ADDRESS: packet(MAG7_ADDRESS, "MAG7.SSI"),
            AERO_ADDRESS: packet(AERO_ADDRESS, "AERO"),
        }
        requested = []

        def research(contracts):
            requested.append(contracts)
            return {
                "service": "lumen-base-research-agent",
                "schema_version": 2,
                "mode": "observation_only",
                "execution": "disabled",
                "generated_at": NOW.isoformat(),
                "packets": [payloads[c] for c in contracts if c in payloads],
            }

        runtime = Runtime(
            (
                OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("25"), 6),
                OnchainTokenBalance(MAG7_ADDRESS, Decimal("0.00000106"), 8),
            )
        )
        config = ExecutorConfig(
            mode=EXECUTOR_MODE_SHADOW_ONLY,
            kill_switch_state=KILL_SWITCH_HALTED,
            max_data_age_seconds=120,
            max_future_skew_seconds=30,
        )
        registry = Path(self.temp_dir.name) / "lifecycle.json"

        first = run_live_cycle(
            runtime=runtime,
            research_payload=research,
            universe=governed("MAG7.SSI", MAG7_ADDRESS, 8),
            authorized_capital_usdc=Decimal("500"),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            risk_journal_path=self.risk,
            lifecycle_registry_path=registry,
            now=NOW,
            live_config=load_live_trading_config(),
            executor_config=config,
        )
        second = run_live_cycle(
            runtime=runtime,
            research_payload=research,
            universe=governed("AERO", AERO_ADDRESS, 18),
            authorized_capital_usdc=Decimal("500"),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            risk_journal_path=self.risk,
            lifecycle_registry_path=registry,
            now=NOW + timedelta(minutes=1),
            live_config=load_live_trading_config(),
            executor_config=config,
        )

        self.assertEqual(first.status, CYCLE_POLICY_BLOCKED)
        self.assertEqual(second.status, CYCLE_POLICY_BLOCKED, second.reason)
        self.assertEqual(second.portfolio_value_usdc, Decimal("25.000000530"))
        self.assertIn(MAG7_ADDRESS, requested[-1])
        self.assertNotEqual(second.status, CYCLE_VALUATION_BLOCKED)
        self.assertEqual(runtime.requests, [])

    def test_material_held_asset_without_exact_valuation_blocks_before_risk(self) -> None:
        payload = research_payload()
        payload["packets"] = []
        runtime = Runtime(
            (
                OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("25"), 6),
                OnchainTokenBalance(AERO_ADDRESS, Decimal("10"), 18),
            )
        )

        result = run_live_cycle(
            runtime=runtime,
            research_payload=payload,
            universe=universe(),
            authorized_capital_usdc=Decimal("500"),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            risk_journal_path=self.risk,
            now=NOW,
        )

        self.assertEqual(result.status, CYCLE_VALUATION_BLOCKED)
        self.assertEqual(result.held_required, 1)
        self.assertEqual(result.held_covered, 0)
        self.assertFalse(self.risk.exists())
        self.assertEqual(runtime.requests, [])

    def test_held_candidate_uses_exact_price_without_requiring_entry_liquidity(self) -> None:
        pair = {
            "chainId": "base",
            "dexId": "aerodrome",
            "pairAddress": "0x" + "5" * 40,
            "baseToken": {
                "address": AERO_ADDRESS,
                "name": "Aerodrome",
                "symbol": "AERO",
            },
            "quoteToken": {
                "address": BASE_USDC_ADDRESS,
                "name": "USD Coin",
                "symbol": "USDC",
            },
            "priceUsd": "0.50",
            "liquidity": {"usd": "50000"},
            "volume": {"h24": "200000", "h6": "50000"},
            "priceChange": {"h24": "8", "h6": "3"},
            "txns": {"h24": {"buys": 120, "sells": 90}},
            "pairCreatedAt": 1704067200000,
            "marketCap": "450000000",
            "fdv": "500000000",
            "boosts": {"active": 0},
        }
        packet = build_packet(
            {
                "contract_address": AERO_ADDRESS,
                "discovery_source": "configured_watchlist",
                "profile_url": None,
                "marketing_influenced": False,
                "promotion_type": None,
            },
            pair,
            NOW - timedelta(seconds=10),
            Decimal("100000"),
            5,
            1,
        )
        packet["is_stale"] = False
        payload = research_payload()
        payload["packets"] = [packet]

        for amount in (Decimal("1"), Decimal("40")):
            with self.subTest(amount=amount):
                runtime = Runtime(
                    (
                        OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("25"), 6),
                        OnchainTokenBalance(AERO_ADDRESS, amount, 18),
                    )
                )
                suffix = str(amount)
                result = run_live_cycle(
                    runtime=runtime,
                    research_payload=payload,
                    universe=universe(),
                    authorized_capital_usdc=Decimal("500"),
                    decision_journal_path=Path(self.temp_dir.name)
                    / f"decisions-{suffix}.jsonl",
                    live_audit_path=Path(self.temp_dir.name) / f"audit-{suffix}.jsonl",
                    risk_journal_path=Path(self.temp_dir.name) / f"risk-{suffix}.jsonl",
                    now=NOW,
                    live_config=replace(load_live_trading_config(), enabled=True),
                    executor_config=ExecutorConfig(
                        mode=EXECUTOR_MODE_CONTROLLED_LIVE,
                        kill_switch_state=KILL_SWITCH_ARMED,
                        max_data_age_seconds=120,
                        max_future_skew_seconds=30,
                    ),
                )

                self.assertEqual(result.status, CYCLE_NO_SIGNAL, result.reason)
                self.assertEqual(result.trading_readiness, "ready")
                self.assertEqual(
                    result.portfolio_value_usdc,
                    Decimal("25") + amount * Decimal("0.50"),
                )
                self.assertEqual(result.held_required, 1)
                self.assertEqual(result.held_covered, 1)
                self.assertEqual(runtime.requests, [])

    def test_historical_live_journal_bootstraps_retained_contract_after_restart(self) -> None:
        reserve_live_execution(
            intent_id="historical-chip",
            intent_fingerprint="f" * 64,
            notional_usdc=Decimal("20"),
            route_id="historical-route",
            wallet_address=AUTHORIZED_TREASURY_ADDRESS,
            chain_id=8453,
            quote_id="historical-quote",
            quote_observed_at=NOW - timedelta(days=1),
            from_token=BASE_USDC_ADDRESS,
            to_token=CHIP_ADDRESS,
            from_amount=Decimal("20"),
            from_decimals=6,
            to_decimals=18,
            slippage_bps=50,
            path=self.audit,
            recorded_at=NOW - timedelta(days=1),
        )
        chip_pair = {
            "chainId": "base",
            "dexId": "uniswap",
            "pairAddress": "0x" + "6" * 40,
            "baseToken": {
                "address": CHIP_ADDRESS,
                "name": "Blue Chip",
                "symbol": "CHIP",
            },
            "quoteToken": {
                "address": BASE_USDC_ADDRESS,
                "name": "USD Coin",
                "symbol": "USDC",
            },
            "priceUsd": "0.02",
            "liquidity": {"usd": "2000000"},
            "volume": {"h24": "300000", "h6": "100000"},
            "priceChange": {"h24": "-1", "h6": "-1"},
            "txns": {"h24": {"buys": 100, "sells": 101}},
            "pairCreatedAt": 1704067200000,
            "marketCap": "1000000",
            "fdv": "1000000",
            "boosts": {"active": 0},
        }
        chip_packet = build_packet(
            {
                "contract_address": CHIP_ADDRESS,
                "discovery_source": "configured_watchlist",
                "profile_url": None,
                "marketing_influenced": False,
                "promotion_type": None,
            },
            chip_pair,
            NOW - timedelta(seconds=10),
            Decimal("100000"),
            5,
            1,
        )
        chip_packet["is_stale"] = False
        aero_packet = research_payload()["packets"][0]
        requested = []

        def exact_research(contracts):
            requested.append(contracts)
            packets = {
                AERO_ADDRESS: aero_packet,
                CHIP_ADDRESS: chip_packet,
            }
            return {
                "service": "lumen-base-research-agent",
                "schema_version": 2,
                "mode": "observation_only",
                "execution": "disabled",
                "generated_at": NOW.isoformat(),
                "packets": [packets[item] for item in contracts if item in packets],
            }

        runtime = Runtime(
            (
                OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("25"), 6),
                OnchainTokenBalance(CHIP_ADDRESS, Decimal("536.5"), 18),
            )
        )
        first_runtime = Runtime(
            runtime.balances
            + (OnchainTokenBalance("0x" + "7" * 40, Decimal("1"), 18),)
        )
        registry = Path(self.temp_dir.name) / "lifecycle.json"
        config = ExecutorConfig(
            mode=EXECUTOR_MODE_SHADOW_ONLY,
            kill_switch_state=KILL_SWITCH_HALTED,
            max_data_age_seconds=120,
            max_future_skew_seconds=30,
        )

        first = run_live_cycle(
            runtime=first_runtime,
            research_payload=exact_research,
            universe=universe(),
            authorized_capital_usdc=Decimal("500"),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            risk_journal_path=self.risk,
            lifecycle_registry_path=registry,
            now=NOW,
            live_config=load_live_trading_config(),
            executor_config=config,
        )
        second = run_live_cycle(
            runtime=runtime,
            research_payload=exact_research,
            universe=universe(),
            authorized_capital_usdc=Decimal("500"),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            risk_journal_path=self.risk,
            lifecycle_registry_path=registry,
            now=NOW + timedelta(minutes=1),
            live_config=load_live_trading_config(),
            executor_config=config,
        )

        self.assertEqual(first.status, CYCLE_POLICY_BLOCKED, first.reason)
        self.assertEqual(first.quarantined_count, 1)
        self.assertEqual(second.status, STATUS_DUPLICATE_BLOCKED, second.reason)
        self.assertEqual(second.portfolio_value_usdc, Decimal("35.730"))
        self.assertIn(CHIP_ADDRESS, requested[-1])
        self.assertEqual(runtime.requests, [])

    def test_stale_or_temporarily_unavailable_research_is_a_completed_block(self) -> None:
        stale = research_payload()
        stale["generated_at"] = (NOW - timedelta(minutes=10)).isoformat()
        config = ExecutorConfig(
            mode=EXECUTOR_MODE_SHADOW_ONLY,
            kill_switch_state=KILL_SWITCH_HALTED,
            max_data_age_seconds=120,
            max_future_skew_seconds=30,
        )
        cases = (
            stale,
            lambda _contracts: (_ for _ in ()).throw(TimeoutError()),
        )

        for payload in cases:
            with self.subTest(payload=type(payload).__name__):
                runtime = Runtime(
                    (
                        OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("25"), 6),
                        OnchainTokenBalance(AERO_ADDRESS, Decimal("10"), 18),
                    )
                )
                result = run_live_cycle(
                    runtime=runtime,
                    research_payload=payload,
                    universe=universe(),
                    authorized_capital_usdc=Decimal("500"),
                    decision_journal_path=self.decisions,
                    live_audit_path=self.audit,
                    risk_journal_path=self.risk,
                    now=NOW,
                    live_config=load_live_trading_config(),
                    executor_config=config,
                )
                self.assertEqual(result.status, CYCLE_VALUATION_BLOCKED)
                self.assertEqual(result.trading_readiness, "blocked")
                self.assertEqual(runtime.requests, [])

    def test_wrong_contract_with_known_symbol_is_quarantined(self) -> None:
        wrong_contract = "0x" + "8" * 40
        runtime = Runtime(
            (
                OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("25"), 6),
                OnchainTokenBalance(wrong_contract, Decimal("10"), 18),
            )
        )

        result = run_live_cycle(
            runtime=runtime,
            research_payload=research_payload(),
            universe=universe(),
            authorized_capital_usdc=Decimal("500"),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            risk_journal_path=self.risk,
            now=NOW,
        )

        self.assertEqual(result.status, CYCLE_POLICY_BLOCKED, result.reason)
        self.assertEqual(result.quarantined_count, 1)
        self.assertEqual(result.portfolio_value_usdc, Decimal("25"))
        self.assertTrue(self.risk.exists())
        self.assertEqual(runtime.requests, [])

    def test_health_stays_200_when_trading_readiness_is_blocked(self) -> None:
        _record_cycle_result(
            LiveCycleResult(
                CYCLE_VALUATION_BLOCKED,
                AUTHORIZED_TREASURY_ADDRESS,
                CDP_NETWORK_ID,
                Decimal("0"),
                "blocked",
                held_required=2,
                held_covered=1,
            ),
            cycle_time=NOW,
            correlation_id="cycle-123",
            safety_gates={
                "live_trading_enabled": False,
                "executor_mode": "shadow_only",
                "kill_switch": "halted",
                "agent_commerce_research": "disabled",
            },
        )
        server = HTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/health",
                timeout=2,
            ) as response:
                payload = __import__("json").load(response)
                self.assertEqual(response.status, 200)
            thread.join(timeout=2)
        finally:
            server.server_close()
        self.assertEqual(payload["operational_status"], "operational")
        self.assertEqual(payload["trading_readiness"], "blocked")
        self.assertEqual(payload["cycle_status"], "valuation_blocked")
        self.assertEqual(payload["last_block_reason"], "blocked")
        self.assertEqual(payload["held_required"], 2)
        self.assertEqual(payload["held_covered"], 1)

    def test_integrity_failure_has_nonblank_safe_structured_diagnostic(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            _record_cycle_failure(
                ValueError(),
                cycle_time=NOW,
                correlation_id="cycle-456",
            )

        diagnostic = __import__("json").loads(output.getvalue())
        self.assertEqual(STATE["operational_status"], "failed")
        self.assertEqual(STATE["trading_readiness"], "blocked")
        self.assertEqual(diagnostic["event"], "live_cycle_failed")
        self.assertEqual(diagnostic["correlation_id"], "cycle-456")
        self.assertTrue(diagnostic["message"].strip())

    def test_armed_cycle_submits_at_most_one_governed_trade(self) -> None:
        unsolicited_contract = "0x" + "7" * 40
        runtime = Runtime(
            (
                OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("500"), 6),
                OnchainTokenBalance(unsolicited_contract, Decimal("1000000"), 18),
            )
        )

        result = self.cycle(
            runtime,
            ExecutorConfig(
                mode=EXECUTOR_MODE_CONTROLLED_LIVE,
                kill_switch_state=KILL_SWITCH_ARMED,
                max_data_age_seconds=120,
                max_future_skew_seconds=30,
            ),
        )

        self.assertEqual(result.status, STATUS_CONFIRMED)
        self.assertEqual(result.quarantined_count, 1)
        self.assertEqual(len(runtime.requests), 1)
        self.assertEqual(runtime.requests[0].from_token, BASE_USDC_ADDRESS)
        self.assertEqual(runtime.requests[0].to_token, AERO_ADDRESS)
        self.assertNotEqual(runtime.requests[0].to_token, unsolicited_contract)
        self.assertEqual(runtime.requests[0].notional_usdc, Decimal("20"))

    def test_armed_cycle_skips_stronger_signal_that_fails_relative_liquidity(self) -> None:
        governed = GovernedAssetUniverse(
            observed_at=NOW - timedelta(minutes=5),
            source="cross-verified-test",
            snapshot_sha256="e" * 64,
            assets=(
                GovernedAsset(
                    rank=1,
                    symbol="THIN",
                    name="Thin Liquidity",
                    token_address=THIN_ADDRESS,
                    decimals=18,
                    market_cap_usd=Decimal("900000000"),
                    liquidity_usd=Decimal("1000000"),
                    daily_volume_usd=Decimal("15000000"),
                    oldest_pool_created_at=NOW - timedelta(days=900),
                ),
                universe().assets[0],
            ),
        )

        def packet(
            address: str,
            symbol: str,
            *,
            liquidity: str,
            h24: str,
            h6: str,
        ) -> dict[str, object]:
            pair = {
                "chainId": "base",
                "dexId": "aerodrome",
                "pairAddress": "0x" + "3" * 40,
                "baseToken": {
                    "address": address,
                    "name": symbol,
                    "symbol": symbol,
                },
                "quoteToken": {
                    "address": BASE_USDC_ADDRESS,
                    "name": "USD Coin",
                    "symbol": "USDC",
                },
                "priceUsd": "0.50",
                "liquidity": {"usd": liquidity},
                "volume": {"h24": "15000000", "h6": "4000000"},
                "priceChange": {"h24": h24, "h6": h6},
                "txns": {"h24": {"buys": 1200, "sells": 900}},
                "pairCreatedAt": 1704067200000,
                "marketCap": "450000000",
                "fdv": "500000000",
                "boosts": {"active": 0},
            }
            result = build_packet(
                {
                    "contract_address": address,
                    "discovery_source": "configured_watchlist",
                    "profile_url": None,
                    "marketing_influenced": False,
                    "promotion_type": None,
                },
                pair,
                NOW - timedelta(seconds=10),
                Decimal("100000"),
                90,
                1,
            )
            result["is_stale"] = False
            return result

        payload = {
            "service": "lumen-base-research-agent",
            "schema_version": 2,
            "mode": "observation_only",
            "execution": "disabled",
            "generated_at": NOW.isoformat(),
            "packets": [
                packet(
                    THIN_ADDRESS,
                    "THIN",
                    liquidity="200000",
                    h24="20",
                    h6="10",
                ),
                packet(
                    AERO_ADDRESS,
                    "AERO",
                    liquidity="25000000",
                    h24="8",
                    h6="3",
                ),
            ],
        }
        runtime = Runtime(
            (OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("500"), 6),)
        )
        config = ExecutorConfig(
            mode=EXECUTOR_MODE_CONTROLLED_LIVE,
            kill_switch_state=KILL_SWITCH_ARMED,
            max_data_age_seconds=120,
            max_future_skew_seconds=30,
        )

        result = run_live_cycle(
            runtime=runtime,
            research_payload=payload,
            universe=governed,
            authorized_capital_usdc=Decimal("500"),
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            risk_journal_path=self.risk,
            now=NOW,
            live_config=replace(load_live_trading_config(), enabled=True),
            executor_config=config,
        )

        self.assertEqual(result.status, STATUS_CONFIRMED)
        self.assertEqual(len(runtime.requests), 1)
        self.assertEqual(runtime.requests[0].to_token, AERO_ADDRESS)

    def test_wrong_wallet_network_or_token_decimals_fail_before_submission(self) -> None:
        cases = []
        wrong_wallet = Runtime(())
        wrong_wallet.wallet_address = "0x" + "9" * 40
        cases.append(wrong_wallet)
        wrong_network = Runtime(())
        wrong_network.network_id = "base-sepolia"
        cases.append(wrong_network)
        cases.append(
            Runtime((OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("25"), 18),))
        )
        config = ExecutorConfig(
            mode=EXECUTOR_MODE_SHADOW_ONLY,
            kill_switch_state=KILL_SWITCH_HALTED,
            max_data_age_seconds=120,
            max_future_skew_seconds=30,
        )

        for number, runtime in enumerate(cases):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    self.cycle(runtime, config)
                self.assertEqual(runtime.requests, [])


if __name__ == "__main__":
    unittest.main()
