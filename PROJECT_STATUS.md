# Crypto Trading Agent — Project Status

_Last updated: 2026-08-13_

## Mission

Build a modular AI-assisted crypto trading agent that progresses safely from research and backtesting to paper trading and, only after required safeguards are verified, bounded live execution.

## Current Status

**Overall:** Paper-trading / pre-live hardening

- Repository: active on GitHub
- Runtime target: Railway
- Network: Base mainnet
- Core assets: USDC and ETH
- Live trading: **disabled by default**
- Unattended live execution: **not yet enabled**
- Current Python bot: research, market-data collection, signals, risk checks, simulated orders, and journaling
- Treasury access from the current bot: public/read-only; no private signing credentials should be committed

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

These limits do **not** mean live execution is ready. `LIVE_TRADING_ENABLED` remains fail-closed by default.

## Required Before Unattended Live Trading

- [ ] Implement and verify the live order execution path
- [ ] Implement high-water-mark accounting
- [ ] Implement daily-loss accounting
- [ ] Create durable audit records for every decision and execution
- [ ] Add stale-market-data protection
- [ ] Add and verify an emergency kill switch
- [ ] Verify Railway production configuration and secret handling
- [ ] Run end-to-end paper tests under production-like conditions
- [ ] Verify live limits cannot be bypassed by strategy code
- [ ] Complete a small-capital controlled live validation before scaling

## Deployment

### GitHub

GitHub is the source of truth for code, documentation, tests, and deployment configuration.

### Railway

The intended next deployment milestone is an always-on Railway Hobby deployment. Railway should receive secrets through its environment/secret configuration; credentials must never be committed to GitHub.

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

**Make the bot production-observable and fail-safe before enabling unattended live execution.**

The immediate engineering focus is the Railway deployment plus enforceable accounting, risk controls, audit logging, stale-data protection, and a kill switch.

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
