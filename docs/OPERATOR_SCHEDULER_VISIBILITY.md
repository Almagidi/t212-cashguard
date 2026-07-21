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

## Strategy-signals scheduler gap

Current `origin/main` does not expose a backend-provided strategy-signals
scheduler status field through the operator API schema or operator status route.
Because that read-only backend field is absent, the frontend cannot truthfully
display strategy-signals scheduler health, task name, cadence, last observed
run, or observation state yet.

The UI must not invent these fields from naming conventions, worker assumptions,
local timers, or frontend-only inference. Missing backend scheduler status must
remain absent or unknown in the UI.

## Future UI requirements

When the backend exposes read-only strategy-signals scheduler status, the
operator dashboard may display only backend-provided status fields such as:

- configured yes/no
- task name
- schedule interval
- last observed run timestamp
- observation status
- warning text when configured but not observed end-to-end

The operator dashboard must still provide no start, stop, enable, disable, run
now, live unlock, broker credential, buy, sell, order, or other mutation control.
