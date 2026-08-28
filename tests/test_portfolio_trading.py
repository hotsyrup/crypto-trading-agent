import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.base_asset_universe import GovernedAsset, GovernedAssetUniverse
from app.agent_commerce_research import (
    AgentCommerceResearchGate,
    PreparedResearchPayment,
    PurchasedResearch,
)
from app.controlled_live_execution import (
    CDP_NETWORK_ID,
    PERMIT2_ADDRESS,
    STATUS_CONFIRMED,
    STATUS_POLICY_REJECTED,
    SwapReceipt,
)
from app.live_trading_config import BASE_USDC_ADDRESS, load_live_trading_config
from app.portfolio_trading import (
    PortfolioPosition,
    ResearchSignal,
    VerifiedPortfolio,
    execute_research_portfolio_signal,
    research_signal_from_packet,
)
from app.research_agent import build_packet
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    EXECUTOR_MODE_CONTROLLED_LIVE,
    KILL_SWITCH_ARMED,
    ExecutorConfig,
    RiskSnapshot,
)


NOW = datetime(2026, 8, 24, 5, 0, tzinfo=timezone.utc)
AERO_ADDRESS = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"


def universe() -> GovernedAssetUniverse:
    return GovernedAssetUniverse(
        observed_at=NOW - timedelta(minutes=10),
        source="reviewed-market-snapshot",
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


def signal(**updates: object) -> ResearchSignal:
    value = ResearchSignal(
        packet_id="a" * 64,
        observed_at=NOW - timedelta(seconds=10),
        symbol="AERO",
        token_address=AERO_ADDRESS,
        price_usd=Decimal("0.50"),
        liquidity_usd=Decimal("25000000"),
        daily_volume_usd=Decimal("15000000"),
        change_h6_percent=Decimal("3"),
        change_h24_percent=Decimal("8"),
        buys_h24=1200,
        sells_h24=900,
    )
    return replace(value, **updates)


def portfolio(*positions: PortfolioPosition) -> VerifiedPortfolio:
    return VerifiedPortfolio(
        observed_at=NOW - timedelta(seconds=5),
        treasury_address=AUTHORIZED_TREASURY_ADDRESS,
        total_value_usdc=Decimal("500"),
        usdc_balance=Decimal("500") - sum(item.value_usdc for item in positions),
        positions=tuple(positions),
    )


def risk() -> RiskSnapshot:
    return RiskSnapshot(
        daily_loss_percent=Decimal("0"),
        drawdown_percent=Decimal("0"),
        observed_at=NOW - timedelta(seconds=5),
        trading_capital_usdc=Decimal("500"),
    )


class Backend:
    def __init__(self) -> None:
        self.requests = []

    def submit_swap(self, request):
        self.requests.append(request)
        return SwapReceipt(
            success=True,
            transaction_hash="0x" + f"{len(self.requests):064x}",
            quote_id=f"provider-{request.quote_id}",
            wallet_address=request.wallet_address,
            network_id=CDP_NETWORK_ID,
            from_token=request.from_token,
            to_token=request.to_token,
            from_amount=request.from_amount,
            to_amount=(
                request.notional_usdc
                if request.to_token == BASE_USDC_ADDRESS
                else request.notional_usdc / Decimal("0.50")
            ),
            min_to_amount=(
                request.notional_usdc * Decimal("0.995")
                if request.to_token == BASE_USDC_ADDRESS
                else request.notional_usdc / Decimal("0.50") * Decimal("0.995")
            ),
            slippage_bps=request.slippage_bps,
            approval_transaction_hash="0x" + "b" * 64,
            approval_token=request.from_token,
            approval_spender=PERMIT2_ADDRESS,
            approval_amount=request.from_amount,
        )


class FavorableResearchProvider:
    def __init__(self) -> None:
        self.pay_calls = 0

    def prepare(self, candidate):
        return PreparedResearchPayment({}, object())

    def pay(self, prepared, *, attempt_id, now):
        self.pay_calls += 1
        return PurchasedResearch(
            {
                "report_id": "favorable-report",
                "report": {
                    "as_of": now.date().isoformat(),
                    "verdict": "consider",
                    "thesis_status": "supported",
                    "confidence": "medium",
                    "red_flags": [],
                },
            },
            "0x" + "c" * 64,
        )


class PortfolioTradingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.decisions = root / "decisions.jsonl"
        self.audit = root / "live.jsonl"
        self.live_config = replace(load_live_trading_config(), enabled=True)
        self.executor_config = ExecutorConfig(
            mode=EXECUTOR_MODE_CONTROLLED_LIVE,
            kill_switch_state=KILL_SWITCH_ARMED,
            max_data_age_seconds=120,
            max_future_skew_seconds=30,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def execute(self, research, holdings, backend, risk_snapshot=None):
        return execute_research_portfolio_signal(
            research,
            holdings,
            risk_snapshot or risk(),
            universe(),
            backend,
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            now=NOW,
            live_config=self.live_config,
            executor_config=self.executor_config,
        )

    def test_positive_ranked_research_buys_exactly_20_usdc_of_asset(self) -> None:
        backend = Backend()
        result = self.execute(signal(), portfolio(), backend)

        self.assertEqual(result.status, STATUS_CONFIRMED)
        self.assertEqual(len(backend.requests), 1)
        request = backend.requests[0]
        self.assertEqual(request.from_token, BASE_USDC_ADDRESS)
        self.assertEqual(request.to_token, AERO_ADDRESS)
        self.assertEqual(request.from_amount, Decimal("20"))
        self.assertEqual(request.notional_usdc, Decimal("20"))

    def test_buy_notional_rounds_down_to_official_usdc_precision(self) -> None:
        total = Decimal("29.9803035152")
        holdings = replace(
            portfolio(),
            total_value_usdc=total,
            usdc_balance=Decimal("25"),
        )
        snapshot = replace(
            risk(),
            trading_capital_usdc=total,
            portfolio_value_usdc=total,
        )
        backend = Backend()

        result = self.execute(signal(), holdings, backend, snapshot)

        self.assertEqual(result.status, STATUS_CONFIRMED)
        self.assertEqual(backend.requests[0].notional_usdc, Decimal("1.499015"))
        self.assertEqual(backend.requests[0].from_amount, Decimal("1.499015"))

    def test_observation_only_research_packet_becomes_non_authoritative_signal(self) -> None:
        pair = {
            "chainId": "base",
            "dexId": "aerodrome",
            "pairAddress": "0x" + "1" * 40,
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

        research = research_signal_from_packet(packet, universe(), now=NOW)

        self.assertEqual(research.symbol, "AERO")
        self.assertEqual(research.token_address, AERO_ADDRESS)
        self.assertEqual(research.change_h6_percent, Decimal("3"))
        self.assertEqual(research.packet_id, packet["packet_id"])

    def test_malformed_numeric_research_packet_fails_closed(self) -> None:
        pair = {
            "chainId": "base",
            "dexId": "aerodrome",
            "pairAddress": "0x" + "1" * 40,
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
        packet["metrics"]["price_usd"] = "not-a-number"
        canonical = {
            key: value
            for key, value in packet.items()
            if key not in {"packet_id", "is_stale"}
        }
        import hashlib
        import json

        packet["packet_id"] = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        packet["is_stale"] = False

        with self.assertRaisesRegex(ValueError, "metrics are invalid"):
            research_signal_from_packet(packet, universe(), now=NOW)

    def test_negative_ranked_research_sells_position_back_to_usdc(self) -> None:
        backend = Backend()
        position = PortfolioPosition(
            symbol="AERO",
            token_address=AERO_ADDRESS,
            token_balance=Decimal("40"),
            value_usdc=Decimal("20"),
            average_entry_price_usdc=Decimal("0.60"),
        )
        result = self.execute(
            signal(change_h6_percent=Decimal("-4"), change_h24_percent=Decimal("-9")),
            portfolio(position),
            backend,
        )

        self.assertEqual(result.status, STATUS_CONFIRMED)
        request = backend.requests[0]
        self.assertEqual(request.from_token, AERO_ADDRESS)
        self.assertEqual(request.to_token, BASE_USDC_ADDRESS)
        self.assertEqual(request.from_amount, Decimal("40"))

    def test_stale_unknown_or_mixed_research_never_reaches_wallet(self) -> None:
        cases = (
            signal(observed_at=NOW - timedelta(seconds=121)),
            signal(token_address="0x" + "9" * 40),
            signal(change_h6_percent=Decimal("1"), change_h24_percent=Decimal("-1")),
        )
        for number, research in enumerate(cases):
            with self.subTest(number=number):
                backend = Backend()
                result = self.execute(research, portfolio(), backend)
                self.assertEqual(result.status, STATUS_POLICY_REJECTED)
                self.assertEqual(backend.requests, [])

    def test_universe_that_ages_out_in_memory_never_reaches_wallet(self) -> None:
        backend = Backend()
        stale_universe = replace(
            universe(),
            observed_at=NOW - timedelta(hours=24, seconds=1),
        )

        result = execute_research_portfolio_signal(
            signal(),
            portfolio(),
            risk(),
            stale_universe,
            backend,
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            now=NOW,
            live_config=self.live_config,
            executor_config=self.executor_config,
        )

        self.assertEqual(result.status, STATUS_POLICY_REJECTED)
        self.assertIn("universe is stale", " ".join(result.reasons).lower())
        self.assertEqual(backend.requests, [])

    def test_non_candidate_never_requests_paid_research(self) -> None:
        provider = FavorableResearchProvider()
        gate = AgentCommerceResearchGate(
            mode="enforced",
            provider=provider,
            journal_path=Path(self.temp_dir.name) / "research.jsonl",
        )
        backend = Backend()

        result = execute_research_portfolio_signal(
            signal(change_h6_percent=Decimal("1"), change_h24_percent=Decimal("-1")),
            portfolio(),
            risk(),
            universe(),
            backend,
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            now=NOW,
            live_config=self.live_config,
            executor_config=self.executor_config,
            agent_commerce_research_gate=gate,
        )

        self.assertEqual(result.status, STATUS_POLICY_REJECTED)
        self.assertEqual(provider.pay_calls, 0)
        self.assertEqual(backend.requests, [])

    def test_favorable_research_cannot_override_existing_execution_halt(self) -> None:
        provider = FavorableResearchProvider()
        gate = AgentCommerceResearchGate(
            mode="enforced",
            provider=provider,
            journal_path=Path(self.temp_dir.name) / "research.jsonl",
        )
        backend = Backend()
        halted = replace(self.executor_config, kill_switch_state="halted")

        result = execute_research_portfolio_signal(
            signal(),
            portfolio(),
            risk(),
            universe(),
            backend,
            decision_journal_path=self.decisions,
            live_audit_path=self.audit,
            now=NOW,
            live_config=self.live_config,
            executor_config=halted,
            agent_commerce_research_gate=gate,
        )

        self.assertEqual(provider.pay_calls, 1)
        self.assertEqual(result.status, STATUS_POLICY_REJECTED)
        self.assertEqual(backend.requests, [])


if __name__ == "__main__":
    unittest.main()
