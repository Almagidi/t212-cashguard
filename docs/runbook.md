# Runbook

## Repository And Safety Baseline

Use the active working repo for day-to-day work:

```bash
cd /Users/Ameer/Desktop/t212-cashguard-codex
```

`/Users/Ameer/Desktop/t212-cashguard` is the git host repo for linked worktrees. Do not
move or delete it.

Current post-maintenance baseline:

- audited main SHA: `0c429cb9237d5d3c223aee0418aa92116f73526f`
- maintenance/security queue clear at audit time
- Dependabot alert #58 for dev-only `js-yaml` fixed
- no open PRs at audit time
- project remains Trading 212 DEMO and paper-mode hardening
- live trading remains disabled and not live-ready
- Kraken/crypto trading has not started

## Daily Operations

### Starting the app

```bash
cd /Users/Ameer/Desktop/t212-cashguard-codex
make up           # Full Docker stack
# OR
make dev          # Dev mode with hot reload
```

### Checking health

```bash
curl http://localhost:8000/v1/health/live
curl http://localhost:8000/v1/health/deps
```

Expected:
```json
{"status": "ok", "mode": "mock", ...}
{"database": "ok", "redis": "ok", "broker": "mock", ...}
```

### Checking app state

In the browser:
- Dashboard shows kill switch status, auto-trading status
- Top bar shows connection indicator (green = API reachable)
- Mode badge shows current operating mode (MOCK / DEMO / LIVE)

---

## Mock/Demo Auto-Trading

Auto-trading is not live trading. Keep live trading disabled unless a separate
live-readiness milestone explicitly approves otherwise.

1. Ensure kill switch is **inactive** (Emergency page → status shows "Inactive")
2. Ensure at least one strategy is **enabled** (Strategies page)
3. Dashboard → Auto Trading card → click "Enable" (or `POST /v1/emergency/auto-trading/on`)
4. Monitor the Signals and Orders pages

Do not use this runbook to enable live trading, bypass readiness gates, add frontend order
controls, or weaken broker/provider/execution safety checks.

---

## Triggering Emergency Stop

### Via UI

1. Click **Emergency** in sidebar
2. Click **Execute** next to **Kill Switch**
3. Confirm in dialog

### Via API

```bash
curl -X POST http://localhost:8000/v1/emergency/kill-switch \
  -H "Authorization: Bearer <token>"
```

### What the kill switch does

- Sets `app_settings.kill_switch_active = true`
- Sets `app_settings.auto_trading_enabled = false`
- **Does NOT cancel pending orders** (use Cancel All Pending separately)
- **Does NOT close positions** (use Flatten All separately)
- Logs to `risk_events` and `audit_logs`

---

## Responding to a Daily Loss Breach

The risk engine automatically blocks new orders when daily loss exceeds `max_daily_loss_pct`. To reset:

1. Review Risk Events page to confirm breach
2. Review Positions and Orders to understand what happened
3. If safe to continue: `POST /v1/risk/daily-reset`
4. Or: Wait for automatic midnight reset (if `daily_stats_reset_time = "00:00"`)

---

## Rotating API Credentials

1. Generate new Trading 212 API key in Trading 212 settings
2. Broker page → **Disconnect** existing connection
3. Broker page → enter new credentials → **Connect**
4. Click **Test Connection** to verify
5. Old encrypted credentials are overwritten in the database

---

## Database Backup

```bash
# Backup
docker-compose exec postgres pg_dump -U cashguard cashguard > backup_$(date +%Y%m%d).sql

# Restore
docker-compose exec -T postgres psql -U cashguard cashguard < backup_20240101.sql
```

---

## Rotating Encryption Keys (MASTER_KEY)

> ⚠️ This invalidates all stored broker credentials. You must reconnect after rotating.

1. Stop all services: `make down`
2. Back up the database
3. Update `MASTER_KEY` in `.env`
4. Start services: `make up`
5. Reconnect broker credentials via the Broker page

---

## Upgrading

```bash
cd /Users/Ameer/Desktop/t212-cashguard-codex
git pull
make down
make up           # Rebuilds Docker images
make migrate      # Runs new migrations
# Verify
curl http://localhost:8000/v1/health/live
```

---

## Logs

```bash
make logs          # All services
make logs-api      # Backend only
make logs-worker   # Celery workers

# Filter for errors
docker-compose logs api | grep ERROR
```

---

## Common Recovery Procedures

Prefer the operator dashboard and read-only status endpoints when diagnosing why trading is
blocked. Do not clear safety gates just to make a status panel green.

### Stuck order (status = submitted but no broker response)

```bash
# Force cancel via API
curl -X POST http://localhost:8000/v1/orders/<order_id>/cancel \
  -H "Authorization: Bearer <token>"
```

If broker has the order, it will be cancelled at broker. If not, the local record is marked cancelled.

### Kill switch won't deactivate

Check if `kill_switch_active` is stuck in DB:

```bash
docker-compose exec postgres psql -U cashguard cashguard \
  -c "UPDATE app_settings SET kill_switch_active=false WHERE id=1;"
```

Then restart the API to clear any cached state.

### Database migrations failed

```bash
cd apps/api
alembic current           # See current revision
alembic history           # See all revisions
alembic upgrade head      # Re-run from current point
```

If `alembic downgrade base` is needed (destructive!):
```bash
alembic downgrade base    # Drop all tables
alembic upgrade head      # Recreate
python -m app.db.seed     # Reseed
```
