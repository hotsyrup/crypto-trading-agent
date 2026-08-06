# Security Policy

## Financial safety

This project may eventually propose or submit financial transactions. A code
change, connected wallet, environment variable, or deployed service does not
expand the written trading mandate.

Live execution must remain disabled after any security-control failure,
unreconciled balance, stale price, missing audit record, unexpected account,
unknown asset, duplicate request, or unavailable kill switch.

## Credentials

Never commit or log wallet private keys, seed phrases, recovery information,
exchange keys, API tokens, approval URLs, session cookies, or authentication
codes. Use a policy-controlled signer or secret manager only after a separate
review and authorization.

If a credential is exposed, halt the affected runtime, rotate or revoke it,
inspect relevant transaction and access logs, and record only non-secret
incident facts.
