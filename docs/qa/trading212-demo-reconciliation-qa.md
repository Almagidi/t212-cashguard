# Trading 212 Demo Reconciliation QA

Branch: `feature/trading212-demo-reconciliation`

## Purpose

Add terminal-only reconciliation for a local Trading 212 DEMO order that already
has a `broker_order_id`. The reconciler reads Trading 212 demo order history,
matches the broker history row by broker order id, updates the local order only
after a confirmed match, and records audit events for each attempt and outcome.

## Safety Gates

- `APP_MODE=demo`
- `T212_ENVIRONMENT=demo`
- `LIVE_TRADING_ENABLED=false`
- local order must have `execution_environment=demo`
- local order must have `is_dry_run=false`
- broker adapter environment must be `demo`
- no frontend trade or execute controls are added
- no broker order placement or cancellation endpoints are called

## Read-Only Broker Endpoint

The service reuses the existing adapter method:

- `Trading212Adapter.get_historical_orders()`
- `GET /api/v0/equity/history/orders`

Trading 212 returns order history as `{ "items": [...] }`. Real DEMO history
items may be nested as `{ "order": {...}, "fill": {...} }`; the broker order id
can appear at `order.id`, the status at `order.status`, and fill quantity,
price, and timestamp under `fill`. The reconciliation smoke must keep the
history request capped at `limit=50` because Trading 212 rejects larger values.

## Audit Events

- `demo_order_reconciliation_attempt`
- `demo_order_reconciliation_success`
- `demo_order_reconciliation_missing`
- `demo_order_reconciliation_unknown_status`
- `demo_order_reconciliation_rate_limited`
- `demo_order_reconciliation_failed`

## Smoke Test

Run from the repo root after the controlled demo-order DB contains the local
order to reconcile. Use either the local order id or broker order id.

```bash
T212_API_KEY=... \
T212_API_SECRET=... \
T212_DEMO_RECONCILE_CONFIRM=READ_DEMO_ORDER_HISTORY \
T212_DEMO_RECONCILE_BROKER_ORDER_ID=48850886521 \
make t212-demo-reconcile-order
```

The script prints the local order id, broker order id, previous local status,
broker status, new local status, match result, outcome, and audit event names.
It does not print secrets.

## Non-Goals

- no new broker orders
- no live trading
- no automated strategy-driven demo execution
- no Kraken reconciliation
- no frontend order controls

## Validation Commands

```bash
cd apps/api
python3.12 -m ruff check app/services/demo_order_reconciliation.py scripts/t212_demo_reconcile_order.py tests/integration/test_demo_reconciliation.py --fix
python3.12 -m ruff format app/services/demo_order_reconciliation.py scripts/t212_demo_reconcile_order.py tests/integration/test_demo_reconciliation.py
python3.12 -m ruff check app/services/demo_order_reconciliation.py scripts/t212_demo_reconcile_order.py tests/integration/test_demo_reconciliation.py
python3.12 -m pytest tests/integration/test_demo_reconciliation.py -q --no-cov
python3.12 -m pytest tests/unit/test_demo_rc_boundary.py tests/integration/test_demo_order_boundary.py tests/integration/test_paper_execution.py -q --no-cov
cd ../..
git diff --check
```
