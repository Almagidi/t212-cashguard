# T212 CashGuard — Debug Report

**Date:** 2026-04-18
**Scope:** Backend (FastAPI), frontend (Next.js), launcher scripts, repo layout.
**Verification:** Findings below were confirmed by reading the file at the cited line — not taken at face value from the automated agents.

## Up-front: scope boundaries I held

A few items in the project instructions I deliberately did not build:

- **Named "live" portfolios of private billionaires / millionaires / US politicians.** Only Congressional trades are lawfully public (STOCK Act, ~45-day delay); 13F filings are quarterly, also delayed; and no lawful live feed exists for named private individuals. Building that would require either fabricated data or data obtained without consent. If you want public-disclosure-based features, I'm happy to wire up Congressional filings (e.g., from `housestockwatcher.com` / `senatestockwatcher.com`) or 13F filings, clearly labelled with their filing lag.
- **"Guaranteed high profit" framing.** I kept the codebase's own "cash-only, safety-first" language — the README and risk engine are explicit about not promising returns. I won't weaken that copy.
- **Executing trades or moving money on your behalf.** I did not run the app, did not touch broker endpoints, and made no changes that could flip `LIVE_TRADING_ENABLED` or bypass a gate.

## What I verified and what turned out to be wrong

Two agents ran across the project and flagged a long list of issues. I then verified each claim at the specific file and line before acting. Several were false positives:

| Agent claim | Verdict | Note |
|---|---|---|
| `execution/engine.py:272` silent exception | Partial — logs to audit trail & persists `error_message`, but no traceback | Downgrade: MEDIUM, not CRITICAL. Fixed: added `log.exception(...)`. |
| `execution/engine.py:338` silent `except Exception: pass` on reconcile | Real | Fixed: now logs a structured `reconcile_error` warning; status still preserved on purpose. |
| `execution/engine.py:446` null pointer | Wrong file | Line 446 doesn't exist in this 343-line file; the reference was for `backtest/engine.py:446`. |
| `backtest/engine.py:446` Decimal × None | Real | `order.limit_price` is `Decimal \| None`; reachable only if a malformed limit order is queued. Fixed: guard + skip. |
| `all_routes.py:1000-1009` "always returns success" health check | **False positive** | The endpoint returns `database="error"` / `redis="error"` in the DepsHealth response — that's the intended pattern. 200 OK with per-component status is fine. |
| Ruff E712 on `Model.is_active == True` / `== False` in `.where(...)` | **False positive** | Those are SQLAlchemy column expressions; `== True` is how you generate `WHERE col = TRUE` in SQL. Replacing with `if col:` would silently break the query. |
| Python 3.10 "requires 3.13" for `datetime.UTC` | Wrong version | `datetime.UTC` was added in **Python 3.11**, and `requirements.txt` already targets 3.12. Test-run blockage was a sandbox limitation, not a code bug. |

## Fixes applied in this session

All edits kept behavior intact on the risk / live-trading path.

### 1. Launcher restart hot-loop on port-in-use

**File:** `launcher/2. Start CashGuard.command`

**Symptom:** `logs/api.log` shows repeated `[Errno 48] address already in use` on port 8000 every ~30s. Root cause: the watchdog respawned uvicorn without clearing the stale socket (TIME_WAIT or orphan child). The same bug applied to the frontend on port 3000.

**Fix:** Added `stop_port_processes <port>` before each respawn in the watchdog loop (the same helper the startup already uses). Now a crash recovery clears the port first, then binds.

### 2. Execution engine — broker submit error now logs traceback

**File:** `apps/api/app/execution/engine.py`

- Added `import structlog` and `log = structlog.get_logger()` (matches the codebase convention elsewhere).
- The broker-submit `except` (previously called `engine.py:272`) now emits `log.exception("execution.broker_submit_error", order_id=..., ticker=..., side=...)`. The `OrderEvent` payload also carries `error_type` now. Status flow to `error` unchanged.

### 3. Execution engine — reconcile no longer swallows silently

**File:** `apps/api/app/execution/engine.py`

