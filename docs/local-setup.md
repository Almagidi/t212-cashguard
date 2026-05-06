# Local Setup Guide

## Prerequisites

| Tool | Minimum Version | Check |
|------|----------------|-------|
| Docker | 24+ | `docker --version` |
| Docker Compose | 2.20+ | `docker compose version` |
| Python | 3.11+ | `python3 --version` |
| Node.js | 20+ | `node --version` |
| pip | 23+ | `pip --version` |
| npm | 9+ | `npm --version` |

## Step-by-Step Setup

### Double-click launcher flow

For the normal local app, use the files in `launcher/`:

| Action | Command |
| --- | --- |
| First-time setup | `launcher/1. Setup (Run First).command` |
| Start normal app | `launcher/2. Start CashGuard.command` |
| Stop normal app | `launcher/3. Stop CashGuard.command` |
| Check status | `launcher/5. Check Status.command` or `make launcher-check` |
| Verify normal login | `launcher/8. Verify Login.command` |

The normal launcher uses API port `8000`, web port `3000`, and `APP_MODE` from the root
`.env` file. It will not overwrite an existing `.env`, and it will not enable live trading.
If `.setup_complete` is missing but `.env`, `venv/bin/python`, and
`apps/web/node_modules` already exist, the start command continues with a warning instead
of forcing setup to run again.

Manual operator QA is intentionally separate:

```bash
make operator-manual
```

That safe QA path uses API port `8002`, web port `3002`, `APP_MODE=mock`, seeded SQLite
data, and no Trading 212 or Kraken credentials. Stop it with:

```bash
make operator-manual-stop
```

Inspect ports without stopping anything:

```bash
make launcher-check
```

If normal launcher ports are stale and the listeners belong to this repo:

```bash
make stop-normal-ports
```

If manual/test ports are stale and the listeners belong to this repo:

```bash
make stop-manual-ports
```

Both cleanup targets print process details and leave unrelated apps alone.

### Understanding runtime diagnostics

The dashboard includes a small diagnostics link, and `/app/operator` includes the full
read-only runtime diagnostics panel. Use it when Kraken/DCA visibility is confusing. It
shows the frontend mode, backend mode from `/v1/health/live`, the API URL used by the
browser, auth status, operator route status, and Kraken DCA endpoint status.

Normal launcher mode is usually `http://localhost:3000` calling
`http://localhost:8000`, with `APP_MODE` loaded from root `.env`. On this machine that is
`demo`, so Kraken/DCA mock seed visibility can differ from manual QA.

Manual operator QA is `http://localhost:3002` calling `http://127.0.0.1:8002` in `mock`
mode. In that path, Kraken/DCA readiness data should be visible, read-only, and should not
require broker credentials.

Diagnostics endpoint meanings:

| Status | Meaning |
| --- | --- |
| `200` | Endpoint is reachable |
| `401` | Log in again |
| `404` | Route is not registered in the backend you started |
| `500` | Route is registered but failing; check API logs |
| Network error | Frontend cannot reach the configured API URL |

### 1. Configure environment

```bash
cp .env.example .env
```

Open `.env` and set at minimum:

```bash
# Generate with: openssl rand -hex 32
SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
MASTER_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ADMIN_PASSWORD=choose-a-strong-password
```

Leave `APP_MODE=mock` for initial setup — no broker credentials needed.

### 2. Start infrastructure services

```bash
docker-compose up -d postgres redis
```

Wait for healthy status:

```bash
docker-compose ps
# postgres should show: healthy
# redis should show: healthy
```

### 3. Install backend dependencies

```bash
cd apps/api
pip install -r requirements.txt
```

### 4. Run migrations

```bash
# From apps/api directory
DATABASE_URL="postgresql+asyncpg://cashguard:cashguard_secret@localhost:5432/cashguard" \
  alembic upgrade head
```

Or using make from the repo root (sets env automatically):

```bash
make migrate
```

### 5. Seed database

```bash
make seed
# Or: cd apps/api && python -m app.db.seed
```

This creates:
- Admin user (`admin@localhost` / value of `ADMIN_PASSWORD`)
- Default risk profile (conservative settings)
- 10 demo instruments (AAPL, MSFT, TSLA, NVDA, SPY, etc.)
- Demo ORB strategy (disabled by default)
- App settings (kill switch off, auto trading off)

### 6. Start the backend

```bash
cd apps/api
uvicorn app.main:app --reload --port 8000
```

Verify: http://localhost:8000/docs

### 7. Install frontend dependencies

```bash
cd apps/web
npm install
```

### 8. Start the frontend

```bash
npm run dev
```

Visit: http://localhost:3000

**Login**: admin@localhost / your ADMIN_PASSWORD

---

## Using Docker Compose for Everything

Instead of running services manually, spin up the full stack:

```bash
make up
# Equivalent to: docker-compose up -d
```

Services:
- `http://localhost:3000` — Frontend
- `http://localhost:8000` — Backend API
- `http://localhost:8000/docs` — API documentation
- PostgreSQL on port 5432
- Redis on port 6379

### First run with full Docker stack

```bash
make up
sleep 10   # wait for containers to be healthy
make migrate
make seed
```

---

## Running Celery Workers (Optional)

Workers handle background tasks (order reconciliation, account sync). In development you can skip them — the UI will still work, but order reconciliation won't run automatically.

```bash
# Worker
cd apps/api
celery -A app.workers.celery_app worker --loglevel=info

# Beat scheduler (in another terminal)
celery -A app.workers.celery_app beat --loglevel=info
```

---

## Database Management

```bash
# View current migration state
cd apps/api && alembic current

# Create a new migration (after changing models)
alembic revision --autogenerate -m "your description"

# Reset database completely
make reset   # Drops all tables, re-runs migrations, re-seeds

# Connect directly
docker-compose exec postgres psql -U cashguard -d cashguard
```

---

## Environment Files

| File | Purpose |
|------|---------|
| `.env.example` | Template — commit this |
| `.env` | Your local config — **never commit** |

The `.gitignore` excludes `.env` automatically.

---

## macOS Notes

- If `pip install` fails with SSL errors, try: `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt`
- Docker Desktop must be running before `docker-compose up`

## Windows Notes

- Use WSL2 for the best compatibility
- Or use Git Bash / PowerShell with `docker-compose` installed
- Path separators in commands use `/` — WSL2 handles this natively

## Linux Notes

- Ensure your user is in the `docker` group: `sudo usermod -aG docker $USER`
- Log out and back in for group changes to take effect
