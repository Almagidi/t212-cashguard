# Manual Demo RC Runbook

## 1. Purpose

This runbook is for one manually supervised Trading 212 demo-account test of T212 CashGuard Trader.

The purpose is to verify that the application can safely reach the Trading 212 demo endpoint only while preserving all safety controls:

- live trading remains disabled
- kill switch controls remain effective
- auto-trading remains disabled
- demo execution is clearly labelled
- audit events are created
- no live endpoint is contacted
- no secrets are exposed

This runbook is not permission to use real money.

---

## 2. Non-goals

This runbook is not for:

- live trading
- unattended automation
- autonomous strategy execution
- production deployment
- high-frequency execution
- real-money testing
- financial advice
- enabling guarded Live mode

Guarded Live mode remains future work and must not be enabled during this test.

---

## 3. Preconditions

Before starting, confirm:

- Git working tree is clean, or all local changes are intentional.
- Latest CI is green.
- Backend tests pass locally.
- Frontend lint, typecheck, and tests pass locally.
- Mock/Paper E2E smoke suite passes locally.
- Demo broker-boundary tests pass locally.
- APP_MODE=demo.
- LIVE_TRADING_ENABLED=false.
- Trading 212 demo credentials are available.
- Trading 212 live credentials are not required.
- Kill switch is initially active.
- Auto-trading is disabled.
- Backend and frontend are started on known fresh ports.
- The operator can log into the Trading 212 demo account externally to verify the result.
- Only one tiny manually supervised demo order may be tested.
- The kill switch must be re-enabled immediately after the test.

---

## 4. Required environment variables

Use explicit environment variables. Do not rely on stale .env state.

Required safe demo configuration:

APP_MODE=demo
LIVE_TRADING_ENABLED=false
MARKET_DATA_PROVIDER=mock
NEXT_PUBLIC_APP_MODE=demo
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000

Trading 212 demo credentials must be configured locally:

TRADING212_DEMO_API_KEY=REDACTED_DEMO_KEY
TRADING212_DEMO_API_SECRET=REDACTED_DEMO_SECRET

Trading 212 live credentials should remain unset:

TRADING212_LIVE_API_KEY unset
TRADING212_LIVE_API_SECRET unset

Important:

- Never use NEXT_PUBLIC_* for broker secrets.
- Never print API keys or secrets in logs.
- Live credentials should stay unset for this manual demo test.
- LIVE_TRADING_ENABLED must remain false.

---

## 5. Pre-flight test commands

Run these from the repo root before any manual demo-account test.

Backend tests:

PYTHONPATH=apps/api python3.12 -m pytest apps/api/tests/ -q --no-cov

Backend Ruff checks, using the current CI-equivalent critical backend files:

cd apps/api
python3.12 -m ruff check app/api/deps.py app/broker/trading212.py app/core/config.py app/execution/engine.py app/services/startup_validation.py tests/integration/test_api.py
python3.12 -m ruff format --check app/api/deps.py app/broker/trading212.py app/core/config.py app/execution/engine.py app/services/startup_validation.py tests/integration/test_api.py
cd ../..

Backend mypy changed-file equivalent:

cd apps/api
python3.12 -m mypy app/api/deps.py app/broker/trading212.py app/core/config.py app/execution/engine.py app/services/startup_validation.py --ignore-missing-imports --follow-imports=silent
cd ../..

Frontend checks:

cd apps/web
npm run lint
npm run typecheck
npm test -- --watchAll=false
cd ../..

Mock/Paper E2E smoke suite:

cd apps/web
MARKET_DATA_PROVIDER=mock NEXT_PUBLIC_APP_MODE=mock NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 E2E_WEB_PORT=3004 BASE_URL=http://localhost:3004 npx playwright test tests/e2e/mock-paper-release.spec.ts --timeout 60000
cd ../..

---

## 6. Startup procedure

### Step 1: stop stale servers

Check common ports:

lsof -i :8000
lsof -i :3000
lsof -i :3001
lsof -i :3004

Kill stale backend/frontend processes only if you recognise them:

kill -9 <PID>

### Step 2: start backend in Demo mode

From the repo root:

cd apps/api

APP_MODE=demo LIVE_TRADING_ENABLED=false MARKET_DATA_PROVIDER=mock TRADING212_DEMO_API_KEY="$TRADING212_DEMO_API_KEY" TRADING212_DEMO_API_SECRET="$TRADING212_DEMO_API_SECRET" python3.12 -m uvicorn app.main:app --host 127.0.0.1 --port 8000

Expected safe startup state:

- app mode: demo
- live trading: disabled
- demo credentials: configured
- live credentials: not required
- market data provider: mock
- no secret values printed

### Step 3: start frontend in Demo mode

Open a second terminal:

cd /Users/Ameer/Desktop/t212-cashguard/apps/web

NEXT_PUBLIC_APP_MODE=demo NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 npm run dev -- --port 3000

Open:

http://localhost:3000

Expected frontend state:

