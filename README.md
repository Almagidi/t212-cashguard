# T212 CashGuard Trader

A **cash-only, local-first** intraday trading automation platform for [Trading 212](https://www.trading212.com/).

> **Safety first.** This application will never connect to your bank, initiate deposits, use leverage, exceed your available cash balance, or enable live trading without your explicit action.

---

## Screenshots

| Dashboard | Risk Controls | Emergency |
|-----------|---------------|-----------|
| Real-time P&L, positions, signals, orders | Editable risk profile, kill switch, events | One-click emergency controls with confirmation |

---

## Features

- **Cash-only enforcement** — every order is gated against available cash, hardcoded, cannot be disabled
- **Mock mode** — runs fully without any Trading 212 credentials
- **Demo mode** — connects to Trading 212 demo environment for real API testing
- **Live mode** — explicitly gated, off by default, requires env var + manual confirmation
- **Opening Range Breakout** strategy (working implementation)
- **Portfolio Research Lab** — daily-bar portfolio backtests for five lower-friction long-only strategies
- **Hard risk engine** — kill switch, daily loss limit, position sizing, consecutive loss stop, dedup
- **Execution engine** — order intent → dedup → submit → reconcile lifecycle
- **Full audit log** — every action recorded immutably
- **Emergency controls** — kill switch, cancel-all, flatten-all, disable auto-trading
- **Telegram supervision** — allowlisted monitoring and confirmation-gated remote controls
- **Market-intelligence gating** — morning watchlists are catalyst-ranked, regime-aware, and blocked automatically when feed validation or event risk makes entries unsafe
- **Professional UI** — dark-first fintech dashboard, responsive, accessible

---

## Quick Start

### Requirements

- Docker + Docker Compose
- Python 3.11+
- Node.js 20+

### 1. Clone and configure

```bash
git clone <repo> t212-cashguard
cd t212-cashguard
cp .env.example .env
```

Edit `.env` and set secure values for:

```bash
# Generate with: openssl rand -hex 32
SECRET_KEY=your-secret-key-here
MASTER_KEY=your-master-key-here
ADMIN_PASSWORD=your-admin-password
```

### 2. Start infrastructure

```bash
# Start PostgreSQL and Redis
docker-compose up -d postgres redis

# Or start the full stack (including API and frontend)
make up
```

### 3. Run migrations and seed

```bash
make migrate   # Runs Alembic migrations
make seed      # Seeds admin user, instruments, default strategy
```

On a fresh local database, run migrations and seed before signing in. `make up`
starts the containers; it does not automatically migrate or seed the database.

### 4. Start development servers

```bash
make dev
```

- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:8000
- **API Docs**: http://localhost:8000/docs

**Default login**: `admin@localhost` / the `ADMIN_PASSWORD` value in `.env`.
The checked-in example uses `change-me-on-first-run`; the manual QA launcher
uses `change-me`.

### Local launcher parity

Normal double-click launch:

```text
launcher/2. Start CashGuard.command
```

This starts the normal app on `http://localhost:3000` with API
`http://localhost:8000`, using `APP_MODE` from root `.env`.

Safe manual operator QA is separate:

```bash
make demo-mock
```

That path runs `APP_MODE=mock` on web `3002` and API `8002`, uses seeded local data, and
does not require Trading 212 or Kraken credentials. Stop normal launch with
`launcher/3. Stop CashGuard.command`; stop mock demo QA with `make demo-mock-stop`.
Use `make launcher-check` to inspect both flows without stopping anything.

For a demo-ready mock/paper walkthrough, prefer:

```bash
make demo-mock
```

Open `http://localhost:3002/app/operator`, sign in with
`admin@localhost` / `change-me`, and confirm the page shows Paper-only, Mock
execution, No broker order sent, and Live disabled. This flow is separate from
normal demo/live Trading 212 credential setup.

---

## Operating Modes

### Mock Mode (default, no credentials needed)

```bash
APP_MODE=mock
```

All broker calls return realistic fake data. Safe for UI development and testing. Orders are simulated locally as dry-runs. Mock broker status is synthetic and does not mean a real Trading 212 account is connected.

### Demo Mode (real API, no real money)

```bash
APP_MODE=demo
T212_API_KEY=your-demo-key
T212_API_SECRET=your-demo-secret
T212_ENVIRONMENT=demo
```

Connects to Trading 212 demo environment. Real API calls, no real money.

### Live Mode (real trades — explicit gating required)

```bash
APP_MODE=live
T212_API_KEY=your-live-key
T212_API_SECRET=your-live-secret
T212_ENVIRONMENT=live
```

> **WARNING**: Live mode places real orders with real money. It requires:
> 1. `APP_MODE=live` in server environment
> 2. `LIVE_TRADING_ENABLED=true` in `.env`
> 3. A live Trading 212 connection that has passed a recent broker test
> 4. Telegram supervision configured and verified
> 5. Demo soak review, broker review, Telegram review, and kill-switch drill recorded on the Settings page
> 6. Explicit live unlock in the Settings page before auto-trading can be resumed
>
> Live mode shows persistent red banners throughout the UI.

---

## Adding Trading 212 Credentials

1. Log in to Trading 212
2. Go to **Settings → API** (or [trading212.com/api](https://trading212.com/api))
3. Generate an API key and secret
4. In CashGuard: go to **Broker** page → enter credentials → select **Demo** → **Connect**
5. Click **Test Connection** to verify

Credentials are encrypted with AES-256 (Fernet) using your `MASTER_KEY` before being stored in the database. They are never logged or transmitted in plaintext.

---

## Environment Variables

See [`.env.example`](.env.example) for the full list. Key variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `APP_MODE` | `mock` / `demo` / `live` | `mock` |
| `SECRET_KEY` | JWT signing key (32+ chars) | auto-generated (insecure) |
| `MASTER_KEY` | Credential encryption key (32+ chars) | auto-generated (insecure) |
| `DATABASE_URL` | PostgreSQL async URL | localhost default |
| `REDIS_URL` | Redis URL with auth | localhost default |
| `ADMIN_EMAIL` | First admin account email | `admin@localhost` |
| `ADMIN_PASSWORD` | First admin account password | `change-me` |
| `T212_API_KEY` | Trading 212 API key | empty |
| `T212_API_SECRET` | Trading 212 API secret | empty |
| `T212_ENVIRONMENT` | `demo` or `live` | `demo` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | empty |
| `TELEGRAM_CHAT_ID` | Telegram alert chat id | empty |
| `TELEGRAM_ALLOWED_CHAT_IDS` | CSV allowlist for Telegram control chats | empty |
| `TELEGRAM_ALLOWED_USER_IDS` | CSV allowlist for Telegram control users | empty |
| `TELEGRAM_WEBHOOK_SECRET` | Shared secret for Telegram webhook validation | empty |
| `TELEGRAM_CONFIRM_WINDOW_SECONDS` | Confirmation TTL for risky Telegram commands | `120` |
| `BENZINGA_API_KEY` | Optional structured news/catalyst feed for watchlist intelligence | empty |
| `SENTRY_DSN` / `NEXT_PUBLIC_SENTRY_DSN` | Optional backend/frontend Sentry DSNs. Leave empty to disable. | empty |
| `DISCORD_WEBHOOK_URL` / `SLACK_WEBHOOK_URL` | Optional alert fan-out webhooks | empty |
| `LIVE_TRADING_ENABLED` | Additional live-mode execution gate | `false` |

---

## Commands

```bash
make setup        # First-time setup: copy .env, install deps, migrate, seed
make dev          # Start backend + frontend in dev mode
make up           # Start full Docker stack
make down         # Stop Docker stack
make migrate      # Run database migrations
make seed         # Seed demo data
make reset        # Drop + recreate + migrate + seed
make test         # Run all tests
make test-backend # Backend tests only
make test-frontend # Frontend tests only
make lint         # Run linters
make typecheck    # Run type checkers
make e2e          # Run Playwright e2e tests
make logs         # Tail Docker logs
make clean        # Remove build artifacts
```

---

## Running Tests

```bash
# All tests
make test

# Backend only (pytest)
cd apps/api
pytest tests/ -v

# Frontend unit tests (jest)
cd apps/web
npm test

# End-to-end tests (Playwright)
# Requires the app running on localhost:3000
cd apps/web
npx playwright test

# With UI
npx playwright test --ui
```

---

## Continuous Integration

The GitHub Actions CI pipeline protects the mock/paper trading lab release candidate with the following quality gates:

- **Frontend**: `npm ci`, `npm run lint`, `npm run typecheck`, `npm run build`, and `npm test`.
- **Backend**: Targeted `pytest` for paper execution and API safety, plus targeted `ruff` linting on changed files (balancing quality with pre-existing technical debt).
- **End-to-End**: A seeded Playwright smoke test (`mock-paper-release.spec.ts`) running against a live mock-mode stack.
- **Security**: Secret scanning, prohibited financial pattern checks, and hardcoded safety invariant validation.

---

## Production Operations Runbook

### Boot and validate

```bash
cp .env.example .env
# Set real POSTGRES_PASSWORD, REDIS_PASSWORD, SECRET_KEY, MASTER_KEY,
# ADMIN_PASSWORD, GRAFANA_PASSWORD, and notification credentials.
docker compose -f docker-compose.prod.yml --env-file .env config
docker compose -f docker-compose.prod.yml --env-file .env up -d --build
docker compose -f docker-compose.prod.yml --env-file .env ps
```

Prometheus loads `infra/prometheus/prometheus.yml` plus `infra/prometheus/alerts.yml`. Alertmanager expands SMTP/email environment variables at startup. Grafana is available behind `/grafana/` through nginx when the prod stack is healthy.

### Alert response

| Alert | First response |
|-------|----------------|
| `KillSwitchActivated` | Open **Emergency Controls**, confirm auto trading is halted, review **Audit Log**, then inspect broker/order state before re-enabling anything. |
| `CircuitBreakerOpen` | Stop strategy promotion/live execution, test the broker connection, check Trading 212/API credentials and recent order errors. |
| `APIDown` | Check `docker compose ... logs api`, then Postgres/Redis health. Do not restart repeatedly until database connectivity is understood. |
| `PostgresDown` / `RedisDown` | Check the backing container and exporter logs, verify disk/memory pressure, then restart the affected service if the dependency itself is unhealthy. |
| `CashGuardTaskFailuresCritical` | Inspect worker logs and the dead-letter queue path, identify the failing task, and keep auto trading disabled if order execution or reconciliation is affected. |
| `BrokerRequestLatencyCritical` | Treat broker-backed order flow as degraded. Keep or activate the kill switch if latency coincides with order errors or stale reconciliation. |

### Sentry

Sentry is optional. Leave `SENTRY_DSN` and `NEXT_PUBLIC_SENTRY_DSN` empty to run with no Sentry initialization or webpack wrapping. When enabling it, set backend and frontend DSNs explicitly and confirm no auth headers or cookies appear in captured events.

---

## Portfolio Research Pack

The Backtest page now includes a **Portfolio Research Lab** for longer-horizon, Trading 212-friendly strategies that can be implemented honestly with the repo's validated daily price data.

Implemented portfolio strategies:

- `buy_hold_core` — diversified buy-and-hold core with annual rebalancing
- `equal_weight_rebalance` — equal-weight portfolio with periodic rebalancing
- `cross_sectional_momentum` — monthly long-only winner rotation
- `low_volatility_tilt` — monthly allocation toward lower-volatility assets
- `trend_following_tactical` — long-only moving-average timing with cash when trends break

These are intentionally **price-based only**. The repo does not yet have a validated fundamentals/dividends pipeline, so value, profitability, dividend-growth, and shareholder-yield strategies are not faked with unreliable proxies.

---

## Intelligence Layer

- **Primary/validator split**: Alpaca is the live market-data source, while Polygon cross-checks quotes/bars and provides fallback when validation is strong enough.
- **Morning scan ranking**: premarket candidates are ranked by gap, RVOL, and optional catalyst context from Benzinga or Polygon news.
- **Regime-aware suppression**: strategies are automatically suppressed in unsafe, risk-off, or mismatched regimes before new entries are considered.
- **Event-risk filter**: fresh high-impact catalysts can block mean-reversion entries when continuation risk is too high.
- **Operator alerts**: Telegram and in-app alerts fire on regime shifts and market-data degradation, with deduping so repeated warnings do not spam the operator.

---

## Project Structure

```
t212-cashguard/
├── apps/
│   ├── api/                    # FastAPI backend
│   │   ├── app/
│   │   │   ├── api/v1/routes/  # All route handlers
│   │   │   ├── broker/         # T212 adapter + mock adapter
│   │   │   ├── core/           # Config, security
│   │   │   ├── db/             # Models, migrations, seed
│   │   │   ├── execution/      # Order execution engine
│   │   │   ├── market_data/    # Market data providers
│   │   │   ├── risk/           # Risk engine
│   │   │   ├── strategies/     # Strategy implementations
│   │   │   └── workers/        # Celery tasks
│   │   └── tests/
│   └── web/                    # Next.js 14 frontend
│       ├── app/                # App Router pages
│       ├── components/         # UI components
│       ├── hooks/              # React Query hooks
│       ├── services/           # API client
│       ├── stores/             # Zustand state
│       └── tests/              # Unit + e2e tests
├── infra/
│   ├── docker/
│   └── scripts/
├── docs/
├── docker-compose.yml
├── Makefile
└── .env.example
```

---

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the full system design.
See [`docs/telegram-integration.md`](docs/telegram-integration.md) for Telegram setup and command flows.
See [`docs/implementation-roadmap.md`](docs/implementation-roadmap.md) for the tracked build roadmap and next repo workstreams.

**Key components:**

| Component | Technology |
|-----------|------------|
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind CSS |
| Backend | FastAPI, Python 3.12, async SQLAlchemy |
| Database | PostgreSQL 16 |
| Cache/Queue | Redis 7 |
| Workers | Celery + Celery Beat |
| Auth | JWT (jose) + bcrypt |
| Encryption | Fernet (AES-256-CBC) |
| Testing | pytest, Jest, Playwright |

---

## Safety Guarantees

| Guarantee | Implementation |
|-----------|----------------|
| Cash-only | `CASH_ONLY_MODE = True` hardcoded; pre-order cash guard check in risk engine |
| No deposits | Zero deposit/bank/card routes, models, or UI exist anywhere in codebase |
| No leverage | Only equity positions; no margin, CFD, or leverage parameters |
| Live mode gated | Requires `APP_MODE=live` + explicit DB flag + confirmation modal |
| Order dedup | SHA-256 `client_order_key` prevents duplicate submissions |
| Sell convention | `make_sell_quantity()` always negates for T212 compliance |
| Audit trail | Every significant action written to `audit_logs` table |
| Kill switch | Single DB flag checked as first gate in risk engine |

---

## Common Issues

**`alembic upgrade head` fails — can't connect to database**
```bash
# Make sure postgres is running first
docker-compose up -d postgres
sleep 3
make migrate
```

**Frontend shows blank page / 401**
- Make sure the backend is running: `curl http://localhost:8000/v1/health/live`
- Check `NEXT_PUBLIC_API_URL` in `.env`
- Default login: `admin@localhost` / `change-me`

**`Secret key too short` error**
```bash
# Generate proper keys
openssl rand -hex 32  # paste into SECRET_KEY in .env
openssl rand -hex 32  # paste into MASTER_KEY in .env
```

**Celery workers not starting**
- Redis must be running before workers start
- Check `REDIS_URL` in `.env` includes the password

---

## Known Limitations

1. **Market data quality depends on your plan tier**: Alpaca is used as the live truth source and Polygon as validator/fallback, but free-tier feeds still have coverage limitations versus premium consolidated feeds.
2. **Trading 212 streaming**: The app still uses polling rather than broker-side streaming for order and position updates.
3. **Multi-user**: Designed for single-user local use; RBAC exists but multi-user flows remain lightly tested.
4. **News intelligence**: Structured watchlist catalysts are live, but premium quality depends on optional Benzinga credentials; without them the app falls back to lighter Polygon-based news context.
5. **Execution venue limits**: Trading 212 remains the main bottleneck for advanced execution tactics because the API is still less feature-rich than institutional broker APIs.
6. **No cloud deployment**: Local-first by design; cloud deployment still needs a reverse proxy, proper secrets management, and TLS configuration.

---

## Security

See [`docs/security.md`](docs/security.md) for full details.

- Never commit `.env` to version control
- Rotate `SECRET_KEY` and `MASTER_KEY` if compromised (requires re-encrypting stored credentials)
- The application stores NO bank credentials, card details, or Open Banking tokens — ever
- Credentials are encrypted before DB storage and decrypted only for API calls
