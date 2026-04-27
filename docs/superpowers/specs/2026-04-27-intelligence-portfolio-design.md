# Intelligence Portfolio System — Design Spec

**Date:** 2026-04-27  
**Status:** Approved for implementation planning  
**Scope:** Four-layer intelligence system with unified AI gate across all trades, full lifecycle management (entry, stop loss, take profit, exit)

---

## 1. Overview

Add a four-layer intelligence system to CashGuard that gates every trade — intraday and long-term — through AI reasoning before execution. The system runs entirely on free APIs and free LLM tiers. It introduces a new Track B (intelligence portfolio) for long-term positions alongside the existing Track A (intraday strategies), which remains unchanged except for the addition of the AI gate.

**Core guarantee:** No trade of any kind executes without passing through the AI gate. If AI is unavailable, the trade is held in a manual review queue for user decision — it is never silently dropped or auto-executed blind.

---

## 2. Two-Track Architecture

### Track A — Intraday (existing, 60% of capital)
- Strategies: ORB, Opening Fade, VWAP Reclaim, Closing Momentum, Intraday Periodicity
- All positions flattened EOD (hard rule, unchanged)
- Now gated: every signal passes through the AI gate (fast path) before execution

### Track B — Intelligence Portfolio (new, 40% of capital)
- Long-term positions: swing (2–10 days), weekly, and monthly holds
- Positions are NOT flattened EOD
- Initiated by AI conviction scores, not strategy signals
- 40% capital ceiling enforced at DB transaction level before every new entry

---

## 3. Stock Universe

A `StockUniverseService` maintains the tradeable universe and refreshes it daily:

- **Base**: S&P 500 + Russell 2000 + NASDAQ composite tickers via Polygon screener
- **Auto-additions**: any ticker appearing in a congressional trade disclosure is automatically added
- **Momentum additions**: daily volume/momentum screener adds top movers (volume spike >3× 20-day avg with positive price action)
- **Stored in Redis** with a 24-hour TTL; Celery beat task refreshes at market open each day
- Covers small and large caps; no manual curation required after initial setup

---

## 4. Intelligence Data Sources (all free)

| Source | Data | Poll interval | Library |
|---|---|---|---|
| Capitol Trades API | Congressional STOCK Act disclosures | 30 min | `httpx` |
| GDELT | Global news events, sentiment, geopolitics | 15 min | REST API |
| Polygon News | Ticker-scoped and macro news | 10 min | already integrated |
| Yahoo Finance News | Deeper news analysis | 10 min | `yfinance` |
| Reddit | r/wallstreetbets, r/stocks mention counts | 10 min | `praw` |
| StockTwits | Per-ticker bullish/bearish ratio | 5 min | REST API |

All sources polled by Celery beat tasks. Results stored in Postgres (`intelligence_snapshots`) and cached in Redis (30-minute TTL).

---

## 5. Rule Scorer

Runs instantly on every intelligence event. No API calls. Produces a `rule_score` in the range −20 to +20.

| Signal | Score |
|---|---|
| Congressional buy | +15 |
| Congressional sell | −15 |
| GDELT positive tone on sector | +8 |
| GDELT negative tone on sector | −8 |
| StockTwits bullish ratio >70% | +6 |
| StockTwits bearish ratio >70% | −6 |
| Reddit mention spike (>3× avg) bullish | +4 |
| Yahoo/Polygon news positive sentiment | +5 |
| Yahoo/Polygon news negative sentiment | −5 |

Scores are additive and capped at ±20.

---

## 6. LLM Reasoning Service

Called only when `abs(rule_score) >= 8.0` — roughly 5–15 times per day, well within free tier limits.

### Fallback chain
1. **Gemini 2.0 Flash** (primary) — 1,500 req/day free, 1M tokens/min
2. **Groq / Llama 3.1 70B** (fallback) — 14,400 req/day free, fast inference
3. **OpenRouter free models** (last resort) — Mistral 7B, Phi-3

### Behaviour
- All three providers receive an identical JSON prompt containing: ticker, sector, rule_score, and up to 10 recent event summaries (text only)
- Each must return `{ "score": float, "reasoning": str }` where score is in [−10, +10]
- Any of the following counts as a provider failure and moves to the next: HTTP error, timeout, JSON parse failure, score outside [−10, +10], empty reasoning string
- **If all three providers fail** → raise `AIUnavailableError` → trade enters manual review queue

