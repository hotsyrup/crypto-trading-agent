# Lumen Trading Executor

## Current State

The deterministic `TradeIntent` and risk gate now has two modes. `shadow_only`
is the default and cannot submit. `controlled_live` can submit exact-contract
Base spot buys and sells through a Coinbase CDP wallet supplied by AgentKit,
but only when live trading and the independent kill switch are also enabled.

No strategy, model output, research feed, or backend response can raise the
hard limits or expand the route.

## Authority Flow

1. A strategy creates a unique, versioned `TradeIntent` with source evidence.
2. The Risk Manager supplies a complete, fresh daily-loss and drawdown snapshot.
3. The Executor verifies wallet, recipient, Base chain, exact assets, spot-only
   scope, freshness, replay identity, position limits, and kill-switch state.
4. The decision is durably recorded in `execution_decisions.jsonl`.
5. A fresh `ApprovedSwap` binds the CDP quote ID and timestamp to official Base
   USDC and one exact governed token contract, its decimals, wallet, chain,
   notional, and slippage.
6. The live journal atomically reserves the notional against the UTC daily cap.
7. The CDP adapter creates a slippage-bound quote, executes it, waits for the
   onchain receipt, and returns normalized evidence.
8. The executor validates and audit-records the receipt or failure.

## Absolute Controlled-Live Boundary

- Maximum notional: $20 per intent.
- Maximum reserved notional: $100 per UTC day.
- Maximum authorized contributed trading capital: $500; gains may make the
  verified portfolio value higher.
- Maximum slippage: 100 basis points.
- Wallet: the adopted treasury only.
- Network: Base mainnet, chain ID 8453 only.
- Route: official Base USDC to one governed asset, or that exact asset back to
  official Base USDC.
- Direction: BUY and SELL.
- Product: unleveraged spot only.
- Approval transactions: exact-amount Permit2 only for ERC-20 input; native ETH
  needs no approval. Wrong or unlimited approvals fail closed.

The eligible universe contains exactly 25 assets and expires after 24 hours.
The refresh job combines CoinGecko's Base ecosystem market-cap ordering with
GeckoTerminal's exact-contract metadata, liquidity, 24-hour volume, and oldest
pool date. Assets below $100,000 liquidity, below $100,000 daily volume, or
younger than 30 days are excluded. USDC is the settlement asset and does not
consume one of the 25 slots. WETH evidence maps to native ETH for execution.

Research is not authority. Packets must remain `OBSERVE_ONLY`, name the exact
governed contract, be fresh, come from the configured watchlist, and pass the
same liquidity and volume floor. The portfolio interface independently checks
the wallet snapshot and risk ledger, then uses simple momentum entries plus 8%
stop-loss and 15% take-profit exits. The bot is long-only: it may retreat to
USDC during a decline, but it cannot short or guarantee positive returns.

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

Changing these variables does not itself produce an intent or fund a wallet.
`app.live_portfolio_worker` is the production entry point and remains inert by
default. With only `LIVE_WORKER_ENABLED=true`, it can verify the exact wallet,
network, balances, universe, and risk inputs while the executor remains
shadow-only and halted.

## Coinbase CDP / AgentKit Backend

`CdpAgentKitBackend` imports the pinned `coinbase-agentkit==0.7.4` and
`cdp-sdk==1.48.0` packages only when instantiated. The live image also pins
`eth-utils==6.0.0` so every contract, wallet, and Permit2 address is converted
to its canonical EVM checksum at the AgentKit boundary. AgentKit provides the
configured CDP EVM wallet provider;
the adapter then uses its CDP client to create a quote with the approved
slippage, execute it, and await a Base receipt. The generic natural-language
AgentKit tool surface is never exposed to strategy or model output.

The adapter handles native ETH without an approval. For ERC-20 inputs it reads
the current allowance and replaces missing, stale, or oversized permission
with an exact-amount approval to Coinbase's Permit2 spender. The audited receipt
must match the exact token, spender, and amount.

## Journals and Recovery

`data/execution_decisions.jsonl` records policy decisions and replay identity.
`data/live_execution_audit.jsonl` separately records `RESERVED`, `CONFIRMED`,
`BACKEND_FAILED`, and `RECEIPT_REJECTED` events. Both files use exclusive locks,
monotonic sequences, previous-entry hashes, SHA-256 entry hashes, flush, and
`fsync`.

Mount `/app/data` on a persistent Railway volume. Missing, corrupt, unwritable,
or inconsistent journal state blocks execution. A reservation without a final
event is ambiguous and remains charged to the daily ceiling.

## Railway / CDP Activation Status

1. Coinbase created the reviewed CDP EVM API-key wallet
   `lumen-trading-agent` at
   `0x716b5d6bf67a4c01103b52365c8fb5fdfef0ff06`.
2. `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, and `CDP_WALLET_SECRET` are stored
   only as masked Railway service variables.
3. Railway deployed them with `LIVE_WORKER_ENABLED=true`, live disabled,
   shadow-only mode, and the kill switch halted.
4. The existing persistent `/app/data` storage remains mounted across redeploys.
5. Deployment `af9bb3a0-e5e1-4d10-b56d-f45d0a0ef020` verified
   `base-mainnet`, chain ID 8453, the exact wallet identity, and the
   `no_funds_ready` worker state.
6. Configure the research service for 25 candidates and automatic governed
   universe refresh; verify its public feed contains fresh exact-contract packets.
7. Exercise restart, timeout, ambiguous receipt, approval, and emergency-stop
   recovery in a production-like environment.
8. Obtain separate approval before arming, depositing funds, or sending a first
   small canary.

Secrets, owner private keys, seed phrases, wallet exports, and approval links
must never enter GitHub, logs, prompts, or either journal.

Last updated: 2026-08-23
