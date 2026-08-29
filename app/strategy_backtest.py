from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.portfolio_trading import PortfolioPosition, ResearchSignal
from app.strategy_profile import (
    CAUTIOUS_PROFILE,
    MEDIUM_HIGH_PROFILE,
    AssetCostBasis,
    ExitOutcome,
    StrategyObservation,
    evaluate_medium_high,
)


BACKTEST_ASSET = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
MAX_TRADE_USDC = Decimal("20")
MAX_POSITION_PERCENT = Decimal("20")
MAX_INITIAL_PERCENT = Decimal("5")
DAILY_LOSS_PERCENT = Decimal("5")
MAX_DRAWDOWN_PERCENT = Decimal("20")
GAS_USDC = Decimal("0.03")
BASE_SLIPPAGE_BPS = Decimal("5")


@dataclass(frozen=True)
class MarketBar:
    observed_at: datetime
    price_usd: Decimal
    change_h6_percent: Decimal
    change_h24_percent: Decimal
    buys_h24: int
    sells_h24: int
    volume_h24_usd: Decimal
    baseline_volume_usd: Decimal
    liquidity_usd: Decimal
    stale: bool = False
    route_available: bool = True
    fill_fraction: Decimal = Decimal("1")


@dataclass(frozen=True)
class BacktestTrace:
    observed_at: datetime
    action: str
    reason: str
    score: int | None


@dataclass(frozen=True)
class StrategyBacktestResult:
    scenario: str
    profile: str
    starting_value_usdc: Decimal
    ending_value_usdc: Decimal
    realized_pl_usdc: Decimal
    max_drawdown_percent: Decimal
    entries: int
    additions: int
    exits: int
    eligible_signals: int
    rejected_signals: int
    stale_rejections: int
    failed_routes: int
    partial_fills: int
    turnover_usdc: Decimal
    slippage_usdc: Decimal
    gas_usdc: Decimal
    trace: tuple[BacktestTrace, ...]


def _signal(bar: MarketBar, index: int) -> ResearchSignal:
    return ResearchSignal(
        packet_id=f"{index:064x}",
        observed_at=bar.observed_at,
        symbol="AERO",
        token_address=BACKTEST_ASSET,
        price_usd=bar.price_usd,
        liquidity_usd=bar.liquidity_usd,
        daily_volume_usd=bar.volume_h24_usd,
        change_h6_percent=bar.change_h6_percent,
        change_h24_percent=bar.change_h24_percent,
        buys_h24=bar.buys_h24,
        sells_h24=bar.sells_h24,
    )


def _basis(
    *,
    quantity: Decimal,
    cost: Decimal,
    first_entry: datetime | None,
    last_entry: datetime | None,
    last_exit: datetime | None,
    buy_count: int,
    realized: Decimal,
    exits: tuple[ExitOutcome, ...],
) -> AssetCostBasis | None:
    if buy_count == 0 and not exits:
        return None
    return AssetCostBasis(
        token_address=BACKTEST_ASSET,
        confirmed_quantity=quantity,
        remaining_cost_usdc=cost,
        average_entry_price_usdc=(cost / quantity if quantity > 0 else Decimal("0")),
        first_entry_at=first_entry,
        last_entry_at=last_entry,
        last_exit_at=last_exit,
        last_failed_at=None,
        confirmed_buy_count=buy_count,
        realized_pl_usdc=realized,
        exits=exits,
    )


