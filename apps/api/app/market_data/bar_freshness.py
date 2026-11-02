"""Bar-age gate: are the latest intraday bars too old to open new risk on?

Applies to ENTRIES only. Protective exits must never consult this gate.

Rules (fail-closed for entries):
  * missing, naive, non-datetime or future-dated (beyond a small clock-skew
    tolerance) timestamps are STALE;
  * outside a regular exchange session bars cannot be "stale" for this
    purpose (entries are blocked by the session checks anyway);
  * inside a session, bars are stale when the newest bar's age exceeds
    ``max_intervals`` bar intervals plus a grace period. Age is measured from
    the bar's start (bars are stamped with their open time) and never from
    before the session open, so the first minutes of a session are not
    penalised for yesterday's close.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.market_data.exchange_calendar import XNYSCalendar, calendar_for_venue

DEFAULT_MAX_INTERVALS = 2.5
DEFAULT_GRACE_SECONDS = 60
FUTURE_TOLERANCE_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class BarFreshness:
    stale: bool
    reason: str
    age_seconds: float | None = None


def assess_bar_freshness(
    last_bar_time: Any,
    interval_minutes: int,
    *,
    now: datetime | None = None,
    calendar: XNYSCalendar | None = None,
    max_intervals: float = DEFAULT_MAX_INTERVALS,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
) -> BarFreshness:
    if not isinstance(interval_minutes, int | float) or interval_minutes <= 0:
        return BarFreshness(True, "invalid_interval")
    if last_bar_time is None:
        return BarFreshness(True, "missing_timestamp")
    if not isinstance(last_bar_time, datetime):
        return BarFreshness(True, "invalid_timestamp")
    if last_bar_time.tzinfo is None or last_bar_time.utcoffset() is None:
        return BarFreshness(True, "naive_timestamp")

    current = now if now is not None else _utcnow()
    if current.tzinfo is None or current.utcoffset() is None:
        return BarFreshness(True, "naive_now")

    last_utc = last_bar_time.astimezone(UTC)
    current_utc = current.astimezone(UTC)
    if last_utc > current_utc + timedelta(seconds=FUTURE_TOLERANCE_SECONDS):
        return BarFreshness(True, "future_timestamp")

    cal = calendar or calendar_for_venue("XNYS")
    try:
        session = cal.session_for_timestamp(current_utc)
    except ValueError:
        return BarFreshness(True, "calendar_unsupported")
    if session is None:
        return BarFreshness(False, "market_closed")

    reference = max(last_utc, cal.session_open(session))
    age = max((current_utc - reference).total_seconds(), 0.0)
    limit = float(interval_minutes) * 60.0 * max_intervals + grace_seconds
    if age > limit:
        return BarFreshness(True, "bars_stale", age)
    return BarFreshness(False, "fresh", age)


def bars_are_stale(last_bar_time: Any, interval_minutes: int, **kwargs: Any) -> bool:
    """True when entries must be blocked because the newest bar is too old/invalid."""
    return assess_bar_freshness(last_bar_time, interval_minutes, **kwargs).stale
