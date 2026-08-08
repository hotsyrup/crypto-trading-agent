
# Crypto Trading Agent

A modular AI-assisted crypto trading agent designed for research, backtesting, paper trading, and eventually secure live execution.

## Goals

* Collect and analyze crypto market data
* Generate trading signals
* Apply strict risk-management rules
* Simulate trades using paper trading
* Record every trading decision and result
* Support collaborative strategy development without exposing wallet keys or exchange credentials

## Current Paper-Trading MVP

The current version can:

* Connect to Base mainnet in read-only mode
* Read a public Base USDC balance
* Collect ETH/USD market prices
* Generate a moving-average signal
* Apply a 0.5% simulated risk limit
* Simulate an ETH order
* Record decisions in a private local journal
* Run automatic tests

It cannot sign or submit wallet transactions.

## Current Base MCP Gateway

This repository includes project-scoped Codex MCP configuration for Base's
current hosted services:

* `base-mcp` — `https://mcp.base.org/` for Base Account wallets, balances,
  swaps, sends, signatures, contract calls, and x402 payments
* `base-docs` — `https://docs.base.org/mcp` for live, read-only Base
  documentation

The retired `base-mcp` npm package is not used. Open this repository as a
trusted Codex project, restart Codex after first checkout, and authenticate
`base-mcp` with Base Account when prompted. Verify the connections with:

```bash
codex mcp list
```

Base MCP does not silently authorize live trading. Every send, swap,
signature, or contract call returns an approval link that must be reviewed and
confirmed in Base Account. The Python bot remains paper-only; adding the MCP
gateway does not give scheduled jobs or the container unattended wallet access.

### Wallet Roles

The app keeps two Base wallet roles separate:

* **Trading treasury:** `ihaveonefriend.base.eth`, resolved and pinned as
  `0x3c981ec319107be8b8bb614da0742fc5b28e8d9c`
* **Lumen Agentic Wallet:** configured separately through
  `LUMEN_AGENTIC_WALLET_ADDRESS`; do not commit its credentials

The public addresses identify accounts but do not grant signing authority.
Never add private keys, seed phrases, recovery data, passwords, session tokens,
or approval credentials to the repository or Railway environment.

## Adopted Live Trading Limits

Ben adopted a bounded live mandate on 2026-08-06. The application now carries
the initial limits as validated environment configuration:

* USDC and ETH on Base only
* 20% maximum allocation to one position
* 5% maximum initial allocation to a newly promoted strategy
* Stop new positions after a 5% daily loss
* Halt at a 20% drawdown pending human review
* No leverage, borrowing, derivatives, shorting, unknown contracts, or
  unlimited approvals

`LIVE_TRADING_ENABLED` intentionally defaults to `false`. The mandate grants
authority, but execution must remain fail-closed until the live order path,
high-water-mark accounting, daily-loss accounting, audit journal, stale-data
checks, and emergency kill switch are implemented and verified.

### Run the Paper Bot

```bash
python -m app.run_paper_bot
```

## Seven-Day Trending Token Trial

The optional trial scans trending Base pools and paper-trades qualifying
non-core tokens using their exact contract addresses. It starts with $40 in
simulated USDC, limits each entry to $4, holds at most three positions, models
1% trading costs on entries and exits, and applies 8% stop-loss and 15%
take-profit exits.

```bash
python -m app.trending_trial
```

The scheduled GitHub Actions workflow can run one cycle every six hours.
It preserves paper state in a GitHub Actions cache and uploads each run's
paper journal as an artifact. Scheduled workflows only begin after the
workflow is merged into the default branch.

This scanner is suitable for a paper experiment only. Its liquidity, volume,
pool-age, and momentum filters are not a complete token security audit.

## Security

* Never store private keys, seed phrases, API secrets, or passwords in this repository
* Store credentials in environment variables or a secure secret manager
* Use exchange API keys with withdrawals disabled
* Require testing and approval before deploying strategy changes
* Begin with paper trading and small position sizes

## Project Status

The live mandate and treasury identity are recorded, but execution remains
disabled. The current runtime can research, paper trade, and read the public
treasury balance; it cannot yet submit unattended live orders. The next
milestone is an always-on Railway Hobby deployment with enforceable accounting,
risk controls, audit records, stale-data protection, and a kill switch.

## Disclaimer

This project is for educational and research purposes. Cryptocurrency trading involves substantial financial risk. Nothing in this repository is financial advice.
