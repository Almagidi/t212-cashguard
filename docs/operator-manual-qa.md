# Operator Dashboard — Local Manual QA Gate

## Purpose

t-ops-010-local-operator-manual-qa
Use this gate to manually test the operator dashboard against a real FastAPI backend in
safe mock mode. It requires no Trading212 or Kraken credentials, makes no broker order
calls, and must stay read-only.

Post-maintenance baseline:

- active repo: `/Users/Ameer/Desktop/t212-cashguard-codex`
- git host repo for linked worktrees: `/Users/Ameer/Desktop/t212-cashguard`
- audited main SHA: `0c429cb9237d5d3c223aee0418aa92116f73526f`
- maintenance/security queue clear at audit time
- Dependabot alert #58 for dev-only `js-yaml` fixed
- live trading disabled and not live-ready
- Kraken/crypto trading not started; Kraken content here is mock/read-only visibility only

## Modes

The normal double-click launcher, `launcher/2. Start CashGuard.command`, loads root `.env`
and writes `apps/web/.env.local` from it. On this machine that means the normal launcher
runs the app on ports 8000/3000 in `APP_MODE=demo`.

The operator manual QA path is separate:

This QA gate lets you manually test the operator dashboard against a **real FastAPI backend**
running in `APP_MODE=mock` — no broker credentials, no live trading, no real money.

Use it to verify that the dashboard renders correctly, safety flags are displayed, kill
switches are visible, and mutation controls are absent, before merging operator-facing changes.

---

## How to run

```bash
make operator-manual
```

t-ops-010-local-operator-manual-qa
It runs the API on port 8002 and web on port 3002 with `APP_MODE=mock` and
`NEXT_PUBLIC_APP_MODE=mock`. This is the preferred safe path for operator/Kraken manual
QA because it uses seeded SQLite data and does not need real broker credentials.

## Commands

Start the manual QA servers:

```bash
make operator-manual
```

Check the manual/test ports without stopping anything:

```bash
make manual-status
```

Run authenticated backend checks after `make operator-manual` is running:

```bash
make operator-manual-check
```

Stop only the manual QA servers that wrote PID files:

```bash
make operator-manual-stop
```

If known manual/test ports are stale, inspect first, then clear only those ports:

```bash
make manual-status
make stop-manual-ports
```

`stop-manual-ports` is intentionally limited to ports 8001, 8002, 3001, 3002, and
3100, and it only stops listeners whose current working directory is inside this repo.
It prints and leaves unrelated processes alone.

## Login

| Field | Value |
| --- | --- |
| Email | `admin@localhost` |
| Password | `change-me` |

Open:

```text
http://localhost:3002/app/operator
```

Allow 15-30 seconds for the first Next.js compile.

## Ports

| Flow | API | Web | Mode |
| --- | ---: | ---: | --- |
| Normal launcher | 8000 | 3000 | Root `.env` (`demo` on this machine) |
| Mock operator E2E | mock routes only | 3100 | `mock` |
| Real-backend integration E2E | 8001 | 3001 | `mock` |
| Manual operator QA | 8002 | 3002 | `mock` |

## Backend Checks

With `make operator-manual` running, this should pass:

```bash
make operator-manual-check
```

It logs in with the seeded admin and checks:

```text
POST /v1/auth/login
GET  /v1/auth/me
GET  /v1/health/live
GET  /v1/operator/status
GET  /v1/kraken/dca/status
GET  /v1/kraken/dca/activity
GET  /v1/kraken/dca/configs
GET  /v1/account/summary
GET  /v1/account/cash-guard
GET  /v1/positions
```

Expected result: every GET returns `200`; login returns a bearer token.

## Browser Checklist

- Log in at `http://localhost:3002/auth/login`.
- Open `http://localhost:3002/app/operator`.
- Confirm these visible sections:
  - `Trading212 Summary`
  - `Kraken Summary`
  - `DCA Summary`
  - `Scheduler / Worker Health`
  - `Safety Flags`
  - venue cards for `T212` and `KRAKEN`
- Confirm DCA shows BTC/USD and ETH/USD when seeded configs are present.
- Confirm worker heartbeat is visible, even if it reports a safe degraded/missing state.
- Confirm safety flags show read-only posture:
  - endpoint read-only true
  - creates orders false
  - calls brokers false
  - triggers schedulers false
  - runs strategies false
  - DCA live enabled false
  - Kraken live enabled false
  - live disabled / paper-only visible where applicable
