# Trading 212 DEMO Reconciliation Worker QA

## Purpose

The Trading 212 DEMO reconciliation worker runs one bounded, read-only pass that keeps local demo broker-backed orders aligned with Trading 212 order history. It replaces repeated manual single-order smoke scripts for routine status refreshes.

## Safety Boundaries

- Runs only when `APP_MODE=demo`.
- Runs only when `T212_ENVIRONMENT=demo`.
- Refuses to run when `LIVE_TRADING_ENABLED=true`.
- Requires a demo broker adapter/environment.
- Selects only local `execution_environment="demo"` orders.
- Ignores paper/dry-run orders, live orders, terminal orders, and orders without `broker_order_id`.
- Calls only `GET /api/v0/equity/history/orders` through the existing reconciliation service.
- Does not submit, cancel, modify, deposit, or withdraw.
- Does not log Trading 212 credentials or secrets.

## Configuration

Defaults are conservative:

- `DEMO_RECONCILIATION_WORKER_ENABLED=false`
- `DEMO_RECONCILIATION_BATCH_SIZE=10`
- `DEMO_RECONCILIATION_MIN_INTERVAL_SECONDS=30`
- `DEMO_RECONCILIATION_LOOKBACK_HOURS=24`
- `DEMO_RECONCILIATION_MAX_ATTEMPTS_PER_RUN=10`
- `DEMO_RECONCILIATION_HISTORY_LIMIT=50`

The worker is scheduler-neutral and one-shot. A future PR can wire it to Celery beat, cron, APScheduler, or another scheduler.

## Manual Smoke Command

Use the controlled demo-order database created by the existing demo order flow, or set `DATABASE_URL` to a database containing local demo orders with `broker_order_id`.

```bash
T212_DEMO_RECONCILE_CONFIRM=READ_DEMO_ORDER_HISTORY \
T212_API_KEY=... \
T212_API_SECRET=... \
make t212-demo-reconciliation-worker
```

Expected output includes a JSON summary with:

- `outcome`
- `worker_enabled`
- `candidates_found`
- `attempted`
- `succeeded`
- `missing`
- `rate_limited`
- `failed`
- `no_broker_order_sent: true`
- `read_only_broker_calls: true`
- `live_trading_enabled: false`

## API Endpoints

- `GET /v1/broker/trading212/reconciliation/status`
- `POST /v1/broker/trading212/reconciliation/run-once`

The status endpoint is read-only. The run-once endpoint requires admin auth, uses the same demo safety gates, and returns the typed worker summary.

## Audit Events

Worker-level events:

- `demo_reconciliation_worker_started`
- `demo_reconciliation_worker_completed`
- `demo_reconciliation_worker_rate_limited`

The existing per-order reconciliation events remain unchanged:

- `demo_order_reconciliation_attempt`
- `demo_order_reconciliation_success`
- `demo_order_reconciliation_missing`
- `demo_order_reconciliation_rate_limited`
- `demo_order_reconciliation_failed`
- `demo_order_reconciliation_unknown_status`

## Known Limitations

- No automated strategy execution.
- No live trading.
- No frontend buy/sell controls.
- No scheduled recurring task yet.
- History pagination remains limited to the configured first page.
- Rate-limit behavior is conservative: the worker stops the current batch and preserves local order status unless reconciliation succeeds.
- Multi-order real broker validation should be performed in a later controlled DEMO QA pass.
