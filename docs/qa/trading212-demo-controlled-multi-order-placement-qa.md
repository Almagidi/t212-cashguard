# Trading 212 DEMO Controlled Multi-Order Placement QA

## Purpose

Validate a terminal-only, explicitly confirmed Trading 212 DEMO smoke that places a tiny bounded set of broker-backed orders through the existing backend `/v1/orders` route.

This is higher risk than reconciliation because it intentionally calls a broker write endpoint. Reconciliation only reads Trading 212 order history. This smoke therefore requires stronger operator confirmation, a bounded operator-supplied order plan, DEMO-only credentials, live trading disabled, and the demo reconciliation scheduler disabled during placement.

## Safety Boundaries

- DEMO only: `APP_MODE=demo` and `T212_ENVIRONMENT=demo`.
- Live trading must stay disabled: `LIVE_TRADING_ENABLED=false`.
- Controlled demo order gate must be open: `T212_DEMO_ORDER_ENABLED=true`.
- Multi-order smoke gate must be open only in the terminal: `T212_DEMO_MULTI_ORDER_ENABLED=true`.
- Operator must confirm: `T212_DEMO_MULTI_ORDER_CONFIRM=PLACE_MULTI_DEMO_ORDERS`.
- Scheduler must be disabled: `DEMO_RECONCILIATION_SCHEDULER_ENABLED=false`.
- Order plan is explicit. There are no hidden default broker orders.
- Default maximum is 2 orders. Hard cap is 3 orders.
- Quantity must be positive and no larger than `0.05`.
- Duplicate tickers are rejected.
- The script stops after the first placement failure.
- No strategy-generated orders are added.
- No frontend buy/sell controls are added.
- No live Trading 212 endpoint is used.

## Required Environment

Preferred credentials:

```bash
T212_DEMO_API_KEY=...
T212_DEMO_API_SECRET=...
```

Fallback credentials, only if the demo-specific names are absent:

```bash
T212_API_KEY=...
T212_API_SECRET=...
```

Placement gates:

```bash
APP_MODE=demo
T212_ENVIRONMENT=demo
LIVE_TRADING_ENABLED=false
T212_DEMO_ORDER_ENABLED=true
T212_DEMO_MULTI_ORDER_ENABLED=true
T212_DEMO_MULTI_ORDER_CONFIRM=PLACE_MULTI_DEMO_ORDERS
DEMO_RECONCILIATION_SCHEDULER_ENABLED=false
T212_DEMO_MULTI_ORDER_PLAN=AAPL_US_EQ:0.01,MSFT_US_EQ:0.01
```

## Exact Command

Start the disposable controlled-order API:

```bash
make t212-demo-controlled-order-start
```

Arm only the disposable local SQLite DB:

```bash
T212_DEMO_MULTI_ORDER_CONFIRM=PLACE_MULTI_DEMO_ORDERS \
make t212-demo-controlled-multi-order-arm
```

Run the placement smoke:

```bash
T212_DEMO_MULTI_ORDER_CONFIRM=PLACE_MULTI_DEMO_ORDERS \
T212_DEMO_MULTI_ORDER_PLAN="AAPL_US_EQ:0.01,MSFT_US_EQ:0.01" \
T212_DEMO_API_KEY=... \
T212_DEMO_API_SECRET=... \
make t212-demo-controlled-multi-order
```

Stop the disposable API when finished:

```bash
make t212-demo-controlled-order-stop
```

## Expected Output

The script prints a dry summary before broker placement:

```text
Controlled Trading 212 DEMO multi-order placement smoke
run_id=t212-demo-multi-order-...
Live trading: disabled
Broker environment: Trading 212 demo
Scheduler: disabled
Order plan:
  1. ticker=AAPL_US_EQ quantity=0.01
  2. ticker=MSFT_US_EQ quantity=0.01
```

The final JSON summary includes:

```json
{
  "run_id": "...",
  "requested_orders": 2,
  "attempted": 2,
  "accepted": 2,
  "failed": 0,
  "skipped_rejected_before_broker": 0,
  "safety": {
    "live_trading_enabled": false,
    "broker_environment": "demo",
    "no_live_endpoint": true,
    "operator_confirmed": true,
    "bounded_order_count": true
  }
}
```

Each order row reports `ticker`, `quantity`, `local_order_id`, `broker_order_id`, `local_status`, `execution_environment`, `is_dry_run`, `outcome`, and `error_category` when failed.

## Verify Local DB Rows

Use the disposable DB path unless `T212_DEMO_ORDER_DB_PATH` was overridden:

```bash
sqlite3 /tmp/t212_demo_controlled_order.db \
  "select id,ticker,quantity,status,broker_order_id,execution_environment,is_dry_run from orders order by created_at desc limit 5;"
```

Expected rows have `execution_environment=demo`, `is_dry_run=0`, and non-empty `broker_order_id` values for accepted placements.

Audit checks:

```bash
sqlite3 /tmp/t212_demo_controlled_order.db \
  "select action,payload from audit_logs where action like 'demo_broker_order_%' order by occurred_at desc limit 10;"
```

Expected actions include `demo_broker_order_attempt` and `demo_broker_order_success`. Secrets must not appear in audit payloads.

## Reconciliation Handoff

Do not run scheduled reconciliation for this smoke. After placement, run the existing manual read-only multi-order reconciliation smoke:

```bash
T212_DEMO_RECONCILE_CONFIRM=READ_DEMO_ORDER_HISTORY \
T212_DEMO_API_KEY=... \
T212_DEMO_API_SECRET=... \
LIVE_TRADING_ENABLED=false \
DEMO_RECONCILIATION_WORKER_ENABLED=true \
DEMO_RECONCILIATION_SCHEDULER_ENABLED=false \
make t212-demo-multi-order-reconciliation-smoke
```

## Confirm Live Trading Stayed Disabled

The placement command forces `LIVE_TRADING_ENABLED=false`, `APP_MODE=demo`, and `T212_ENVIRONMENT=demo`. Confirm no live endpoint was used by checking the script summary safety markers and API logs:

```bash
grep -i "live" /tmp/t212_demo_controlled_order_api.log
```

There should be no live broker connection or live order submission.

## Confirm No Frontend Controls Were Added

This milestone is terminal-only. Confirm the diff does not touch frontend app code:

```bash
git diff -- apps/web
```

The command should be empty for this PR.

## Limitations

- DEMO only.
- Terminal-only.
- Explicitly confirmed.
- Bounded order count.
- No strategy-generated orders.
- No live trading.
- No automated trading.
- No scheduler involvement during placement smoke.
- No guarantee all symbols are available in Trading 212 DEMO.
- Broker rate limits, auth errors, and unsupported-instrument errors may occur.
- Reconciliation still depends on Trading 212 history availability.

## Rollback And Cleanup

- Stop the disposable API: `make t212-demo-controlled-order-stop`.
- Remove the disposable DB if no longer needed: `rm -f /tmp/t212_demo_controlled_order.db`.
- Do not reuse the smoke confirmation variables in a normal shell session.
- If a broker order is accepted but later cannot be reconciled immediately, wait for Trading 212 history availability and rerun the read-only reconciliation smoke.
