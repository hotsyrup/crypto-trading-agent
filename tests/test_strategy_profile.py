import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.controlled_live_execution import NATIVE_ETH_ADDRESS
from app.live_execution_journal import (
    append_live_execution_event,
    read_live_execution_events,
    reserve_live_execution,
)
from app.live_portfolio_worker import (
    CYCLE_POLICY_BLOCKED,
    OnchainTokenBalance,
    _verified_portfolio,
    run_live_cycle,
)
from app.live_trading_config import BASE_USDC_ADDRESS
from app.portfolio_trading import (
    PortfolioPosition,
    ResearchSignal,
    execute_research_portfolio_signal,
)
from app.strategy_profile import (
    CAUTIOUS_PROFILE,
    MEDIUM_HIGH_PROFILE,
    AssetCostBasis,
    ExitOutcome,
    StrategyDecision,
    StrategyObservation,
    StrategyProfileError,
    append_strategy_decision,
    composite_entry_score,
    evaluate_medium_high,
    load_strategy_profile,
    portfolio_cooldown_reason,
    read_strategy_events,
    reconstruct_cost_basis,
)
from tests.test_portfolio_trading import Backend, portfolio, risk, universe
from app.live_trading_config import load_live_trading_config
from app.trading_executor import (
    EXECUTOR_MODE_CONTROLLED_LIVE,
    EXECUTOR_MODE_SHADOW_ONLY,
    KILL_SWITCH_ARMED,
    KILL_SWITCH_HALTED,
    ExecutorConfig,
)
from tests.test_live_portfolio_worker import NOW as WORKER_NOW, Runtime, research_payload


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
AERO_ADDRESS = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"


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


def basis(**updates: object) -> AssetCostBasis:
    value = AssetCostBasis(
        token_address=AERO_ADDRESS,
        confirmed_quantity=Decimal("40"),
        remaining_cost_usdc=Decimal("40"),
        average_entry_price_usdc=Decimal("1"),
        first_entry_at=NOW - timedelta(hours=24),
        last_entry_at=NOW - timedelta(hours=7),
        last_exit_at=None,
        last_failed_at=None,
        confirmed_buy_count=1,
        realized_pl_usdc=Decimal("0"),
        exits=(),
    )
    return replace(value, **updates)


def position(price: str, *, verified: bool = True) -> PortfolioPosition:
    current = Decimal(price)
    return PortfolioPosition(
        symbol="AERO",
        token_address=AERO_ADDRESS,
        token_balance=Decimal("40"),
        value_usdc=Decimal("40") * current,
        average_entry_price_usdc=Decimal("1") if verified else Decimal("0"),
        cost_basis_verified=verified,
    )


class CostBasisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.audit = Path(self.temp.name) / "audit.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def reserve(
        self,
        intent_id: str,
        *,
        side: str,
        amount: Decimal,
        recorded_at: datetime,
        exit_reason: str | None = None,
    ) -> None:
        reserve_live_execution(
            intent_id=intent_id,
            intent_fingerprint=intent_id.ljust(64, "f")[:64],
            notional_usdc=Decimal("20"),
            route_id="test-route",
            wallet_address="0x" + "1" * 40,
            chain_id=8453,
            quote_id=f"quote-{intent_id}",
            quote_observed_at=recorded_at,
            from_token=BASE_USDC_ADDRESS if side == "BUY" else AERO_ADDRESS,
            to_token=AERO_ADDRESS if side == "BUY" else BASE_USDC_ADDRESS,
            from_amount=amount,
            from_decimals=6 if side == "BUY" else 18,
            to_decimals=18 if side == "BUY" else 6,
            slippage_bps=50,
            strategy_profile=MEDIUM_HIGH_PROFILE,
            entry_score=88,
            exit_reason=exit_reason,
            path=self.audit,
            recorded_at=recorded_at,
        )

    def confirm(
        self,
        intent_id: str,
        *,
        from_amount: str,
        to_amount: str,
        recorded_at: datetime,
    ) -> None:
        append_live_execution_event(
            event="CONFIRMED",
            intent_id=intent_id,
            intent_fingerprint=intent_id.ljust(64, "f")[:64],
            details={
                "from_amount": from_amount,
                "to_amount": to_amount,
                "min_to_amount": to_amount,
            },
            path=self.audit,
            recorded_at=recorded_at,
        )

    def test_weighted_basis_and_realized_pl_come_only_from_confirmed_receipts(self) -> None:
        first = NOW - timedelta(days=2)
        second = NOW - timedelta(days=1)
        self.reserve("buy-1", side="BUY", amount=Decimal("20"), recorded_at=first)
        self.confirm("buy-1", from_amount="20", to_amount="40", recorded_at=first)
        self.reserve("buy-2", side="BUY", amount=Decimal("20"), recorded_at=second)
        self.confirm("buy-2", from_amount="20", to_amount="20", recorded_at=second)
        self.reserve(
            "sell-1",
            side="SELL",
            amount=Decimal("30"),
            recorded_at=NOW,
            exit_reason="partial_profit_15",
        )
        self.confirm("sell-1", from_amount="30", to_amount="27", recorded_at=NOW)

        item = reconstruct_cost_basis(path=self.audit)[AERO_ADDRESS]

        self.assertEqual(item.confirmed_quantity, Decimal("30"))
        self.assertEqual(item.remaining_cost_usdc, Decimal("20"))
        self.assertEqual(item.average_entry_price_usdc, Decimal("20") / Decimal("30"))
        self.assertEqual(item.realized_pl_usdc, Decimal("7"))
        self.assertEqual(item.additions, 1)
        self.assertEqual(item.exits[0].exit_reason, "partial_profit_15")

    def test_failed_and_receipt_rejected_reservations_do_not_create_basis(self) -> None:
        for event_name in ("BACKEND_FAILED", "RECEIPT_REJECTED"):
            intent = event_name.lower()
            self.reserve(intent, side="BUY", amount=Decimal("20"), recorded_at=NOW)
            append_live_execution_event(
                event=event_name,
                intent_id=intent,
                intent_fingerprint=intent.ljust(64, "f")[:64],
                details={"error_type": "test"},
                path=self.audit,
                recorded_at=NOW,
            )

        item = reconstruct_cost_basis(path=self.audit)[AERO_ADDRESS]

        self.assertEqual(item.confirmed_quantity, Decimal("0"))
        self.assertEqual(item.remaining_cost_usdc, Decimal("0"))
        self.assertEqual(item.confirmed_buy_count, 0)
        self.assertEqual(item.last_failed_at, NOW)
        self.assertEqual(
            sum(
                Decimal(entry["notional_usdc"])
                for entry in read_live_execution_events(path=self.audit)
                if entry["event"] == "RESERVED"
            ),
            Decimal("40"),
        )

    def test_oversell_and_corrupt_hash_chain_fail_closed(self) -> None:
        self.reserve("buy", side="BUY", amount=Decimal("20"), recorded_at=NOW)
        self.confirm("buy", from_amount="20", to_amount="10", recorded_at=NOW)
        self.reserve("sell", side="SELL", amount=Decimal("11"), recorded_at=NOW)
        self.confirm("sell", from_amount="11", to_amount="19", recorded_at=NOW)

        unresolved = reconstruct_cost_basis(path=self.audit)[AERO_ADDRESS]
        self.assertFalse(unresolved.verified)
        self.assertEqual(unresolved.average_entry_price_usdc, Decimal("0"))

        lines = self.audit.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[0])
        entry["from_amount"] = "999"
        lines[0] = json.dumps(entry)
        self.audit.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "hash"):
            reconstruct_cost_basis(path=self.audit)

    def test_onchain_portfolio_uses_receipt_basis_with_bounded_reconciliation(self) -> None:
        self.reserve("buy", side="BUY", amount=Decimal("20"), recorded_at=NOW)
        self.confirm("buy", from_amount="20", to_amount="40", recorded_at=NOW)
        bases = reconstruct_cost_basis(path=self.audit)
        research = signal(price_usd=Decimal("0.60"))

        portfolio = _verified_portfolio(
            (
                OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("80"), 6),
                OnchainTokenBalance(AERO_ADDRESS, Decimal("40.00001"), 18),
            ),
            (research,),
            universe(),
            wallet_address="0x716b5d6bf67a4c01103b52365c8fb5fdfef0ff06",
            native_gas_reserve_eth=Decimal("0"),
            now=NOW,
            cost_bases=bases,
        )

        self.assertTrue(portfolio.positions[0].cost_basis_verified)
        self.assertEqual(portfolio.positions[0].average_entry_price_usdc, Decimal("0.5"))


