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


def _configured_mock_provider() -> Any:
    if settings.MOCK_MARKET_PROFILE != "default" and settings.APP_MODE != "mock":
        raise RuntimeError("Non-default mock market profile is forbidden outside APP_MODE=mock.")

    from app.market_data.mock_provider import MockMarketDataProvider

    return MockMarketDataProvider(
        profile=settings.MOCK_MARKET_PROFILE, seed=settings.MOCK_MARKET_SEED
    )


def resolve_provider_kind() -> str:
    """Name the provider ``get_live_provider()`` would construct, without building it.

    Returns one of ``mock``, ``alpaca``, ``polygon``, ``validated``, or
    ``unresolved`` (live mode whose configuration resolves no real source).
    Includes the silent paper/demo fallback to the mock provider (``auto`` or an
    unconfigured vendor with no keys), so callers cannot disagree with the
    factory about what is actually serving data.
    """
    provider = settings.MARKET_DATA_PROVIDER.strip().lower()
    if settings.APP_MODE == "live":
        from app.market_data.live_source import LiveMarketSourceError, resolve_live_source

        try:
            provider = resolve_live_source()
        except LiveMarketSourceError:
            return "unresolved"
    if provider in {"mock", "polygon", "alpaca"}:
        return provider
    if provider in {"validated", "alpaca_primary_polygon_validator"}:
        return "validated"
    has_alpaca = bool(settings.ALPACA_API_KEY and settings.ALPACA_API_SECRET)
    if has_alpaca and settings.POLYGON_API_KEY:
        return "validated"
    if has_alpaca:
        return "alpaca"
    if settings.POLYGON_API_KEY:
        return "polygon"
    return "mock"


def provider_records_feed_health(provider: Any | None = None) -> bool:
    """True when the active provider writes per-symbol feed-health records.

    Only ``ValidatedMarketDataProvider`` calls ``record_feed_health`` (it sets
    ``records_feed_health = True``). This is the single predicate shared by the
    risk engine (unknown-symbol fail-closed rule, configuration-based) and the
    strategy runner (``is_trade_safe`` gate; passes the constructed provider).
    An ``unresolved`` live configuration counts as health-recording so that it
    fails closed.

    Live policy: a live deployment must run the validated provider (enforced by
    the ``market_data_provider`` startup check); a plain single-vendor provider
    is never exempted from the health gate in live mode.
    """
    if provider is not None:
        return bool(getattr(provider, "records_feed_health", False))
    if settings.APP_MODE == "live":
        return True  # live is never exempt; startup also requires the validated provider
    return resolve_provider_kind() == "validated"


def is_mock_market_data() -> bool:
    """True when synthetic data is serving and the mode is not live (never in live)."""
    return settings.APP_MODE != "live" and resolve_provider_kind() == "mock"


def get_live_provider() -> Any:
    """
    Return the best available live market-data provider.
    Alpaca is the primary live source; Polygon validates and falls back when available.
    """
    kind = resolve_provider_kind()
    if kind == "unresolved":
        from app.market_data.live_source import resolve_live_source

        resolve_live_source()  # raises LiveMarketSourceError with the real reason
        raise RuntimeError("Live market-data source could not be resolved.")

    if kind == "mock":
        return _configured_mock_provider()

    if kind == "polygon":
        if settings.MARKET_DATA_PROVIDER.strip().lower() != "polygon" and (
            settings.APP_MODE != "live"
        ):
            import structlog

            structlog.get_logger().warning(
                "market_data.using_polygon_only",
                message=(
                    "Using Polygon without Alpaca validation. "
                    "If your Polygon plan is delayed, live signal quality will degrade. "
                    "Add ALPACA_API_KEY + ALPACA_API_SECRET for primary live data."
                ),
            )
        from app.market_data.polygon_provider import PolygonMarketDataProvider

        return PolygonMarketDataProvider()

    if kind == "alpaca":
        from app.market_data.alpaca_provider import AlpacaMarketDataProvider

        return AlpacaMarketDataProvider()

    from app.market_data.validated_provider import ValidatedMarketDataProvider

    return ValidatedMarketDataProvider()


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
        "POLYGON_API_KEY required for backtesting. Get a free key at https://polygon.io"
    )


def get_kraken_provider() -> Any:
    """
    Return the Kraken public market data provider.
    No credentials required — all Kraken OHLC/Ticker endpoints are public.
    Used exclusively by Kraken crypto strategy families.
    """
    from app.market_data.kraken_provider import KrakenMarketDataProvider

    return KrakenMarketDataProvider()


def get_provider_name() -> str:
    """Return name of active live data provider for display in UI."""
    provider = settings.MARKET_DATA_PROVIDER.strip().lower()
    if settings.APP_MODE == "live":
        from app.market_data.live_source import LiveMarketSourceError, resolve_live_source

        try:
            provider = resolve_live_source()
        except LiveMarketSourceError:
            return "unavailable"

    if provider == "mock":
        return "mock"
    if provider == "polygon":
        return "polygon_only"
    if provider == "alpaca":
        return "alpaca_realtime"
    if provider in {"validated", "alpaca_primary_polygon_validator"}:
        return "alpaca_primary_polygon_validator"
    if settings.ALPACA_API_KEY and settings.ALPACA_API_SECRET and settings.POLYGON_API_KEY:
        return "alpaca_primary_polygon_validator"
    if settings.ALPACA_API_KEY and settings.ALPACA_API_SECRET:
        return "alpaca_realtime"
    if settings.POLYGON_API_KEY:
        return "polygon_only"
    return "mock"
