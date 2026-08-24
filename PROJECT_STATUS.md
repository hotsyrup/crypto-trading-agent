# Crypto Trading Agent — Project Status

_Last updated: 2026-08-24_

## Mission

Build a modular AI-assisted crypto trading agent that progresses safely from research and backtesting to paper trading and, only after required safeguards are verified, bounded live execution.

## Current Status

**Overall:** funded CDP wallet and live path verified through a fail-closed canary

- Repository: active on GitHub
- Runtime target: Railway
- Network: Base mainnet
- Core assets: official Base USDC plus a fresh governed top-25 Base universe
- Live trading: **currently halted after the first fail-closed canary**
- Unattended live execution: **implemented; corrected canary retry requires explicit approval**
- Current Python bot: research, market-data collection, signals, risk checks, simulated orders, and journaling
- Runtime treasury: 25 USDC in the dedicated CDP wallet; signing credentials remain deployment-only

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
workers refresh their own exact-25 snapshots before using them. Research
deployment `f4a6d487-1a4e-437a-a247-4fee1789f6f7` stored all 25 governed packets.
The trading worker verified 25 USDC in the exact treasury on `base-mainnet`,
chain ID 8453. The first $1.25 canary was reserved, failed at the AgentKit
address boundary, and was audit-recorded as `BACKEND_FAILED`; no confirmed
swap receipt exists. Deployment `b3524e5a-8d26-46d0-8deb-4654bf8a2cb2`
contains the tested checksum correction and is currently shadow-only with the
kill switch halted. A retry requires setting all three gates deliberately:
`LIVE_TRADING_ENABLED=true`, `TRADING_EXECUTOR_MODE=controlled_live`, and
`TRADING_EXECUTOR_KILL_SWITCH=armed`.

Do not retry the real-money canary without explicit approval for that action.

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

**Validate the top-25 research feed, then obtain approval for a small canary.**

The no-funds exact-wallet/network check is complete. The immediate focus is
top-25 research-feed verification and one separately approved small canary.

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
