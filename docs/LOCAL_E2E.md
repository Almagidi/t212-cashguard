# Local E2E

## Recommended Ports

- API: `127.0.0.1:8000`
- Web: `127.0.0.1:3000`
- Manual QA alternative: API `8002`, web `3002`

Avoid stale servers before running tests:

```bash
make normal-status
make manual-status
```

Use project-owned cleanup only when needed:

```bash
make stop-normal-ports
make stop-manual-ports
```

## Required Environment

For mock/paper E2E:

```bash
APP_MODE=mock
MARKET_DATA_PROVIDER=mock
LIVE_TRADING_ENABLED=false
NEXT_PUBLIC_APP_MODE=mock
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
```

`MARKET_DATA_PROVIDER=mock` is required for mock/paper smoke tests. Do not
use `auto`, Alpaca, or Polygon for this suite unless you are intentionally
running a dedicated non-mock market-data test.

Do not set broker secrets or market-data API keys in `NEXT_PUBLIC_*`.

## Start API

```bash
cd apps/api
MARKET_DATA_PROVIDER=mock PYTHONPATH=. uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Readiness should use:

```bash
curl -fsS http://127.0.0.1:8000/v1/health/ready
```

## Start Web

```bash
cd apps/web
MARKET_DATA_PROVIDER=mock NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 NEXT_PUBLIC_APP_MODE=mock npm run dev
```

Check the expected app, not just an open port:

```bash
curl -fsS http://127.0.0.1:3000/auth/login
```

## Run Tests

One E2E test:

```bash
cd apps/web
npx playwright test tests/e2e/mock-paper-release.spec.ts -g "orders page supports safe paper order"
```

Full mock/paper smoke suite:

```bash
cd apps/web
npx playwright test tests/e2e/mock-paper-release.spec.ts --timeout 60000
```

Inspect failures:

```bash
cd apps/web
npx playwright show-report
```

Traces are recorded on first retry, screenshots are captured on failure, and videos are retained on failure.
