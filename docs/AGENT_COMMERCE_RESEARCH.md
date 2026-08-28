# Lumen Agent Commerce Research Gate

Status: implemented and tested; production activation is deployment-gated

Date: 2026-08-28

## Authority boundary

The Agent Commerce integration is a risk-control gate for an independently
generated trading candidate. It is invoked only after the existing portfolio
strategy has selected `BUY` or `SELL`, bounded the notional, and derived the
deterministic trading-decision ID. A report can veto that candidate. It cannot
create a candidate, choose an asset or side, change notional or position sizing,
authorize execution, or bypass any existing risk, kill-switch, live-mode,
receipt, or wallet control.

The exact decision seam is in
`app.portfolio_trading.execute_research_portfolio_signal`: after `intent_id` is
derived and before `TradeIntent`, `ApprovedSwap`, and
`execute_controlled_live_trade` are reached.

## Runtime modes

`LUMEN_AGENT_COMMERCE_RESEARCH_MODE` is the only feature flag and defaults to
`disabled`.

- `disabled`: no service call, signature, payment, or behavior change.
- `shadow`: validate the unpaid pinned x402 challenge, create no signature,
  spend nothing, audit the candidate, and reject it safely.
- `enforced`: apply cache, budget, payment, settlement, report, and veto rules.

The public health response exposes the mode and hard limits, but no credential,
payment authorization, or secret material.

## Hard payment policy

- Endpoint:
  `https://lumen-agent-commerce-production.up.railway.app/v1/research`
- Scheme: x402 v2 `exact` only
- Network: Base mainnet `eip155:8453`
- Asset: official Base USDC
  `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- Amount: exactly 1,000,000 atomic USDC (`$1.00`)
- Recipient: pinned to the reviewed Agent Commerce payee
- Maximum authorization life: 300 seconds
- Per-report ceiling: `$1.00`
- Rolling 24-hour ceiling: `$5.00`
- Per normalized asset: at most one authorization in any rolling 24 hours

The limits, network, asset, endpoint, recipient, and payment scheme are
hard-coded and revalidated before the CDP signer boundary independently checks
the EIP-712 domain and complete EIP-3009 message.

## Single-attempt settlement

The client performs one unpaid request to obtain the x402 challenge and at most
one signed request. HTTP retries and redirects are disabled. The signed payment
header is kept in memory and is never logged or journaled.

Before signing, a hash-chained, file-locked, fsynced journal reserves `$1.00`.
Reservations are never automatically released. A timeout, invalid settlement,
missing report, process interruption, or other ambiguous outcome remains
charged against both the asset and rolling budget until a separate human-led
reconciliation is implemented. A subsequent candidate cannot repay it.

Success requires all of the following:

1. A successful pinned x402 settlement header for the exact payer, network,
   amount, and transaction hash.
2. A successful Base receipt.
3. Exactly one official-USDC `Transfer` log from the CDP wallet to the pinned
   payee for 1,000,000 atomic units.
4. A bounded, current, structurally valid research report.

Any failure rejects only that candidate; the worker continues its normal cycle.

## Cache and veto policy

The durable audit journal caches each successful report's decision-relevant
fields for 24 hours per normalized Base asset. Every candidate in that window
reuses the same report and records its own linked evaluation with a cache hit.

A candidate is vetoed when:

- `verdict` is `avoid`;
- `thesis_status` is `contradicted`; or
- `confidence` is `high` and `red_flags` is non-empty.

All other valid reports merely allow the pre-existing candidate to proceed to
the unchanged executor. They are not trade approvals.

## Durable audit record

Every shadow or enforced evaluation records the normalized asset, request time,
amount, payment and settlement status, report ID and `as_of`, verdict, thesis
status, confidence, red flags, cache status, final veto/pass, and associated
trading-decision ID. The journal also records the settlement transaction hash
when available. It deliberately excludes signatures, full authorization
headers, wallet credentials, API credentials, private keys, seeds, and OTPs.

The default path is `data/agent_commerce_research_v1.jsonl` on the Railway
persistent volume.

## Activation gates

Production may move from `disabled` to `enforced` only after:

- canonical repository, Railway project, environment, and service confirmation;
- focused and full test success;
- a successful production build and healthy runtime;
- verification that the exact CDP Base account can sign only the pinned
  `$1.00` official-USDC authorization;
- a non-spending `shadow` cycle; and
- explicit readback of the `$1/report`, `$5/24-hour`, and one-per-asset limits.

Activation does not authorize an immediate test purchase. The first purchase
must wait for the unchanged strategy to produce a genuine candidate.
