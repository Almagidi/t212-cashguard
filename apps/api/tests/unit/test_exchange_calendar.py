from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.market_data.exchange_calendar import (
    SessionClassification,
    calendar_for_venue,
)

NY = ZoneInfo("America/New_York")


def test_expected_sessions_exclude_weekends_and_full_market_holidays() -> None:
    calendar = calendar_for_venue("XNYS")

    sessions = calendar.expected_sessions(date(2025, 1, 1), date(2025, 1, 6))

    assert [session.local_date for session in sessions] == [
        date(2025, 1, 2),
        date(2025, 1, 3),
        date(2025, 1, 6),
    ]
    assert [session.session_id for session in sessions] == [
        "XNYS:2025-01-02",
        "XNYS:2025-01-03",
        "XNYS:2025-01-06",
    ]


def test_exceptional_full_market_closure_is_excluded() -> None:
    calendar = calendar_for_venue("XNYS")

    assert calendar.expected_sessions(date(2025, 1, 9), date(2025, 1, 9)) == ()


@pytest.mark.parametrize(
    ("session_date", "expected_open_utc", "expected_close_utc"),
    [
        (
            date(2025, 1, 6),
            datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
            datetime(2025, 1, 6, 21, 0, tzinfo=UTC),
        ),
        (
            date(2025, 3, 10),
            datetime(2025, 3, 10, 13, 30, tzinfo=UTC),
            datetime(2025, 3, 10, 20, 0, tzinfo=UTC),
        ),
        (
            date(2025, 11, 3),
            datetime(2025, 11, 3, 14, 30, tzinfo=UTC),
            datetime(2025, 11, 3, 21, 0, tzinfo=UTC),
        ),
    ],
)
def test_regular_sessions_preserve_exchange_local_time_across_dst(
    session_date: date,
    expected_open_utc: datetime,
    expected_close_utc: datetime,
) -> None:
    calendar = calendar_for_venue("NYSE")
    (session,) = calendar.expected_sessions(session_date, session_date)

    assert calendar.session_open(session) == expected_open_utc
    assert calendar.session_close(session) == expected_close_utc
    assert session.open_at.astimezone(NY).time().isoformat() == "09:30:00"
    assert session.close_at.astimezone(NY).time().isoformat() == "16:00:00"
    assert calendar.is_early_close(session) is False


@pytest.mark.parametrize(
    ("session_date", "expected_close_utc"),
    [
        (date(2025, 7, 3), datetime(2025, 7, 3, 17, 0, tzinfo=UTC)),
        (date(2026, 11, 27), datetime(2026, 11, 27, 18, 0, tzinfo=UTC)),
    ],
)
def test_official_early_closes_are_explicit(
    session_date: date,
    expected_close_utc: datetime,
) -> None:
    calendar = calendar_for_venue("XNYS")
    (session,) = calendar.expected_sessions(session_date, session_date)

    assert calendar.is_early_close(session) is True
    assert calendar.session_close(session) == expected_close_utc
    assert session.close_at.astimezone(NY).time().isoformat() == "13:00:00"


@pytest.mark.parametrize(
    ("session_date", "terminal_bar_utc"),
    [
        (date(2025, 1, 6), datetime(2025, 1, 6, 20, 55, tzinfo=UTC)),
        (date(2025, 7, 3), datetime(2025, 7, 3, 16, 55, tzinfo=UTC)),
    ],
)
def test_terminal_bar_recognises_regular_and_early_closes(
    session_date: date,
    terminal_bar_utc: datetime,
) -> None:
    calendar = calendar_for_venue("XNYS")
    (session,) = calendar.expected_sessions(session_date, session_date)

    assert calendar.is_terminal_bar(session, terminal_bar_utc, interval_minutes=5)
    assert not calendar.is_terminal_bar(
        session,
        terminal_bar_utc - timedelta(minutes=5),
        interval_minutes=5,
    )


