# Medium-High Strategy Validation — 2026-08-29

## Decision

**BLOCKED for controlled-live promotion.** The implementation and synthetic
stress suite pass, but the live audit copy proves that cost basis is not yet
complete for every held asset. Four receipt-derived asset inventories were
verified, one historical asset inventory was internally unresolved, six held
signals could not safely use stop/target math, and one retained holding lacked
enough governed metadata for an executable exit evaluation. No deployment,
configuration mutation, Agent Commerce call, reservation, signature, or
transaction was made during validation.

## Implementation

- `medium_high_v1` is behind `TRADING_STRATEGY_PROFILE`; `cautious_v1` remains
  the default and preserves its prior intent fingerprints for replay-safe
  rollback.
- `TRADING_PARALLEL_SHADOW_ENABLED=true` journals both profiles from the same
  packets while only the selected primary profile can create an intent. It
  defaults to `false`.
- Weighted-average basis is replayed from hash-validated `RESERVED` plus
  `CONFIRMED` live-audit pairs. Failed, receipt-rejected, and unresolved events
  never create basis. A history gap disables basis-dependent exits only for the
  affected asset.
- Exact formulas and exit ordering are in `docs/MEDIUM_HIGH_STRATEGY.md`.

## Validation

- Full unit/adversarial suite: 287 tests passed.
- Secret-pattern scan: passed across 104 files.
- Python compilation and diff whitespace validation: passed.
- Lookahead check: a 48-bar prefix produced the exact same first 48 decisions
  as the corresponding 96-bar run.
- Determinism/indicator stability: repeated runs were identical; a 0.1% uniform
  price perturbation changed eligible-signal count by no more than two.
- Duplicate timestamps, corrupt journal hashes, receipt-inventory oversells,
  stale inputs, unsupported profiles, missing basis, dust exits, addition caps,
  cooldowns, repeated losses, and partial fills have adversarial coverage.

## Synthetic regime comparison

These are deterministic stress paths, not historical-market evidence. Each
96-bar period includes bounded liquidity impact, maximum 100-bps slippage,
$0.03 simulated gas per fill, stale packets, route failures, partial fills,
5% daily purchase stops, and 20% drawdown halts.

| Regime | Profile | Ending value | Entries | Additions | Exits | Eligible | Turnover | Max drawdown |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Bullish | cautious_v1 | $103.3448 | 5 | 0 | 4 | 9 | $45.09 | 0.084% |
| Bullish | medium_high_v1 | $100.8460 | 15 | 0 | 16 | 32 | $146.04 | 0.199% |
| Bearish | cautious_v1 | $100.0000 | 0 | 0 | 0 | 0 | $0.00 | 0.000% |
| Bearish | medium_high_v1 | $100.0000 | 0 | 0 | 0 | 0 | $0.00 | 0.000% |
| Sideways | cautious_v1 | $99.9549 | 2 | 0 | 1 | 3 | $14.89 | 0.242% |
| Sideways | medium_high_v1 | $99.7594 | 2 | 0 | 3 | 5 | $18.62 | 0.296% |
| High volatility | cautious_v1 | $101.1520 | 4 | 0 | 3 | 8 | $36.05 | 0.951% |
| High volatility | medium_high_v1 | $99.4640 | 5 | 0 | 5 | 10 | $44.25 | 1.313% |

Across the four synthetic periods, medium-high produced 2.00x as many initial
entries, 2.35x as many eligible signals, and about 2.18x turnover. The 5%
initial and 20% position ceilings did not change, but the profile spent more
time invested and incurred more exit, slippage, and gas drag. It underperformed
the cautious profile in three active regimes.

## One-cycle live-data shadow comparison

A read-only copy of the production universe, lifecycle registry, live audit,
and public balances was evaluated locally with a backend that raises before any
submission. The primary executor was also explicitly shadow-only and halted.

