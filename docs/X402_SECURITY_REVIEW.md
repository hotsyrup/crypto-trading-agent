# x402 Security Review

Status: completed for the current observation-only architecture

Review date: 2026-08-08
Scope: Lumen operating-wallet service purchases and the trading-agent boundary

## Conclusion

The trading application currently has no x402 client, signer, facilitator
credential, payment retry loop, or paid-service execution path. Its exposure is
therefore limited to future integration risk. Lumen's separate operating wallet
has a bounded one-time budget and a verified history of small x402 purchases,
but those purchases are not controlled by this repository.

No unattended x402 payment path should be added until every mandatory control
below is implemented and tested. The new `TreasuryPolicy` layer is deliberately
observation-only and cannot execute or authorize a payment.

## Evidence reviewed

- Wang et al., *When HTTP 402 Meets the Blockchain: Risks on Emerging x402
  Payments* (2026-07-21): defines authorization-correctness and
  execution-safety rules and reports violations across all 15 facilitators
  tested. https://arxiv.org/abs/2607.19545
- Ling et al., *Free-Riding in the AI Economy* (2026-05-29): describes
  context-substitution, concurrency, atomicity, and dynamic-allowance risks.
  https://arxiv.org/abs/2605.30998
- Li et al., *402Pilot* (2026-08-02): supports separating buyer-side purchasing
  policy from payment execution and evaluating value under a finite wallet.
  https://arxiv.org/abs/2608.01341
- x402 Foundation specification: documents the client, resource-server, and
  facilitator trust boundary and the exact, upto, and batch schemes.
  https://github.com/x402-foundation/x402
- Coinbase facilitator and troubleshooting documentation: documents
  verification, settlement, version, network, signature, expiry, KYT, and
  mainnet-facilitator failure modes.
  https://docs.cdp.coinbase.com/x402/core-concepts/facilitator
  https://docs.cdp.coinbase.com/x402/support/troubleshooting

## Mandatory buyer-side controls

### Before payment

- [x] Use a separate operating wallet with a bounded balance.
- [x] Keep the trading treasury and operating wallet as distinct roles.
- [x] Require a recorded purpose, expected benefit, price, remaining budget,
  evidence, and cheaper-alternative analysis.
- [x] Reject recurring commitments and purposes outside the wallet mandate.
- [x] Keep the initial policy in observation-only mode with no signer or payer.
- [ ] Allowlist HTTPS service domains and block redirects to another origin.
- [ ] Pin or independently verify provider identity and the expected resource.
- [ ] Validate the x402 version, scheme, network, asset contract, amount,
  recipient, expiry, and nonce against the original payment requirements.
- [ ] Bind the authorization to the exact resource and request context so a
  proof cannot be substituted across resources.
- [ ] Permit only the reviewed `exact` scheme initially. Reject `upto`, batch,
  escrow, Permit2, arbitrary calldata, contract deployment, and unknown
  extensions until separately reviewed.
- [ ] Enforce deterministic per-request, daily, and lifetime spending ceilings
  outside the model and outside the service response.
- [ ] Remove personal data, secrets, private URLs, and unnecessary rationale
  from payment metadata before it leaves Lumen.

### During verification and settlement

- [ ] Use an explicitly allowlisted mainnet facilitator and verify that its
  network support matches Base mainnet (`eip155:8453`).
- [ ] Treat verification as provisional. Do not deliver success internally
  until the intended settlement is confirmed or the provider's atomic flow is
  independently verified.
- [ ] Reject expired, reused, malformed, underfunded, mismatched, or
  non-settleable authorizations.
- [ ] Enforce single-use idempotency keys and a durable replay/nonce journal.
- [ ] Bound facilitator-sponsored gas and reject any attacker-selected
  execution path.
- [ ] Never retry automatically after an ambiguous verification, timeout, or
  settlement response; reconcile first.

### After payment

- [ ] Reconcile the quoted amount, recipient, asset, transaction hash, receipt,
  wallet balance change, and delivered service result.
- [x] Record failed, rejected, timed-out, duplicated, and partially fulfilled
  attempts as well as successful purchases.
- [x] Feed service quality and usefulness back into TreasuryPolicy so future
  recommendations reflect observed value rather than provider claims.
- [ ] Halt the payment path on an unexpected transfer, duplicate, mismatch,
  facilitator anomaly, or missing audit record.
- [ ] Maintain a human-readable nightly spending report and a recoverable
  append-only machine journal.

## Current readiness decision

`NOT READY FOR UNATTENDED X402 PAYMENTS`

Observation and research may continue. A future implementation may advance
only after the unchecked controls above have code, adversarial tests, and a
recorded approval under Lumen's operating-wallet mandate.

The checked post-payment controls above are implemented as an append-only
local receipt ledger and observation-only provider scorecard. They do not
capture payments automatically and do not create an x402 client, signer,
facilitator credential, retry loop, or authorization path.