class MediumHighStrategyTests(unittest.TestCase):
    def test_profile_flag_is_explicit_and_defaults_to_cautious(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_strategy_profile(), CAUTIOUS_PROFILE)
        with patch.dict(os.environ, {"TRADING_STRATEGY_PROFILE": "medium_high"}):
            self.assertEqual(load_strategy_profile(), MEDIUM_HIGH_PROFILE)
        with patch.dict(os.environ, {"TRADING_STRATEGY_PROFILE": "aggressive"}):
            with self.assertRaises(ValueError):
                load_strategy_profile()

    def test_composite_score_has_exact_seven_bounded_components(self) -> None:
        score, components = composite_entry_score(
            signal(),
            baseline_volume_usd=Decimal("15000000"),
            position_value_usdc=Decimal("0"),
            portfolio_value_usdc=Decimal("500"),
            additions=0,
        )

        self.assertEqual(score, 76)
        self.assertEqual(
            components,
            {
                "momentum_h6": 15,
                "momentum_h24": 17,
                "transaction_imbalance": 6,
                "relative_volume": 8,
                "liquidity_impact": 15,
                "trend_consistency": 10,
                "exposure_history": 5,
            },
        )
        self.assertTrue(all(value >= 0 for value in components.values()))

    def evaluate(
        self,
        market: ResearchSignal,
        *,
        held: PortfolioPosition | None = None,
        inventory: AssetCostBasis | None = None,
        observations: tuple[StrategyObservation, ...] = (),
        all_bases: dict[str, AssetCostBasis] | None = None,
    ):
        return evaluate_medium_high(
            market,
            position=held,
            basis=inventory,
            all_bases=all_bases or ({AERO_ADDRESS: inventory} if inventory else {}),
            baseline_volume_usd=Decimal("15000000"),
            portfolio_value_usdc=Decimal("500"),
            observations=observations,
            now=NOW,
        )

    def test_thresholds_and_mixed_momentum_behavior(self) -> None:
        self.assertEqual(self.evaluate(signal()).action, "buy")
        watch = self.evaluate(
            signal(change_h6_percent=Decimal("1"), change_h24_percent=Decimal("-1"))
        )
        self.assertEqual(watch.classification, "watch")
        self.assertEqual(watch.action, "hold")
        strong_mixed = self.evaluate(
            signal(
                change_h6_percent=Decimal("10"),
                change_h24_percent=Decimal("-1"),
                buys_h24=1800,
                sells_h24=200,
                daily_volume_usd=Decimal("30000000"),
            )
        )
        self.assertGreaterEqual(strong_mixed.entry_score, 70)
        self.assertEqual(strong_mixed.action, "buy")

    def test_additions_require_profit_score_limit_and_cooldown(self) -> None:
        strong = signal(
            price_usd=Decimal("1.10"),
            change_h6_percent=Decimal("10"),
            change_h24_percent=Decimal("20"),
            buys_h24=1800,
            sells_h24=200,
            daily_volume_usd=Decimal("30000000"),
        )
        allowed = self.evaluate(strong, held=position("1.10"), inventory=basis())
        self.assertEqual(allowed.action, "add")
        self.assertGreaterEqual(allowed.entry_score, 85)
        locked = self.evaluate(
            strong,
            held=position("1.10"),
            inventory=basis(last_entry_at=NOW - timedelta(hours=1)),
        )
        self.assertEqual(locked.action, "hold")
        self.assertEqual(locked.exit_reason, "entry_cooldown")
        exhausted = self.evaluate(
            strong,
            held=position("1.10"),
            inventory=basis(confirmed_buy_count=3),
        )
        self.assertEqual(exhausted.action, "hold")

    def test_triple_barrier_and_momentum_exits(self) -> None:
        hard = self.evaluate(
            signal(price_usd=Decimal("0.87"), change_h6_percent=Decimal("-1"), change_h24_percent=Decimal("-2")),
            held=position("0.87"),
            inventory=basis(),
        )
        self.assertEqual((hard.action, hard.exit_reason), ("sell", "hard_stop_loss"))
        self.assertGreaterEqual(hard.stop_loss_percent, Decimal("8"))
        self.assertLessEqual(hard.stop_loss_percent, Decimal("12"))

        partial = self.evaluate(
            signal(price_usd=Decimal("1.16")),
            held=position("1.16"),
            inventory=basis(),
        )
        self.assertEqual(partial.exit_reason, "partial_profit_15")
        self.assertEqual(partial.sell_fraction, Decimal("0.5"))

        final = self.evaluate(
            signal(price_usd=Decimal("1.26")),
            held=position("1.26"),
            inventory=basis(exits=(ExitOutcome(NOW - timedelta(hours=2), Decimal("2"), "partial_profit_15"),)),
        )
        self.assertEqual(final.exit_reason, "final_profit_target_25")

        momentum = self.evaluate(
            signal(price_usd=Decimal("1.02"), change_h6_percent=Decimal("-1"), change_h24_percent=Decimal("-1")),
            held=position("1.02"),
            inventory=basis(),
        )
        self.assertEqual(momentum.exit_reason, "dual_momentum_reversal")

    def test_trailing_two_cycle_and_stagnation_exits(self) -> None:
        observations = (
            StrategyObservation(NOW - timedelta(hours=2), "1" * 64, AERO_ADDRESS, Decimal("1.10"), Decimal("15000000"), 50),
            StrategyObservation(NOW - timedelta(hours=1), "2" * 64, AERO_ADDRESS, Decimal("1.20"), Decimal("15000000"), 30),
        )
        trailing = self.evaluate(
            signal(price_usd=Decimal("1.10"), change_h6_percent=Decimal("1"), change_h24_percent=Decimal("2")),
            held=position("1.10"),
            inventory=basis(),
            observations=observations,
        )
        self.assertEqual(trailing.exit_reason, "trailing_profit_stop")

        breakdown = self.evaluate(
            signal(price_usd=Decimal("1.01"), change_h6_percent=Decimal("-5"), change_h24_percent=Decimal("0"), buys_h24=0, sells_h24=100, daily_volume_usd=Decimal("0"), liquidity_usd=Decimal("25000")),
            held=position("1.01"),
            inventory=basis(),
            observations=(StrategyObservation(NOW - timedelta(minutes=1), "3" * 64, AERO_ADDRESS, Decimal("1.01"), Decimal("100000"), 20),),
        )
        self.assertEqual(breakdown.exit_reason, "two_cycle_score_breakdown")

        stagnant = self.evaluate(
            signal(price_usd=Decimal("1.01"), change_h6_percent=Decimal("0.1"), change_h24_percent=Decimal("0.2")),
            held=position("1.01"),
            inventory=basis(first_entry_at=NOW - timedelta(hours=73)),
        )
        self.assertEqual(stagnant.exit_reason, "stagnant_72h_exit")

    def test_unverified_basis_and_dust_never_trigger_irrational_exit(self) -> None:
        unverified = self.evaluate(
            signal(price_usd=Decimal("0.50"), change_h6_percent=Decimal("-5"), change_h24_percent=Decimal("-10")),
            held=position("0.50", verified=False),
            inventory=None,
        )
        self.assertEqual((unverified.action, unverified.exit_reason), ("hold", "cost_basis_unverified"))
        dust = PortfolioPosition(
            "AERO", AERO_ADDRESS, Decimal("1"), Decimal("1.50"), Decimal("2"), True
        )
        suppressed = self.evaluate(
            signal(price_usd=Decimal("1.50"), change_h6_percent=Decimal("-1"), change_h24_percent=Decimal("-1")),
            held=dust,
            inventory=basis(confirmed_quantity=Decimal("1"), remaining_cost_usdc=Decimal("2"), average_entry_price_usdc=Decimal("2")),
        )
        self.assertEqual(suppressed.exit_reason, "dust_exit_suppressed")

    def test_portfolio_and_asset_loss_guards(self) -> None:
        exits = tuple(
            ExitOutcome(NOW - timedelta(hours=3 - index), Decimal("-1"), "hard_stop_loss")
            for index in range(3)
        )
        losing = basis(exits=exits, last_exit_at=NOW - timedelta(hours=1))
        self.assertEqual(
            portfolio_cooldown_reason({AERO_ADDRESS: losing}, now=NOW),
            "consecutive_loss_guard",
        )
        decision = self.evaluate(signal(), inventory=losing)
        self.assertEqual(decision.action, "hold")