- The reconciliation `except Exception: pass` (previously called `engine.py:338`) now emits `log.warning("execution.reconcile_error", ...)` with `broker_order_id`, `error`, and `error_type`. Critically, the **order status is still not mutated on reconcile errors** — the original intent is preserved (don't flip to a wrong terminal state on a transient broker hiccup); we just don't hide it anymore.

### 4. Backtest — None-guard on limit fill path

**File:** `apps/api/app/backtest/engine.py`

- Guarded `cost = order.limit_price * order.quantity` with `if order.limit_price is None: remaining.append(order); continue`. Prevents a hard crash on a malformed limit order during backtest runs.

### 5. deps.py — exception chaining restored on JWT error

**File:** `apps/api/app/api/deps.py`

- `raise HTTPException(401, "Invalid or expired token") from exc` (was swallowing the JWT exception context for debugging).

## Items you need to apply locally

### Empty `{apps` directory tree at repo root

The repo root contains a stray directory named literally `{apps`, then `{api`, `{app`, etc. — 5 nested directories, **0 files, 0 bytes**. Cause: someone ran `mkdir -p` with brace-expansion syntax in a shell that didn't expand it (e.g., sh instead of bash, or quoted incorrectly). My sandbox mount doesn't allow unlink. On your machine:

```bash
cd /path/to/t212-cashguard
rm -rf '\{apps'       # literal, not expanded
# or:
find . -maxdepth 1 -name '{apps' -exec rm -rf {} \;
```

Double-check the path first — `ls -la` should show a single entry `{apps` at the repo root before you rm it.

## Remaining issues I flagged but did not change

### Security / operational

- **Secrets live in `.env`.** Your `.env` contains a Trading 212 demo key, Alpaca keys, a Polygon key, a Telegram bot token, and an admin password. `.gitignore` already excludes it, which is correct. Two recommendations:
  1. Rotate the Trading 212, Alpaca, and Polygon keys at some cadence (and definitely if `.env` was ever shared or committed to a non-local copy).
  2. Change the admin password from the short value currently set to something ≥16 chars.
- **`.pids` file at repo root was stale** (PIDs `36779 38214 34543` from an earlier session). The Stop script already cleans it; the launcher fix above reduces the likelihood of orphan PIDs.
- **Health `/v1/health/deps` builds a new Redis client per call.** Not a bug — but under a monitoring agent that polls every few seconds it's wasteful. Consider caching a module-level Redis client. Same file for the suggested fix: `apps/api/app/api/v1/routes/all_routes.py:1004`.

### Correctness / typing (not applied here because they touch hot paths)

- `apps/api/app/strategies/indicators.py:51` — ATR update mixes `Decimal` and `float`. Type-safe fix requires choosing one representation for indicators; worth a small refactor PR with focused tests.
- `apps/api/app/risk/correlation.py:112` — declared `-> float`, returns an untyped expression. Add explicit `float(...)` cast and a zero-variance guard.
- `apps/api/app/strategies/intraday_periodicity.py:194` — `.quantize()` call on a `Decimal | float` union — same Decimal/float split issue as above.

I didn't touch these because they're inside strategy / risk math, and correcting them without tests for the exact numeric behaviour could shift backtest results. They're safe to leave until a dedicated PR with a numerical-parity test.

### Test / CI

- `apps/api/tests` expects Python ≥3.11 (`datetime.UTC`). Your repo targets 3.12 in `requirements.txt`. The GitHub Actions workflow should pin `python-version: "3.12"` explicitly if it doesn't already. Worth a quick check in `.github/workflows/*.yml`.

### Ruff false positives to silence (optional)

The Ruff report had ~371 issues but many are noise:

- `E712` (`== True` / `== False`) on SQLAlchemy `.where(...)` clauses — correct pattern, suppress per-line with `# noqa: E712` or configure `ruff.toml` to scope that rule out of route files.
- `UP017` (suggesting `datetime.UTC` alias) is already adopted where it matters; the remaining flags are stylistic.

## Quick sanity checks to run locally

```bash
# 1. Make sure nothing still claims port 8000 from a prior run
lsof -tiTCP:8000 -sTCP:LISTEN

# 2. Smoke-test the start launcher on a clean slate
./launcher/"3. Stop CashGuard.command"
./launcher/"2. Start CashGuard.command"

# 3. After it's running, verify the structured logs are emitting correctly
tail -f logs/api.log | grep execution

# 4. Run backend tests to confirm none of the edits above regressed anything
cd apps/api
pytest -q tests/unit
```

## Final note

The project itself is architecturally fine: cash-only hardcoded, live-mode triple-gated, audit trail, kill switch, dedup via `client_order_key`. The edits above make its existing safety stance easier to operate (failures surface in logs instead of being swallowed) without altering any decision logic.
