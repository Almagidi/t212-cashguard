# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      Browser (localhost:3000)                    │
│                    Next.js 14 — App Router                       │
│   React Query ◄──► Zustand ◄──► API Client (Axios)              │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTP/JSON (proxied via next.config.js)
┌───────────────────────────▼─────────────────────────────────────┐
│                    FastAPI (localhost:8000)                       │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │  Auth    │  │  Broker  │  │ Strategy │  │  Emergency   │   │
│  │  Routes  │  │  Routes  │  │  Routes  │  │  Controls    │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────┘   │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Risk Engine                            │   │
│  │  kill_switch → cash_guard → dedup → daily_loss →        │   │
│  │  position_size → max_trades → consecutive_losses        │   │
│  └──────────────────────┬───────────────────────────────────┘   │
│                         │                                         │
│  ┌──────────────────────▼───────────────────────────────────┐   │
│  │                  Execution Engine                         │   │
│  │  Intent → Dedup (client_order_key) → Submit → Reconcile  │   │
│  └──────────────────────┬───────────────────────────────────┘   │
│                         │                                         │
│  ┌──────────────────────▼───────────────────────────────────┐   │
│  │              Broker Adapter (pluggable)                   │   │
│  │     MockAdapter ◄──────────────────► Trading212Adapter   │   │
│  └──────────────────────────────────────────────────────────┘   │
└───────────────────────────┬─────────────────────────────────────┘
                            │
           ┌────────────────┴──────────────────┐
           │                                   │
┌──────────▼──────────┐           ┌────────────▼────────────┐
│    PostgreSQL 16     │           │       Redis 7            │
│                      │           │                          │
│  users               │           │  Celery broker           │
│  sessions            │           │  Celery result backend   │
│  broker_connections  │           │  Rate-limit counters     │
│  instruments         │           │                          │
│  strategies          │           └─────────────────────────┘
│  signals             │
│  orders              │           ┌─────────────────────────┐
│  order_events        │           │   Celery Workers         │
│  positions_snapshots │           │                          │
│  trades              │           │  reconcile_pending (30s) │
│  risk_events         │           │  sync_account (60s)      │
│  audit_logs          │           │  eod_flatten_check (5m)  │
│  app_settings        │           │                          │
└─────────────────────┘           └─────────────────────────┘
```

## Request Lifecycle (Order Placement)

```
User clicks "Place Order"
    │
    ▼
Frontend validates form (Zod schema)
    │
    ▼
POST /v1/orders  (with JWT)
    │
    ▼
FastAPI auth middleware (JWT verification)
    │
    ▼
Fetch live account summary from broker
    │
    ▼
Risk Engine — run_all_checks():
    ├── check_kill_switch()           [BLOCK if active]
    ├── check_auto_trading_enabled()  [BLOCK if disabled]
    ├── check_cash_guard()            [BLOCK if cost > available cash]
    ├── check_duplicate_order()       [BLOCK if active order exists]
    ├── check_daily_loss_limit()      [BLOCK if breach]
    ├── check_max_open_positions()    [BLOCK if at limit]
    ├── check_position_size()         [BLOCK if too large]
    └── check_max_trades_today()      [BLOCK if at limit]
    │
    ▼ (all checks pass)
Execution Engine:
    ├── create_order_intent()   [writes DB record, status=pending_intent]
    ├── dedup check             [client_order_key lookup — abort if duplicate]
    └── submit_order():
        ├── status → submitted
        ├── T212: negate quantity if side=sell
        ├── call broker.place_market_order() / place_limit_order() / etc.
        ├── persist broker_request + broker_response
        └── status → accepted | filled | error
    │
    ▼
Write audit log entry
    │
    ▼
Return order response to frontend
    │
    ▼
React Query invalidates ['orders', 'positions', 'account']
    │
    ▼
UI updates
```

## Safety Architecture

### Cash Guard (mandatory, hardcoded)

```python
# app/core/config.py
CASH_ONLY_MODE: bool = True   # Cannot be False without code change

# app/risk/engine.py  
async def check_cash_guard(ticker, quantity, estimated_price, available_cash):
    if quantity <= 0:      # Sell — no cash needed
        return
    cost = quantity * estimated_price
    if cost > available_cash:
        raise RiskViolation(f"Cash guard: {cost:.2f} > {available_cash:.2f}")
```

No deposit, withdrawal, or bank endpoint exists. Grep the entire codebase for `deposit`, `withdrawal`, `bank_account`, `open_banking` — zero results.

### Sell Quantity Convention

Trading 212 requires negative quantity for sell orders. This is enforced at the execution engine boundary:

```python
# app/broker/trading212.py
def make_sell_quantity(quantity: Decimal) -> Decimal:
    return -abs(quantity)   # Always negative, always

# app/execution/engine.py
if order.side == "sell":
    submit_qty = make_sell_quantity(order.quantity)
```

This is covered by dedicated unit tests that assert the quantity is negative for every sell scenario.

### Idempotency / Dedup

Trading 212 order placement is **not idempotent** per their API docs. The app generates a `client_order_key` (SHA-256 hash of `signal_id:ticker:side`) before any broker call. If a record with that key already exists in the orders table, the submission is aborted.

```python
client_key = hashlib.sha256(f"{signal_id}:{ticker}:{side}".encode()).hexdigest()[:40]
existing = await db.execute(select(Order).where(Order.client_order_key == client_key))
if existing:
    return existing   # Return existing order, do NOT re-submit
```

## Database Schema

18 tables with full foreign keys, indexes, and JSONB for broker payloads. Key design decisions:

- Money columns use `NUMERIC(20, 8)` — never float
- Broker API responses stored as `JSONB` for full auditability
- `audit_logs` is append-only by convention (no updates, no deletes)
- `order_events` provides a full state-machine history for every order
- `app_settings` is a single-row config table (id=1 always)

## Worker Tasks

| Task | Schedule | Purpose |
|------|----------|---------|
| `reconcile_pending_orders` | Every 30s | Poll broker for status of accepted/submitted orders |
| `sync_account_snapshot` | Every 60s | Save account summary snapshot to DB |
| `check_eod_flatten` | Every 5 min | Trigger EOD flatten if enabled and past session end |

## Pluggable Adapters

### Broker Adapter Interface

Both `Trading212Adapter` and `MockBrokerAdapter` implement the same async context manager interface:

```python
async def get_account_summary() -> dict
async def get_positions() -> list[dict]
async def place_market_order(ticker, quantity, *, time_validity) -> dict
async def place_limit_order(ticker, quantity, limit_price, ...) -> dict
async def cancel_order(order_id) -> None
async def test_connection() -> dict
# ... etc
```

The execution engine receives a broker instance and never knows which one it is.

### Market Data Adapter

`MockMarketDataProvider` generates realistic random-walk OHLCV data. A real provider (Polygon.io, Alpha Vantage, etc.) can be plugged in by implementing the same interface:

```python
def get_quote(ticker: str) -> Quote
def get_ohlcv(ticker, interval_minutes, bars) -> list[dict]
def is_market_open(ticker) -> bool
```
