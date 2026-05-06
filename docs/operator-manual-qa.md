# Operator Dashboard — Local Manual QA Gate

## Purpose

Use this gate to manually test the operator dashboard against a real FastAPI backend in
safe mock mode. It requires no Trading212 or Kraken credentials, makes no broker order
calls, and must stay read-only.

## Modes

The normal double-click launcher, `launcher/2. Start CashGuard.command`, loads root `.env`
and writes `apps/web/.env.local` from it. On this machine that means the normal launcher
runs the app on ports 8000/3000 in `APP_MODE=demo`.

The operator manual QA path is separate:

```bash
make operator-manual
```

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

`stop-manual-ports` is intentionally limited to ports 8001, 8002, 3001, 3002, and 3100.

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

For the normal launcher, use `launcher/3. Stop CashGuard.command`; it targets ports 8000 and
3000 plus saved launcher PIDs.

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
make smoke
make e2e-operator
make e2e-operator-integration
make readiness-full
```

The E2E targets must stay mock/read-only and must not require broker credentials.
