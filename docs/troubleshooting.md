# Troubleshooting

## Backend

### `Connection refused` to PostgreSQL

```
sqlalchemy.exc.OperationalError: (asyncpg.exceptions.ConnectionRefusedError)
```

**Fix:**
```bash
docker-compose up -d postgres
docker-compose ps  # wait until healthy
```

### `alembic: command not found`

```bash
pip install alembic
# Or ensure virtualenv is activated
```

### Virtualenv commands break after moving the project folder

Some `venv/bin/*` wrapper scripts keep the original absolute path from when the
virtualenv was created.

**Fix:**
```bash
cd /path/to/t212-cashguard
rm -rf venv
python3.12 -m venv venv
./venv/bin/python -m pip install -r apps/api/requirements.txt
```

Until the virtualenv is rebuilt, prefer `./venv/bin/python -m <module>` over
calling wrapper scripts like `./venv/bin/pytest` directly.

### `Could not validate credentials` (401)

- Token expired (8h default) — log in again
- `SECRET_KEY` changed after tokens were issued — all existing tokens invalidated, users must re-login
- Clock skew > 5 minutes — check system time

### `Stored broker credentials could not be decrypted`

- The `MASTER_KEY` in `.env` no longer matches the key that was used when the
  Trading 212 credentials were originally saved
- Restore the original `MASTER_KEY` if you still have it, or reconnect the
  broker so the credentials are encrypted again with the current key
- Worker jobs now skip safely when this happens, but broker-backed API calls
  will stay blocked until the credentials are re-saved

### `asyncpg.exceptions.InvalidPasswordError`

```bash
# Check env vars
echo $POSTGRES_PASSWORD
# Default is cashguard_secret — must match DATABASE_URL
```

### `celery: Error connecting to Redis`

```bash
# Verify Redis is running
docker-compose up -d redis
# Check Redis URL includes password
echo $REDIS_URL  # should be redis://:password@host:port/db
```

### `ModuleNotFoundError: No module named 'app'`

You must run Python commands from the `apps/api/` directory where `PYTHONPATH` is set, or set it manually:

```bash
cd apps/api
PYTHONPATH=/path/to/apps/api python -m app.db.seed
```

### Migration fails with `already exists`

```bash
cd apps/api
alembic current       # Check what's applied
alembic stamp head    # Mark as up-to-date without running (if tables already exist manually)
```

---

## Frontend

### Blank page / white screen

1. Open browser DevTools → Console
2. Look for errors. Common causes:
   - API URL mismatch: check `NEXT_PUBLIC_API_URL` in `.env`
   - Auth token expired: clear localStorage, reload, log in again

### `CORS error` in console

```
Access-Control-Allow-Origin: ...
```

Check that `CORS_ORIGINS` in backend `.env` includes both `http://localhost:3000` and `http://127.0.0.1:3000` if you use either local address.

### `npm install` fails

```bash
# Clear npm cache
npm cache clean --force
# Delete node_modules and reinstall
rm -rf node_modules package-lock.json
npm install
```

### TypeScript errors during `npm run build`

```bash
cd apps/web
npm run typecheck  # See full error list
```

Most common: missing type for API response. Check `types/index.ts` matches backend response shape.

### `hydration error` in browser console

Usually caused by server/client rendering mismatch with `localStorage`. The auth store uses `persist` from Zustand which accesses `localStorage` — this is guarded with `typeof window !== 'undefined'` checks throughout.

---

## Trading 212 API

### `401 Unauthorized`

- API key or secret is wrong
- Key was revoked in Trading 212 settings
- Environment mismatch (using live key with demo URL or vice versa)
- **Fix**: Disconnect broker → reconnect with correct credentials

### `429 Too Many Requests`

The adapter handles this automatically by waiting `Retry-After` seconds. If you see persistent 429s:
- Reduce polling frequency in Celery beat schedule
- Only have one browser tab open (multiple tabs = multiple polling)

### Order returns `REJECTED`

Common Trading 212 rejection reasons:
- Instrument not tradeable at current time (market closed)
- Quantity too small (below minimum trade value)
- Cash guard would block it (insufficient free funds)
- Symbol not available in your account type

Check the order detail drawer (Orders page → click order → expand broker_response JSON).

### Sell order rejected

Verify:
1. You actually hold the position
2. `quantity_available > 0` in the position
3. The sell quantity (negative) matches available quantity

---

## Docker

### `docker-compose up` fails with port conflict

```bash
# Find what's using port 5432 / 6379 / 8000 / 3000
lsof -i :5432
# Kill it or change port in docker-compose.yml
```

### Container keeps restarting

```bash
docker-compose logs api  # Check the error
```

Common causes:
- Missing env vars (SECRET_KEY, DATABASE_URL)
- DB not ready when API starts (use `depends_on: condition: service_healthy`)
- Python import error in app code

### Out of disk space

```bash
docker system prune -a   # WARNING: removes all unused images
docker volume prune      # WARNING: removes unused volumes (not postgres_data if in use)
```

---

## Tests

### `pytest: no tests ran`

```bash
cd apps/api
pytest tests/ -v  # Check test discovery
# Ensure test files are named test_*.py
```

### `asyncio RuntimeError: event loop closed`

```
# pytest.ini must have:
asyncio_mode = auto
```

### Playwright test fails with `browserType.launch: Executable doesn't exist`

```bash
cd apps/web
npx playwright install chromium
```

### e2e test `page.waitForURL` timeout

The app or API may be slow to start. Increase timeout or ensure servers are running:

```bash
# Terminal 1
cd apps/api && uvicorn app.main:app --reload --port 8000
# Terminal 2
cd apps/web && npm run dev
# Terminal 3 (after both are ready)
npx playwright test
```
