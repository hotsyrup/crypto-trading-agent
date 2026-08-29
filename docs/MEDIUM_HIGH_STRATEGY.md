# Medium-High Strategy Profile

Status: implemented for validation and shadow comparison; not authorized for
controlled-live activation.

## Configuration and rollback

`TRADING_STRATEGY_PROFILE` accepts exactly:

- `cautious_v1` (default): the existing dual-positive momentum entry and
  dual-negative/8%-stop/15%-take-profit behavior.
- `medium_high_v1`: composite scoring, receipt-derived cost basis, mature-bot
  cooldowns, and triple-barrier exits.

Rollback is a configuration-only change to `cautious_v1`, followed by a
halted deployment and terminal deployment/health verification. The medium-high
strategy journal remains audit history and does not grant execution authority.

`TRADING_PARALLEL_SHADOW_ENABLED=true` evaluates and journals both profiles on
the same fresh packets while only `TRADING_STRATEGY_PROFILE` may produce an
intent. It defaults to `false`; shadow evaluation does not call Agent Commerce,
create a reservation, sign, or submit a transaction.

## Entry score

The score is an integer from 0 through 100. Component values are independently
clamped before rounding and summing:

1. 6-hour momentum, 0-20:
   `clamp((h6_percent + 6) / 12 * 20, 0, 20)`
2. 24-hour momentum, 0-20:
   `clamp((h24_percent + 12) / 24 * 20, 0, 20)`
3. Buy/sell imbalance, 0-15. With
   `imbalance = (buys - sells) / (buys + sells)`:
   `clamp((imbalance + 0.2) / 0.8 * 15, 0, 15)`
4. Relative volume, 0-15. With
   `volume_ratio = current_h24_volume / governed_baseline_volume`:
   `clamp((volume_ratio - 0.5) * 15, 0, 15)`
   The baseline is the median of up to twelve prior unique fresh observations
   once at least three exist; until then it is the governed-universe baseline.
5. Liquidity/impact, 0-15: the lower of liquidity headroom and impact quality.
   Liquidity headroom rises from 5 points at $100,000 to 15 points at
   $500,000. Estimated impact is `notional / liquidity * 10,000`; impact
   quality is `clamp((100 - impact_bps) / 100 * 15, 0, 15)`.
6. Trend consistency, 0-10: 10 when both timeframes are positive, 0 when both
   are non-positive, otherwise `clamp(6 + weaker_percent / 2, 0, 10)`.
7. Exposure/history, 0-5:
   `clamp(5 - position_percent / 5 - confirmed_additions, 0, 5)`.

Scores below 55 are rejected, 55-69 are watch-only, 70-84 permit an initial
entry, and 85-100 permit an initial entry or a controlled addition. At least
one momentum timeframe must be positive. Additions also require a verified
profitable receipt-derived position, a clear cooldown, and fewer than two
prior additions. The existing 5% initial, 20% position, $20/trade, $100/day,
$500 capital, loss, drawdown, asset, slippage, approval, journal, and kill-switch
limits remain downstream and unchanged.

## Persistent cost basis

`live_execution_audit.jsonl` is replayed under its existing hash-chain
validation. Only `CONFIRMED` outcomes affect inventory:

- confirmed USDC-to-asset receipts add token output and USDC input cost;
- confirmed asset-to-USDC receipts remove weighted-average cost and record
  realized P/L;
- reservations, backend failures, and receipt-rejected outcomes remain charged
  where applicable but do not create inventory;
- a sell larger than reconstructed inventory marks that asset's basis
  unverified; its stop/target exits remain disabled while other assets continue;
- the reconstructed quantity must agree with the on-chain balance within the
  greater of ten token quanta or 0.1% before a stop or target may use it.

## Exit order

Exit evaluation is deterministic and ordered:

1. Hard stop: `8% + 0.25 * observed_volatility`, clamped to 8%-12%.
2. Trailing protection after 10% profit. Distance is
   `6% + 0.15 * observed_volatility`, plus 2 points below $250,000 liquidity,
   clamped to 6%-9%.
3. Final target at 25% profit.
4. One 50% partial profit near 15% profit.
5. Both 6-hour and 24-hour momentum negative.
6. Score below 35 on two consecutive unique fresh packets.
7. Full exit after 72 hours, or 50% reduction after 48 hours, when peak
   progress remained below 3%.

Sell notional remains capped at $20 per cycle. Exits below $2 are suppressed;
a partial exit that would leave less than $1 is converted to a full exit.
Emergency exits rank before entries and do not require paid Agent Commerce
research. Agent Commerce remains veto-only for medium-high entries.

## Cooldowns and metrics

- Six hours after entry, twelve hours after exit, six hours after a failed or
  receipt-rejected transaction, and twenty-four hours after a stop-loss.
- Two stop-losses in seven days lock that asset for seven days.
- Three consecutive realized losses, or three realized losses in 24 hours,
  trigger a 24-hour portfolio purchase cooldown.
- The hash-chained strategy journal records each unique fresh packet's score,
  components, classification, action, and exit reason.
- Health metrics expose eligible/rejected/watch signals, entries, exits,
  realized P/L, observed drawdown, receipt output spread, known gas cost, and
  turnover. Missing gas evidence is reported as unavailable, not zero.

## Validation boundary

The included regime suite is a deterministic synthetic stress backtest, not
historical-market evidence. It uses bullish, bearish, sideways, and
high-volatility paths with stale packets, route failures, partial fills,
liquidity impact, slippage, gas, daily-loss limits, and drawdown halts. Promotion
requires a separate controlled-live confirmation after shadow evidence is
reviewed.
