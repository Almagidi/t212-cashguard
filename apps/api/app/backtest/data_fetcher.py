"""
Backtest data fetcher.
Downloads historical OHLCV bars from Polygon.io for backtesting.
Caches locally to avoid repeated API calls.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from app.strategies.indicators import Bar

log = structlog.get_logger()

CACHE_DIR = Path("/tmp/cashguard_backtest_cache")
CACHE_DIR.mkdir(exist_ok=True)


class BacktestDataFetcher:
    """
    Fetches historical 5-minute bars from Polygon for backtesting.
    Caches responses to disk so subsequent runs are instant.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def fetch_bars(
        self,
        ticker: str,
        from_date: date,
        to_date: date,
        multiplier: int = 5,
        timespan: str = "minute",
    ) -> tuple[list[Bar], list[datetime]]:
        """
        Returns (bars, bar_times) for the requested period.
        Uses disk cache if available.
        """
        cache_key = f"{ticker}_{multiplier}{timespan}_{from_date}_{to_date}"
        cache_file = CACHE_DIR / f"{cache_key}.json"

        if cache_file.exists():
            log.info("backtest_data.cache_hit", ticker=ticker)
            data = json.loads(cache_file.read_text())
            return self._parse_cached(data)

        log.info("backtest_data.fetching", ticker=ticker,
                 from_date=from_date.isoformat(), to_date=to_date.isoformat())

        import httpx
        all_results = []
        current_from = from_date

        # Polygon returns max 50,000 results per call — paginate over date ranges
        while current_from <= to_date:
            chunk_to = min(current_from + timedelta(days=30), to_date)
            url = (
                f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range"
                f"/{multiplier}/{timespan}"
                f"/{current_from.isoformat()}/{chunk_to.isoformat()}"
            )
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params={
                    "adjusted": "true",
                    "sort": "asc",
                    "limit": 50000,
                    "apiKey": self.api_key,
                })
                if resp.status_code == 200:
                    data = resp.json()
                    all_results.extend(data.get("results", []))
                elif resp.status_code == 403:
                    raise ValueError("Polygon API key invalid or insufficient permissions")
                else:
                    log.warning("backtest_data.fetch_error",
                                status=resp.status_code, ticker=ticker)

            current_from = chunk_to + timedelta(days=1)

        # Cache results
        cache_file.write_text(json.dumps(all_results))
        log.info("backtest_data.fetched", ticker=ticker, bars=len(all_results))

        return self._parse_raw(all_results)

    def _parse_raw(self, results: list[dict[str, Any]]) -> tuple[list[Bar], list[datetime]]:
        bars, times = [], []
        for r in results:
            bars.append(Bar(
                open=Decimal(str(r["o"])),
                high=Decimal(str(r["h"])),
                low=Decimal(str(r["l"])),
                close=Decimal(str(r["c"])),
                volume=Decimal(str(r.get("v", 0))),
            ))
            times.append(datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc))
        return bars, times

    def _parse_cached(self, results: list[dict[str, Any]]) -> tuple[list[Bar], list[datetime]]:
        return self._parse_raw(results)

    def clear_cache(self, ticker: str | None = None) -> int:
        """Clear cached bar data. Returns number of files deleted."""
        count = 0
        for f in CACHE_DIR.iterdir():
            if ticker is None or f.name.startswith(ticker):
                f.unlink()
                count += 1
        return count


# ── Parameter grid for walk-forward optimisation ─────────────────────────────

ORB_PARAM_GRID = [
    {
        "orb_minutes": 15,
        "min_rvol": 1.5,
        "atr_stop_multiplier": 2.0,
        "atr_trail_multiplier": 2.5,
        "risk_per_trade_pct": 0.75,
        "reward_risk_ratio_min": 1.5,
    },
    {
        "orb_minutes": 15,
        "min_rvol": 2.0,
        "atr_stop_multiplier": 1.5,
        "atr_trail_multiplier": 2.0,
        "risk_per_trade_pct": 1.0,
        "reward_risk_ratio_min": 2.0,
    },
    {
        "orb_minutes": 30,
        "min_rvol": 1.5,
        "atr_stop_multiplier": 2.0,
        "atr_trail_multiplier": 3.0,
        "risk_per_trade_pct": 0.75,
        "reward_risk_ratio_min": 1.5,
    },
    {
        "orb_minutes": 15,
        "min_rvol": 1.8,
        "atr_stop_multiplier": 2.5,
        "atr_trail_multiplier": 3.0,
        "risk_per_trade_pct": 0.5,
        "reward_risk_ratio_min": 2.0,
    },
    {
        "orb_minutes": 30,
        "min_rvol": 2.0,
        "atr_stop_multiplier": 2.0,
        "atr_trail_multiplier": 2.5,
        "risk_per_trade_pct": 1.0,
        "reward_risk_ratio_min": 1.5,
    },
]