- Confirm no mutation controls are visible:
  - no Buy
  - no Sell
  - no Execute
  - no Trade
  - no Enable trading
  - no Disable kill switch
  - no Unlock live trading
- In DevTools Network, reload and confirm operator page load uses GET requests for
  operator/DCA status data and does not send POST, PUT, PATCH, or DELETE to operator or DCA
  paths.

## Expected Safe State

The seeded manual QA database is intentionally conservative:

| Property | Expected value |
| --- | --- |
| Global kill switch | Active / ON |
| T212 kill switch | Active / ON |
| Kraken kill switch | Active / ON |
| `live_trading_unlocked` | `false` |
| `auto_trading_enabled` | `false` |
| DCA configs | disabled |
| DCA mode | paper-only |
| Mutation controls | none |

Live trading must remain disabled. The manual QA route must not require real Trading212 or
Kraken credentials.

## Kraken Visibility

The normal dashboard may not show Kraken because it focuses on account, positions, orders,
alerts, and Trading212 broker status from the normal app data. Kraken readiness is expected
on the operator page and the Kraken DCA endpoints.

This is visibility only. It does not mean Kraken/crypto trading has started, and it must
not be used to add live crypto trading, real Kraken credentials, or mutation controls.

## Understanding Dashboard Runtime Diagnostics

Open `/app/operator` to see the runtime diagnostics panel. The normal dashboard also links
to this panel and shows the frontend mode plus the API URL that the browser is using.

The diagnostics panel is read-only. It checks:

```text
GET /v1/health/live
GET /v1/auth/me
GET /v1/operator/status
GET /v1/kraken/dca/status
GET /v1/kraken/dca/activity
GET /v1/kraken/dca/configs
```

Use the runtime profile to confirm the intended path:

| Profile | Web | API | Expected mode |
| --- | ---: | ---: | --- |
| Normal launcher | 3000 | 8000 | Root `.env` (`demo` on this machine) |
| Manual QA | 3002 | 8002 | `mock` |
| Integration | 3001 | 8001 | `mock` |
| Mock E2E | 3100 | mocked | `mock` |

In normal launcher demo mode, Kraken/DCA cards may not be seeded or visible unless the
normal database has Kraken DCA config/state data. For mock Kraken/DCA QA, use:

```bash
make operator-manual
```

In manual QA mock mode, Kraken/DCA readiness data should be visible, read-only, and should
not require Trading212 or Kraken credentials.

Endpoint meanings:

| Status | Meaning | Next action |
| --- | --- | --- |
| `200` | Route is reachable and returning data | Continue QA |
| `401` | Browser token is missing or invalid | Log in again |
| `404` | Route is not registered in this backend build | Check the backend/branch you started |
| `500` | Route exists but failed | Check API logs |
| Network error | Frontend cannot reach the configured API URL | Check ports and launcher/manual QA status |

If `/v1/operator/status` returns Kraken data but the UI does not show it:

1. Confirm you are on `/app/operator`, not the normal dashboard.
2. Hard-refresh after login.
3. Clear old localStorage if the browser is using a stale token.
4. Inspect `apps/web/components/operator/operator-dashboard.tsx`; the page should render
   `Kraken Summary` from `status.kraken` and venue cards from `status.venues`.

## Troubleshooting

### Stale Ports

Known test/manual ports are 8001, 8002, 3001, 3002, and 3100.

```bash
make manual-status
```

If a listed process is clearly a stale test/manual server:

```bash
make stop-manual-ports
```

For the normal launcher, use `launcher/3. Stop CashGuard.command`; it stops saved launcher
PIDs and then reports any remaining listeners on ports 8000 and 3000 without killing
unknown processes.

To inspect both normal and manual ports:

```bash
make launcher-check
```

To inspect only normal ports:

```bash
make normal-status
```

If a normal-port listener is clearly stale and belongs to this repo:

```bash
make stop-normal-ports
```

`stop-normal-ports` only stops listeners on 8000/3000 whose current working directory is
inside this repo. If another app owns the port, stop that app directly.

### Setup Marker Missing

The normal launcher checks `.setup_complete`. If the marker is missing but `.env`,
`venv/bin/python`, and `apps/web/node_modules` already exist, it continues with a warning.
If dependencies are missing, run:

