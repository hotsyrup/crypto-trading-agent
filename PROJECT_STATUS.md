# Crypto Trading Agent — Project Status

_Last updated: 2026-08-28_

## Mission

Build a modular AI-assisted crypto trading agent that progresses safely from research and backtesting to paper trading and, only after required safeguards are verified, bounded live execution.

## Current Status

**Overall:** corrected Base worker deployed, verified, and armed

- Repository: active on GitHub
- Runtime target: Railway
- Network: Base mainnet
- Core assets: official Base USDC plus a fresh governed top-25 Base universe
- Live trading: **armed under the existing hard limits**
- Unattended live execution: **active for the exact dedicated CDP wallet**
- Current Python bot: research, market-data collection, signals, risk checks, simulated orders, and journaling
- Runtime treasury after the first rearmed cycle: 50.331139 USDC plus governed
  Base assets in the dedicated CDP wallet; signing credentials remain
  deployment-only

## What Works Today

- Read a public Base USDC balance
- Collect ETH/USD market prices
- Generate a moving-average trading signal
- Apply the current simulated risk limit
- Simulate ETH orders
- Record paper-trading decisions locally
- Run automated tests
- Run the optional seven-day trending-token paper trial
- Use project-scoped Base MCP configuration for interactive Base Account operations that require explicit human approval
- Build a 25-asset universe from market-cap order cross-verified against exact
  Base contracts, liquidity, volume, and pool age
- Convert observation-only research into deterministic buy/sell proposals
  without granting the research service execution authority
- Evaluate and reserve bounded USDC-to-asset buys and asset-to-USDC sells
- Submit exact-contract routes through a Coinbase CDP wallet supplied by AgentKit
- Restrict ERC-20 Permit2 approvals to the exact token and input amount
- Record hash-chained reservations, backend failures, rejected receipts, and confirmed transaction receipts
- Persist live high-water-mark and UTC daily-loss state with fail-closed
  corruption and clock-rollback handling
- Run a disabled-by-default Railway worker that verifies the CDP wallet and
  Base network, reads paginated balances, maintains the universe, and permits
  at most one governed attempt per cycle
- Apply a disabled-by-default, veto-only Agent Commerce research gate after the
  existing strategy creates a candidate and before the existing controlled-live
  executor; hard limits are `$1/report`, `$5/rolling 24h`, and one paid report
  per normalized asset per rolling 24h
- Persist paid-research reservations and candidate evaluations in a locked,
  fsynced, hash-chained journal, with no automatic retry after ambiguity

## Live Trading Guardrails Adopted

The repository records the following bounded mandate:

- Official Base USDC and at most 25 exact-contract governed Base assets
- Maximum 20% allocation to one position
- Maximum 5% initial allocation to a newly promoted strategy
- Stop opening new positions after a 5% daily loss
- Halt at a 20% drawdown pending human review
- No leverage
- No borrowing
- No derivatives
- No shorting
- No unknown contracts
- No unlimited approvals

The controlled-live layer also hard-codes absolute ceilings of $20 per trade,
$100 reserved per UTC day, and $500 of trading capital. These limits cannot be
raised with environment variables. `LIVE_TRADING_ENABLED=false`,
`TRADING_EXECUTOR_MODE=shadow_only`, and a halted executor kill switch remain
the defaults.

## Required Before Unattended Live Trading

- [x] Implement and unit-test the minimal CDP controlled-live order path
- [x] Implement high-water-mark accounting
- [x] Implement daily-loss accounting
- [x] Create durable audit records for every controlled-live decision and execution attempt
- [x] Add stale market, risk, intent, and swap-quote protection
- [x] Add and verify the in-process emergency kill switch
- [x] Verify Railway production configuration and masked CDP secret handling
- [x] Verify the deployed worker reaches `no_funds_ready` for the exact Base wallet
- [x] Verify the funded wallet balance and complete a fail-closed $1.25 backend canary
- [ ] Run end-to-end paper tests under production-like conditions
- [x] Unit-test live limits against strategy and backend bypass attempts
- [x] Implement the verified CDP balance/portfolio reader and runnable worker
- [ ] Reconcile live wallet balances and confirmed receipts after restart/provider timeouts
- [ ] Independently verify the kill switch outside the executor process
- [ ] Complete a small-capital controlled live validation before scaling

