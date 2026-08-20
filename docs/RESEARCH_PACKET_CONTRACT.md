# Research Packet Contract Version 2

The Railway Research Agent emits observation-only evidence. Version 2 adds
DEX Screener pool-selection and promotion provenance without adding strategy,
risk, wallet, signer, order, transaction, or execution authority.

## Envelope

The response contains exactly `service`, `schema_version`, `mode`, `execution`,
`generated_at`, and `packets`. `schema_version` is `2`, `mode` is
`observation_only`, and `execution` is `disabled`.

The canonical Base route is `/research/crypto/base/latest`. The legacy
`/research/latest` route is a compatibility alias and is not accepted by the
strict Trading Agent reader. Reserved equities and Bitcoin-network routes
return explicit HTTP 501 `not_configured` responses until separately built and
verified.

## Packet enrichment

Each packet retains the version-1 identity, timestamps, market fields,
warnings, digest, and explicit `OBSERVE_ONLY` boundary. Version 2 adds:

- `source.marketing_influenced`: true for profile, boost, and advertisement
  discovery; false only for the configured watchlist.
- `source.promotion_type`: `profile`, `boost`, `advertisement`, or null for the
  configured watchlist.
- `source.eligible_pair_count`: number of Base pools considered before the
  most-liquid eligible pool was selected.
- `source.base_contract_address` and `source.quote_contract_address`: exact
  contract identities used to constrain pool selection. The watchlist accepts
  WETH quoted in official USDC and official USDC quoted in legacy Base USDbC.
- `metrics.market_cap_usd` and `metrics.fdv_usd`: nullable DEX Screener values.
- `metrics.active_boosts`: nonnegative active boost count for the selected pool.

The packet digest covers every field except the response-only `is_stale` flag.

## Trading handoff

The Trading Agent accepts only exact version-2 envelopes and packet fields. It
requires current, complete configured-watchlist packets for pinned Base WETH
and official USDC contracts. Venue and pair addresses may change only through
deterministic most-liquid selection among those exact approved base/quote
identities. Promotional discovery packets cannot satisfy that gate. Missing,
extra, stale, contradictory, malformed, non-finite, negative, or
execution-claiming evidence fails closed.

This contract carries evidence only and grants no authority to trade.
