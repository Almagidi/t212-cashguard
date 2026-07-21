# Operator Scheduler Visibility

This document records the frontend/operator scheduler-visibility state after
the paper-trade readiness guardrail pass. It is not a live-readiness claim.
Live trading remains disabled and not live-ready.

## Current frontend evidence

- `/app/operator` is a read-only reporting surface backed by
  `/v1/operator/status` plus read-only health/status queries.
- Frontend tests prove the operator dashboard exposes no buy, sell, order,
  execute, trade, start automation, stop automation, run strategy, run strategy
  now, or live-unlock action controls.
- Source-audit tests prove the operator component source has no mutation
  controls, mutation hooks, live order endpoint calls, live-readiness mutation
  endpoint calls, or emergency auto-trading mutation endpoint calls.
- Paper order UI remains outside the operator dashboard and is explicitly
  paper-only through `usePlacePaperOrder` and `/orders/paper`.

## Strategy-signals scheduler status

Current `origin/main` exposes backend-provided strategy-signals scheduler status
fields through the operator API schema and operator status route. The frontend
renders those fields on `/app/operator` as a reporting surface only:

- `strategy_signals_registered`
- `strategy_signals_cadence`
- `strategy_signals_task_name`
- `strategy_signals_observation_status`
- `strategy_signals_last_seen_at`
- `strategy_signals_observation_detail`

The UI displays registered state, cadence, task name, observation status, last
seen time, and backend detail text exactly as status visibility. If the backend
reports the scheduler as registered but the latest beat and worker observation
is missing, stale, or unknown, the UI warns:

`Configured in Celery beat, but no real beat+worker run has been observed yet.`

This status is read-only. It does not start, stop, or run strategies.

## Continuing UI requirements

The operator dashboard must still provide no start, stop, enable, disable, run
now, live unlock, broker credential, buy, sell, order, or other mutation control.