class StrategyJournalTests(unittest.TestCase):
    def test_restart_history_is_hash_chained_and_duplicate_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.jsonl"
            decision = StrategyDecision(
                MEDIUM_HIGH_PROFILE,
                76,
                {"momentum_h6": 15},
                "entry",
                "buy",
            )
            self.assertTrue(
                append_strategy_decision(
                    signal=signal(), decision=decision, path=path, recorded_at=NOW
                )
            )
            self.assertFalse(
                append_strategy_decision(
                    signal=signal(), decision=decision, path=path, recorded_at=NOW
                )
            )
            self.assertEqual(len(read_strategy_events(path=path)), 1)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["entry_score"] = 99
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(StrategyProfileError, "hash"):
                read_strategy_events(path=path)

    def test_medium_high_execution_audits_profile_score_and_keeps_hard_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "decisions.jsonl"
            audit = root / "audit.jsonl"
            strategy = root / "strategy.jsonl"
            backend = Backend()
            live = replace(load_live_trading_config(), enabled=True)
            executor = ExecutorConfig(
                mode=EXECUTOR_MODE_CONTROLLED_LIVE,
                kill_switch_state=KILL_SWITCH_ARMED,
                max_data_age_seconds=120,
                max_future_skew_seconds=30,
            )

            result = execute_research_portfolio_signal(
                signal(observed_at=NOW - timedelta(seconds=10)),
                replace(portfolio(), observed_at=NOW - timedelta(seconds=5)),
                replace(risk(), observed_at=NOW - timedelta(seconds=5)),
                replace(universe(), observed_at=NOW - timedelta(minutes=5)),
                backend,
                decision_journal_path=decisions,
                live_audit_path=audit,
                now=NOW,
                live_config=live,
                executor_config=executor,
                strategy_profile=MEDIUM_HIGH_PROFILE,
                cost_bases={},
                strategy_journal_path=strategy,
            )

            self.assertEqual(result.status, "CONFIRMED")
            self.assertEqual(backend.requests[0].notional_usdc, Decimal("20"))
            reservation = read_live_execution_events(path=audit)[0]
            self.assertEqual(reservation["strategy_profile"], MEDIUM_HIGH_PROFILE)
            self.assertEqual(reservation["entry_score"], 76)
            self.assertIsNone(reservation["exit_reason"])
            evaluated = read_strategy_events(path=strategy)[0]
            self.assertEqual(evaluated["action"], "buy")
            self.assertEqual(evaluated["entry_score"], 76)

    def test_parallel_shadow_journals_both_profiles_without_extra_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strategy = root / "strategy.jsonl"
            runtime = Runtime(
                (OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("100"), 6),)
            )
            config = ExecutorConfig(
                mode=EXECUTOR_MODE_SHADOW_ONLY,
                kill_switch_state=KILL_SWITCH_HALTED,
                max_data_age_seconds=120,
                max_future_skew_seconds=30,
            )

            result = run_live_cycle(
                runtime=runtime,
                research_payload=research_payload(),
                universe=replace(
                    universe(), observed_at=WORKER_NOW - timedelta(minutes=5)
                ),
                authorized_capital_usdc=Decimal("500"),
                decision_journal_path=root / "decisions.jsonl",
                live_audit_path=root / "audit.jsonl",
                risk_journal_path=root / "risk.jsonl",
                lifecycle_registry_path=root / "lifecycle.json",
                strategy_journal_path=strategy,
                strategy_profile=CAUTIOUS_PROFILE,
                parallel_shadow=True,
                now=WORKER_NOW,
                live_config=load_live_trading_config(),
                executor_config=config,
            )

            self.assertEqual(result.status, CYCLE_POLICY_BLOCKED)
            self.assertEqual(runtime.requests, [])
            profiles = {item["profile"] for item in read_strategy_events(path=strategy)}
            self.assertEqual(profiles, {CAUTIOUS_PROFILE, MEDIUM_HIGH_PROFILE})

    def test_medium_high_primary_cycle_records_each_packet_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strategy = root / "strategy.jsonl"
            for index, volume in enumerate(("1000000", "2000000"), start=1):
                append_strategy_decision(
                    signal=signal(
                        packet_id=str(index) * 64,
                        observed_at=WORKER_NOW - timedelta(minutes=4 - index),
                        daily_volume_usd=Decimal(volume),
                    ),
                    decision=StrategyDecision(
                        MEDIUM_HIGH_PROFILE,
                        60,
                        {"relative_volume": 0},
                        "watch",
                        "hold",
                    ),
                    path=strategy,
                    recorded_at=WORKER_NOW - timedelta(minutes=4 - index),
                )
            runtime = Runtime(
                (OnchainTokenBalance(BASE_USDC_ADDRESS, Decimal("100"), 6),)
            )
            config = ExecutorConfig(
                mode=EXECUTOR_MODE_SHADOW_ONLY,
                kill_switch_state=KILL_SWITCH_HALTED,
                max_data_age_seconds=120,
                max_future_skew_seconds=30,
            )

            results = []
            for offset in (0, 60):
                results.append(
                    run_live_cycle(
                        runtime=runtime,
                        research_payload=research_payload(),
                        universe=replace(
                            universe(),
                            observed_at=WORKER_NOW - timedelta(minutes=5),
                        ),
                        authorized_capital_usdc=Decimal("500"),
                        decision_journal_path=root / "decisions.jsonl",
                        live_audit_path=root / "audit.jsonl",
                        risk_journal_path=root / "risk.jsonl",
                        lifecycle_registry_path=root / "lifecycle.json",
                        strategy_journal_path=strategy,
                        strategy_profile=MEDIUM_HIGH_PROFILE,
                        now=WORKER_NOW + timedelta(seconds=offset),
                        live_config=load_live_trading_config(),
                        executor_config=config,
                    )
                )

            self.assertTrue(all(result.status for result in results), results)
            self.assertFalse(
                any("reused with different" in result.reason for result in results),
                results,
            )
            self.assertEqual(runtime.requests, [])
            self.assertEqual(len(read_strategy_events(path=strategy)), 3)


if __name__ == "__main__":
    unittest.main()
