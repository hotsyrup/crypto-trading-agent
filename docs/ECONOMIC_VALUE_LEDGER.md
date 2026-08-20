# Economic Value Ledger

## Purpose

The economic-value ledger connects an agent request to its monetary cost, its
hashed result, and evidence of whether another agent actually used it. It is a
treasury-intelligence record, not spending authority and not an accounting
substitute.

## Event model

The append-only JSONL ledger has three strict event types:

1. `request_recorded` — request ID, requesting agent, provider, purpose, USD
   cost, and whether that cost is known, estimated, or unknown.
2. `result_recorded` — request ID, result ID, output type, SHA-256 output hash,
   and outcome. Full external payloads are not copied into the ledger.
3. `usage_recorded` — request ID, result ID, consuming agent, use type, and a
   bounded reference to the downstream decision or artifact.

Events are immutable, sequence-numbered, locked while appending, flushed to
disk, and SHA-256 hash chained. Tampering, truncation, extra fields, malformed
costs, invalid identifiers, and unsupported use types fail closed.

`cost_status=unknown` requires `cost=null`; a zero cost must be deliberately
recorded as `cost_status=known` and `cost="0"`. This prevents missing cost data
from masquerading as free work.

## Current state and next integration

The ledger implementation and adversarial unit tests exist in
`app/economic_value_ledger.py` and `tests/test_economic_value_ledger.py`.
Automatic writes are intentionally not yet connected to live services. The
next application step is to record one request/result pair around each research
provider call and one usage event only when the Trading Agent accepts the
packet into a named paper decision. Provider charges and allocated Railway or
model costs must remain separate fields in a future additive schema version.

This ledger cannot authorize a paid call, trade, transfer, wallet action, or
budget increase.

Last updated: 2026-08-18
