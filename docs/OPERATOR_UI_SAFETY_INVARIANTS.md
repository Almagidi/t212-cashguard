# Operator UI Safety Invariants

This document records the frontend/operator proof points for the current
Trading 212 DEMO / paper-mode hardening phase. It is not a live-readiness
claim. Live trading remains disabled and not live-ready.

## Read-only operator dashboard

- `/app/operator` renders `OperatorDashboard` and `RuntimeDiagnostics` from
  operator status and health queries.
- The operator page has no forms, text inputs, selects, or enabled buy, sell,
  trade, execute, enable, or disable controls.
- The operator dashboard displays `operator-read-only-badge` with
  "Read-only endpoint" and `operator-no-broker-order-badge` with
  "No broker order sent".
- Reconciliation, protective-stop, scheduler, DCA, paper execution, and broker
  health sections are reporting surfaces only.

## No live-trading unlock/control

- The operator UI reports live state through backend status fields; it does not
  expose a live-trading unlock button.
- Source-level tests prevent UI code from reintroducing `placeOrder`,
  `usePlaceOrder`, or direct `/orders` endpoint calls.
- E2E safety sweeps reject visible enabled controls labelled as buy, sell,
  place order, submit live order, execute trade, go live, or enable live.

## Scheduled automation visibility is read-only

- Scheduler and worker state on `/app/operator` is status visibility only.
- The operator surface must not provide start automation, stop automation,
  enable automation, disable automation, run strategy, or run strategy now
  controls.
- When additional scheduled strategy health is exposed by the backend, the UI
  may display backend-provided task name, cadence, last observed run, and
  observation state as read-only fields only.
- If backend scheduler health is missing or unknown, the UI must present that as
  unknown or unavailable rather than inventing a healthy state.
- Strategy-signals scheduler visibility is read-only. The operator dashboard
  may show registered state, cadence, task name, observation status, last seen
  time, and backend detail text, but it does not start, stop, or run strategies.
- If the strategy-signals scheduler is configured but not observed, the UI must
  show a caution that Celery beat configuration alone is not proof of a real
  beat and worker run.

## Paper order form boundary

- The only order-entry form is on `/app/orders` inside
  `paper-order-panel`.
- The form submits through `usePlacePaperOrder` and includes `paper_only: true`.
- The paper client method posts only to `/orders/paper`.
- The paper panel explicitly shows "Broker execution disabled" and "No real
  broker order will be placed".
- Paper history rows include "No broker order sent".

## Defensive controls

- Defensive kill-switch controls remain available where intended.
- Pending-order cancellation controls are scoped to existing orders/emergency
  workflows and are not order-placement controls.
- Emergency cancel/flatten and auto-trading controls remain separate from the
  read-only operator dashboard.

## Verification commands

Frontend/operator validation for this phase uses:

- `npm audit --audit-level=moderate`
- `npm run lint`
- `npm run typecheck`
- `npm test`
- `npm run build`

The broader E2E guard is `npm run e2e`, including
`apps/web/tests/e2e/safety-invariants.spec.ts`, when the mock backend/browser
environment is available.