def run_strategy_backtest(
    bars: tuple[MarketBar, ...],
    *,
    scenario: str,
    profile: str,
    starting_value_usdc: Decimal = Decimal("100"),
) -> StrategyBacktestResult:
    if profile not in {CAUTIOUS_PROFILE, MEDIUM_HIGH_PROFILE}:
        raise ValueError("Backtest profile is unsupported.")
    if len(bars) < 5:
        raise ValueError("Backtest requires at least five chronological bars.")
    if any(
        bars[index].observed_at <= bars[index - 1].observed_at
        for index in range(1, len(bars))
    ):
        raise ValueError("Backtest bars must be strictly chronological.")

    cash = starting_value_usdc
    quantity = Decimal("0")
    cost = Decimal("0")
    first_entry = None
    last_entry = None
    last_exit = None
    buy_count = 0
    realized = Decimal("0")
    outcomes: tuple[ExitOutcome, ...] = ()
    observations: tuple[StrategyObservation, ...] = ()
    high_water = starting_value_usdc
    day_start = starting_value_usdc
    current_day = bars[0].observed_at.date()
    max_drawdown = Decimal("0")
    entries = additions = exits = eligible = rejected = 0
    stale_rejections = failed_routes = partial_fills = 0
    turnover = slippage_cost = gas_cost = Decimal("0")
    trace: list[BacktestTrace] = []

    for index, bar in enumerate(bars):
        if bar.price_usd <= 0 or bar.liquidity_usd <= 0:
            raise ValueError("Backtest market values must be positive.")
        value_before = cash + quantity * bar.price_usd
        if bar.observed_at.date() != current_day:
            current_day = bar.observed_at.date()
            day_start = value_before
        high_water = max(high_water, value_before)
        drawdown = (
            (high_water - value_before) / high_water * Decimal("100")
            if value_before < high_water
            else Decimal("0")
        )
        max_drawdown = max(max_drawdown, drawdown)
        daily_loss = (
            (day_start - value_before) / day_start * Decimal("100")
            if value_before < day_start
            else Decimal("0")
        )
        market = _signal(bar, index)
        if bar.stale:
            stale_rejections += 1
            rejected += 1
            trace.append(BacktestTrace(bar.observed_at, "hold", "stale_data", None))
            continue
        held = (
            PortfolioPosition(
                "AERO",
                BACKTEST_ASSET,
                quantity,
                quantity * bar.price_usd,
                cost / quantity,
                True,
            )
            if quantity > 0
            else None
        )
        inventory = _basis(
            quantity=quantity,
            cost=cost,
            first_entry=first_entry,
            last_entry=last_entry,
            last_exit=last_exit,
            buy_count=buy_count,
            realized=realized,
            exits=outcomes,
        )
        score = None
        reason = "no_signal"
        if profile == MEDIUM_HIGH_PROFILE:
            decision = evaluate_medium_high(
                market,
                position=held,
                basis=inventory,
                all_bases=({BACKTEST_ASSET: inventory} if inventory else {}),
                baseline_volume_usd=bar.baseline_volume_usd,
                portfolio_value_usdc=value_before,
                observations=observations,
                now=bar.observed_at,
            )
            action = decision.action
            score = decision.entry_score
            reason = decision.exit_reason or decision.classification
            sell_fraction = decision.sell_fraction
            observations = observations + (
                StrategyObservation(
                    bar.observed_at,
                    market.packet_id,
                    BACKTEST_ASSET,
                    bar.price_usd,
                    bar.volume_h24_usd,
                    decision.entry_score,
                ),
            )
        else:
            buy = (
                bar.change_h6_percent > 0
                and bar.change_h24_percent > 0
                and bar.buys_h24 > bar.sells_h24
            )
            average = cost / quantity if quantity > 0 else Decimal("0")
            stop_or_profit = quantity > 0 and (
                bar.price_usd <= average * Decimal("0.92")
                or bar.price_usd >= average * Decimal("1.15")
            )
            sell = quantity > 0 and (
                (bar.change_h6_percent < 0 and bar.change_h24_percent < 0)
                or stop_or_profit
            )
            action = "sell" if sell else "buy" if buy and quantity == 0 else "hold"
            sell_fraction = Decimal("1") if sell else Decimal("0")
            reason = "cautious_rule" if action != "hold" else "no_signal"

        if drawdown >= MAX_DRAWDOWN_PERCENT:
            action, reason = "hold", "drawdown_halt"
        elif action in {"buy", "add"} and daily_loss >= DAILY_LOSS_PERCENT:
            action, reason = "hold", "daily_loss_purchase_halt"
        if action in {"buy", "add", "sell"}:
            eligible += 1
        else:
            rejected += 1
            trace.append(BacktestTrace(bar.observed_at, action, reason, score))
            continue
        if not bar.route_available:
            failed_routes += 1
            trace.append(BacktestTrace(bar.observed_at, "hold", "failed_route", score))
            continue

        fill = min(Decimal("1"), max(Decimal("0"), bar.fill_fraction))
        if fill < 1:
            partial_fills += 1
        impact_bps = min(
            Decimal("95"),
            MAX_TRADE_USDC / bar.liquidity_usd * Decimal("10000"),
        )
        total_slippage_bps = min(Decimal("100"), BASE_SLIPPAGE_BPS + impact_bps)
        if action in {"buy", "add"}:
            position_value = quantity * bar.price_usd
            room = value_before * MAX_POSITION_PERCENT / Decimal("100") - position_value
            initial_limit = (
                value_before * MAX_INITIAL_PERCENT / Decimal("100")
                if quantity == 0
                else MAX_TRADE_USDC
            )
            requested = min(
                MAX_TRADE_USDC,
                max(Decimal("0"), cash - GAS_USDC),
                room,
                initial_limit,
            )
            executed = max(Decimal("0"), requested * fill)
            if executed <= GAS_USDC:
                trace.append(BacktestTrace(bar.observed_at, "hold", "uneconomic_fill", score))
                continue
            adverse_price = bar.price_usd * (
                Decimal("1") + total_slippage_bps / Decimal("10000")
            )
            acquired = executed / adverse_price
            slippage = acquired * (adverse_price - bar.price_usd)
            was_open = quantity > 0
            cash -= executed + GAS_USDC
            quantity += acquired
            cost += executed + GAS_USDC
            first_entry = first_entry or bar.observed_at
            last_entry = bar.observed_at
            buy_count += 1
            additions += int(was_open)
            entries += int(not was_open)
            turnover += executed
            slippage_cost += slippage
            gas_cost += GAS_USDC
        else:
            requested_quantity = min(
                quantity,
                quantity * sell_fraction,
                MAX_TRADE_USDC / bar.price_usd,
            )
            sold = requested_quantity * fill
            if sold * bar.price_usd <= GAS_USDC or quantity <= 0:
                trace.append(BacktestTrace(bar.observed_at, "hold", "uneconomic_fill", score))
                continue
            adverse_price = bar.price_usd * (
                Decimal("1") - total_slippage_bps / Decimal("10000")
            )
            proceeds = sold * adverse_price
            sold_cost = cost * sold / quantity
            trade_pl = proceeds - GAS_USDC - sold_cost
            cash += proceeds - GAS_USDC
            quantity -= sold
            cost -= sold_cost
            if quantity < Decimal("0.000000000001"):
                quantity = Decimal("0")
                cost = Decimal("0")
            realized += trade_pl
            last_exit = bar.observed_at
            outcomes = outcomes + (ExitOutcome(bar.observed_at, trade_pl, reason),)
            exits += 1
            turnover += proceeds
            slippage_cost += sold * (bar.price_usd - adverse_price)
            gas_cost += GAS_USDC
        trace.append(BacktestTrace(bar.observed_at, action, reason, score))

    ending = cash + quantity * bars[-1].price_usd
    return StrategyBacktestResult(
        scenario=scenario,
        profile=profile,
        starting_value_usdc=starting_value_usdc,
        ending_value_usdc=ending,
        realized_pl_usdc=realized,
        max_drawdown_percent=max_drawdown,
        entries=entries,
        additions=additions,
        exits=exits,
        eligible_signals=eligible,
        rejected_signals=rejected,
        stale_rejections=stale_rejections,
        failed_routes=failed_routes,
        partial_fills=partial_fills,
        turnover_usdc=turnover,
        slippage_usdc=slippage_cost,
        gas_usdc=gas_cost,
        trace=tuple(trace),
    )