```text
launcher/1. Setup (Run First).command
```

Do not copy secrets into docs, commits, or tickets while debugging setup.

### Old Browser Token

If login behaves oddly or the app keeps redirecting, clear the local token for the matching
origin:

```js
localStorage.removeItem('cg_token')
```

Then reload and log in again.

### CORS-looking Errors

Browser messages like "blocked by CORS policy" can be a symptom of backend 500s. Before
changing CORS, check the backend directly:

```bash
make operator-manual-check
tail -f /tmp/t212_manual_api.log
```

If `/v1/account/summary`, `/v1/account/cash-guard`, or `/v1/positions` returns 500, fix the
backend response first. Do not loosen CORS unless a direct backend check proves CORS itself
is misconfigured.

### Playwright LocalStorage Warning

You may see:

```text
Warning: `--localstorage-file` was provided without a valid path
```

This is harmless when the Playwright suite passes. Treat it as noise unless a test fails
while trying to seed or read localStorage.

## Verification Gates

Before merging manual QA or launcher changes, run:

```bash
git diff --check
make launcher-check
make smoke
make e2e-operator
make e2e-operator-integration
make readiness-full
```

The E2E targets must stay mock/read-only and must not require broker credentials.

This single command:

1. Initialises a fresh SQLite database at `/tmp/t212_manual_qa.db` with seeded admin user,
   venue configs, DCA configs, and kill switches all set to **active/safe**.
2. Starts the FastAPI backend on **port 8002** (`APP_MODE=mock`, no broker creds needed).
3. Starts the Next.js frontend on **port 3002** (`NEXT_PUBLIC_APP_MODE=mock`).
4. Prints the URLs, credentials, and instructions.

When you are done:

```bash
make operator-manual-stop
```

---

## Login credentials (mock mode)

| Field    | Value             |
|----------|-------------------|
| Email    | `admin@localhost` |
| Password | `change-me`       |

These are seeded by `apps/api/scripts/init_integration_db.py`. No real credentials are
ever required.

---

## Page to open

After the servers start (allow 15–30 s for the Next.js first compile):

```
http://localhost:3002/app/operator
```

---

## Manual browser QA checklist

Work through each item in order. Mark each ✅ pass or ❌ fail.

### 1. Authentication

- [ ] Navigate to `http://localhost:3002/app/operator`.
- [ ] You are redirected to the login page (or the login form is shown).
- [ ] Enter email `admin@localhost` and password `change-me`.
- [ ] Login succeeds and you land on the operator dashboard.

### 2. Page load

- [ ] `/app/operator` loads without a blank screen or 5xx error.
- [ ] No full-page error boundary is shown.
- [ ] Page title and navigation are visible.

### 3. Venue readiness

- [ ] A **Trading212** venue section is visible with a readiness indicator.
- [ ] A **Kraken** venue section is visible with a readiness indicator.
- [ ] Both venues show a status (e.g. "ready", "mock", "degraded") — the exact label is
      acceptable as long as it is present and not an unhandled exception.

### 4. DCA section

- [ ] A DCA (Dollar-Cost Averaging) section is visible.
- [ ] BTC/USD and ETH/USD configs are listed.
- [ ] Both configs show `enabled: false` or equivalent disabled state.
- [ ] Both configs show `paper_only: true` or equivalent paper-only state.

### 5. Worker / heartbeat / readiness area

- [ ] A worker or heartbeat status area is visible.
- [ ] It renders without crashing (exact values depend on mock implementation).

### 6. Safety flags

- [ ] A global kill switch indicator is visible.
- [ ] Global kill switch reads **active / ON / engaged** (seeded as `kill_switch_active=True`).
- [ ] T212 venue kill switch reads **active / ON / engaged**.
- [ ] Kraken venue kill switch reads **active / ON / engaged**.
- [ ] `live_trading_unlocked` flag reads **false / locked**.
- [ ] `auto_trading_enabled` flag reads **false / disabled**.

### 7. No mutation controls

- [ ] No **Buy**, **Sell**, **Execute**, or **Trade** buttons are visible.
- [ ] No **Enable trading**, **Disable kill switch**, or **Unlock live trading** buttons
      are visible anywhere on the operator page.
- [ ] No form inputs for placing orders are present.

