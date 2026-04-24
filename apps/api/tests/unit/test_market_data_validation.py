from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.market_data.validated_provider import ValidatedMarketDataProvider
from app.services.feed_health import get_feed_health_snapshot, reset_feed_health


@dataclass
class FakeQuote:
    ticker: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: int
    timestamp: datetime


@dataclass
class FakeBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class FakePrimaryProvider:
    def __init__(self, *, quote: FakeQuote | None = None, bars: list[FakeBar] | None = None, quote_error: Exception | None = None):
        self.quote = quote
        self.bars = bars or []
        self.quote_error = quote_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_quote(self, ticker: str):
        del ticker
        if self.quote_error:
            raise self.quote_error
        return self.quote

    async def get_bars(self, ticker: str, **kwargs):
        del ticker, kwargs
        return self.bars

    async def get_opening_range_bars(self, ticker: str, **kwargs):
        del ticker, kwargs
        return self.bars

    async def is_market_open(self):
        return True

    def validate_staleness(self, quote: FakeQuote, max_age_seconds: int = 60) -> bool:
        age = (datetime.now(UTC) - quote.timestamp).total_seconds()
        return age <= max_age_seconds


class FakeValidatorProvider(FakePrimaryProvider):
    pass


@pytest.mark.asyncio
async def test_validated_provider_prefers_primary_when_validator_agrees():
    reset_feed_health()
    now = datetime.now(UTC)
    primary = FakePrimaryProvider(
        quote=FakeQuote("AAPL", Decimal("100.0"), Decimal("100.1"), Decimal("100.05"), 1000, now),
        bars=[FakeBar(now, Decimal("99"), Decimal("101"), Decimal("98"), Decimal("100"), Decimal("100000"))],
    )
    validator = FakeValidatorProvider(
        quote=FakeQuote("AAPL", Decimal("100.0"), Decimal("100.1"), Decimal("100.08"), 1200, now),
        bars=[FakeBar(now, Decimal("99"), Decimal("101"), Decimal("98"), Decimal("100.2"), Decimal("120000"))],
    )

    async with ValidatedMarketDataProvider(primary=primary, validator=validator) as provider:
        quote = await provider.get_quote("AAPL")
        bars = await provider.get_bars("AAPL")

    snapshot = get_feed_health_snapshot()
    assert quote.last == Decimal("100.05")
    assert bars[-1].close == Decimal("100")
    assert provider.is_trade_safe("AAPL") is True
    assert snapshot["status"] == "ok"
    assert snapshot["symbols"][0]["used_source"] == "alpaca"


@pytest.mark.asyncio
async def test_validated_provider_falls_back_when_primary_quote_is_stale():
    reset_feed_health()
    now = datetime.now(UTC)
    stale = now - timedelta(minutes=5)
    primary = FakePrimaryProvider(
        quote=FakeQuote("MSFT", Decimal("100.0"), Decimal("100.1"), Decimal("100.05"), 1000, stale),
    )
    validator = FakeValidatorProvider(
        quote=FakeQuote("MSFT", Decimal("100.2"), Decimal("100.3"), Decimal("100.25"), 1200, now),
    )

    async with ValidatedMarketDataProvider(primary=primary, validator=validator) as provider:
        quote = await provider.get_quote("MSFT")

    snapshot = get_feed_health_snapshot()
    assert quote.last == Decimal("100.25")
    assert provider.is_trade_safe("MSFT") is True
    assert snapshot["status"] == "fallback"
    assert snapshot["symbols"][0]["fallback_used"] is True


@pytest.mark.asyncio
async def test_validated_provider_marks_symbol_degraded_on_large_divergence():
    reset_feed_health()
    now = datetime.now(UTC)
    primary = FakePrimaryProvider(
        quote=FakeQuote("NVDA", Decimal("100.0"), Decimal("100.1"), Decimal("100.0"), 1000, now),
    )
    validator = FakeValidatorProvider(
        quote=FakeQuote("NVDA", Decimal("103.0"), Decimal("103.1"), Decimal("103.0"), 1000, now),
    )

    async with ValidatedMarketDataProvider(primary=primary, validator=validator) as provider:
        quote = await provider.get_quote("NVDA")

    snapshot = get_feed_health_snapshot()
    assert quote.last == Decimal("100.0")
    assert provider.is_trade_safe("NVDA") is False
    assert snapshot["status"] == "degraded"
    assert snapshot["symbols"][0]["divergence_pct"] is not None
