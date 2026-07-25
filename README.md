
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

Paper trading only. No real funds or live wallet access.

## Disclaimer

This project is for educational and research purposes. Cryptocurrency trading involves substantial financial risk. Nothing in this repository is financial advice.
