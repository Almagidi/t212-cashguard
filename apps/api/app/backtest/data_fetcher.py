"""
Backtest data fetcher.
Downloads historical OHLCV bars from Polygon.io for backtesting.
Caches locally to avoid repeated API calls.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from app.backtest.data_contract import validate_bar_series
from app.backtest.dataset_cache import (
    DatasetHandle,
    DatasetRequest,
    ImmutableDatasetCache,
)
from app.strategies.indicators import Bar

log = structlog.get_logger()


def _code_sha() -> str:
    return (
        os.environ.get("T212_CODE_SHA")
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )


CACHE_DIR = Path("/tmp/cashguard_backtest_cache")
CACHE_DIR.mkdir(exist_ok=True)


class BacktestDataFetcher:
    """
    Fetches historical 5-minute bars from Polygon for backtesting.
    Caches responses to disk so subsequent runs are instant.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._manifests: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _request(
        ticker: str,
        from_date: date,
        to_date: date,
        multiplier: int,
        timespan: str,
        membership_source: str,
        universe: tuple[str, ...] | None,
    ) -> DatasetRequest:
        return DatasetRequest(
            provider="polygon",
            source_id="polygon_aggregate_bars",
            source_version="v2",
            symbols=(ticker,),
            start=from_date,
            end=to_date,
            multiplier=multiplier,
            timespan=timespan,
            venue="XNYS",
            timezone="America/New_York",
            adjustment_mode="provider_adjusted",
            corporate_action_policy="provider_adjusted_unqualified",
            membership_source=membership_source,
            universe=universe,
        )

    def manifest_for(self, ticker: str) -> dict[str, Any]:
        try:
            return self._manifests[ticker.strip().upper()]
        except KeyError as exc:
            raise RuntimeError(f"No immutable dataset manifest for {ticker}") from exc

    async def fetch_bars(
        self,
        ticker: str,
        from_date: date,
        to_date: date,
        multiplier: int = 5,
        timespan: str = "minute",
        membership_source: str = "single_symbol_request",
        universe: tuple[str, ...] | None = None,
    ) -> tuple[list[Bar], list[datetime]]:
        """
        Returns (bars, bar_times) for the requested period.
        Uses disk cache if available.
        """
        request = self._request(
            ticker,
            from_date,
            to_date,
            multiplier,
            timespan,
            membership_source,
            universe,
        )
        cache = ImmutableDatasetCache(CACHE_DIR)
        cached = cache.load(request) if cache.reference_path(request).exists() else None
        if cached is not None:
            log.info("backtest_data.cache_hit", ticker=ticker)
            self._record_manifest(ticker, cached)
            return cached.bars, cached.times

        log.info(
            "backtest_data.fetching",
            ticker=ticker,
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
        )

        import httpx

        all_results: list[dict[str, Any]] = []
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
                resp = await client.get(
                    url,
                    params={
                        "adjusted": "true",
                        "sort": "asc",
                        "limit": 50000,
                        "apiKey": self.api_key,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ERROR":
                        raise RuntimeError(
                            f"Polygon request failed for {ticker}: "
                            f"{data.get('error') or data.get('message') or 'unknown provider error'}"
                        )
                    results = data.get("results", [])
                    if not isinstance(results, list):
                        raise RuntimeError(
                            f"Polygon returned an invalid results payload for {ticker}"
                        )
                    if data.get("next_url"):
                        raise RuntimeError(f"Polygon returned a partial provider page for {ticker}")
                    result_count = data.get("resultsCount")
                    if result_count is not None and result_count != len(results):
                        raise RuntimeError(f"Polygon result count mismatch for {ticker}")
                    all_results.extend(results)
                elif resp.status_code == 403:
                    raise ValueError("Polygon API key invalid or insufficient permissions")
                else:
                    raise RuntimeError(
                        f"Polygon request failed for {ticker} with HTTP {resp.status_code}"
                    )

            current_from = chunk_to + timedelta(days=1)

        handle = cache.publish(
            request,
            all_results,
            retrieved_at=datetime.now(UTC),
            code_sha=_code_sha(),
        )
        self._record_manifest(ticker, handle)
        log.info("backtest_data.fetched", ticker=ticker, bars=len(all_results))
        return handle.bars, handle.times

    def _record_manifest(self, ticker: str, handle: DatasetHandle) -> None:
        self._manifests[ticker.strip().upper()] = {
            "manifest_id": handle.manifest_id,
            **handle.manifest,
        }

    def _parse_raw(self, results: list[dict[str, Any]]) -> tuple[list[Bar], list[datetime]]:
        bars, times = [], []
        for r in results:
            bars.append(
                Bar(
                    open=Decimal(str(r["o"])),
                    high=Decimal(str(r["h"])),
                    low=Decimal(str(r["l"])),
                    close=Decimal(str(r["c"])),
                    volume=Decimal(str(r.get("v", 0))),
                )
            )
            times.append(datetime.fromtimestamp(r["t"] / 1000, tz=UTC))
        validate_bar_series(bars, times, label="Polygon bar series")
        return bars, times

    def clear_cache(self, ticker: str | None = None) -> int:
        """Clear immutable research-cache files. Ticker filtering is unsupported."""
        if ticker is not None:
            raise ValueError("content-addressed cache clearing cannot filter by ticker")
        count = 0
        for f in CACHE_DIR.rglob("*.json"):
            if f.is_file():
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
