"""Exchange-session timing proofs for the EOD flatten safety boundary."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.services.eod_flatten import EOD_DUE_WINDOW_MINUTES, eod_due_window


def _strategy(*, venue: str = "t212", session_end: str = "16:00") -> SimpleNamespace:
    return SimpleNamespace(venue=venue, session_end=session_end)


@pytest.mark.parametrize(
    ("now_utc", "expected_session_date"),
    [
        (datetime(2026, 7, 6, 20, 0, tzinfo=UTC), date(2026, 7, 6)),
        (datetime(2026, 1, 5, 21, 0, tzinfo=UTC), date(2026, 1, 5)),
    ],
)
def test_t212_close_uses_xnys_exchange_timezone_across_dst(
    now_utc: datetime,
    expected_session_date: date,
) -> None:
    window = eod_due_window(_strategy(), now_utc)

    assert window is not None
    assert window.exchange == "XNYS"
    assert window.exchange_session_date == expected_session_date
    assert window.cutoff_at_utc == now_utc


def test_dst_transition_changes_utc_close_without_changing_local_contract() -> None:
    winter_friday = eod_due_window(_strategy(), datetime(2026, 3, 6, 21, 0, tzinfo=UTC))
    summer_monday = eod_due_window(_strategy(), datetime(2026, 3, 9, 20, 0, tzinfo=UTC))

    assert winter_friday is not None
    assert summer_monday is not None
    assert winter_friday.cutoff_at_utc.hour == 21
    assert summer_monday.cutoff_at_utc.hour == 20


def test_due_window_is_bounded_and_end_is_exclusive() -> None:
    # 16:05 UTC is 12:05 New York in summer, not five minutes after close.
    assert eod_due_window(_strategy(), datetime(2026, 7, 6, 16, 5, tzinfo=UTC)) is None
    assert eod_due_window(_strategy(), datetime(2026, 7, 6, 19, 59, 59, tzinfo=UTC)) is None
    assert eod_due_window(_strategy(), datetime(2026, 7, 6, 20, 0, tzinfo=UTC)) is not None
    assert eod_due_window(_strategy(), datetime(2026, 7, 6, 20, 9, 59, tzinfo=UTC)) is not None
    assert EOD_DUE_WINDOW_MINUTES == 10
    assert eod_due_window(_strategy(), datetime(2026, 7, 6, 20, 10, tzinfo=UTC)) is None


@pytest.mark.parametrize(
    "now_utc",
    [
        datetime(2026, 12, 25, 21, 0, tzinfo=UTC),
        datetime(2026, 7, 5, 20, 0, tzinfo=UTC),
    ],
)
def test_holidays_and_weekends_are_never_due(now_utc: datetime) -> None:
    assert eod_due_window(_strategy(), now_utc) is None


def test_early_close_overrides_later_configured_close() -> None:
    window = eod_due_window(
        _strategy(session_end="16:00"),
        datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
    )

    assert window is not None
    assert window.cutoff_at_utc == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert window.is_early_close is True


@pytest.mark.parametrize("venue", ["kraken", "unknown", ""])
def test_unsupported_runtime_venues_fail_closed(venue: str) -> None:
    assert (
        eod_due_window(
            _strategy(venue=venue),
            datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
        )
        is None
    )


@pytest.mark.parametrize("session_end", ["", "4pm", "25:00", "09:00:00"])
def test_invalid_session_end_fails_closed(session_end: str) -> None:
    assert (
        eod_due_window(
            _strategy(session_end=session_end),
            datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
        )
        is None
    )


def test_unresolvable_calendar_session_fails_closed() -> None:
    assert (
        eod_due_window(
            _strategy(),
            datetime(2101, 7, 6, 20, 0, tzinfo=UTC),
        )
        is None
    )
