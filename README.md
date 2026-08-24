
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

## TreasuryPolicy Observation Layer

`app.treasury_policy` evaluates proposed paid service calls before any x402 or
other operating-wallet purchase. It records the service, provider, purpose,
expected benefit, price, remaining budget, evidence, cheaper alternative, and
the decision it would recommend. The initial mode is permanently
`observation_only`: an affirmative observation is not payment authorization,
and the module contains no wallet, signer, HTTP payment, or transaction path.

The evaluator fails closed for recurring commitments, prohibited purposes,
missing evidence, missing cheaper-alternative analysis, and proposals above
the remaining budget. Observations can be appended to
`data/treasury_policy_journal.jsonl` for later comparison with actual results.
The current security gate and remaining requirements are recorded in
`docs/X402_SECURITY_REVIEW.md`.

`app.service_receipts` adds an append-only, privacy-minimized receipt ledger
and provider scorecard. It records quoted and settled USDC, delivery outcome,
usefulness, a masked transaction reference, and the post-attempt balance.
Malformed journals fail closed. A provider that has charged without returning
a result receives a `manual_review` recommendation that TreasuryPolicy treats
as a reason not to recommend the next purchase. This remains observation-only:
neither module can call a service, sign, settle, retry, or move funds.

To inspect current scorecards without contacting a provider or wallet:

```bash
python scripts/report_service_scorecards.py
```

The default receipt journal is local runtime data under
`data/x402_service_receipts.jsonl` and remains excluded from Git. Durable
human-readable purchase history stays in Ben AI Home's `LUMEN_WALLET.md`.

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
* Base USDC is pinned to its exact contract address; copied symbols and
  unsolicited assets fail closed
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

## Lumen Trading Executor — Controlled Live

`app.trading_executor` is the first deterministic execution-policy layer. It
accepts a structured trade intent and current risk snapshot, then verifies:

* the exact authorized Base treasury and return address
* Base mainnet chain ID 8453
* native ETH and the official Base USDC contract
* ETH/USDC unleveraged spot scope only
* the 20% position and 5% new-strategy limits
* the 5% daily-loss and 20% drawdown halts
* fresh, timezone-aware market, risk, and intent timestamps
* strategy identity, version, and source provenance
* complete, non-contradictory risk state

The composed entry point records every decision in a locked, append-only JSONL
journal with sequence numbers and a SHA-256 hash chain. Reusing an intent ID is
blocked after restart, changing the content behind an existing ID fails closed,
and corruption or journal unavailability prevents progress.

`shadow_only` remains the default and never submits a transaction. The optional
`controlled_live` mode sits below the same deterministic policy and requires
all three independent gates: `LIVE_TRADING_ENABLED=true`,
`TRADING_EXECUTOR_MODE=controlled_live`, and
`TRADING_EXECUTOR_KILL_SWITCH=armed`.

The first production route is deliberately one-way: native Base ETH to the
official Base USDC contract through a Coinbase CDP wallet provided by AgentKit.
It is risk-reducing only and cannot create an ERC-20 approval. Absolute limits
are hard-coded at $20 per intent, $100 of reservations per UTC day, $500 total
trading capital, and 100 bps maximum slippage. A fresh, exact quote is bound to
the approved wallet, Base chain ID 8453, token pair, input amount, notional, and
slippage before any backend call.

The live audit reserves daily capacity under a file lock before submission.
Reservations survive backend failures and ambiguous timeouts so retries cannot
bypass the daily cap. Confirmed receipts, provider failures, and rejected
receipts are appended to a separate SHA-256 hash chain. Wrong wallet/network,
changed route fields, unexpected approvals, invalid outputs, and backend
failures all fail closed.

The decision journal defaults to `data/execution_decisions.jsonl`; the live
reservation and receipt audit defaults to `data/live_execution_audit.jsonl`.
Both remain excluded from Git and require persistent Railway storage. See
`docs/LUMEN_TRADING_EXECUTOR.md` for the contract and remaining activation
steps.

### Railway and CDP activation (remaining operator work)

