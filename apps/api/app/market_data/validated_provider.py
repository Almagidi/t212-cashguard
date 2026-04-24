"""Alpaca-primary market data with Polygon validation and fallback."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.market_data.alpaca_provider import AlpacaMarketDataProvider
from app.market_data.polygon_provider import PolygonMarketDataProvider
from app.services.feed_health import is_symbol_trade_safe, record_feed_health


class ValidatedMarketDataProvider:
    """Use Alpaca as the live source and Polygon as validator/fallback."""

    QUOTE_DIVERGENCE_WARN_PCT = Decimal("0.75")
    QUOTE_DIVERGENCE_FAIL_PCT = Decimal("2.00")
    BAR_DIVERGENCE_WARN_PCT = Decimal("1.00")
    BAR_DIVERGENCE_FAIL_PCT = Decimal("3.00")

    def __init__(
        self,
        *,
        primary: AlpacaMarketDataProvider | None = None,
        validator: PolygonMarketDataProvider | None = None,
    ) -> None:
        self.primary = primary or AlpacaMarketDataProvider()
        self.validator = validator or PolygonMarketDataProvider()
        self._entered_primary: Any | None = None
        self._entered_validator: Any | None = None

    async def __aenter__(self) -> ValidatedMarketDataProvider:
        self._entered_primary = await self.primary.__aenter__()
        try:
            self._entered_validator = await self.validator.__aenter__()
        except Exception:
            self._entered_validator = None
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._entered_validator is not None:
            await self.validator.__aexit__(*args)
            self._entered_validator = None
        if self._entered_primary is not None:
            await self.primary.__aexit__(*args)
            self._entered_primary = None

    async def get_bars(
        self,
        ticker: str,
        *,
        multiplier: int = 5,
        timespan: str = "minute",
        from_date: Any | None = None,
        to_date: Any | None = None,
        limit: int = 50,
    ) -> list[Any]:
        primary_bars: list[Any] = []
        validator_bars: list[Any] = []
        primary_error: Exception | None = None

        try:
            primary_bars = await self.primary.get_bars(
                ticker,
                multiplier=multiplier,
                timespan=timespan,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
            )
        except Exception as exc:
            primary_error = exc

        if self._entered_validator is not None:
            try:
                validator_bars = await self.validator.get_bars(
                    ticker,
                    multiplier=multiplier,
                    timespan=timespan,
                    from_date=from_date,
                    to_date=to_date,
                    limit=limit,
                )
            except Exception:
                validator_bars = []

        if primary_bars:
            self._record_bar_health(ticker, primary_bars, validator_bars)
            return primary_bars

        if validator_bars:
            latest = validator_bars[-1]
            record_feed_health(
                provider="alpaca_primary_polygon_validator",
                ticker=ticker,
                status="fallback",
                detail=f"Primary Alpaca bars unavailable; using Polygon fallback ({primary_error}).",
                used_source="polygon",
                validator_source="polygon",
                fallback_used=True,
                primary_timestamp=None,
                validator_timestamp=getattr(latest, "timestamp", None),
            )
            return validator_bars

        raise primary_error or RuntimeError("No market-data bars available")

    async def get_opening_range_bars(
        self,
        ticker: str,
        session_date: Any | None = None,
        orb_minutes: int = 15,
    ) -> list[Any]:
        primary_bars: list[Any] = []
        validator_bars: list[Any] = []
        primary_error: Exception | None = None

        try:
            primary_bars = await self.primary.get_opening_range_bars(
                ticker,
                session_date=session_date,
                orb_minutes=orb_minutes,
            )
        except Exception as exc:
            primary_error = exc

        if self._entered_validator is not None:
            try:
                validator_bars = await self.validator.get_opening_range_bars(
                    ticker,
                    session_date=session_date,
                    orb_minutes=orb_minutes,
                )
            except Exception:
                validator_bars = []

        if primary_bars:
            self._record_bar_health(ticker, primary_bars, validator_bars)
            return primary_bars

        if validator_bars:
            latest = validator_bars[-1]
            record_feed_health(
                provider="alpaca_primary_polygon_validator",
                ticker=ticker,
                status="fallback",
                detail=f"Primary Alpaca opening-range bars unavailable; using Polygon fallback ({primary_error}).",
                used_source="polygon",
                validator_source="polygon",
                fallback_used=True,
                primary_timestamp=None,
                validator_timestamp=getattr(latest, "timestamp", None),
            )
            return validator_bars

        raise primary_error or RuntimeError("No opening-range bars available")

    async def get_quote(self, ticker: str) -> Any:
        primary_quote: Any | None = None
        validator_quote: Any | None = None
        primary_error: Exception | None = None

        try:
            primary_quote = await self.primary.get_quote(ticker)
        except Exception as exc:
            primary_error = exc

        if self._entered_validator is not None:
            try:
                validator_quote = await self.validator.get_quote(ticker)
            except Exception:
                validator_quote = None

        if primary_quote is not None:
            status = self._record_quote_health(ticker, primary_quote, validator_quote)
            if status == "fallback" and validator_quote is not None and self._quote_is_trade_safe(validator_quote):
                return validator_quote
            return primary_quote

        if validator_quote is not None:
            record_feed_health(
                provider="alpaca_primary_polygon_validator",
                ticker=ticker,
                status="fallback",
                detail=f"Primary Alpaca quote unavailable; using Polygon fallback ({primary_error}).",
                used_source="polygon",
                validator_source="polygon",
                fallback_used=True,
                primary_timestamp=None,
                validator_timestamp=getattr(validator_quote, "timestamp", None),
            )
            return validator_quote

        raise primary_error or RuntimeError("No market-data quote available")

    async def is_market_open(self) -> bool:
        primary_error: Exception | None = None
        primary_open: bool | None = None
        validator_open: bool | None = None

        try:
            primary_open = await self.primary.is_market_open()
        except Exception as exc:
            primary_error = exc

        if self._entered_validator is not None:
            try:
                validator_open = await self.validator.is_market_open()
            except Exception:
                validator_open = None

        if primary_open is not None:
            status = "ok"
            detail = "Alpaca market status healthy."
            if validator_open is not None and validator_open != primary_open:
                status = "degraded"
                detail = "Alpaca and Polygon market-status endpoints disagree."
            record_feed_health(
                provider="alpaca_primary_polygon_validator",
                ticker="__market__",
                status=status,
                detail=detail,
                used_source="alpaca",
                validator_source="polygon" if validator_open is not None else None,
                primary_timestamp=datetime.now(UTC),
                validator_timestamp=datetime.now(UTC) if validator_open is not None else None,
            )
            return primary_open

        if validator_open is not None:
            record_feed_health(
                provider="alpaca_primary_polygon_validator",
                ticker="__market__",
                status="fallback",
                detail=f"Primary Alpaca market status unavailable; using Polygon fallback ({primary_error}).",
                used_source="polygon",
                validator_source="polygon",
                fallback_used=True,
                validator_timestamp=datetime.now(UTC),
            )
            return validator_open

        raise primary_error or RuntimeError("Market status unavailable")

    def validate_staleness(self, quote: Any, max_age_seconds: int = 60) -> bool:
        if hasattr(self.primary, "validate_staleness"):
            return bool(self.primary.validate_staleness(quote, max_age_seconds=max_age_seconds))
        return True

    def is_trade_safe(self, ticker: str) -> bool:
        return is_symbol_trade_safe(ticker)

    def _record_quote_health(self, ticker: str, primary_quote: Any, validator_quote: Any | None) -> str:
        primary_fresh = self._quote_is_trade_safe(primary_quote)
        primary_ts = getattr(primary_quote, "timestamp", None)
        validator_ts = getattr(validator_quote, "timestamp", None) if validator_quote is not None else None

        if not primary_fresh:
            status = "stale"
            detail = "Alpaca quote is stale beyond the trading threshold."
            if validator_quote is not None and self._quote_is_trade_safe(validator_quote):
                status = "fallback"
                detail = "Alpaca quote is stale; Polygon validator quote is being used as fallback."
            record_feed_health(
                provider="alpaca_primary_polygon_validator",
                ticker=ticker,
                status=status,
                detail=detail,
                used_source="polygon" if status == "fallback" else "alpaca",
                validator_source="polygon" if validator_quote is not None else None,
                fallback_used=status == "fallback",
                primary_timestamp=primary_ts,
                validator_timestamp=validator_ts,
            )
            return status

        divergence_pct = self._percent_diff(
            Decimal(str(getattr(primary_quote, "last", 0))),
            Decimal(str(getattr(validator_quote, "last", 0))) if validator_quote is not None else None,
        )
        status = "ok"
        detail = "Alpaca quote validated."
        if divergence_pct is not None and divergence_pct >= float(self.QUOTE_DIVERGENCE_FAIL_PCT):
            status = "degraded"
            detail = "Alpaca and Polygon quotes diverge beyond the hard tolerance."
        elif divergence_pct is not None and divergence_pct >= float(self.QUOTE_DIVERGENCE_WARN_PCT):
            status = "degraded"
            detail = "Alpaca and Polygon quotes diverge beyond the warning tolerance."

        record_feed_health(
            provider="alpaca_primary_polygon_validator",
            ticker=ticker,
            status=status,
            detail=detail,
            used_source="alpaca",
            validator_source="polygon" if validator_quote is not None else None,
            fallback_used=False,
            primary_timestamp=primary_ts,
            validator_timestamp=validator_ts,
            divergence_pct=divergence_pct,
        )
        return status

    def _record_bar_health(self, ticker: str, primary_bars: list[Any], validator_bars: list[Any]) -> None:
        latest_primary = primary_bars[-1]
        latest_validator = validator_bars[-1] if validator_bars else None
        primary_ts = getattr(latest_primary, "timestamp", None)
        validator_ts = getattr(latest_validator, "timestamp", None) if latest_validator is not None else None
        status = "ok"
        detail = "Alpaca bars validated."
        divergence_pct = self._percent_diff(
            Decimal(str(getattr(latest_primary, "close", 0))),
            Decimal(str(getattr(latest_validator, "close", 0))) if latest_validator is not None else None,
        )
        if divergence_pct is not None and divergence_pct >= float(self.BAR_DIVERGENCE_FAIL_PCT):
            status = "degraded"
            detail = "Alpaca and Polygon latest bars diverge beyond the hard tolerance."
        elif divergence_pct is not None and divergence_pct >= float(self.BAR_DIVERGENCE_WARN_PCT):
            status = "degraded"
            detail = "Alpaca and Polygon latest bars diverge beyond the warning tolerance."

        record_feed_health(
            provider="alpaca_primary_polygon_validator",
            ticker=ticker,
            status=status,
            detail=detail,
            used_source="alpaca",
            validator_source="polygon" if latest_validator is not None else None,
            primary_timestamp=primary_ts,
            validator_timestamp=validator_ts,
            divergence_pct=divergence_pct,
        )

    def _quote_is_trade_safe(self, quote: Any) -> bool:
        if quote is None:
            return False
        if hasattr(self.primary, "validate_staleness"):
            return bool(self.primary.validate_staleness(quote, max_age_seconds=60))
        return True

    def _percent_diff(self, left: Decimal, right: Decimal | None) -> float | None:
        if right is None:
            return None
        if left <= 0 or right <= 0:
            return None
        mid = (left + right) / Decimal("2")
        if mid <= 0:
            return None
        return float(((left - right).copy_abs() / mid) * Decimal("100"))
