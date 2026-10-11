# Reconciliation ownership

Reconciliation asks the broker what happened to an order and updates the local order row.
It must have one owner at a time, so that two runs never decide the fate of the same order
at the same moment.

## What starts a reconciliation

| Entry point | Where |
|---|---|
| Scheduled task `reconcile_pending_orders` | Celery worker, `app/workers/tasks.py` |
| Demo reconciliation worker, run by the in-process scheduler | API process, `app/services/demo_reconciliation_scheduler.py` |
| Operator routes `POST /v1/broker/trading212/reconciliation/run-once` and `.../scheduler/run-once` | API process |
| Terminal scripts `t212_demo_reconciliation_worker.py`, `t212_demo_multi_order_reconciliation_smoke.py`, `t212_demo_reconcile_order.py` | `apps/api/scripts` |

## The rule

Every entry point takes the same distributed lock (`app/services/reconciliation_owner.py`,
Redis key `celery:task_lock:reconcile_pending_orders`) before it selects orders or reads
the broker. It commits its changes before it lets the lock go, and a run that fails or is
stopped discards its unsaved changes before it lets the lock go.

- **Not the owner:** nothing is selected and the broker is not read. The demo worker
  returns outcome `skipped_locked` and writes one audit row; the scheduler records a
  skipped tick (reason `reconciliation_owner_busy`), not a successful run; the scheduled
  task returns `already_running`.
- **Redis unavailable:** nobody can tell whether the lock is held, so nothing is
  reconciled. The demo worker raises; the operator route answers 503; the scheduler
  records a failed run. The scheduled task skips (it logs the skip at debug level only).
- **Time limit:** a demo run is stopped at `DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS`
  (60 by default), always before the lock's lease ends (90 by default, never above 300
  unless the run limit itself is longer). A stopped run discards its unsaved work; if the
  stop lands while the run is saving, some results may already be saved. The operator
  route answers 504 and the next run checks the orders again.
- The lock name is the one the scheduled task has always used, so workers already running
  exclude the new code.

## What this does not do yet

- The lock is a lease without renewal and without a fencing token. A process that stalls
  past its lease and then resumes could still write. The time limit above makes that
  unlikely, not impossible.
- There is no per-order row lock or version check. Other tasks change order state under
  their own locks: `cancel_timed_out_orders`, the position monitor and end-of-day flatten.
- The scheduled task selects up to 50 active orders with no ordering and no environment,
  venue or account filter, so with more than 50 the same ones can be picked every time,
  and its selection overlaps the demo worker's.
- The scheduled task and the demo worker reconcile differently (order lookup by id versus
  order history) and can transition an order differently.
- The demo worker fetches the same order history once per candidate order.
- When Redis is unavailable the scheduled task's skip is visible only as a missing
  heartbeat.
- An API process still running the previous version takes no lock; during a rolling
  deployment two owners are possible until every API process is replaced.
- A stop that lands while the lock is being taken or released can leave the lock in place
  until its lease ends (90 seconds by default), with nobody reconciling meanwhile.
- The Redis connection has no socket timeout of its own; the run limit is what cuts off a
  Redis server that does not answer.
