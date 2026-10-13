"""The wait between two looks at the same order grows, has a ceiling, and cannot be removed."""

from __future__ import annotations

import pytest

from app.services.reconciliation_backoff import (
    BACKOFF_STEPS_SECONDS,
    MAX_BACKOFF_LEVEL,
    MAX_BACKOFF_SECONDS,
    backoff_delay_seconds,
    level_after_unsettled_attempt,
    usable_retry_after_seconds,
)


def test_the_schedule_doubles_from_thirty_seconds_to_a_fifteen_minute_ceiling() -> None:
    assert BACKOFF_STEPS_SECONDS == (30, 60, 120, 240, 480, 900)
    assert MAX_BACKOFF_LEVEL == 6
    assert MAX_BACKOFF_SECONDS == 900


@pytest.mark.parametrize(
    ("level", "seconds"),
    [
        (-3, 0),
        (0, 0),
        (1, 30),
        (2, 60),
        (3, 120),
        (4, 240),
        (5, 480),
        (6, 900),
        (7, 900),
        (10**9, 900),
    ],
)
def test_each_level_has_a_fixed_wait(level: int, seconds: int) -> None:
    assert backoff_delay_seconds(level) == seconds


@pytest.mark.parametrize(
    ("current", "expected"),
    [(None, 1), (-4, 1), (0, 1), (1, 2), (4, 5), (5, 6), (6, 6), (40, 6)],
)
def test_one_unsettled_attempt_moves_one_step_and_stops_at_the_ceiling(
    current: int | None, expected: int
) -> None:
    assert level_after_unsettled_attempt(current) == expected


@pytest.mark.parametrize(
    ("current", "retry_after", "expected"),
    [
        (0, 1, 1),
        (0, 30, 1),
        (0, 30.5, 2),
        (0, 100, 3),
        (0, 900, 6),
        (0, 86_400, 6),
        (3, 10, 4),
        (5, 10, 6),
        (6, 5_000, 6),
    ],
)
def test_a_retry_after_raises_the_level_until_the_wait_covers_it(
    current: int, retry_after: float, expected: int
) -> None:
    level = level_after_unsettled_attempt(current, retry_after)

    assert level == expected
    assert backoff_delay_seconds(level) >= min(retry_after, MAX_BACKOFF_SECONDS)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "60",
        b"60",
        float("nan"),
        float("inf"),
        float("-inf"),
        0,
        0.0,
        -1,
        -0.001,
        True,
        False,
        [],
        {},
    ],
)
def test_an_untrustworthy_retry_after_is_ignored(value: object) -> None:
    assert usable_retry_after_seconds(value) is None
    assert level_after_unsettled_attempt(2, value) == 3


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(0.5, 0.5), (1, 1.0), (899, 899.0), (900, 900.0), (901, 900.0), (1e300, 900.0)],
)
def test_a_usable_retry_after_is_capped_at_the_longest_wait(value: float, seconds: float) -> None:
    assert usable_retry_after_seconds(value) == seconds


def test_an_integer_too_large_for_a_float_is_capped_and_its_negative_ignored() -> None:
    assert usable_retry_after_seconds(10**400) == 900.0
    assert usable_retry_after_seconds(-(10**400)) is None
    assert level_after_unsettled_attempt(0, 10**400) == 6


def test_no_input_produces_a_wait_longer_than_the_ceiling_or_shorter_than_the_first_step() -> None:
    for current in (None, -1, 0, 1, 3, 6, 99):
        for retry_after in (None, -1, 0, 0.1, 29, 31, 450, 900, 10**12, float("inf"), "x"):
            wait = backoff_delay_seconds(level_after_unsettled_attempt(current, retry_after))
            assert 30 <= wait <= 900
