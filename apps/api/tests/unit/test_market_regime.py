from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.market_data.mock_provider import MockMarketDataProvider
from app.risk.engine import RiskEngine, RiskViolation
from app.services import market_regime as market_regime_module
from app.services.feed_health import record_feed_health, reset_feed_health
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

    async def get_bars(
        self, ticker: str, *, multiplier: int = 1, timespan: str = "day", limit: int = 50, **kwargs
    ):
        del kwargs, ticker
        now = datetime(2026, 4, 11, tzinfo=UTC)
        bars: list[FakeBar] = []
        if self.direction == "empty":
            return bars
        if timespan == "day":
            base = Decimal("500")
            if self.direction == "flat":
                for idx in range(limit):
                    close = base
                    bars.append(
                        FakeBar(
                            now - timedelta(days=limit - idx),
                            close - 1,
                            close + 1,
                            close - 2,
                            close,
                            Decimal("1000000"),
                        )
                    )
                return bars
            if self.direction == "volatile":
                for idx in range(limit):
                    close = base + (Decimal("25") if idx % 2 else Decimal("-25"))
                    bars.append(
                        FakeBar(
                            now - timedelta(days=limit - idx),
                            close - 1,
                            close + 1,
                            close - 2,
                            close,
                            Decimal("1000000"),
                        )
                    )
                return bars
            slope = Decimal("1.2") if self.direction == "up" else Decimal("-1.2")
            for idx in range(limit):
                close = base + (slope * Decimal(str(idx)))
                bars.append(
                    FakeBar(
                        now - timedelta(days=limit - idx),
                        close - 1,
                        close + 1,
                        close - 2,
                        close,
                        Decimal("1000000"),
                    )
                )
            return bars

        base = Decimal("600") if self.direction == "volatile" else Decimal("540")
        if self.direction == "flat":
            slope = Decimal("0")
        elif self.direction == "up":
            slope = Decimal("0.25")
        else:
            slope = Decimal("-0.30")
        for idx in range(limit):
            close = base + (slope * Decimal(str(idx)))
            bars.append(
                FakeBar(
                    now - timedelta(minutes=5 * (limit - idx)),
                    close - 1,
                    close + 1,
                    close - 2,
                    close,
                    Decimal("250000"),
                )
            )
        return bars


@pytest.fixture(autouse=True)
def reset_market_regime_state():
    market_regime_module._cached_regime = None
    market_regime_module._last_evaluated_at = None
    reset_feed_health()
    yield
    market_regime_module._cached_regime = None
    market_regime_module._last_evaluated_at = None
    reset_feed_health()


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


@pytest.mark.asyncio
async def test_market_regime_service_sideways(monkeypatch):
    monkeypatch.setattr(market_regime_module, "get_live_provider", lambda: FakeProvider("flat"))
    service = MarketRegimeService()
    payload = await service.evaluate()

    assert payload["regime"] == "ranging"
    assert payload["primary_trend"] == "mixed"


@pytest.mark.asyncio
async def test_market_regime_service_high_volatility(monkeypatch):
    monkeypatch.setattr(market_regime_module, "get_live_provider", lambda: FakeProvider("volatile"))
    service = MarketRegimeService()
    payload = await service.evaluate()

    assert payload["regime"] == "high_volatility"
    assert "closing_momentum" in payload["suppressed_strategies"]


@pytest.mark.asyncio
async def test_market_regime_service_missing_data_is_unknown(monkeypatch):
    monkeypatch.setattr(market_regime_module, "get_live_provider", lambda: FakeProvider("empty"))
    service = MarketRegimeService()
    payload = await service.evaluate()

    assert payload["regime"] == "unknown"
    assert payload["active_strategies"] == []


@pytest.mark.asyncio
async def test_market_regime_cache_does_not_mask_stale_feed(monkeypatch):
    monkeypatch.setattr(market_regime_module, "get_live_provider", lambda: FakeProvider("up"))
    service = MarketRegimeService()

    cached_payload = await service.evaluate()
    assert cached_payload["regime"] == "trending_up"

    record_feed_health(
        provider="alpaca_primary_polygon_validator",
        ticker="SPY",
        status="stale",
        detail="Primary quote is stale.",
        used_source="alpaca",
        validator_source="polygon",
    )

    payload = await service.evaluate()

    assert payload["regime"] == "unsafe"
    assert payload["active_strategies"] == []
    assert "orb" in payload["suppressed_strategies"]


@pytest.mark.asyncio
async def test_market_regime_service_mock_provider_produces_real_snapshots_not_unknown():
    """Regression test for the real bug: MarketRegimeService used to return
    regime="unknown" in APP_MODE=mock purely because MockMarketDataProvider
    has no __aenter__/__aexit__, so _load_snapshots()'s
    ``if hasattr(provider, "__aenter__")`` branch was silently skipped and
    every benchmark snapshot was dropped -- with no exception, no log, just
    an empty snapshot list. This does not monkeypatch get_live_provider at
    all: it exercises the real app.market_data.get_live_provider() ->
    MockMarketDataProvider() path exactly as MarketIntelligenceMonitor does.
    """
    service = MarketRegimeService()
    payload = await service.evaluate()

    assert payload["regime"] != "unknown"
    assert payload["detail"] != "Not enough benchmark data to classify regime."


def test_market_regime_service_mock_provider_has_no_async_context_manager():
    """Locks in the chosen fix shape: MockMarketDataProvider intentionally
    stays without __aenter__/__aexit__. strategy_runner.py._fetch_market_context
    branches on hasattr(provider, "__aenter__") to pick between the async
    get_bars() path (Alpaca/Polygon/Validated) and the sync get_ohlcv() path
    (mock) -- giving MockMarketDataProvider an async context manager would
    silently flip that branch too and is out of scope for this fix.
    """
    assert not hasattr(MockMarketDataProvider(), "__aenter__")


@pytest.mark.asyncio
async def test_real_mock_regime_feeds_risk_engine_without_regime_block(db):
    """End-to-end proof the reported blocker is closed: feed the real
    MarketRegimeService.evaluate() payload (real MockMarketDataProvider,
    nothing stubbed) into the real RiskEngine.check_market_conditions() --
    the exact call StrategyRunner makes at app/services/strategy_runner.py:754
    before signal generation -- and confirm it is never blocked for being an
    unknown/invalid regime.

    A strategy_type that appears in no regime's suppressed_strategies list
    is used deliberately: MockMarketDataProvider's underlying price walk is
    shared, mutable module state (app.market_data.mock_provider._current_prices)
    that drifts across the whole test session, so this test cannot assume
    which *trusted* regime (ranging/trending_up/trending_down/risk_off/
    high_volatility) evaluate() will land on -- only that it lands on one.
    high_volatility is the one regime RiskEngine blocks unconditionally
    (independent of strategy_type or suppression), so it is the only
    tolerated exception here.
    """
    from app.db.models import AppSettings

    db.add(AppSettings(id=1))
    await db.commit()

    regime = await MarketRegimeService().evaluate()
    assert regime["regime"] != "unknown"

    engine = RiskEngine(db)
    if regime["regime"] == "high_volatility":
        with pytest.raises(RiskViolation, match="high_volatility"):
            await engine.check_market_conditions(
                ticker="SPY",
                strategy_type="__regression_probe__",
                market_regime=regime,
            )
    else:
        await engine.check_market_conditions(
            ticker="SPY",
            strategy_type="__regression_probe__",
            market_regime=regime,
        )
