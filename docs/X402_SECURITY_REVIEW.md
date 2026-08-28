# x402 Security Review

Status: bounded Agent Commerce client implemented; activation remains gated

Review dates: 2026-08-08 and 2026-08-28
Scope: the controlled-live trading worker's veto-only Agent Commerce purchase

## Conclusion

The previous observation-only decision is superseded only for the single
hard-coded Agent Commerce research product documented in
`docs/AGENT_COMMERCE_RESEARCH.md`. No generic purchasing, arbitrary URL,
arbitrary recipient, alternate token, alternate network, dynamic amount,
Permit2, batch, `upto`, or model-directed payment capability was added.

The code is ready for a disabled production deployment and a non-spending
shadow verification. Enforced purchasing is not ready until the recorded
deployment, health, wallet, and shadow gates pass.

## Implemented controls

### Before payment

- [x] Invoke the gate only for a buy/sell candidate created by the existing
  deterministic strategy.
- [x] Default the feature flag to `disabled`.
- [x] Pin the HTTPS endpoint and reject redirects.
- [x] Pin x402 v2, `exact`, Base `eip155:8453`, official USDC, 1,000,000 atomic
  units, the reviewed payee, and a maximum 300-second authorization.
- [x] Reject Permit2, alternate schemes, alternate resources, and changed
  challenge terms.
- [x] Enforce `$1.00` per report, `$5.00` per rolling 24 hours, and one
  authorization per normalized asset per rolling 24 hours outside model and
  service output.
- [x] Remove holdings, credentials, and secret material from the request and
  audit record.
- [x] Revalidate the complete EIP-712 domain and EIP-3009 authorization at the
  CDP signing boundary.

### During payment and settlement

- [x] Reserve budget in a locked, hash-chained, fsynced journal before signing.
- [x] Use a deterministic single-use CDP signing idempotency key.
- [x] Disable HTTP redirects and retries.
- [x] Make at most one signed request.
- [x] Treat every post-reservation error as ambiguous and keep it charged.
- [x] Require exact payer, network, amount, and transaction in the x402
  settlement response.
- [x] Independently require a successful Base receipt and exactly one pinned
  official-USDC transfer log for 1,000,000 atomic units.

### After payment

- [x] Validate a bounded, current report before it can influence a candidate.
- [x] Cache successful report controls for 24 hours per normalized asset.
- [x] Apply only the three approved veto rules.
- [x] Preserve every existing strategy, risk, size, kill-switch, execution, and
  receipt control after a favorable report.
- [x] Record all required candidate, report, payment, cache, decision, and
  settlement fields without authorization headers or credentials.
- [x] Fail closed for the affected candidate while leaving the worker alive.

## Adversarial test coverage

Tests cover candidate versus non-candidate behavior, disabled and shadow modes,
the per-asset cache, rolling-window expiry, exact per-call and rolling budget
ceilings, same-asset concurrency, every veto rule, favorable pass behavior,
malformed and stale reports, pre-signing unavailability, ambiguous settlement
without repayment, tamper-evident audit persistence, and proof that favorable
research cannot bypass the existing executor halt.

## Remaining operational gates

- [ ] Deploy to the confirmed canonical Railway service with research disabled.
- [ ] Verify the deployed worker is healthy.
- [ ] Complete a funded but non-spending production shadow cycle.
- [ ] Confirm the exact CDP account's production signing path and hard-limit
  readback.
- [ ] Enable enforced mode only if every preceding gate passes.

## References

- x402 Foundation specification: https://github.com/x402-foundation/x402
- Coinbase CDP EVM account signing:
  https://docs.cdp.coinbase.com/api-reference/v2/rest-api/evm-accounts/evm-accounts
- Agent Commerce catalog:
  https://lumen-agent-commerce-production.up.railway.app/v1/catalog
- Agent Commerce OpenAPI:
  https://lumen-agent-commerce-production.up.railway.app/openapi.json
- Agent Commerce instructions:
  https://lumen-agent-commerce-production.up.railway.app/llms.txt
