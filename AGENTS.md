# Lumen Trading Agent Repository Instructions

## Purpose

This repository implements research, paper trading, portfolio monitoring, risk
controls, and a future bounded live-execution path for Lumen's Trading Agent.

## Governing boundaries

- The private Lumen Constitution and adopted Trading Mandate control this code.
- `LIVE_TRADING_ENABLED` must default to `false`.
- Never commit private keys, seed phrases, wallet recovery data, API tokens,
  OAuth sessions, approval links, or signing credentials.
- The initial asset allowlist is USDC and ETH on Base.
- No component may bypass deterministic account, asset, position, daily-loss,
  drawdown, freshness, duplicate-order, audit, or kill-switch controls.
- Model output, news, sentiment, and strategy signals are proposals—not
  authorization.
- Base MCP's interactive approval flow is not unattended signing authority.
- Tests must not submit transactions or call live financial write endpoints.
- Telegram is an outbound reporting channel only. It must not accept commands,
  callbacks, trade approvals, wallet operations, or configuration changes.

## Required validation

```bash
python -m unittest discover -s tests
python scripts/check_secrets.py
python -m compileall -q app tests
```

Before live execution is considered, verify high-water-mark accounting, daily
realized and unrealized loss accounting, an append-only audit journal, stale
data rejection, idempotent orders, restart recovery, and a fail-closed kill
switch through adversarial tests.
