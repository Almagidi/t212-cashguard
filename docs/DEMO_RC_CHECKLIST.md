# Demo RC Checklist

Demo mode is broker-backed Trading 212 demo execution only. It is not guarded Live mode and must not enable live trading.

## Required Configuration

- Set `APP_MODE=demo`.
- Keep `LIVE_TRADING_ENABLED=false`.
- Configure demo credentials only with `T212_DEMO_API_KEY` and `T212_DEMO_API_SECRET`, or connect an encrypted demo broker connection through the backend.
- Do not use `T212_LIVE_API_KEY` or `T212_LIVE_API_SECRET` for Demo RC. If live credentials are present, demo mode must still select `https://demo.trading212.com`.
- Never place broker secrets in `NEXT_PUBLIC_*`.

## Automated Proof

- `apps/api/tests/unit/test_demo_rc_boundary.py` verifies demo adapter construction, demo-only base URL selection, missing demo credentials, paper/mock broker blocking, kill-switch blocking, and demo audit events.
- The mocked `httpx.MockTransport` test fails immediately if `https://live.trading212.com` is called.
- Integration coverage verifies missing demo credentials are surfaced as a safe blocked broker status and mocked demo broker execution does not require real Trading 212 credentials.
- Existing mock/paper E2E must continue to run with `MARKET_DATA_PROVIDER=mock`.

## Audit Expectations

Demo order attempts must produce safe, distinct audit actions:

- `demo_broker_order_attempt`
- `demo_broker_order_success`
- `demo_broker_order_failure`
- `demo_order_blocked_by_kill_switch`

Payloads may include runtime mode, broker environment, ticker, side, order id, decision, reason, and broker order id. Payloads must not include API keys, secrets, auth headers, tokens, or raw credential values.

## Manual Demo Test Gate

Before any manually supervised real demo order test:

- Run the full backend test suite.
- Run the mock/paper Playwright smoke suite.
- Confirm `/v1/health/startup` reports demo credentials as configured and live trading disabled.
- Confirm the UI shows demo mode, demo endpoint only, and live endpoint blocked.
- Keep the order size deliberately tiny and supervise the broker account directly.

## Manual supervised demo test

Before any real Trading 212 demo endpoint test, follow:

- [Manual Demo RC Runbook](./MANUAL_DEMO_RC_RUNBOOK.md)

The manual demo test must remain:

- supervised
- demo-only
- live-disabled
- kill-switch controlled
- auto-trading disabled
- auditable
