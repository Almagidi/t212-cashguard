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
