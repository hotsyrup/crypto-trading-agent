# Lumen Trading Executor

## Current State

Stage 1 is a deterministic, shadow-only policy executor. It evaluates and
records proposed ETH/USDC spot trades for the Base treasury, but it cannot
construct, approve, sign, submit, retry, or broadcast a transaction.

A `SHADOW_APPROVED` result means only that the supplied intent and risk
snapshot passed the current written policy. Every run returns
`ready_for_submission=false` and `signing_authority=none`.

## Authority Flow

1. A strategy creates a structured proposal with a unique intent ID, version,
   source references, and current market timestamp.
2. The independent Risk Manager supplies a complete, fresh snapshot of daily
   loss and high-water-mark drawdown.
3. The Executor checks the adopted account, network, asset identities, product
   scope, limits, freshness, provenance, and emergency switch.
4. The Executor records the decision in the tamper-evident journal.
5. Stage 1 stops. There is no signing or submission adapter.

Neither Lumen, Buzz agents, natural-language output, news, nor a strategy may
bypass steps 2 through 4.

## Fail-Closed Controls

- Shadow-only mode is the only accepted mode.
- `LIVE_TRADING_ENABLED=true` is rejected by this build.
- The executor kill switch defaults to `halted`.
- Only the adopted treasury and Base chain ID 8453 are accepted.
- Proceeds must return to the same treasury address.
- Only native ETH against official Base USDC is implemented.
- Missing, stale, future-dated, contradictory, or unverifiable state rejects.
- A BUY that breaches allocation or loss limits rejects.
- A 20% drawdown blocks BUY and SELL; a 5% daily loss blocks new BUY activity.
- A SELL cannot exceed the current verified position value.
- Every intent ID is single-use; replay and conflicting reuse fail closed.
- Journal corruption or write failure prevents progress.

## Journal

The default path is `data/execution_decisions.jsonl`. Each entry includes a
schema version, sequence, UTC time, previous-entry hash, intent fingerprint,
decision, and entry hash. The file is locked during validation and append, and
the write is flushed to disk before success is returned.

This is an audit of policy decisions, not an order ledger or proof of onchain
execution. A future live implementation must reserve an approved intent before
submission, reconcile the exact transaction outcome, and preserve recovery
semantics across crashes and provider timeouts.

## Still Required Before a Live Canary

1. Bind a verified live portfolio and P&L reader to the Risk Manager.
2. Verify both current Base Smart Wallet owners with Ben.
3. Design a narrowly permissioned signing adapter without exposing credentials
   to a model, repository, Buzz, logs, or prompts.
4. Add exact quote binding, slippage and gas limits, allowance policy, nonce
   handling, submission state, receipt reconciliation, timeout recovery, and
   failed/ambiguous transaction handling.
5. Deploy and independently exercise an emergency stop outside the strategy
   and executor processes.
6. Complete shadow acceptance, adversarial restart tests, and a separately
   approved bounded live-canary plan.

## Buzz Team Review Boundary

Fizz may review implementation and recovery logic. Honey may review monitoring,
operator clarity, and daily reporting. Bumble may independently challenge the
safety claims and identify missing evidence. Their work is advisory and
read-only: it grants no repository writes, deployment actions, wallet access,
signing authority, trades, approvals, transfers, or permission changes.

Last updated: 2026-08-08
