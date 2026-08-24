# Lumen Trading Executor

## Current State

The deterministic `TradeIntent` and risk gate now has two modes. `shadow_only`
is the default and cannot submit. `controlled_live` can submit exactly one
risk-reducing Base route through a Coinbase CDP wallet supplied by AgentKit,
but only when live trading and the independent kill switch are also enabled.

No strategy, model output, research feed, or backend response can raise the
hard limits or expand the route.

## Authority Flow

1. A strategy creates a unique, versioned `TradeIntent` with source evidence.
2. The Risk Manager supplies a complete, fresh daily-loss and drawdown snapshot.
3. The Executor verifies wallet, recipient, Base chain, exact assets, spot-only
   scope, freshness, replay identity, position limits, and kill-switch state.
4. The decision is durably recorded in `execution_decisions.jsonl`.
5. A fresh `ApprovedSwap` binds the CDP quote ID and timestamp to the exact
   native ETH input, official Base USDC output, wallet, chain, notional, and
   slippage.
6. The live journal atomically reserves the notional against the UTC daily cap.
7. The CDP adapter creates a slippage-bound quote, executes it, waits for the
   onchain receipt, and returns normalized evidence.
8. The executor validates and audit-records the receipt or failure.

## Absolute Controlled-Live Boundary

- Maximum notional: $20 per intent.
- Maximum reserved notional: $100 per UTC day.
- Maximum reported trading capital: $500.
- Maximum slippage: 100 basis points.
- Wallet: the adopted treasury only.
- Network: Base mainnet, chain ID 8453 only.
- Route: native ETH (`0xeeee...eeee`) to official Base USDC only.
- Direction: SELL only; BUY is not implemented in controlled-live mode.
- Product: unleveraged spot only.
- Approval transactions: prohibited for this route.

The daily journal counts reservations, not only successful receipts. A backend
failure, timeout, crash after reservation, or receipt mismatch continues to
consume capacity until a future explicit reconciliation workflow proves the
outcome. This intentionally favors stopping over accidental double execution.

## Modes and Activation Gates

Defaults are fail-closed:

```text
LIVE_TRADING_ENABLED=false
TRADING_EXECUTOR_MODE=shadow_only
TRADING_EXECUTOR_KILL_SWITCH=halted
```

All three must be changed deliberately before the controlled-live entry point
can reach its backend:

```text
LIVE_TRADING_ENABLED=true
TRADING_EXECUTOR_MODE=controlled_live
TRADING_EXECUTOR_KILL_SWITCH=armed
```

Changing these variables does not itself produce an intent, fund a wallet, or
start a trade. The existing Railway shadow monitor also continues to refuse
live flags; a controlled-live worker must call the explicit composed entry
point with a fresh `RiskSnapshot` and `ApprovedSwap`.

## Coinbase CDP / AgentKit Backend

`CdpAgentKitBackend` imports the pinned `coinbase-agentkit==0.7.4` and
`cdp-sdk==1.48.0` packages only when instantiated. AgentKit provides the
configured CDP EVM wallet provider;
the adapter then uses its CDP client to create a quote with the approved
slippage, execute it, and await a Base receipt. The generic natural-language
AgentKit tool surface is never exposed to strategy or model output.

The production route starts with native ETH, so it does not need an ERC-20
allowance and rejects any receipt that reports an approval transaction.

## Journals and Recovery

`data/execution_decisions.jsonl` records policy decisions and replay identity.
`data/live_execution_audit.jsonl` separately records `RESERVED`, `CONFIRMED`,
`BACKEND_FAILED`, and `RECEIPT_REJECTED` events. Both files use exclusive locks,
monotonic sequences, previous-entry hashes, SHA-256 entry hashes, flush, and
`fsync`.

Mount `/app/data` on a persistent Railway volume. Missing, corrupt, unwritable,
or inconsistent journal state blocks execution. A reservation without a final
event is ambiguous and remains charged to the daily ceiling.

## Remaining Railway / CDP Setup

1. Create or select the CDP EVM server wallet whose address is exactly the
   adopted trading treasury; do not let the runtime create an unreviewed wallet.
2. Add `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, and `CDP_WALLET_SECRET` only as
   Railway service variables.
3. Deploy with live disabled and the kill switch halted.
4. Mount and verify persistent `/app/data` storage across a redeploy.
5. Verify the deployed commit, `base-mainnet`, chain ID 8453, and exact wallet
   identity with no funds present.
6. Bind a verified live portfolio/P&L reader to the supplied `RiskSnapshot`.
7. Exercise restart, timeout, ambiguous receipt, and external emergency-stop
   recovery in a production-like environment.
8. Obtain separate approval before arming, depositing funds, or sending a first
   small canary.

Secrets, owner private keys, seed phrases, wallet exports, and approval links
must never enter GitHub, logs, prompts, or either journal.

Last updated: 2026-08-23
