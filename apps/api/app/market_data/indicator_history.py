"""Validated cross-session history for indicators that need more bars than a session has.

Early in a session there are too few bars for ATR(14), a 20-bar volume average or the
Choppiness Index. The runner and the backtest engine already hold earlier regular-session
bars; this module decides how much of that history may be joined to the current session.
Only an unbroken run of bars is accepted, so an indicator never spans a hole in the data.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.market_data.exchange_calendar import calendar_for_venue

if TYPE_CHECKING:
    from app.market_data.exchange_calendar import XNYSCalendar
    from app.strategies.indicators import Bar


def _follows(
    calendar: XNYSCalendar, earlier: datetime, later: datetime, *, interval_minutes: int
) -> bool:
    """True when ``later`` is the regular-session bar immediately after ``earlier``."""
    try:
        earlier_session = calendar.session_for_timestamp(earlier)
        later_session = calendar.session_for_timestamp(later)
        if earlier_session is None or later_session is None:
            return False
        if earlier_session.session_id == later_session.session_id:
            return later - earlier == timedelta(minutes=interval_minutes)
        return (
            calendar.next_session(earlier_session).session_id == later_session.session_id
            and calendar.is_terminal_bar(
                earlier_session, earlier, interval_minutes=interval_minutes
            )
            and later == calendar.session_open(later_session)
        )
    except (ValueError, OverflowError):
        # A timestamp the calendar cannot place (outside its supported years) joins nothing.
        return False


def _opens_its_session(calendar: XNYSCalendar, moment: datetime) -> bool:
    """True when ``moment`` is the opening bar of a regular session."""
    try:
        session = calendar.session_for_timestamp(moment)
    except (ValueError, OverflowError):
        return False
    return session is not None and moment == calendar.session_open(session)


def continuous_indicator_bars(
    history_bars: list[Bar],
    history_times: list[datetime],
    session_bars: list[Bar],
    session_times: list[datetime],
    *,
    interval_minutes: int,
    venue: str = "XNYS",
) -> list[Bar]:
    """The longest unbroken run of regular-session bars that ends with the current session.

    Returns an empty list when no history can be trusted, in which case a strategy has only
    its session bars, exactly as before. The history is used only if it ends with precisely
    the session's bars and timestamps (so nothing later than the latest session bar can
    enter) and the session's first bar is its opening bar. It is cut at the first hole
    walking backwards: a missing bar, a bar outside regular hours, a repeated or
    out-of-order timestamp, or a skipped session. Timestamps without a timezone, or that
    the calendar cannot place, join nothing.

    "Unbroken" is about data, not price: the run includes the overnight gap between one
    session's close and the next session's open, and indicators computed over it see it.
    """
    count = len(session_bars)
    if (
        count == 0
        or count != len(session_times)
        or len(history_bars) != len(history_times)
        or len(history_bars) <= count
        or history_bars[-count:] != session_bars
        or history_times[-count:] != session_times
    ):
        return []

    calendar = calendar_for_venue(venue)
    if not _opens_its_session(calendar, session_times[0]):
        return []
    start = len(history_bars) - 1
    while start > 0 and _follows(
        calendar, history_times[start - 1], history_times[start], interval_minutes=interval_minutes
    ):
        start -= 1

    session_start = len(history_bars) - count
    if start >= session_start:
        # The run does not reach back past the session's first bar: nothing to add,
        # or the session itself has a hole.
        return []
    return history_bars[start:]
