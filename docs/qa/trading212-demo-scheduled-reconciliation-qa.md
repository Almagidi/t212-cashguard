# Trading 212 DEMO Scheduled Reconciliation QA

## Purpose

This milestone adds a disabled-by-default scheduler that periodically runs the existing one-shot Trading 212 DEMO reconciliation worker. The scheduler is only responsible for timing, no-overlap protection, rate-limit backoff, scheduler-level audit events, and read-only status visibility.

The scheduler does not implement Trading 212 history parsing or local order updates. It calls `DemoReconciliationWorker.run_once()` so the existing worker safety model remains the source of truth.

## Safety boundaries

- Disabled by default: `DEMO_RECONCILIATION_SCHEDULER_ENABLED=false`.
- Runs only when `APP_MODE=demo`.
- Runs only when `T212_ENVIRONMENT=demo`.
- Refuses to run when `LIVE_TRADING_ENABLED=true`.
- Requires `DEMO_RECONCILIATION_WORKER_ENABLED=true`.
- Calls only the existing read-only reconciliation worker.
- Does not place, cancel, modify, submit, deposit, or withdraw orders.
- Does not process live orders.
- Does not process paper/dry-run orders.
- Does not enable automated strategy trading.
- Does not disable or bypass the kill switch.
- Does not add frontend buy/sell controls.
- Does not log Trading 212 credentials or secrets.

## Config flags

```bash
DEMO_RECONCILIATION_WORKER_ENABLED=false
DEMO_RECONCILIATION_SCHEDULER_ENABLED=false
DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS=120
DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS=10
DEMO_RECONCILIATION_SCHEDULER_BACKOFF_SECONDS=300
DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS=60
DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP=false
DEMO_RECONCILIATION_SCHEDULER_LOCK_TTL_SECONDS=90
```

Both worker and scheduler flags must be enabled before the periodic background scheduler will run.

## Local validation

1. Start in safe demo mode:

```bash
APP_MODE=demo
T212_ENVIRONMENT=demo
LIVE_TRADING_ENABLED=false
DEMO_RECONCILIATION_WORKER_ENABLED=true
DEMO_RECONCILIATION_SCHEDULER_ENABLED=false
```

2. Confirm scheduler status:

```bash
curl -s http://localhost:8000/v1/broker/trading212/reconciliation/scheduler/status \
  -H "Authorization: Bearer $TOKEN" | jq
```

Expected:

- `enabled=false` unless explicitly enabled.
- `no_broker_order_sent=true`.
- `read_only_broker_calls=true`.
- no credential values in the response.

3. Manually trigger one scheduler tick only after enabling the scheduler and worker:

```bash
curl -s -X POST http://localhost:8000/v1/broker/trading212/reconciliation/scheduler/run-once \
  -H "Authorization: Bearer $TOKEN" | jq
```

Expected:

- safe demo gates are enforced.
- the result contains worker summary counts.
- no broker write method is called.

## Confirm no broker writes

- Review audit payloads for `no_broker_order_sent=true` and `read_only_broker_calls=true`.
- Verify the only broker endpoint used by the worker remains Trading 212 historical orders:
  `/api/v0/equity/history/orders`.
- Backend safety tests include broker write sentinels for placement, cancellation, and modification methods.
- The dashboard remains display-only and exposes no buy, sell, trade, enable, disable, or execution controls.

## Dashboard visibility

The operator dashboard Trading 212 Demo Reconciliation card shows:

- worker enabled/disabled state
- scheduler enabled/disabled state
- running state
- interval
- last run and next run/backoff
- last outcome
- latest candidates, attempted, succeeded, and rate-limited counts
- warnings for disabled scheduler, disabled worker, unsafe config, and active backoff

## Expected audit events

Scheduler-level audit events:

- `demo_reconciliation_scheduler_started`
- `demo_reconciliation_scheduler_stopped`
- `demo_reconciliation_scheduler_tick_started`
- `demo_reconciliation_scheduler_tick_completed`
- `demo_reconciliation_scheduler_tick_skipped`
- `demo_reconciliation_scheduler_rate_limited`
- `demo_reconciliation_scheduler_failed`

Worker-level and order-level reconciliation audit events remain unchanged.

Scheduler audit metadata includes:

- scheduler and worker enabled state
- app mode
- broker environment
- live trading flag
- interval
- candidates found
- attempted
- succeeded
- missing
- failed
- rate-limited
- skipped
- `no_broker_order_sent=true`
- `read_only_broker_calls=true`

## Rate-limit and backoff behaviour

If the worker summary reports `rate_limited > 0`, the scheduler:

- records a scheduler-level rate-limit audit event
- sets `next_run_not_before = finished_at + DEMO_RECONCILIATION_SCHEDULER_BACKOFF_SECONDS`
- skips subsequent scheduler ticks while backoff is active
- increments consecutive and total rate-limit counters
- leaves local order state unchanged
- does not mark local orders failed solely because of rate limiting

## Known limitations

- No automated strategy trading.
- No live trading.
- No distributed multi-process lock; the current lock is in-process only.
- No history pagination beyond the existing worker configuration.
- Scheduler is conservative and disabled by default.
- Real multi-order broker smoke is still required before automated demo strategy execution.