| Profile | Signals | Buy | Hold | Watch | Rejected | Score range |
|---|---:|---:|---:|---:|---:|---:|
| cautious_v1 | 12 | 3 | 9 | n/a | 9 | 43-80 |
| medium_high_v1 | 16 | 1 | 15 | 6 | 9 | 0-80 |

The medium-high set includes retained held assets so emergency exits can be
evaluated before entries. Six were held because basis was unverified and one
because retained governance metadata was unavailable. The cycle ended
`POLICY_BLOCKED`, and the non-submitting backend recorded zero submission
attempts.

## Main risks and failure modes

- Historical inventory gaps can suppress otherwise desirable exits until a
  separate verified reconciliation supplies opening inventory and cost.
- Receipt output is the best currently journaled fill evidence, but it is not a
  direct decoded balance-delta proof; the 0.1% reconciliation bound protects
  against relying on material disagreement.
- Short history makes volatility and median-volume baselines less informative;
  governed snapshot values are used until enough fresh observations exist.
- Synthetic paths are too small and stylized to establish out-of-sample edge.
- More frequent exits caused material churn and cost drag in the stress suite.
- A retained contract without complete lifecycle metadata remains valuation
  only; exact-contract identity alone is insufficient execution authority.
- In-process cooldowns remain dependent on durable journal availability and
  correct timestamps; corruption or clock rollback fails closed.

## Rollback and next gate

Rollback is `TRADING_STRATEGY_PROFILE=cautious_v1` with
`TRADING_PARALLEL_SHADOW_ENABLED=false`, deployed while all execution gates are
halted, then verified to terminal Railway `SUCCESS`, healthy runtime state, and
an unchanged live-audit chain before any rearm decision.

Promotion should remain blocked until all held positions have verified basis or
are explicitly classified as basis-unavailable, retained execution metadata is
complete, a longer production-like parallel shadow window is collected, and a
historical out-of-sample backtest shows that the additional turnover earns back
its gas/slippage cost. Controlled-live activation still requires separate final
confirmation.

## Production promotion

Ben provided the separate final confirmation on 2026-08-29. Production was
first set to `LIVE_TRADING_ENABLED=false`, `shadow_only`, kill switch `halted`,
Agent Commerce `disabled`, and `medium_high_v1`. Halted code deployment
`97d72596-81f8-43a2-a85b-235d517419ee` reached Railway `SUCCESS`; the exact CDP
wallet and Base network matched, all four journals validated, cost basis
reconstructed, and the live-execution journal remained unchanged.

The first rearm failed closed before any reservation or submission because a
research packet was re-evaluated after its rolling volume baseline changed,
conflicting with its prior strategy-journal identity. All three execution gates
were immediately halted. A deterministic repeated-cycle regression test
reproduced the exact error. The repair treats each strategy packet as
single-use, journals its decision once, hands that exact decision to execution,
and waits for a fresh packet on later cycles. The full 287-test suite and all
required safety checks passed after the repair.

Corrected halted deployment `b0288fff-16a0-4418-9ac3-e526046cf502` and armed
redeployment `6185f588-dad1-4727-8cf2-9e1703cf80af` both reached terminal
Railway `SUCCESS`. Final observed health was operational with 8/8 held-asset
valuation coverage, `medium_high_v1`, controlled-live execution armed, Agent
Commerce disabled, no runtime error, and no new live reservation or submission.
Fresh candidates initially remained watch, reject, or basis-protected holds.
A later fresh packet produced the first canary: a 74-score entry spent
5.151615 USDC for 9.629356944096538793 O at a 50-bps maximum, with
0.000002327046 ETH recorded gas. The approval and swap were independently
verified as Base status 1 with 39 confirmations. Direct `balanceOf` and the CDP
portfolio reader both returned 9.629356668767061757 O; the
0.000000275329477036-token difference from the provider receipt was about
0.000286 bps and remained inside the existing 10-bps reconciliation tolerance.
Receipt-derived basis was present and verified. Two subsequent cycles covered
9/9 governed holdings and made no duplicate reservation or submission.
