"""Alpaca 429 retry/backoff handling (fake transport, no real sleeping)."""
from __future__ import annotations

import httpx
import pytest

from app.market_data.alpaca_provider import AlpacaError, AlpacaMarketDataProvider


def _provider(handler):
    provider = AlpacaMarketDataProvider(api_key="test-key", api_secret="test-secret")
    provider._client = httpx.AsyncClient(
        base_url=provider.DATA_BASE_URL, transport=httpx.MockTransport(handler)
    )
    provider._random = lambda: 1.0  # full computed backoff (no jitter reduction)
    return provider



class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _rate_limited_then_ok(statuses: list[int], retry_after: str | None = None):
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[min(seen["n"], len(statuses) - 1)]
        seen["n"] += 1
        headers = {"Retry-After": retry_after} if retry_after and status == 429 else {}
        if status == 200:
            return httpx.Response(200, json={"bars": []})
        return httpx.Response(status, headers=headers, json={"message": "slow down"})

    return handler, seen


@pytest.mark.asyncio
async def test_429_retries_with_exponential_backoff_then_succeeds() -> None:
    handler, seen = _rate_limited_then_ok([429, 429, 200])
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps

    assert await provider._get("/v2/stocks/AAPL/bars") == {"bars": []}
    assert seen["n"] == 3
    assert sleeps.calls == [pytest.approx(1.0), pytest.approx(2.0)]


@pytest.mark.asyncio
async def test_429_honours_retry_after_header() -> None:
    handler, seen = _rate_limited_then_ok([429, 200], retry_after="7")
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps

    await provider._get("/v2/stocks/AAPL/bars")

    assert sleeps.calls == [7.0]
    assert seen["n"] == 2


@pytest.mark.asyncio
async def test_429_retry_after_beyond_the_cap_raises_without_sleeping() -> None:
    handler, seen = _rate_limited_then_ok([429, 200], retry_after="3600")
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps

    with pytest.raises(AlpacaError, match="429"):
        await provider._get("/v2/stocks/AAPL/bars")

    assert sleeps.calls == [] and seen["n"] == 1


@pytest.mark.asyncio
async def test_429_gives_up_after_three_attempts_with_alpaca_error() -> None:
    handler, seen = _rate_limited_then_ok([429])
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps

    with pytest.raises(AlpacaError, match="429"):
        await provider._get("/v2/stocks/AAPL/bars")

    assert seen["n"] == 3
    assert len(sleeps.calls) == 2  # no sleep after the final attempt


@pytest.mark.asyncio
async def test_non_429_errors_are_not_retried() -> None:
    handler, seen = _rate_limited_then_ok([403])
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps

    with pytest.raises(AlpacaError):
        await provider._get("/v2/stocks/AAPL/bars")

    assert seen["n"] == 1 and sleeps.calls == []


@pytest.mark.asyncio
async def test_backoff_has_equal_jitter_with_injected_random() -> None:
    handler, _ = _rate_limited_then_ok([429, 429, 200])
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps
    provider._random = lambda: 0.0  # lowest jitter: half of the base delay

    await provider._get("/v2/stocks/AAPL/bars")

    assert sleeps.calls == [0.5, 1.0]


@pytest.mark.asyncio
async def test_total_sleep_cap_raises_on_large_retry_after() -> None:
    handler, seen = _rate_limited_then_ok([429], retry_after="3600")
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps
    provider.MAX_TOTAL_SLEEP_SECONDS = 40.0

    with pytest.raises(AlpacaError):
        await provider._get("/v2/stocks/AAPL/bars")

    # Retry-After 3600 s fits neither the per-sleep cap nor the budget: raise,
    # do not ignore it and retry early.
    assert seen["n"] == 1
    assert sleeps.calls == []


@pytest.mark.asyncio
async def test_exit_path_sleep_cap_limits_total_stall_but_default_keeps_45s() -> None:
    handler, seen = _rate_limited_then_ok([429, 429, 429], retry_after="30")
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps

    with pytest.raises(AlpacaError):
        await provider._get("/v2/stocks/AAPL/bars", max_total_sleep=10.0)
    assert sleeps.calls == []  # 30 s Retry-After exceeds the 10 s budget: raise at once

    handler2, _ = _rate_limited_then_ok([429, 429, 429], retry_after="30")
    default = _provider(handler2)
    sleeps2 = _Sleeps()
    default._sleep = sleeps2
    with pytest.raises(AlpacaError):
        await default._get("/v2/stocks/AAPL/bars")
    # non-exit callers keep the 45 s budget: 30 s fits, the next 30 s exceeds the
    # 15 s left, so it raises after 2 requests rather than retrying early.
    assert sleeps2.calls == [30.0]


@pytest.mark.asyncio
async def test_exit_cap_cannot_raise_the_provider_budget() -> None:
    handler, _ = _rate_limited_then_ok([429, 429, 429], retry_after="30")
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps
    provider.MAX_TOTAL_SLEEP_SECONDS = 5.0

    with pytest.raises(AlpacaError):
        await provider._get("/v2/stocks/AAPL/bars", max_total_sleep=100.0)
    assert sum(sleeps.calls) <= 5.0


@pytest.mark.asyncio
async def test_get_bars_forwards_max_total_sleep() -> None:
    handler, _ = _rate_limited_then_ok([429, 429, 429], retry_after="30")
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps

    with pytest.raises(AlpacaError):
        await provider.get_bars("AAPL", limit=5, max_total_sleep=10.0)
    assert sum(sleeps.calls) <= 10.0


@pytest.mark.asyncio
async def test_exhausted_budget_stops_retrying_instead_of_zero_sleep_loop() -> None:
    handler, seen = _rate_limited_then_ok([429, 429, 429, 429])
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps
    provider.MAX_TOTAL_SLEEP_SECONDS = 1.0  # first backoff (1 s) uses it all

    with pytest.raises(AlpacaError, match="429"):
        await provider._get("/v2/stocks/AAPL/bars")

    assert sleeps.calls == [1.0]
    assert seen["n"] == 2  # second 429 with no budget left: no third request


@pytest.mark.asyncio
async def test_retry_after_larger_than_remaining_budget_raises() -> None:
    handler, seen = _rate_limited_then_ok([429, 429, 200], retry_after="8")
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps
    provider.MAX_TOTAL_SLEEP_SECONDS = 10.0

    with pytest.raises(AlpacaError, match="429"):
        await provider._get("/v2/stocks/AAPL/bars")

    assert sleeps.calls == [8.0]  # 8 s fits; the next 8 s exceeds the 2 s left
    assert seen["n"] == 2