## Deployment

### GitHub

GitHub is the source of truth for code, documentation, tests, and deployment configuration.

### Railway

Railway has a healthy persistent volume mounted at `/app/data` and now stores
`CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, and `CDP_WALLET_SECRET` as masked service
variables; credentials remain outside GitHub. Coinbase created the dedicated
`lumen-trading-agent` API-key wallet at
`0x716b5d6bf67a4c01103b52365c8fb5fdfef0ff06`. The execution and research
workers use the exact-25 governed snapshot, while the observation-only research
service also emits the required official USDC identity packet. Research
deployment `bdd69396-98d7-40b4-ba40-85775687ad7` reached `SUCCESS` with 26
fresh stored packets per cycle. Worker deployment
`eb1032e1-911f-45d4-a630-e490a8c9e62e` was verified while all execution gates
were halted; armed redeployment `ece14685-d2e7-4b60-a503-e6a6758081a1`
reached `SUCCESS`.

The first armed cycle used 5.260705 USDC to purchase
129.402901872644870541 CHIP. Base confirmed approval transaction
`0x9828966679911b35ec0d6bbb0131d34e8a9934254b3b4fed2f453febc640b629`
and swap transaction
`0x09c0de4d08bdeb7c7bba5535e8f2299dcafed21ade0e51709a070743ec252c91`.
Independent Base RPC then reported 50.331139 official USDC and
129.40301232553952 CHIP in the exact wallet.

Through 2026-08-27 15:01 UTC, six confirmed cycles had spent 21.278888 USDC
for 522.984386248333337919 CHIP. Every approval and swap receipt returned Base
status 1. The read-only wallet snapshot then showed 34.312956 USDC,
522.984496701227983292 CHIP, and 0.001913206172781036 ETH.

## Wallet Roles

### Trading Treasury

The repository identifies the Base trading treasury by public address. Public wallet information can be used for read-only balance/account monitoring, but it does not grant signing authority.

### Lumen Agentic Wallet

Lumen's agentic wallet is configured separately through environment configuration. Its private credentials, recovery information, session credentials, and signing material must never be stored in this repository.

## Security Rules

- Never commit private keys or seed phrases
- Never commit passwords, session tokens, approval credentials, or exchange secrets
- Keep live execution fail-closed
- Use least-privilege credentials
- Require testing before strategy promotion
- Preserve an auditable record of trading decisions
- Prefer small controlled exposure when moving from paper to live trading

## Current Priority

**Deploy and verify Agent Commerce research while its feature flag remains
disabled, then complete a non-spending shadow cycle before any activation.**

The exact wallet, Base network, fresh research coverage for every held governed
asset, and all three production hash chains were verified before rearm. Preserve
the unresolved 3.745010-USDC reservation from 2026-08-24 as charged and never
retry it automatically.

The Agent Commerce integration is not a strategy or execution authority. A
favorable report can only let an existing candidate continue through every
unchanged control; an adverse, invalid, unavailable, stale, or ambiguous result
rejects that candidate. Activation must not create a test purchase and must wait
for a genuine strategy candidate.

## Project Dashboard Roadmap

This file is the first lightweight project dashboard. It can evolve into a phone- and desktop-friendly status view showing:

- Deployment health
- Bot/agent health
- Trading mode: paper vs. live
- Read-only wallet balances
- Open positions and exposure
- Recent trades and decisions
- Daily P&L and drawdown
- Risk-limit status
- Kill-switch status
- Last successful market-data update
- Strategy performance
- Alerts and items requiring human review

## Definition of “Ready for Live”

Live trading should only be considered ready when the execution path, accounting, risk enforcement, audit trail, stale-data checks, emergency shutdown, deployment configuration, and controlled validation have all been implemented and verified.