### Score merge
```
final_score = 0.4 × rule_score + 0.6 × llm_score
```
Both scores stored separately in `intelligence_snapshots` for audit.

---

## 7. Unified AI Gate

Every trade — intraday or long-term — passes through the AI gate before execution.

### Fast path (Track A intraday)
- Triggered when a strategy fires a signal
- Context: recent news + sentiment for that specific ticker (last 30 min)
- Sources: Polygon news, Yahoo Finance news, StockTwits ratio
- LLM: Gemini Flash, 2–3 second latency budget
- If fast path LLM times out: trade goes to manual review queue

### Deep path (Track B long-term)
- Triggered when rule scorer crosses conviction threshold
- Context: full intelligence pipeline (congressional, GDELT, macro, social, Yahoo news)
- LLM: full fallback chain, runs asynchronously
- Result written to Redis and DB; executor reads from cache

### Three outcomes
1. **Execute** — AI confident, no anomalies, score clears threshold → trade fires automatically
2. **Manual Review Queue** — AI unavailable, or anomaly detected, or data flagged → held for user decision; expires after 30 minutes
3. **Block** — AI confident the signal is negative (score below reject threshold) → trade suppressed, logged

---

## 8. Anomaly Detection

Always running, regardless of path. Any of the following automatically routes to the manual review queue and prevents execution:

- AI score and price action direction conflict by more than 30%
- A data source returned stale or empty data (last poll older than 2× expected interval)
- Volume spike >5× 20-day average with no matching news explanation
- Conflicting buy and sell signals on the same ticker in the same window
- Congressional sentiment on an open position reverses (member sells a stock you hold long)
- Social sentiment spike >90% bullish or bearish (potential manipulation signal)

Flagged items are highlighted in the dashboard with the specific reason shown. The trade is held regardless of AI confidence until the user reviews.

---

## 9. Full Trade Lifecycle

### Entry
- AI gate approves → executor places order via T212 API
- Stop loss and take profit placed at the same moment as entry
- If stop loss placement fails → entry is aborted

### Stop loss levels (set at entry)
- Track A intraday: 1–2× ATR below entry
- Track B swing: 5–8% below entry based on recent volatility
- Track B weekly/monthly: 8–15% below entry

### Take profit targets (set at entry)
- Derived from conviction score magnitude: higher conviction → wider target
- Track A intraday: 1.5–2× the stop loss distance (risk:reward minimum 1:1.5)
- Track B swing: 10–20% above entry
- Track B weekly/monthly: 20–40% above entry

### Position monitoring and exit
`IntelPositionMonitor` runs every 5 minutes on all open positions. It triggers an exit when any of these occur:

- Price hits the pre-set take-profit target
- Price hits the stop loss (T212 handles execution; monitor confirms and updates DB)
- Conviction score on the ticker reverses by ≥10 points from entry score
- A negative anomaly is detected on an open position
- Track A only: EOD flatten — all intraday positions closed regardless of profit/loss

### Exit AI gate
Exit decisions pass through the same AI gate:
- AI confirms exit → order placed automatically
- AI unavailable during exit → routed to manual review queue (user approves or rejects exit)
- Exit looks anomalous → flagged and held for review

---

## 10. Data Model

### `intelligence_snapshots`

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK, gen_random_uuid() |
| ticker | VARCHAR(10) | null for macro events |
| sector | VARCHAR(50) | |
| event_type | intel_event_type | congressional_trade, macro_news, gdelt_event, yahoo_news, social_sentiment |
| source | VARCHAR(30) | capitol_trades, gdelt, polygon_news, yahoo_finance, stocktwits, reddit |
| rule_score | FLOAT | always set, range −20/+20 |
| llm_score | FLOAT | null if LLM not called |
| final_score | FLOAT | rule_score if no LLM |
| congressional_member | VARCHAR(100) | null for non-congress rows |
| trade_direction | VARCHAR(4) | buy / sell |
| trade_value_usd | BIGINT | in cents |
| summary | TEXT | human-readable headline |
| reasoning | TEXT | LLM chain-of-thought |
| raw_data | JSONB | original API payload |
| created_at | TIMESTAMPTZ | default now() |
| expires_at | TIMESTAMPTZ | now() + 90 days |

Indexes: `(ticker, created_at DESC)`, `(sector, created_at DESC)`, `(expires_at)`, `(event_type, created_at DESC)`