- UI shows Demo mode.
- Live endpoint is blocked or disabled.
- Demo endpoint-only state is visible.
- Kill switch state is visible.
- Auto-trading state is visible.
- Demo credentials status is visible without exposing keys.
- No UI element implies live trading is enabled.

---

## 7. Safety verification before any demo order

Before attempting a demo order, verify:

- Runtime mode is demo.
- LIVE_TRADING_ENABLED=false.
- Kill switch is active.
- Auto-trading is disabled.
- Broker status says demo only.
- Live endpoint is blocked.
- Demo credentials are configured.
- Live credentials are not required.
- Audit log page is accessible.
- Order panel clearly says demo.
- Order size is tiny.
- The order is manually triggered only.
- No strategy automation is running.
- No worker is submitting orders autonomously.

Abort if any of these checks fail.

---

## 8. Controlled demo order test sequence

### Phase A: kill-switch block test

With kill switch active:

1. Open the orders page.
2. Attempt a demo order.
3. Expected result: order is blocked.
4. Expected result: no broker order submission occurs.
5. Expected result: audit log records a blocked demo attempt.
6. Expected result: blocked reason is visible.

Do not continue if the kill switch does not block.

### Phase B: one supervised demo order

Only after Phase A passes:

1. Manually deactivate the kill switch.
2. Confirm auto-trading remains disabled.
3. Confirm runtime mode still shows demo.
4. Confirm live endpoint remains blocked.
5. Confirm order size is tiny.
6. Submit one manually supervised demo order.
7. Confirm audit event: demo_broker_order_attempt.
8. Confirm either demo_broker_order_success or demo_broker_order_failure.
9. Confirm no secret values appear in logs.
10. Confirm no live endpoint was contacted.
11. Verify the order in the Trading 212 demo account externally.
12. Immediately re-enable the kill switch.
13. Keep auto-trading disabled.

---

## 9. Abort criteria

Abort immediately if:

- UI shows live mode.
- LIVE_TRADING_ENABLED=true.
- A live endpoint appears anywhere.
- Demo credentials are missing.
- Broker status is ambiguous.
- Kill switch fails to block.
- Auto-trading enables unexpectedly.
- Audit log does not record the attempt.
- Any API key, secret, token, or auth header appears in logs.
- Any unexpected request to Trading 212 live endpoint appears.
- Backend is connected to a stale frontend.
- Frontend is connected to a stale backend.
- Runtime mode changes unexpectedly.
- The order panel does not clearly say demo.

Abort by stopping backend/frontend with CTRL+C.

---

## 10. Post-test checklist

After the manual demo test:

- Kill switch is active.
- Auto-trading is disabled.
- App is stopped or returned to mock mode.
- Logs reviewed for secret leakage.
- Audit events reviewed.
- Trading 212 demo account checked externally.
- Test result documented.
- Live credentials remain unset.
- No accidental .env changes are committed.
- No uncontrolled worker process remains running.

---

## 11. Rollback plan

Stop backend/frontend with CTRL+C.

Unset credentials from the current shell:

unset TRADING212_DEMO_API_KEY
unset TRADING212_DEMO_API_SECRET
unset TRADING212_LIVE_API_KEY
unset TRADING212_LIVE_API_SECRET

Return to mock mode:

export APP_MODE=mock
export LIVE_TRADING_ENABLED=false
export MARKET_DATA_PROVIDER=mock
export NEXT_PUBLIC_APP_MODE=mock

Re-run mock/paper smoke suite:

cd apps/web
MARKET_DATA_PROVIDER=mock NEXT_PUBLIC_APP_MODE=mock NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 E2E_WEB_PORT=3004 BASE_URL=http://localhost:3004 npx playwright test tests/e2e/mock-paper-release.spec.ts --timeout 60000
cd ../..

---

## 12. Evidence template

Manual Demo RC Evidence

Date/time:
Commit hash:
Operator:
Backend port:
Frontend port:

APP_MODE:
LIVE_TRADING_ENABLED:
MARKET_DATA_PROVIDER:

Demo credentials configured: yes/no
Live credentials unset: yes/no

Kill switch before blocked test:
Auto-trading before blocked test:

Blocked test result:
Blocked audit event ID(s):

Kill switch before demo order:
Auto-trading before demo order:

Symbol:
Side:
Quantity/order size:
Order type:

Demo order result:
Trading 212 demo account verified externally: yes/no

Audit event IDs:
- demo_broker_order_attempt:
- demo_broker_order_success:
- demo_broker_order_failure:

Kill switch after test:
Auto-trading after test:

Secrets found in logs: yes/no
Live endpoint contacted: yes/no
Issues found:
Decision:

---

## 13. Final decision gate

The project is ready for one manually supervised demo-account test only if:

- all pre-flight checks pass
- UI clearly shows Demo mode
- live trading is disabled
- kill switch blocks first
- auto-trading remains disabled
- no secrets are exposed
- audit log is working
- operator can verify the order externally in Trading 212 demo

This is not approval for live trading or unattended automation.
