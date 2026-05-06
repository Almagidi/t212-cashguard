# Operator Dashboard — Local Manual QA Gate

## Purpose

This QA gate lets you manually test the operator dashboard against a **real FastAPI backend**
running in `APP_MODE=mock` — no broker credentials, no live trading, no real money.

Use it to verify that the dashboard renders correctly, safety flags are displayed, kill
switches are visible, and mutation controls are absent, before merging operator-facing changes.

---

## How to run

```bash
make operator-manual
```

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

---

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

---

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