def test_reference_session_clock_preserves_exchange_local_offset_across_dst() -> None:
    calendar = calendar_for_venue("XNYS")
    winter = datetime(2025, 1, 6, 14, 35, tzinfo=UTC)
    summer = datetime(2025, 3, 10, 13, 35, tzinfo=UTC)

    assert calendar.to_reference_session_clock(winter, reference_open_utc="14:30") == datetime(
        2025, 1, 6, 14, 35, tzinfo=UTC
    )
    assert calendar.to_reference_session_clock(summer, reference_open_utc="14:30") == datetime(
        2025, 3, 10, 14, 35, tzinfo=UTC
    )


def test_timestamp_boundaries_are_half_open_and_timezone_aware() -> None:
    calendar = calendar_for_venue("XNYS")
    (session,) = calendar.expected_sessions(date(2025, 1, 6), date(2025, 1, 6))

    assert calendar.supports_extended_hours is False
    assert (
        calendar.classify_timestamp(session.open_at - timedelta(minutes=1))
        is SessionClassification.OUT_OF_SESSION
    )
    assert calendar.session_for_timestamp(session.open_at) == session
    assert calendar.classify_timestamp(session.open_at) is SessionClassification.REGULAR_SESSION
    assert calendar.session_for_timestamp(session.close_at) is None
    assert calendar.classify_timestamp(session.close_at) is SessionClassification.OUT_OF_SESSION

    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.session_for_timestamp(datetime(2025, 1, 6, 9, 30))


def test_out_of_session_mapping_uses_exchange_local_date_when_utc_date_differs() -> None:
    calendar = calendar_for_venue("XNYS")
    after_hours = datetime(2025, 1, 7, 0, 30, tzinfo=UTC)

    assert after_hours.astimezone(NY).date() == date(2025, 1, 6)
    assert calendar.classify_timestamp(after_hours) is SessionClassification.OUT_OF_SESSION
    assert calendar.session_for_timestamp(after_hours) is None


def test_non_session_day_is_distinct_from_out_of_session() -> None:
    calendar = calendar_for_venue("XNYS")

    assert (
        calendar.classify_timestamp(datetime(2025, 1, 4, 15, 0, tzinfo=UTC))
        is SessionClassification.NON_SESSION_DAY
    )


def test_previous_and_next_session_cross_weekends_and_holidays() -> None:
    calendar = calendar_for_venue("XNYS")
    (monday,) = calendar.expected_sessions(date(2025, 1, 6), date(2025, 1, 6))
    (july_fifth,) = calendar.expected_sessions(date(2024, 7, 5), date(2024, 7, 5))

    assert calendar.previous_session(monday).local_date == date(2025, 1, 3)
    assert calendar.next_session(calendar.previous_session(monday)) == monday
    assert calendar.previous_session(july_fifth).local_date == date(2024, 7, 3)


def test_invalid_ranges_and_unsupported_venues_fail_explicitly() -> None:
    calendar = calendar_for_venue("XNYS")

    with pytest.raises(ValueError, match="on or before"):
        calendar.expected_sessions(date(2025, 1, 6), date(2025, 1, 5))
    with pytest.raises(ValueError, match="supported from 1993-01-01"):
        calendar.expected_sessions(date(1992, 12, 31), date(1993, 1, 4))
    with pytest.raises(ValueError, match="supported through 2100-12-31"):
        calendar.expected_sessions(date(2101, 1, 1), date(2101, 1, 4))
    with pytest.raises(ValueError, match="supported through 2100-12-31"):
        calendar.classify_timestamp(datetime(2101, 1, 4, 15, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="Unsupported exchange calendar"):
        calendar_for_venue("t212")


def test_next_session_fails_closed_at_calendar_rule_boundary() -> None:
    calendar = calendar_for_venue("XNYS")
    sessions = calendar.expected_sessions(date(2100, 12, 20), date(2100, 12, 31))

    with pytest.raises(ValueError, match="No next supported XNYS session"):
        calendar.next_session(sessions[-1])
