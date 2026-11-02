"""Alpaca get_bars newest-N semantics, cache keying, and 429 handling (fake transport only)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.market_data.alpaca_provider import AlpacaMarketDataProvider


def _bar(ts: datetime, close: str) -> dict[str, Any]:
    return {
        "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": close, "h": close, "l": close, "c": close, "v": 10,
    }


class FakeAlpaca:
    """Emulates Alpaca's bars endpoint: honours sort + limit over a fixed series."""

    def __init__(self, series: list[dict[str, Any]]) -> None:
        self.series = series  # ascending
        self.requests: list[httpx.Request] = []
        self.page_cap = 10_000  # server-side max rows per page

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        q = request.url.params
        rows = [r for r in self.series if q["start"] <= r["t"] <= q["end"]]
        rows = rows if q["sort"] == "asc" else list(reversed(rows))
        offset = int(q["page_token"]) if "page_token" in q else 0
        size = min(int(q["limit"]), self.page_cap)
        page = rows[offset: offset + size]
        body: dict[str, Any] = {"bars": page}
        if offset + size < len(rows):
            body["next_page_token"] = str(offset + size)
        return httpx.Response(200, json=body)


def _provider(handler: Any) -> AlpacaMarketDataProvider:
    provider = AlpacaMarketDataProvider(api_key="test-key", api_secret="test-secret")
    provider._client = httpx.AsyncClient(
        base_url=provider.DATA_BASE_URL, transport=httpx.MockTransport(handler)
    )
    return provider


def _recent_series(count: int) -> list[dict[str, Any]]:
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    return [
        _bar(end - timedelta(minutes=5 * (count - 1 - i)), str(100 + i))
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_default_request_returns_latest_n_bars_in_ascending_order() -> None:
    series = _recent_series(200)
    fake = FakeAlpaca(series)
    provider = _provider(fake)

    bars = await provider.get_bars("AAPL", multiplier=5, timespan="minute", limit=50)

    assert [b.close for b in bars] == [Decimal(100 + i) for i in range(150, 200)]
    assert all(a.timestamp < b.timestamp for a, b in zip(bars, bars[1:], strict=False))
    assert fake.requests[0].url.params["sort"] == "desc"
    assert isinstance(bars[0].close, Decimal)


@pytest.mark.asyncio
async def test_explicit_window_returns_newest_limit_bars_ascending() -> None:
    fake = FakeAlpaca(_recent_series(250))
    provider = _provider(fake)
    today = datetime.now(UTC).date()

    bars = await provider.get_bars(
        "AAPL", multiplier=5, timespan="minute",
        from_date=today - timedelta(days=3), to_date=today, limit=50,
    )

    assert fake.requests[0].url.params["sort"] == "desc"
    assert fake.requests[0].url.params["start"] == (today - timedelta(days=3)).isoformat() + "T00:00:00Z"
    assert [b.close for b in bars] == [Decimal(100 + i) for i in range(200, 250)]
    assert all(a.timestamp < b.timestamp for a, b in zip(bars, bars[1:], strict=False))


@pytest.mark.asyncio
async def test_explicit_window_follows_pagination_across_three_pages() -> None:
    fake = FakeAlpaca(_recent_series(250))
    fake.page_cap = 100
    provider = _provider(fake)
    today = datetime.now(UTC).date()

    bars = await provider.get_bars(
        "AAPL", multiplier=5, timespan="minute",
        from_date=today - timedelta(days=3), to_date=today, limit=250,
    )

    assert len(fake.requests) == 3
    assert fake.requests[1].url.params["page_token"] == "100"
    assert [b.close for b in bars] == [Decimal(100 + i) for i in range(250)]
    assert all(a.timestamp < b.timestamp for a, b in zip(bars, bars[1:], strict=False))


@pytest.mark.asyncio
async def test_pagination_stops_once_limit_reached() -> None:
    fake = FakeAlpaca(_recent_series(250))
    fake.page_cap = 30
    provider = _provider(fake)
    today = datetime.now(UTC).date()

    bars = await provider.get_bars(
        "AAPL", multiplier=5, timespan="minute",
        from_date=today - timedelta(days=3), to_date=today, limit=50,
    )

    assert len(fake.requests) == 2
    assert [b.close for b in bars] == [Decimal(100 + i) for i in range(200, 250)]


@pytest.mark.asyncio
async def test_limit_larger_than_available_returns_all_ascending() -> None:
    fake = FakeAlpaca(_recent_series(40))
    provider = _provider(fake)
    today = datetime.now(UTC).date()

    bars = await provider.get_bars(
        "AAPL", multiplier=5, timespan="minute",
        from_date=today - timedelta(days=3), to_date=today, limit=500,
    )

    assert [b.close for b in bars] == [Decimal(100 + i) for i in range(40)]
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_empty_window_returns_empty_list() -> None:
    fake = FakeAlpaca([])
    provider = _provider(fake)
    today = datetime.now(UTC).date()

    bars = await provider.get_bars("AAPL", from_date=today, to_date=today, limit=20)

    assert bars == []
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_daily_default_window_is_wide_enough_for_regime_history() -> None:
    fake = FakeAlpaca([])
    provider = _provider(fake)

    await provider.get_bars("SPY", multiplier=1, timespan="day", limit=90)

    params = fake.requests[0].url.params
    start = date.fromisoformat(params["start"][:10])
    window_days = (datetime.now(UTC).date() - start).days
    assert window_days >= 90 * 7 // 5  # >= 90 trading days of calendar room
    assert params["sort"] == "desc"
    assert params["limit"] == "90"


@pytest.mark.asyncio
async def test_cache_key_separates_limit_and_dates() -> None:
    fake = FakeAlpaca(_recent_series(100))
    provider = _provider(fake)
    today = datetime.now(UTC).date()

    small = await provider.get_bars("AAPL", limit=5)
    large = await provider.get_bars("AAPL", limit=20)
    dated = await provider.get_bars("AAPL", from_date=today, to_date=today, limit=20)
    again = await provider.get_bars("AAPL", limit=5)

    assert len(small) == 5 and len(large) == 20 and len(dated) <= 20
    assert again is small  # identical request is served from cache
    assert len(fake.requests) == 3

