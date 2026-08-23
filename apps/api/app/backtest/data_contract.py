"""Strict validation for market-data inputs used by research engines."""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from app.strategies.indicators import Bar


def validate_bar_series(
    bars: list[Bar],
    bar_times: list[datetime],
    *,
    label: str = "bar series",
) -> None:
    """Reject ambiguous or impossible OHLCV data before research begins."""
    if len(bars) != len(bar_times):
        raise ValueError(f"{label}: bars and bar_times must be same length")

    previous_time_utc: datetime | None = None
    for index, (bar, bar_time) in enumerate(zip(bars, bar_times, strict=True)):
        if bar_time.tzinfo is None or bar_time.utcoffset() is None:
            raise ValueError(f"{label}: timestamp at index {index} must be timezone-aware")
        bar_time_utc = bar_time.astimezone(UTC)
        if previous_time_utc is not None:
            if bar_time_utc == previous_time_utc:
                raise ValueError(f"{label}: duplicate timestamp at index {index}")
            if bar_time_utc < previous_time_utc:
                raise ValueError(f"{label}: timestamps must be strictly increasing")
        previous_time_utc = bar_time_utc

        values = {
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for field_name, value in values.items():
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{label}: {field_name} at index {index} must be a finite Decimal")

        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            raise ValueError(f"{label}: OHLC prices at index {index} must be positive")
        if bar.volume < 0:
            raise ValueError(f"{label}: volume at index {index} must be non-negative")
        if bar.high < max(bar.open, bar.low, bar.close):
            raise ValueError(f"{label}: high at index {index} is below another OHLC price")
        if bar.low > min(bar.open, bar.high, bar.close):
            raise ValueError(f"{label}: low at index {index} is above another OHLC price")
