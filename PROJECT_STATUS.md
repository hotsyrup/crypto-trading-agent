# Crypto Trading Agent — Project Status

_Last updated: 2026-08-23_

## Mission

Build a modular AI-assisted crypto trading agent that progresses safely from research and backtesting to paper trading and, only after required safeguards are verified, bounded live execution.

## Current Status

**Overall:** Controlled-live execution implemented; activation still pending

- Repository: active on GitHub
- Runtime target: Railway
- Network: Base mainnet
- Core assets: USDC and ETH
- Live trading: **disabled by default**
- Unattended live execution: **implemented but disabled pending CDP/Railway setup**
- Current Python bot: research, market-data collection, signals, risk checks, simulated orders, and journaling
- Default runtime treasury access: public/read-only; the disabled controlled-live adapter uses deployment-only CDP credentials

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
- Evaluate and reserve a bounded controlled-live native ETH-to-USDC swap
- Submit that single route through a Coinbase CDP wallet supplied by AgentKit
- Record hash-chained reservations, backend failures, rejected receipts, and confirmed transaction receipts

## Live Trading Guardrails Adopted

The repository records the following bounded mandate:

- USDC and ETH on Base only
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
- [ ] Implement high-water-mark accounting
- [ ] Implement daily-loss accounting
- [x] Create durable audit records for every controlled-live decision and execution attempt
- [x] Add stale market, risk, intent, and swap-quote protection
- [x] Add and verify the in-process emergency kill switch
- [ ] Verify Railway production configuration and secret handling
- [ ] Run end-to-end paper tests under production-like conditions
- [x] Unit-test live limits against strategy and backend bypass attempts
- [ ] Reconcile live wallet balances and confirmed receipts after restart/provider timeouts
- [ ] Independently verify the kill switch outside the executor process
- [ ] Complete a small-capital controlled live validation before scaling

## Deployment

### GitHub

GitHub is the source of truth for code, documentation, tests, and deployment configuration.

### Railway

The next deployment milestone is an always-on Railway deployment with a
persistent volume mounted at `/app/data`. Railway must receive
`CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, and `CDP_WALLET_SECRET` through service
variables; credentials must never be committed to GitHub. Before activation,
verify that those credentials resolve the exact adopted treasury on
`base-mainnet`, then set all three activation gates deliberately:
`LIVE_TRADING_ENABLED=true`, `TRADING_EXECUTOR_MODE=controlled_live`, and
`TRADING_EXECUTOR_KILL_SWITCH=armed`.

Do not deposit trading capital or arm the switch until the deployed revision,
volume persistence, wallet identity, and a no-funds startup check are verified.

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

**Deploy and verify the controlled-live boundary without funding or arming it.**

The immediate engineering focus is Railway/CDP secret setup, persistent audit
storage, verified live portfolio/P&L inputs, restart reconciliation, and an
independent kill switch before a separately approved small canary.

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
