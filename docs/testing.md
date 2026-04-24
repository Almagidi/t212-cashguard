# Testing

## Test Stack

| Layer | Tool | Location |
|-------|------|----------|
| Backend unit | pytest + pytest-asyncio | `apps/api/tests/unit/` |
| Backend integration | pytest + httpx | `apps/api/tests/integration/` |
| Frontend unit | Jest + Testing Library | `apps/web/tests/unit/` |
| End-to-end | Playwright | `apps/web/tests/e2e/` |

---

## Running Tests

### All tests
```bash
make test
```

### Backend only
```bash
cd apps/api
pytest tests/ -v --tb=short

# With coverage
pytest tests/ -v --cov=app --cov-report=term-missing
```

### Frontend unit tests
```bash
cd apps/web
npm test

# Watch mode
npm run test:watch
```

### End-to-end tests
```bash
# Requires app running on localhost:3000 and :8000
cd apps/web
npx playwright test

# With visible browser
npx playwright test --headed

# Interactive UI
npx playwright test --ui

# Specific file
npx playwright test tests/e2e/app.spec.ts
```

---

## Key Test Coverage

### Backend Unit Tests (`tests/unit/test_core.py`)

| Test Class | What It Tests |
|-----------|--------------|
| `TestSecurity` | Password hashing, JWT creation/decode, field encryption roundtrip |
| `TestSellQuantityConvention` | **Critical**: T212 sell orders always use negative quantity |
| `TestORBStrategy` | Opening range computation, range validation, signal generation, quantity sizing |
| `TestMockBroker` | Mock adapter: account summary, positions, buy/sell orders |
| `TestRiskEngine` | Cash guard blocks overspend, allows affordable orders, kill switch blocks all |

### Backend Integration Tests (`tests/integration/test_api.py`)

| Test Class | What It Tests |
|-----------|--------------|
| `TestAuthFlow` | Login success/failure, /me endpoint, protected route requires auth |
| `TestAccountFlow` | Account summary, cash guard status, **cash_only_mode always True** |
| `TestHealthFlow` | Health live/ready, root endpoint |
| `TestInstrumentsFlow` | List, sync, get by ticker, 404 on missing |
| `TestStrategiesFlow` | Create, list, enable/disable, disabled by default |
| `TestRiskFlow` | Get/update profile, kill switch on/off |
| `TestOrderFlow` | Place order, validation errors, quantity must be positive from API |
| `TestEmergencyFlow` | Kill switch, auto-trading off, **cannot enable auto-trading with kill switch active** |
| `TestPositionsFlow` | Mock mode positions |
| `TestAuditFlow` | Login events appear in audit log |

### Critical Safety Tests

These tests exist specifically to catch regressions in safety-critical behaviour:

```python
# T212 sell quantity is always negative
def test_sell_order_uses_negative_quantity():
    from app.broker.trading212 import make_sell_quantity
    result = make_sell_quantity(Decimal("10"))
    assert result == Decimal("-10")
    assert result < 0

# Cash guard blocks overspend
async def test_cash_guard_blocks_overspend():
    await engine.check_cash_guard("AAPL", Decimal("1000"), Decimal("200"), Decimal("100"))
    # Raises RiskViolation

# Cash-only mode cannot be false
async def test_cash_guard_status():
    resp = await client.get("/v1/account/cash-guard")
    assert resp.json()["cash_only_mode"] is True   # Always

# Kill switch blocks everything
async def test_kill_switch_blocks_all_trades():
    settings.kill_switch_active = True
    with pytest.raises(RiskViolation) as exc:
        await engine.check_kill_switch()
    assert "kill switch" in exc.value.reason.lower()
```

### Frontend Unit Tests (`tests/unit/utils.test.ts`)

Tests `formatCurrency`, `formatPnL`, `pnlClass`, `orderStatusBg`, `truncate`, `timeAgo`.

### E2e Tests (`tests/e2e/app.spec.ts`)

50+ test cases covering:
- Login page rendering and validation
- Dashboard stats, sidebar navigation
- Broker page: cash-only warning, Live restricted badge
- Strategies: create, enable/disable
- Risk controls: kill switch form
- Emergency: confirmation dialogs required before execution, cancel works
- Orders: tab filters
- Audit log: filtering
- Settings: no deposit/bank/card UI
- **Safety invariants**: checks 5 pages for zero deposit/bank/card strings

---

## Test Configuration

### Backend — `pytest.ini`

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

### Backend — `tests/conftest.py`

Uses **SQLite in-memory** database so tests run without PostgreSQL. All DB operations are rolled back after each test. No real broker calls — mock mode is forced via environment overrides.

### Frontend — `jest.config.ts`

Uses `jsdom` environment + `@testing-library/jest-dom` matchers.

### E2e — `playwright.config.ts`

Targets `http://localhost:3000`. Starts dev server automatically if not running. Screenshots on failure.

---

## Writing New Tests

### Adding a backend test

```python
# tests/unit/test_myfeature.py
import pytest

@pytest.mark.asyncio
async def test_my_thing(db):
    # db is the in-memory SQLite session from conftest
    from app.risk.engine import RiskEngine
    engine = RiskEngine(db)
    # ... test
```

### Adding an e2e test

```typescript
// tests/e2e/myfeature.spec.ts
import { test, expect } from '@playwright/test'

test('my feature works', async ({ page }) => {
  await page.goto('/auth/login')
  await page.fill('[name=email]', 'admin@localhost')
  // ...
})
```
