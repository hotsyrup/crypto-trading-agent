import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from app.strategy_backtest import (
    run_regime_suite,
    run_strategy_backtest,
    synthetic_regime,
)
from app.strategy_profile import CAUTIOUS_PROFILE, MEDIUM_HIGH_PROFILE


class StrategyBacktestTests(unittest.TestCase):
    def test_regime_suite_compares_both_profiles_with_execution_friction(self) -> None:
        results = run_regime_suite()

        self.assertEqual(len(results), 8)
        by_key = {(item.scenario, item.profile): item for item in results}
        for regime in ("bullish", "bearish", "sideways", "high_volatility"):
            self.assertIn((regime, CAUTIOUS_PROFILE), by_key)
            self.assertIn((regime, MEDIUM_HIGH_PROFILE), by_key)
        self.assertGreater(
            sum(item.eligible_signals for item in results if item.profile == MEDIUM_HIGH_PROFILE),
            sum(item.eligible_signals for item in results if item.profile == CAUTIOUS_PROFILE),
        )
        self.assertTrue(any(item.stale_rejections for item in results))
        self.assertTrue(any(item.failed_routes for item in results))
        self.assertTrue(any(item.partial_fills for item in results))
        self.assertTrue(all(item.slippage_usdc >= 0 for item in results))
        self.assertTrue(all(item.gas_usdc >= 0 for item in results))
        self.assertTrue(all(item.max_drawdown_percent < Decimal("20") for item in results))

    def test_backtest_has_no_future_bar_dependency(self) -> None:
        full_bars = synthetic_regime("high_volatility")
        prefix_bars = full_bars[:48]

        full = run_strategy_backtest(
            full_bars,
            scenario="full",
            profile=MEDIUM_HIGH_PROFILE,
        )
        prefix = run_strategy_backtest(
            prefix_bars,
            scenario="prefix",
            profile=MEDIUM_HIGH_PROFILE,
        )

        self.assertEqual(full.trace[:48], prefix.trace)

    def test_results_are_deterministic_and_indicators_are_not_hair_triggered(self) -> None:
        bars = synthetic_regime("bullish")
        first = run_strategy_backtest(
            bars,
            scenario="base",
            profile=MEDIUM_HIGH_PROFILE,
        )
        second = run_strategy_backtest(
            bars,
            scenario="base",
            profile=MEDIUM_HIGH_PROFILE,
        )
        perturbed = tuple(
            replace(item, price_usd=item.price_usd * Decimal("1.001"))
            for item in bars
        )
        shifted = run_strategy_backtest(
            perturbed,
            scenario="perturbed",
            profile=MEDIUM_HIGH_PROFILE,
        )

        self.assertEqual(first, second)
        self.assertLessEqual(abs(first.eligible_signals - shifted.eligible_signals), 2)
        self.assertEqual(len({item.observed_at for item in first.trace}), len(first.trace))

    def test_invalid_ordering_and_too_short_periods_fail_closed(self) -> None:
        bars = synthetic_regime("sideways")
        with self.assertRaises(ValueError):
            run_strategy_backtest(
                bars[:4], scenario="short", profile=MEDIUM_HIGH_PROFILE
            )
        duplicated = list(bars[:5])
        duplicated[1] = replace(
            duplicated[1], observed_at=duplicated[0].observed_at
        )
        with self.assertRaises(ValueError):
            run_strategy_backtest(
                tuple(duplicated),
                scenario="duplicate-time",
                profile=MEDIUM_HIGH_PROFILE,
            )


if __name__ == "__main__":
    unittest.main()
