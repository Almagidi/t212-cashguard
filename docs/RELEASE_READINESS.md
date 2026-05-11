# Release Readiness

## Current Status

The mock/paper release candidate is green. Demo RC readiness is now the active release target. Live trading remains disabled by default and is not a goal of this pass.

Verified before this pass: `mock-paper-release.spec.ts` passed 6/6, lint passed, typecheck passed, and CI passed.

## Improvements In This Pass

- Central backend safety policy for mode and broker boundaries.
- Final execution-engine gate for kill switch, broker environment, and live readiness.
- No automatic auto-trading resume from daily reset.
- Safer startup diagnostics for separated demo/live broker credential presence.
- Stronger Playwright failure artifacts and readiness checks.
- Clearer frontend safety selectors and blocked-reason display.
- Demo-mode broker boundary tests with mocked Trading 212 network calls.
- Explicit demo audit actions for broker attempt, success, failure, and kill-switch blocks.
- Frontend demo runtime badges for demo endpoint only, live endpoint blocked, and demo credential status.

## Remaining Risks

- Real demo broker order testing is still manual-only and must be supervised outside CI.
- Some worker read paths still intentionally contact a configured broker outside mock mode; they are filtered by runtime environment but should eventually share one broker factory.
- Audit logging is structured but correlation/request IDs are not yet consistently attached to every event.
- Live readiness exists, but live trading should remain disabled until a separate live-readiness milestone.

## Before Demo Checklist

- Configure only demo Trading 212 credentials or connect an encrypted demo broker connection.
- Confirm `APP_MODE=demo` and `LIVE_TRADING_ENABLED=false`.
- Prove no live endpoint can be reached from demo flows with `apps/api/tests/unit/test_demo_rc_boundary.py`.
- Run backend safety tests, frontend checks, and the mock/paper E2E suite.
- Confirm audit log distinguishes paper, demo, and blocked paths.

## Before Live Checklist

- Complete Demo RC first.
- Set `APP_MODE=live` only in an isolated environment.
- Keep `LIVE_TRADING_ENABLED=false` until all live readiness checks pass.
- Verify live broker test, Telegram supervision, demo soak, kill-switch drill, final admin unlock, and rollback plan.
- Run a manual dry-run checklist with the kill switch active.

## Rollback Plan

- Set `LIVE_TRADING_ENABLED=false`.
- Activate the kill switch.
- Disable auto-trading.
- Stop workers.
- Revert to `APP_MODE=mock` or restore the last known-good deployment.
- Preserve audit logs and server logs for incident review.

## Non-Goals

- New strategies.
- Deposits, withdrawals, or banking integrations.
- Enabling live Trading 212 order placement by default.
- Weakening or skipping existing tests.
