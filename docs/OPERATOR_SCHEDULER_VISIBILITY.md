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

Operator interpretation:

- `ok` means a scheduler task or worker heartbeat was observed in the current
  mock/test context. It is not a live-readiness claim and does not mean live
  trading can be enabled. Scheduler OK is not paper-fill success; scheduled
  paper-fill success requires separate backend-derived signal/fill evidence.
  Market-regime validity is separate backend risk evidence and must not be
  inferred from scheduler health.
- `stale` means the scheduler is known but the latest observation is too old or
  incomplete. Operators should treat the automation path cautiously until fresh
  backend evidence appears.
- `unknown` means the backend cannot prove the task/worker observation yet. It
  must be treated as unverified rather than healthy.
- A null `strategy_signals_last_seen_at` means the UI must show "Not observed
  yet", even if another status field is present.

This status is read-only. It does not start, stop, or run strategies.

Signal/fill observation, when present, must remain backend evidence only. The
operator UI may display backend-provided facts about signals, scheduled paper
fills, timestamps, and detail text, but it must not add a button or link that
starts automation, stops automation, runs a strategy, unlocks live trading, or
places an order.

Market-regime and RiskEngine observation, when present, must also remain
backend evidence only. Operator UI may display backend-provided evidence
read-only, but it must never force market regime, must never bypass RiskEngine,
and must never synthesize a valid-regime or risk-approved claim from scheduler
status alone.

The UI must never trigger strategies or fills. It must not infer paper-fill
success from scheduler registration, cadence, heartbeat status, or observation
status alone.

Live-readiness remains separate from mock and paper observations. Mock or paper
scheduler, market-regime, RiskEngine, signal, and fill evidence may improve
operator visibility, but none of those observations unlocks live trading.

## Continuing UI requirements

The operator dashboard must still provide no start, stop, enable, disable, run
now, live unlock, broker credential, buy, sell, order, or other mutation control.