def synthetic_regime(name: str, *, bars: int = 96) -> tuple[MarketBar, ...]:
    if name not in {"bullish", "bearish", "sideways", "high_volatility"}:
        raise ValueError("Synthetic regime is unsupported.")
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    price = Decimal("1")
    prices: list[Decimal] = []
    result = []
    for index in range(bars):
        if name == "bullish":
            change = Decimal("1.2") if index % 7 else Decimal("-1.5")
        elif name == "bearish":
            change = Decimal("0.8") if index % 6 == 0 else Decimal("-1.1")
        elif name == "sideways":
            change = Decimal("1.1") if index % 4 < 2 else Decimal("-1.0")
        else:
            change = Decimal("7") if index % 4 in {0, 3} else Decimal("-6")
        price *= Decimal("1") + change / Decimal("100")
        prices.append(price)
        four_back = prices[max(0, index - 4)]
        h24 = (price / four_back - Decimal("1")) * Decimal("100")
        positive = change > 0
        result.append(
            MarketBar(
                observed_at=timestamp + timedelta(hours=6 * index),
                price_usd=price,
                change_h6_percent=change,
                change_h24_percent=h24,
                buys_h24=1400 if positive else 700,
                sells_h24=700 if positive else 1400,
                volume_h24_usd=Decimal("18000000") * (
                    Decimal("1.8") if name == "high_volatility" else Decimal("1")
                ),
                baseline_volume_usd=Decimal("15000000"),
                liquidity_usd=(
                    Decimal("250000") if name == "high_volatility" else Decimal("25000000")
                ),
                stale=index > 0 and index % 23 == 0,
                route_available=not (index > 0 and index % 17 == 0),
                fill_fraction=(Decimal("0.65") if index > 0 and index % 19 == 0 else Decimal("1")),
            )
        )
    return tuple(result)


def run_regime_suite() -> tuple[StrategyBacktestResult, ...]:
    return tuple(
        run_strategy_backtest(
            synthetic_regime(regime),
            scenario=regime,
            profile=profile,
        )
        for regime in ("bullish", "bearish", "sideways", "high_volatility")
        for profile in (CAUTIOUS_PROFILE, MEDIUM_HIGH_PROFILE)
    )
