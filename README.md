
# Crypto Trading Agent

A modular AI-assisted crypto trading agent designed for research, backtesting, paper trading, and eventually secure live execution.

## Goals

* Collect and analyze crypto market data
* Generate trading signals
* Apply strict risk-management rules
* Backtest strategies before deployment
* Simulate trades using paper trading
* Record every trading decision and result
* Support collaborative strategy development without exposing wallet keys or exchange credentials

## Planned Architecture

The project will use separate components for:

* Market data
* Trading strategies
* Risk management
* Portfolio management
* Trade execution
* Performance tracking
* Monitoring and alerts

No single component should have unrestricted access to both strategy development and live funds.

## Security

* Never store private keys, seed phrases, API secrets, or passwords in this repository
* Store credentials in environment variables or a secure secret manager
* Use exchange API keys with withdrawals disabled
* Require testing and approval before deploying strategy changes
* Begin with paper trading and small position sizes

## Initial Milestone

The first version will:

1. Download historical market data
2. Run a basic trading strategy
3. Apply position-sizing and loss limits
4. Simulate orders
5. Save trade logs
6. Generate a simple performance report

## Initial Technology

* Python
* FastAPI
* PostgreSQL
* Docker
* GitHub Actions

## Project Status

Early development. Paper trading only. No real funds or live wallet access.

## Disclaimer

This project is for educational and research purposes. Cryptocurrency trading involves substantial financial risk. Nothing in this repository is financial advice.
