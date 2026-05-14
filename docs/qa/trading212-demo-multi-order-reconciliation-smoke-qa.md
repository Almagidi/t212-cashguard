# Trading 212 DEMO Multi-Order Reconciliation Smoke QA

## Purpose

Validate that multiple local Trading 212 DEMO broker-backed orders can be reconciled in one bounded manual smoke run. The smoke uses existing local orders that already have `broker_order_id` values and delegates reconciliation to the existing read-only worker and order reconciliation service.

This milestone verifies the reliability layer after controlled DEMO order placement and single-order reconciliation: local multi-order reconciliation can read broker history, update matching local orders, stop safely on rate limits, and summarize every attempted order without broker write calls.

## Safety Boundaries

- Requires `APP_MODE=demo`.
- Requires `T212_ENVIRONMENT=demo`.
- Requires `LIVE_TRADING_ENABLED=false`.
- Requires `DEMO_RECONCILIATION_WORKER_ENABLED=true`.
- Requires `DEMO_RECONCILIATION_SCHEDULER_ENABLED=false` during the manual smoke to avoid concurrent scheduler interference.
- Selects only local `execution_environment="demo"` orders with `venue="t212"`.
- Skips terminal orders, dry-run/paper orders, non-demo orders, stale orders outside the lookback window, recently reconciled orders, and orders without `broker_order_id`.
- Calls only the Trading 212 DEMO order history read path: `GET /api/v0/equity/history/orders`.
- Wraps the broker in a read-only guard that fails if a write-like method is invoked.
- Does not submit, cancel, modify, deposit, or withdraw.
- Does not execute strategy code.
- Does not add frontend buy/sell controls.
- Does not enable live trading.


> Credential note: the smoke script prefers `T212_DEMO_API_KEY` and `T212_DEMO_API_SECRET`. It falls back to generic `T212_API_KEY` and `T212_API_SECRET` only if the demo-specific names are absent.

## Required Env Vars

```bash
T212_DEMO_RECONCILE_CONFIRM=READ_DEMO_ORDER_HISTORY
T212_DEMO_API_KEY=...
T212_DEMO_API_SECRET=...
LIVE_TRADING_ENABLED=false
DEMO_RECONCILIATION_WORKER_ENABLED=true
DEMO_RECONCILIATION_SCHEDULER_ENABLED=false
```

Optional DB selection:

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/t212_demo_controlled_order.db
```

If `DATABASE_URL` is omitted, the Make target defaults to the existing controlled demo-order local SQLite DB path configured by `T212_DEMO_ORDER_DB_PATH`.

## Exact Command

```bash
T212_DEMO_RECONCILE_CONFIRM=READ_DEMO_ORDER_HISTORY \
T212_DEMO_API_KEY=... \
T212_DEMO_API_SECRET=... \
LIVE_TRADING_ENABLED=false \
DEMO_RECONCILIATION_WORKER_ENABLED=true \
DEMO_RECONCILIATION_SCHEDULER_ENABLED=false \
make t212-demo-multi-order-reconciliation-smoke
```

## Expected Output

The smoke prints one line per attempted order:

```text
Orders:
  1. local_order_id=... broker_order_id=... ticker=AAPL previous_status=accepted broker_status=FILLED new_status=filled matched=true outcome=success
```

It then prints an aggregate JSON payload:

```json
{
  "outcome": "completed",
  "orders_considered": 3,
  "attempted": 3,
  "succeeded": 2,
  "missing": 1,
  "rate_limited": 0,
  "failed": 0,
  "no_broker_order_sent": true,
  "live_trading_enabled": false,
  "broker_write_calls": []
}
```

`orders_considered` mirrors the worker candidate count. `attempted` can be lower when the configured maximum attempts per run is reached or a rate limit stops the batch.

## No Broker Writes

The smoke uses `DemoReconciliationWorker`, which delegates to `DemoOrderReconciler`. That path reads broker order history only. The script also wraps the Trading 212 adapter in a guard that imports the central Trading 212 broker write-method inventory and fails closed if reconciliation attempts any inventoried write method, including order placement, stop-order placement, cancellation, submission, or modification methods.

Update the central broker write-method inventory whenever `Trading212Adapter` gains a new write-like method. Smoke guards and tests reuse that inventory so new adapter write methods are blocked consistently.

The expected aggregate output must include:

```json
{
  "no_broker_order_sent": true,
  "broker_write_calls": []
}
```

## No Live Trading

The smoke refuses to start unless the environment is explicitly DEMO and `LIVE_TRADING_ENABLED=false`. It does not arm the controlled demo-order placement gate and does not place new broker orders.

## Limitations

- It reconciles existing eligible local DEMO orders only.
- It does not automatically place multiple Trading 212 broker orders.
- It depends on the configured local database already containing demo broker-backed orders with `broker_order_id`.
- It reads the configured Trading 212 history page size only.
- It stops the current batch on Trading 212 rate limits.
- It is a manual smoke, not a recurring scheduler run.
- It does not validate frontend behavior.

## Next Milestone

Add a controlled, explicitly confirmed multi-order DEMO placement QA milestone that can seed a small bounded set of broker-backed demo orders before this reconciliation smoke runs. That future milestone should use a confirmation gate at least as strong as the existing single controlled demo-order gate and remain DEMO-only.
