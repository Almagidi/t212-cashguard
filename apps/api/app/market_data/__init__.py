"""
Market data module.

Provider selection (in priority order):
  1. Alpaca + Polygon validator  → LIVE SIGNALS + cross-source validation
  2. Alpaca only                 → LIVE SIGNALS
  3. Polygon only                → degraded fallback / backtesting
  4. Mock                        → DEVELOPMENT

For live trading: set ALPACA_API_KEY + ALPACA_API_SECRET in .env
For validation/backtesting: set POLYGON_API_KEY in .env
"""
from __future__ import annotations

from typing import Any

from app.core.config import settings


def get_live_provider() -> Any:
    """
    Return the best available live market-data provider.
    Alpaca is the primary live source; Polygon validates and falls back when available.
    """
    if settings.ALPACA_API_KEY and settings.ALPACA_API_SECRET and settings.POLYGON_API_KEY:
        from app.market_data.validated_provider import ValidatedMarketDataProvider
        return ValidatedMarketDataProvider()

    if settings.ALPACA_API_KEY and settings.ALPACA_API_SECRET:
        from app.market_data.alpaca_provider import AlpacaMarketDataProvider
        return AlpacaMarketDataProvider()

    if settings.POLYGON_API_KEY:
        import structlog
        log = structlog.get_logger()
        log.warning(
            "market_data.using_polygon_only",
            message=(
                "Using Polygon without Alpaca validation. "
                "If your Polygon plan is delayed, live signal quality will degrade. "
                "Add ALPACA_API_KEY + ALPACA_API_SECRET for primary live data."
            )
        )
        from app.market_data.polygon_provider import PolygonMarketDataProvider
        return PolygonMarketDataProvider()

    from app.market_data.mock_provider import MockMarketDataProvider
    return MockMarketDataProvider()


def get_backtest_provider(api_key: str | None = None) -> Any:
    """
    Return a provider suitable for backtesting (needs historical data).
    Polygon is used here since Alpaca free tier has limited history.
    """
    key = api_key or settings.POLYGON_API_KEY
    if key:
        from app.market_data.polygon_provider import PolygonMarketDataProvider
        return PolygonMarketDataProvider(key)

    raise ValueError(
        "POLYGON_API_KEY required for backtesting. "
        "Get a free key at https://polygon.io"
    )


def get_provider_name() -> str:
    """Return name of active live data provider for display in UI."""
    if settings.ALPACA_API_KEY and settings.ALPACA_API_SECRET and settings.POLYGON_API_KEY:
        return "alpaca_primary_polygon_validator"
    if settings.ALPACA_API_KEY and settings.ALPACA_API_SECRET:
        return "alpaca_realtime"
    if settings.POLYGON_API_KEY:
        return "polygon_only"
    return "mock"
