
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
