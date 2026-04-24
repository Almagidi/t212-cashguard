from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services import market_regime as market_regime_module
from app.services.market_regime import MarketRegimeService


@dataclass
class FakeBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class FakeProvider:
    def __init__(self, direction: str = "up") -> None:
        self.direction = direction

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_bars(self, ticker: str, *, multiplier: int = 1, timespan: str = "day", limit: int = 50, **kwargs):
        del kwargs, ticker
        now = datetime(2026, 4, 11, tzinfo=UTC)
        bars: list[FakeBar] = []
        if timespan == "day":
            base = Decimal("500")
            slope = Decimal("1.2") if self.direction == "up" else Decimal("-1.2")
            for idx in range(limit):
                close = base + (slope * Decimal(str(idx)))
                bars.append(FakeBar(now - timedelta(days=limit - idx), close - 1, close + 1, close - 2, close, Decimal("1000000")))
            return bars

        base = Decimal("540")
        slope = Decimal("0.25") if self.direction == "up" else Decimal("-0.30")
        for idx in range(limit):
            close = base + (slope * Decimal(str(idx)))
            bars.append(FakeBar(now - timedelta(minutes=5 * (limit - idx)), close - 1, close + 1, close - 2, close, Decimal("250000")))
        return bars


@pytest.mark.asyncio
async def test_market_regime_service_trending_up(monkeypatch):
    monkeypatch.setattr(market_regime_module, "get_live_provider", lambda: FakeProvider("up"))
    service = MarketRegimeService()
    payload = await service.evaluate()

    assert payload["regime"] == "trending_up"
    assert "orb" in payload["active_strategies"]
    assert payload["confidence"] > 0.5


@pytest.mark.asyncio
async def test_market_regime_service_risk_off(monkeypatch):
    monkeypatch.setattr(market_regime_module, "get_live_provider", lambda: FakeProvider("down"))
    service = MarketRegimeService()
    payload = await service.evaluate()

    assert payload["regime"] in {"trending_down", "risk_off"}
    assert "opening_fade" in payload["active_strategies"]