### `intel_positions`

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| ticker | VARCHAR(10) | |
| hold_type | intel_hold_type | swing, weekly, monthly |
| status | intel_pos_status | open, reduced, closed |
| size_usd | DECIMAL(12,2) | ≤ 40% of total capital |
| entry_price | DECIMAL(12,4) | fill price at open |
| entry_score | FLOAT | conviction at entry |
| stop_loss_price | DECIMAL(12,4) | placed at entry |
| take_profit_price | DECIMAL(12,4) | placed at entry |
| snapshot_ids | UUID[] | events that triggered entry |
| exit_price | DECIMAL(12,4) | null until closed |
| pnl_usd | DECIMAL(12,2) | null until closed |
| opened_at | TIMESTAMPTZ | default now() |
| closed_at | TIMESTAMPTZ | null until closed |

Indexes: `(ticker, status)`, `(status, hold_type)`, `(opened_at DESC)`

### `trade_review_queue`

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| signal_type | VARCHAR(5) | entry / exit |
| track | VARCHAR(1) | A / B |
| ticker | VARCHAR(10) | |
| strategy | VARCHAR(50) | ORB, VWAP Reclaim, etc. or "Track B" |
| size_usd | DECIMAL(12,2) | |
| rule_score | FLOAT | |
| llm_score | FLOAT | null if AI was down |
| reason | VARCHAR(20) | ai_unavailable / anomaly / flagged_data |
| reason_detail | TEXT | human-readable explanation |
| expires_at | TIMESTAMPTZ | now() + 30 minutes |
| decided_at | TIMESTAMPTZ | null until actioned |
| decision | VARCHAR(8) | approved / rejected |
| created_at | TIMESTAMPTZ | |

---

## 11. New Backend Files

| File | Purpose |
|---|---|
| `services/stock_universe.py` | Daily universe refresh, Redis storage |
| `services/intel_aggregator.py` | Orchestrates all polling tasks, scores, caches |
| `services/congressional_tracker.py` | Capitol Trades poller |
| `services/social_sentiment.py` | StockTwits + Reddit poller |
| `services/llm_reasoning.py` | Gemini → Groq → OpenRouter fallback chain |
| `services/anomaly_detector.py` | Anomaly detection rules |
| `services/intel_portfolio_executor.py` | Track B entry/exit execution with stop loss + take profit |
| `services/intel_position_monitor.py` | Monitors open positions; triggers exits, stop loss, take profit |
| `db/migrations/0008_intelligence.py` | Creates all new tables and enums |

### Modified backend files

| File | Change |
|---|---|
| `services/news_intelligence.py` | Extended to cover macro + Yahoo Finance news |
| `workers/tasks.py` | +6 polling tasks, +position monitor task |
| `workers/celery_app.py` | +beat schedule for all new tasks |
| `services/strategy_runner.py` | +AI gate hook (fast path) before signal execution |

---

## 12. New Frontend Files

| File | Purpose |
|---|---|
| `app/app/intelligence/page.tsx` | Main intelligence dashboard page |
| `components/intelligence/ai-status-bar.tsx` | Provider health + universe count |
| `components/intelligence/review-queue.tsx` | Manual review queue with approve/reject |
| `components/intelligence/congressional-feed.tsx` | Congressional trades feed |
| `components/intelligence/macro-news-feed.tsx` | GDELT + Yahoo + Polygon news feed with anomaly flags |
| `components/intelligence/social-sentiment.tsx` | Per-ticker sentiment bars |
| `components/intelligence/conviction-heatmap.tsx` | Colour-coded conviction score grid |
| `components/intelligence/active-positions.tsx` | Track B open positions with live P&L |
| `hooks/use-intelligence.ts` | Data fetching hook for dashboard |

---

## 13. Safety Guarantees

1. **Track A strategy logic is never modified** — the AI gate is a thin hook in `strategy_runner.py` wrapping existing signal execution
2. **Every trade requires AI gate passage or manual approval** — no auto-execution without one of the two
3. **Stop loss and take profit are placed at entry, before position is considered open** — if either fails, entry is aborted
4. **40% capital ceiling for Track B enforced in DB transaction** — checked atomically before every new entry
5. **Manual review queue expires in 30 minutes** — stale intraday approvals cannot fire after the setup has passed
6. **EOD flatten for Track A is a hard rule** — not overridable by AI or manual action
7. **Anomaly-flagged data never silently used** — anomalies route to manual review regardless of AI confidence
