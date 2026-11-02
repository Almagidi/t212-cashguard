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



# ── opening range: start-anchored ascending fetch ───────────────────────────

def _session_series(day: date, *, one_minute: bool) -> list[dict[str, Any]]:
    """Full regular session 09:30-16:00 ET (DST-correct) as raw bars."""
    from zoneinfo import ZoneInfo

    open_utc = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
    step, n = (1, 390) if one_minute else (5, 78)
    return [_bar(open_utc + timedelta(minutes=step * i), str(100 + i)) for i in range(n)]


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [date(2026, 7, 14), date(2026, 1, 14)])  # EDT and EST
async def test_opening_range_bars_returned_even_when_requested_late_in_day(day: date) -> None:
    fake = FakeAlpaca(_session_series(day, one_minute=True))
    provider = _provider(fake)

    bars = await provider.get_opening_range_bars("AAPL", session_date=day, orb_minutes=15)

    assert len(bars) == 15
    assert [b.close for b in bars] == [Decimal(100 + i) for i in range(15)]
    assert all(a.timestamp < b.timestamp for a, b in zip(bars, bars[1:], strict=False))
    assert fake.requests[0].url.params["sort"] == "asc"


@pytest.mark.asyncio
async def test_opening_range_with_five_minute_series_returns_only_window_bars() -> None:
    day = date(2026, 7, 14)
    fake = FakeAlpaca(_session_series(day, one_minute=False))
    provider = _provider(fake)

    bars = await provider.get_opening_range_bars("AAPL", session_date=day, orb_minutes=15)

    # 09:30, 09:35, 09:40 inside the window; 09:45 is excluded (end-exclusive)
    assert [b.close for b in bars] == [Decimal(100), Decimal(101), Decimal(102)]


@pytest.mark.asyncio
async def test_opening_range_survives_premarket_bars_before_the_open() -> None:
    day = date(2026, 7, 14)
    series = _session_series(day, one_minute=True)
    first = datetime.fromisoformat(series[0]["t"].replace("Z", "+00:00"))
    pre = [_bar(first - timedelta(minutes=m), "1") for m in range(60, 0, -1)]
    provider = _provider(FakeAlpaca(pre + series))

    bars = await provider.get_opening_range_bars("AAPL", session_date=day, orb_minutes=15)

    assert [b.close for b in bars] == [Decimal(100 + i) for i in range(15)]


@pytest.mark.asyncio
async def test_asc_sort_is_start_anchored_and_cache_key_distinct() -> None:
    fake = FakeAlpaca(_recent_series(100))
    provider = _provider(fake)
    today = datetime.now(UTC).date()
    kw: dict[str, Any] = dict(from_date=today - timedelta(days=3), to_date=today, limit=5)

    newest = await provider.get_bars("AAPL", **kw)
    oldest = await provider.get_bars("AAPL", sort="asc", **kw)

    assert [b.close for b in newest] == [Decimal(100 + i) for i in range(95, 100)]
    assert [b.close for b in oldest] == [Decimal(100 + i) for i in range(5)]
    assert len(fake.requests) == 2
    with pytest.raises(ValueError):
        await provider.get_bars("AAPL", sort="sideways")


@pytest.mark.asyncio
async def test_pagination_stops_at_max_pages_and_logs_warning() -> None:
    from structlog.testing import capture_logs

    fake = FakeAlpaca(_recent_series(250))
    fake.page_cap = 10
    provider = _provider(fake)
    provider.MAX_PAGES = 3
    today = datetime.now(UTC).date()

    with capture_logs() as logs:
        bars = await provider.get_bars(
            "AAPL", from_date=today - timedelta(days=3), to_date=today, limit=100,
        )

    assert len(fake.requests) == 3 and len(bars) == 30
    warnings = [e for e in logs if e["event"] == "alpaca.bars_pagination_truncated"]
    assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"


@pytest.mark.asyncio
async def test_no_truncation_warning_when_limit_reached_on_last_page() -> None:
    from structlog.testing import capture_logs

    fake = FakeAlpaca(_recent_series(250))
    fake.page_cap = 10
    provider = _provider(fake)
    provider.MAX_PAGES = 3
    today = datetime.now(UTC).date()

    with capture_logs() as logs:
        await provider.get_bars("AAPL", from_date=today - timedelta(days=3), to_date=today, limit=30)

    assert not [e for e in logs if e["event"] == "alpaca.bars_pagination_truncated"]