The main Docker image installs the pinned `coinbase-agentkit` production
adapter. In Railway, mount a persistent volume at `/app/data`, then add
`CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, and `CDP_WALLET_SECRET` as service
variables. Never paste those values into GitHub, logs, prompts, or committed
files.

Deploy first with the defaults (`LIVE_TRADING_ENABLED=false`,
`TRADING_EXECUTOR_MODE=shadow_only`, kill switch halted). Verify the deployed
commit, durable journal writes, Base mainnet chain ID, and exact treasury
address. Only after a separate no-funds review should an operator set
`LIVE_TRADING_ENABLED=true`, `TRADING_EXECUTOR_MODE=controlled_live`, and
`TRADING_EXECUTOR_KILL_SWITCH=armed`. Funding and a first canary remain separate
human actions; this pull request does not deploy, arm, fund, or trade.

### Run the Paper Bot

The paper runtime has a fail-closed safety gate. It rejects proposals when the
kill switch is halted, the market-data timestamp is missing or lacks a
timezone, the data is stale, the timestamp is implausibly far in the future,
or the safety configuration is invalid. The default is deliberately halted.

To run a deliberate paper simulation with the default two-hour freshness
limit:

```bash
PAPER_KILL_SWITCH=armed python -m app.run_paper_bot
```

The journal records the market-data timestamps, data age, switch state, and
gate decision. This gate can authorize only the paper simulator; it does not
change `LIVE_TRADING_ENABLED=false` or create a live signing path.

### Persistent Paper Risk Accounting

The paper risk engine stores its high-water mark, UTC daily starting value,
last marked portfolio value, and update time in
`data/paper_risk_state_v2.json`. Each cycle values USDC plus ETH at the proposal's
verified reference price, so the daily result includes realized and unrealized
mark-to-market changes. At a UTC date rollover, the last verified portfolio
mark is carried forward as the new day's baseline so an overnight loss is not
silently discarded.

At a 5% UTC daily loss, new paper BUY positions are blocked while a
risk-reducing SELL remains available. At a 20% drawdown from the persistent
high-water mark, all paper execution halts. State writes are atomic; corrupted,
unsupported, clock-rollback, or unwritable state fails closed. These controls
exercise the mandate in simulation only and are not evidence of live-path
readiness.

### Corrected Paper Acceptance Ledger

The pre-fix elapsed-time acceptance file is preserved only as invalidated
historical evidence and is never read for readiness credit. Its SHA-256 and
reported counters are retained in the operator status for reconciliation.
Corrected credit defaults to frozen with
`PAPER_ACCEPTANCE_CREDIT_ENABLED=false`.

Every paper cycle is committed to
`data/paper_cycle_ledger_v2.jsonl`, an append-only SHA-256 hash chain protected
by a process-shared file lock and a stable signal ID. Duplicate retries return
the original outcome without changing balances or counters; reuse of a signal
ID with different evidence and conflicting stale portfolio writes fail closed.
The ledger, rather than the legacy portfolio file, is the source of truth for
simulated balances, costs, P&L, eligibility, and acceptance progress after
restart.

Acceptance can complete only after either 50 unique eligible signal IDs or
seven consecutive completed qualifying UTC days. A qualifying day requires at
least 20 unique observed cycles, with every cycle passing the research,
market-freshness, kill-switch, and accounting health boundaries under the
frozen `eth_usd_sma_3_5` strategy version. A blocked cycle disqualifies that
day. Enabling corrected acceptance credit requires a separate review and
authorization; deployment and restart-verification cycles do not count.

The monitor writes a privacy-safe operator report to
`data/operator_status_v2.json` and emits the same evidence to authenticated
Railway logs. It includes the deployed commit when Railway supplies it,
research packet IDs and quality, rejection reasons, simulated outcomes,
acceptance counters, ledger continuity, and an explicit no-signer boundary.

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

## Free Railway Research Agent

The separate research worker reads recent Base token profiles and market data
from DEX Screener's public API, normalizes the results into expiring research
packets, and stores them in SQLite. Every packet is marked `OBSERVE_ONLY`,
records source limitations, and explicitly denies execution authority. Token
profiles are discovery leads, not endorsements; they may reflect project
marketing, and the worker does not claim to verify contract safety or holder
concentration. The default watchlist guarantees coverage of Base WETH and
USDC even when the latest-profile feeds contain no Base projects.

Research packet schema version 2 records the number of eligible Base pools
considered before selecting the most-liquid pool, nullable market-cap and FDV
values, active boosts, and whether discovery came from a profile, boost, or
advertisement. The strict trading reader accepts only non-promotional
configured-watchlist evidence with exact approved base and quote contracts;
these additional observations do not create a trading instruction.

Run one local cycle:

```bash
python -c 'from app.research_agent import run_research_cycle; run_research_cycle()'
```

Run the long-lived worker and health endpoint:

```bash
python -m app.research_agent
```

The service exposes these public read-only routes:

* `/health` reports provider and execution-boundary status.
* `/research/crypto/base/latest` returns the latest public Base research
  packets and marks expired packets with `is_stale=true`.

`/research/latest` remains a temporary compatibility alias. The reserved
`/research/equities/latest` and `/research/bitcoin-network/latest` routes
return HTTP 501 with `status=not_configured`, empty packets, observation-only
mode, and execution disabled. They prevent callers from mistaking a planned
domain for a verified capability. Future domain workers must define their own
packet contracts, providers, freshness rules, consumers, and authority
boundaries before either route can return evidence.

For a second Railway service, select `/railway.research.json` as its custom
config-as-code file and `Dockerfile.research` as its Dockerfile. Attach a
volume at `/app/data` if the research history must survive deployments. The
free configuration requires `RESEARCH_MODE=observation_only`,
`LIVE_TRADING_ENABLED=false`, `AIXBT_ENABLED=false`, and
`BANKR_ENABLED=false`; it fails closed if any paid or execution flag is
enabled. No API key or wallet funding is required.

## Security

* Never store private keys, seed phrases, API secrets, or passwords in this repository
* Store credentials in environment variables or a secure secret manager
* Use exchange API keys with withdrawals disabled
* Require testing and approval before deploying strategy changes
* Begin with paper trading and small position sizes

## Project Status

The live mandate, treasury identity, and minimal CDP execution adapter are
implemented, but execution remains disabled by default. The only controlled
route is a bounded native Base ETH-to-USDC risk-reducing swap. The next
milestone is Railway/CDP configuration plus a verified live portfolio reader,
restart/timeout reconciliation, and an independently exercised emergency stop.
Any funding or live canary remains a separate activation gate.

## Disclaimer

This project is for educational and research purposes. Cryptocurrency trading involves substantial financial risk. Nothing in this repository is financial advice.
