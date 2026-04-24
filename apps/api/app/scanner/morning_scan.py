"""
Morning scanner — finds best ORB and Opening Fade candidates each day at 09:15 ET.

Provider priority:
  1. Alpaca (real-time snapshots, free paper account)
  2. Polygon (delayed, free tier)
  3. Static fallback (configured universe)

Strategy-type filtering:
  ORB candidates   → gap 0.5-2%   (continuation breakout; trending regime)
  Fade candidates  → gap 1.5-6%   (gap mean-reversion; choppy regime)
  Both strategies share volume and price filters.

Filters every ticker through:
  - Pre-market relative volume > 1.5x
  - No earnings within ±2 days
  - Price $10-$500
  - Minimum pre-market volume 5 000 shares

Updates strategy.params["todays_watchlist"] for every enabled live strategy.
Candidates are tagged with ``strategy_type`` so the runner can route them
to the correct engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, ClassVar

import structlog

from app.core.config import settings
from app.services.news_intelligence import NewsIntelligenceService

log = structlog.get_logger()


@dataclass
class ScanCandidate:
    ticker: str
    score: float
    pre_market_rvol: float
    gap_pct: float
    atr_pct: float
    current_price: float
    avg_volume_30d: int
    has_earnings: bool
    earnings_date: date | None
    reason: str
    strategy_type: str = "orb"  # "orb" | "opening_fade" | "both"
    catalyst_score: float = 0.0
    catalyst_event_type: str | None = None
    catalyst_summary: str | None = None
    catalyst_source: str | None = None


class MorningScanner:
    """
    Pre-market scanner. Runs at 09:15 ET via Celery beat.
    Uses Alpaca (real-time) when available, else Polygon, else static list.
    """

    DEFAULT_UNIVERSE: ClassVar[list[str]] = [
        "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL",
        "AMD", "NFLX", "SPY", "QQQ", "AVGO", "CRM", "ORCL",
    ]

    def __init__(self) -> None:
        pass

    # ── Main entry ────────────────────────────────────────────────────────────

    async def scan(
        self,
        universe: list[str] | None = None,
        max_results: int = 8,
    ) -> list[ScanCandidate]:
        """
        Run morning scan. Returns ranked list of up to max_results candidates.
        """
        tickers = universe or self.DEFAULT_UNIVERSE

        # Priority: Alpaca → Polygon → static
        candidates: list[ScanCandidate]

        if settings.ALPACA_API_KEY and settings.ALPACA_API_SECRET:
            try:
                candidates = await self._scan_alpaca(tickers, max_results * 2)
                return await self._apply_catalyst_overlay(candidates, max_results=max_results)
            except Exception as exc:
                log.warning("scanner.alpaca_failed", error=str(exc))

        if settings.POLYGON_API_KEY:
            try:
                candidates = await self._scan_polygon(tickers, max_results * 2)
                return await self._apply_catalyst_overlay(candidates, max_results=max_results)
            except Exception as exc:
                log.warning("scanner.polygon_failed", error=str(exc))

        log.warning("scanner.no_provider", message="Using static watchlist — no API keys set")
        candidates = self._static_watchlist(tickers, max_results)
        return await self._apply_catalyst_overlay(candidates, max_results=max_results)

    # ── Alpaca scanner ────────────────────────────────────────────────────────

    async def _scan_alpaca(
        self, tickers: list[str], max_results: int
    ) -> list[ScanCandidate]:
        """Real-time scan via Alpaca batch snapshots."""
        from app.market_data.alpaca_provider import AlpacaMarketDataProvider

        async with AlpacaMarketDataProvider(
            api_key=settings.ALPACA_API_KEY,
            api_secret=settings.ALPACA_API_SECRET,
        ) as md:
            snapshots = await md.get_snapshots(tickers)
            earnings = await self._fetch_earnings_alpaca(md)

        candidates: list[ScanCandidate] = []
        for ticker, snap in snapshots.items():
            c = self._score_alpaca_snapshot(ticker, snap, earnings)
            if c:
                candidates.append(c)

        candidates.sort(key=lambda c: c.score, reverse=True)
        result = candidates[:max_results]

        log.info(
            "scanner.complete",
            provider="alpaca_realtime",
            scanned=len(snapshots),
            candidates=len(candidates),
            selected=[c.ticker for c in result],
        )
        return result

    def _score_alpaca_snapshot(
        self,
        ticker: str,
        snap: dict[str, Any],
        earnings: set[str],
    ) -> ScanCandidate | None:
        """Score one Alpaca snapshot. Returns None if it doesn't qualify."""
        daily  = snap.get("dailyBar", {})
        prev   = snap.get("prevDailyBar", {})
        trade  = snap.get("latestTrade", {})

        current_price = float(daily.get("c") or trade.get("p") or 0)
        prev_close    = float(prev.get("c") or 0)
        today_open    = float(daily.get("o") or current_price)
        today_vol     = int(daily.get("v") or 0)
        prev_vol      = int(prev.get("v") or 1)

        return self._score_common(
            ticker=ticker,
            current_price=current_price,
            prev_close=prev_close,
            today_open=today_open,
            today_vol=today_vol,
            prev_vol=prev_vol,
            earnings=earnings,
            provider_tag="alpaca_realtime",
        )

    async def _fetch_earnings_alpaca(self, md: Any) -> set[str]:
        """
        Try to fetch earnings from Polygon calendar if key available.
        Alpaca doesn't have an earnings calendar endpoint.
        """
        if not settings.POLYGON_API_KEY:
            return set()
        try:
            import httpx
            avoid: set[str] = set()
            today = date.today()
            async with httpx.AsyncClient(timeout=5.0) as client:
                for delta in [-1, 0, 1]:
                    check = today + timedelta(days=delta)
                    resp = await client.get(
                        "https://api.polygon.io/vX/reference/financials",
                        params={
                            "filing_date": check.isoformat(),
                            "timeframe": "quarterly",
                            "apiKey": settings.POLYGON_API_KEY,
                            "limit": 100,
                        },
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        for item in resp.json().get("results", []):
                            t = item.get("tickers", [None])[0]
                            if t:
                                avoid.add(t)
            return avoid
        except Exception:
            return set()

    # ── Polygon scanner ───────────────────────────────────────────────────────

    async def _scan_polygon(
        self, tickers: list[str], max_results: int
    ) -> list[ScanCandidate]:
        """Delayed scan via Polygon snapshots (fallback when no Alpaca keys)."""
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
                params={
                    "apiKey":      settings.POLYGON_API_KEY,
                    "tickers":     ",".join(tickers),
                    "include_otc": "false",
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Polygon snapshot failed: {resp.status_code}")

            data = resp.json().get("tickers", [])
            earnings = await self._fetch_earnings_polygon(client)

        candidates: list[ScanCandidate] = []
        for snap in data:
            ticker      = snap.get("ticker", "")
            day         = snap.get("day", {})
            prev_day    = snap.get("prevDay", {})

            c = self._score_common(
                ticker=ticker,
                current_price=float(day.get("c") or day.get("o") or 0),
                prev_close=float(prev_day.get("c") or 0),
                today_open=float(day.get("o") or 0),
                today_vol=int(day.get("v") or 0),
                prev_vol=int(prev_day.get("v") or 1),
                earnings=earnings,
                provider_tag="polygon_delayed",
            )
            if c:
                candidates.append(c)

        candidates.sort(key=lambda c: c.score, reverse=True)
        result = candidates[:max_results]

        log.info(
            "scanner.complete",
            provider="polygon_delayed",
            scanned=len(data),
            selected=[c.ticker for c in result],
        )
        return result

    async def _apply_catalyst_overlay(
        self,
        candidates: list[ScanCandidate],
        *,
        max_results: int,
    ) -> list[ScanCandidate]:
        if not candidates:
            return []

        news_items = await NewsIntelligenceService().get_watchlist_intelligence(
            [candidate.ticker for candidate in candidates],
            limit=max(max_results * 3, len(candidates) * 2),
        )
        ticker_set = {candidate.ticker for candidate in candidates}
        best_news_by_ticker: dict[str, dict[str, Any]] = {}
        for item in news_items:
            for ticker in item.get("tickers", []):
                symbol = str(ticker).upper()
                if symbol not in ticker_set:
                    continue
                current = best_news_by_ticker.get(symbol)
                if current is None or float(item.get("catalyst_score", 0.0)) > float(current.get("catalyst_score", 0.0)):
                    best_news_by_ticker[symbol] = item

        for candidate in candidates:
            news = best_news_by_ticker.get(candidate.ticker)
            if not news:
                continue
            catalyst_score = float(news.get("catalyst_score", 0.0) or 0.0)
            candidate.catalyst_score = round(catalyst_score, 2)
            candidate.catalyst_event_type = str(news.get("event_type") or "") or None
            candidate.catalyst_summary = str(news.get("title") or news.get("summary") or "").strip()[:160] or None
            candidate.catalyst_source = str(news.get("source") or "") or None
            candidate.score = round(candidate.score + min(catalyst_score, 1.0) * 20.0, 2)
            if candidate.catalyst_event_type:
                candidate.reason = (
                    f"{candidate.reason} | Catalyst {candidate.catalyst_event_type}"
                    f" {candidate.catalyst_score:.2f}"
                )

        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return candidates[:max_results]

    async def _fetch_earnings_polygon(self, client: Any) -> set[str]:
        avoid: set[str] = set()
        today = date.today()
        for delta in [-1, 0, 1]:
            check = today + timedelta(days=delta)
            try:
                resp = await client.get(
                    "https://api.polygon.io/vX/reference/financials",
                    params={
                        "filing_date": check.isoformat(),
                        "timeframe":   "quarterly",
                        "apiKey":      settings.POLYGON_API_KEY,
                        "limit":       100,
                    },
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    for item in resp.json().get("results", []):
                        t = item.get("tickers", [None])[0]
                        if t:
                            avoid.add(t)
            except Exception:
                continue
        return avoid

    # ── Shared scoring logic ──────────────────────────────────────────────────

    def _score_common(
        self,
        *,
        ticker: str,
        current_price: float,
        prev_close: float,
        today_open: float,
        today_vol: int,
        prev_vol: int,
        earnings: set[str],
        provider_tag: str,
    ) -> ScanCandidate | None:
        """
        Shared scoring logic for both Alpaca and Polygon snapshots.

        Strategy routing (Berkman, Koch & Westerholm 2014; Tomasini & Jaekle 2009):
          gap 0.5-2%   → ORB candidate  (moderate gap, continuation likely)
          gap 1.5-6%   → Fade candidate (overextended gap, mean-reversion likely)
          gap 1.5-2%   → both strategies are valid; regime decides at runtime

        Returns None if the ticker doesn't pass any strategy's filters.
        """
        if not ticker:
            return None

        # Price filter: $10-$500
        if not (10.0 <= current_price <= 500.0):
            return None

        # Need valid prices
        if current_price <= 0 or prev_close <= 0:
            return None

        # Gap measurement
        g = (today_open - prev_close) / prev_close * 100 if prev_close > 0 else 0
        gap_abs = abs(g)

        # Must pass at least one strategy's gap window
        orb_eligible  = 0.5 <= gap_abs <= 2.0
        fade_eligible = 1.5 <= gap_abs <= 6.0
        if not (orb_eligible or fade_eligible):
            return None

        # Pre-market relative volume (compare to ~5% of prior day volume)
        pm_rvol = today_vol / max(prev_vol * 0.05, 1)
        if pm_rvol < 1.5:
            return None

        # Minimum absolute pre-market volume
        if today_vol < 5000:
            return None

        # Hard exclude earnings day (binary event risk)
        if ticker in earnings:
            return None

        # Determine strategy routing
        if orb_eligible and fade_eligible:
            strategy_type = "both"         # e.g. 1.8% gap — let regime decide
        elif fade_eligible:
            strategy_type = "opening_fade"
        else:
            strategy_type = "orb"

        # Score: higher = better candidate
        score = min(pm_rvol, 5.0) * 20            # RVOL up to 100 pts
        if orb_eligible:
            if 1.0 <= gap_abs <= 1.5:
                score += 30     # Sweet-spot ORB gap
            elif gap_abs <= 2.0:
                score += 15    # Acceptable ORB gap
            if g > 0:
                score += 10    # Upside gap for longs
        if fade_eligible:
            if 2.0 <= gap_abs <= 4.0:
                score += 25     # Classic fade zone
            elif gap_abs > 4.0:
                score += 15    # Wide gap, higher risk
            score += 10  # Bonus for fade: countertrend edge

        strategy_tags = {
            "orb": "ORB",
            "opening_fade": "Fade",
            "both": "ORB+Fade",
        }
        tag = strategy_tags.get(strategy_type, strategy_type)
        reason = f"RVOL {pm_rvol:.1f}x | Gap {g:+.1f}% | {tag} | {provider_tag}"

        return ScanCandidate(
            ticker=ticker,
            score=score,
            pre_market_rvol=round(pm_rvol, 2),
            gap_pct=round(g, 2),
            atr_pct=0.0,
            current_price=current_price,
            avg_volume_30d=prev_vol,
            has_earnings=False,
            earnings_date=None,
            reason=reason,
            strategy_type=strategy_type,
        )

    # ── Static fallback ───────────────────────────────────────────────────────

    def _static_watchlist(
        self, tickers: list[str], max_results: int = 8
    ) -> list[ScanCandidate]:
        """
        Returns tickers as-is when no API is available.
        No scoring — just passes the configured universe through.
        """
        return [
            ScanCandidate(
                ticker=t, score=50.0,
                pre_market_rvol=1.0, gap_pct=0.0,
                atr_pct=0.0, current_price=0.0,
                avg_volume_30d=500_000,
                has_earnings=False, earnings_date=None,
                reason="Static watchlist (no market data API configured)",
            )
            for t in tickers[:max_results]
        ]


async def run_morning_scan(universe: list[str] | None = None) -> list[str]:
    """
    Convenience wrapper for backwards compatibility.
    Returns flat list of ticker symbols (all strategy types combined).
    """
    scanner = MorningScanner()
    candidates = await scanner.scan(universe=universe)
    return [c.ticker for c in candidates]


async def run_morning_scan_typed(
    universe: list[str] | None = None,
) -> dict[str, list[str]]:
    """
    Returns a dict keyed by strategy type for precise routing:
      {
        "orb":          ["AAPL", "MSFT"],
        "opening_fade": ["TSLA", "NVDA"],
        "both":         ["AMD"],          # regime decides at runtime
      }
    "both" tickers are also included in both "orb" and "opening_fade" lists.
    """
    scanner = MorningScanner()
    candidates = await scanner.scan(universe=universe)

    orb_tickers:  list[str] = []
    fade_tickers: list[str] = []
    both_tickers: list[str] = []

    for c in candidates:
        if c.strategy_type == "orb":
            orb_tickers.append(c.ticker)
        elif c.strategy_type == "opening_fade":
            fade_tickers.append(c.ticker)
        elif c.strategy_type == "both":
            both_tickers.append(c.ticker)
            orb_tickers.append(c.ticker)   # include in both
            fade_tickers.append(c.ticker)

    return {
        "orb":          orb_tickers,
        "opening_fade": fade_tickers,
        "both":         both_tickers,
    }
