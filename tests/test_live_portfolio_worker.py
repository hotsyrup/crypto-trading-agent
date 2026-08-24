import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.base_asset_universe import GovernedAsset, GovernedAssetUniverse
from app.controlled_live_execution import (
    CDP_NETWORK_ID,
    PERMIT2_ADDRESS,
    STATUS_CONFIRMED,
    SwapReceipt,
)
from app.live_portfolio_worker import (
    CYCLE_NO_FUNDS,
    CYCLE_POLICY_BLOCKED,
    OnchainTokenBalance,
    run_live_cycle,
)
from app.live_trading_config import BASE_USDC_ADDRESS, load_live_trading_config
from app.research_agent import build_packet
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    EXECUTOR_MODE_CONTROLLED_LIVE,
    EXECUTOR_MODE_SHADOW_ONLY,
    KILL_SWITCH_ARMED,
    KILL_SWITCH_HALTED,
    ExecutorConfig,
)


NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
AERO_ADDRESS = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"


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
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.decisions = root / "decisions.jsonl"
        self.audit = root / "audit.jsonl"
        self.risk = root / "risk.jsonl"

    def tearDown(self) -> None:
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

    def test_armed_cycle_submits_at_most_one_governed_trade(self) -> None:
        runtime = Runtime(
            (OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("500"), 6),)
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
        self.assertEqual(len(runtime.requests), 1)
        self.assertEqual(runtime.requests[0].from_token, BASE_USDC_ADDRESS)
        self.assertEqual(runtime.requests[0].to_token, AERO_ADDRESS)
        self.assertEqual(runtime.requests[0].notional_usdc, Decimal("20"))

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