### 8. Network tab (browser DevTools → Network)

- [ ] Open DevTools → Network tab; reload the page.
- [ ] Confirm requests to the operator/DCA endpoints are **GET only**:
  - `GET /v1/operator/status` → `200`
  - `GET /v1/kraken/dca/status` → `200`
  - `GET /v1/kraken/dca/activity` → `200`
  - `GET /v1/kraken/dca/configs` → `200`
- [ ] No `POST`, `PUT`, `PATCH`, or `DELETE` requests are made on page load.

### 9. Console (browser DevTools → Console)

- [ ] No **red error** messages are present after a clean page load.
- [ ] Warnings are acceptable; errors are a failure.

### 10. Refresh stability

- [ ] Hard-refresh the page (`Cmd+Shift+R` / `Ctrl+Shift+R`).
- [ ] Dashboard re-renders correctly without blank content or broken layout.

### 11. Direct backend URL smoke checks

With the servers running, confirm the following return `200` **with authentication**.
Use the browser (you are already logged in) or curl with a valid JWT:

```
http://127.0.0.1:8002/v1/operator/status
http://127.0.0.1:8002/v1/kraken/dca/status
http://127.0.0.1:8002/v1/kraken/dca/activity
http://127.0.0.1:8002/v1/kraken/dca/configs
```

- [ ] All four return `200 OK` (unauthenticated requests may return `401`, which is expected).



## Expected operator dashboard state

| Property                | Expected value  |
|-------------------------|-----------------|
| Global kill switch      | Active / ON     |
| T212 kill switch        | Active / ON     |
| Kraken kill switch      | Active / ON     |
| `live_trading_unlocked` | `false`         |
| `auto_trading_enabled`  | `false`         |
| DCA BTC/USD enabled     | `false`         |
| DCA ETH/USD enabled     | `false`         |
| DCA paper_only          | `true` for both |
| Mutation controls       | None visible    |



## Expected safety state

The seeded database is constructed so that **all safety gates are engaged**:

- Global kill switch ON → no trades can execute.
- Per-venue kill switches ON → both T212 and Kraken are gated.
- `live_trading_unlocked = false` → the live execution path is locked.
- `auto_trading_enabled = false` → automated DCA is disabled.
- DCA configs `enabled = false` → individual configs are off.
- DCA configs `paper_only = true` → even if enabled, only paper trades.

A correct operator dashboard should reflect all of these states visually.

---

## Expected no-mutation behaviour

The operator dashboard is a **read-only monitoring view**. No action that could place a
trade, modify a config, or change safety state should be reachable from the UI. If any
mutation control is visible, that is a regression.

---

## Ports used

| Service | Port |
|---------|------|
| API     | 8002 |
| Web     | 3002 |

These do not conflict with:

- `make dev` (web 3000, API 8000)
- `make e2e-operator` (web 3100, API 8000)
- `make e2e-operator-integration` (web 3001, API 8001)

---

## Troubleshooting

### Port already in use

If you see `address already in use` for port 8002 or 3002:

```bash
# Check what is using the port
lsof -i tcp:8002
lsof -i tcp:3002

# Stop servers that were started by make operator-manual
make operator-manual-stop
```

`make operator-manual-stop` uses the PID files written by `make operator-manual`.
If those files are missing, inspect `lsof` output and stop only the process you
recognise as the manual QA API or web server.

### Web app shows "API unreachable" or login fails

1. Check the API log: `tail -f /tmp/t212_manual_api.log`
2. Verify the API is healthy: `curl http://127.0.0.1:8002/v1/health/live`
3. If the API exited, re-run `make operator-manual`.

### Web app blank after navigating to `/app/operator`

1. Check the web log: `tail -f /tmp/t212_manual_web.log`
2. Wait 15–30 s for the Next.js initial compile to complete, then hard-refresh.

### `make operator-manual-stop` says "process already gone"

This is normal if the process crashed or was stopped manually. The PID files are cleaned
up automatically.

---

## No real broker credentials required

`APP_MODE=mock` tells the API to use stub implementations for all broker integrations.
You do not need:

- A Trading212 API key
- A Kraken API key or secret
- Any `.env` file beyond the defaults already baked into the Makefile target

The SQLite database at `/tmp/t212_manual_qa.db` is ephemeral and re-created fresh every
time you run `make operator-manual`.
