# Trading 212 Demo Controlled Order QA

## Branch

`feature/trading212-demo-controlled-order`

## Purpose

Validate a terminal-only, explicitly confirmed Trading 212 demo order path through the backend `/v1/orders` route.

## Safety gates used

- `APP_MODE=demo`
- `T212_ENVIRONMENT=demo`
- `LIVE_TRADING_ENABLED=false`
- `T212_DEMO_ORDER_ENABLED=true`
- `T212_DEMO_ORDER_CONFIRM=PLACE_DEMO_ORDER`
- Manual `T212_API_KEY` and `T212_API_SECRET` loaded only in terminal
- Disposable SQLite DB: `/tmp/t212_demo_controlled_order.db`
- Kill switch disabled only in the disposable demo-order DB
- Auto-trading enabled only in the disposable demo-order DB
- No UI order button added

## Controlled demo order result

Order route response showed:

- Ticker: `AAPL_US_EQ`
- Side: `buy`
- Quantity: `0.01`
- Status: `accepted`
- Broker order ID: `48850886521`
- Execution environment: `demo`
- Dry run: `false`

## Audit evidence

The audit trail recorded:

- `demo_broker_order_attempt`
- `demo_broker_order_success`
- `no_broker_order_sent=false`
- `broker_environment=demo`
- `execution_environment=demo`
- `is_dry_run=false`

## Validation

The following checks passed:

- Ruff check passed.
- `tests/integration/test_demo_order_boundary.py` passed.
- `tests/unit/test_demo_rc_boundary.py` passed.
- `tests/integration/test_paper_execution.py` passed.

Total focused test result:

`26 passed`

## Result

Controlled Trading 212 demo broker-backed order validation accepted.

This was demo-only. Live trading remained disabled.
