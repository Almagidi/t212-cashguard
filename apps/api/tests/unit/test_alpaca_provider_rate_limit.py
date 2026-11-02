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
    assert sleeps.calls == [1.0, 2.0]


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
async def test_429_retry_after_is_capped() -> None:
    handler, _ = _rate_limited_then_ok([429, 200], retry_after="3600")
    provider = _provider(handler)
    sleeps = _Sleeps()
    provider._sleep = sleeps

    await provider._get("/v2/stocks/AAPL/bars")

    assert sleeps.calls == [AlpacaMarketDataProvider.MAX_BACKOFF_SECONDS]


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
