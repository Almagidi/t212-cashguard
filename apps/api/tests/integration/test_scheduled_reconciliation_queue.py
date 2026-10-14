"""The scheduled reconciler's queue: a fixed order, a bounded run, and a wait after failure."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.db.models import OrderEvent
from tests.integration.scheduled_reconciliation_cases import (
    T0,
    Brokers,
    add_connection,
    add_order,
    connection_is_active,
    filled,
    install_borrowed_session,
    rewind_last_attempt,
    run_task,
    scope_of,
    stored,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

BATCH = 50


class SimulatedProcessDeath(BaseException):
    """Stands in for the worker being killed: nothing may catch it and carry on."""


@pytest.fixture
def brokers(monkeypatch: pytest.MonkeyPatch, db: AsyncSession) -> Brokers:
    registry = Brokers()
    install_borrowed_session(monkeypatch, registry, db)
    return registry


async def _backoff_events(db: AsyncSession, order_id: uuid.UUID) -> list[OrderEvent]:
    result = await db.execute(
        select(OrderEvent)
        .where(OrderEvent.order_id == order_id, OrderEvent.event_type == "reconciliation_backoff")
        .order_by(OrderEvent.occurred_at)
    )
    return list(result.scalars().all())


async def test_successive_runs_reach_every_order_of_a_backlog_larger_than_one_run(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    for index in range(120):
        await add_order(
            db,
            account_scope=scope,
            broker_order_id=f"o-{index:03d}",
            created_at=T0 + timedelta(seconds=index),
            commit=False,
        )
    await db.commit()
    broker = brokers.for_user(connection.user_id)

    reads_per_run = []
    for _ in range(3):
        before = len(broker.order_reads)
        await run_task()
        reads_per_run.append(len(broker.order_reads) - before)

    # Oldest first, each order once, before any order is read a second time.
    assert reads_per_run == [BATCH, BATCH, BATCH]
    assert broker.order_reads[:120] == [f"o-{index:03d}" for index in range(120)]
    assert broker.order_reads[120:] == [f"o-{index:03d}" for index in range(30)]


async def test_the_order_that_has_waited_longest_is_read_first(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    await add_order(
        db,
        account_scope=scope,
        broker_order_id="read-recently",
        created_at=T0,
        last_reconciled_at=long_ago + timedelta(hours=1),
    )
    await add_order(
        db,
        account_scope=scope,
        broker_order_id="read-long-ago",
        created_at=T0 + timedelta(minutes=5),
        last_reconciled_at=long_ago,
    )
    await add_order(
        db,
        account_scope=scope,
        broker_order_id="never-read-newer",
        created_at=T0 + timedelta(minutes=9),
    )
    await add_order(
        db,
        account_scope=scope,
        broker_order_id="never-read-older",
        created_at=T0 + timedelta(minutes=7),
    )

    await run_task()

    assert brokers.all_order_reads == [
        "never-read-older",
        "never-read-newer",
        "read-long-ago",
        "read-recently",
    ]


async def test_orders_with_identical_times_are_read_in_id_order(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    identifiers = sorted((uuid.uuid4() for _ in range(6)), key=lambda value: value.hex)
    for identifier in reversed(identifiers):
        await add_order(
            db,
            account_scope=scope,
            order_id=identifier,
            broker_order_id=identifier.hex,
            created_at=T0,
        )

    await run_task()

    assert brokers.all_order_reads == [identifier.hex for identifier in identifiers]


async def test_orders_still_waiting_do_not_take_the_place_of_orders_that_are_due(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    just_now = datetime.now(UTC)
    for index in range(BATCH + 10):
        await add_order(
            db,
            account_scope=scope,
            broker_order_id=f"waiting-{index:02d}",
            created_at=T0 + timedelta(seconds=index),
            last_reconciled_at=just_now,
            retry_count=3,
            commit=False,
        )
    for index in range(5):
        await add_order(
            db,
            account_scope=scope,
            broker_order_id=f"due-{index}",
            created_at=T0 + timedelta(hours=1, seconds=index),
            commit=False,
        )
    await db.commit()

    await run_task()

    assert brokers.all_order_reads == [f"due-{index}" for index in range(5)]


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("dry runs", {"is_dry_run": True}),
        ("orders already filled", {"status": "filled"}),
        ("another account's orders", {"account_scope": "trading212:demo:account:someone-else"}),
        ("another environment's orders", {"environment": "live"}),
        ("another venue's orders", {"venue": "kraken"}),
    ],
)
async def test_orders_that_are_not_this_runs_to_read_take_no_place_in_the_queue(
    db: AsyncSession, brokers: Brokers, label: str, overrides: dict[str, object]
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    for index in range(BATCH):
        await add_order(
            db,
            **{
                "account_scope": scope,
                "broker_order_id": f"not-ours-{index:02d}",
                "created_at": T0 + timedelta(seconds=index),
                "commit": False,
                **overrides,
            },
        )
    await add_order(
        db, account_scope=scope, broker_order_id="due", created_at=T0 + timedelta(hours=1)
    )

    summary = await run_task()

    assert brokers.all_order_reads == ["due"], label
    assert "deferred" not in summary
    assert "no_longer_due" not in summary
    assert "left_for_next_run" not in summary


async def test_a_failed_read_is_not_repeated_until_its_wait_is_over(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    order = await add_order(db, account_scope=scope_of(connection), broker_order_id="flaky")
    broker = brokers.for_user(connection.user_id)
    broker.responses["flaky"] = T212APIError(503, "unavailable")

    await run_task()
    after_first = await stored(db, order.id)
    assert broker.order_reads == ["flaky"]
    assert after_first.retry_count == 1
    assert after_first.last_reconciled_at is not None
    assert after_first.status == "accepted"

    await run_task()
    assert broker.order_reads == ["flaky"], "read again before the 30 second wait was over"

    await rewind_last_attempt(db, order.id, 31)
    await run_task()
    assert broker.order_reads == ["flaky", "flaky"]
    assert (await stored(db, order.id)).retry_count == 2

    await rewind_last_attempt(db, order.id, 31)
    await run_task()
    assert len(broker.order_reads) == 2, "the second wait is 60 seconds, not 30"

    await rewind_last_attempt(db, order.id, 30)
    await run_task()
    assert len(broker.order_reads) == 3
    assert (await stored(db, order.id)).retry_count == 3


async def test_every_failed_read_leaves_a_record_of_why_the_order_is_waiting(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    order = await add_order(db, account_scope=scope_of(connection), broker_order_id="flaky")
    brokers.for_user(connection.user_id).responses["flaky"] = T212APIError(503, "secret-body")

    await run_task()

    events = await _backoff_events(db, order.id)
    assert len(events) == 1
    payload = dict(events[0].payload or {})
    assert payload["outcome"] == "broker_read_failed"
    assert payload["error_type"] == "T212APIError"
    assert payload["attempts_in_a_row"] == 1
    assert payload["wait_seconds"] == 30
    assert "secret-body" not in str(payload)
    assert events[0].from_status == events[0].to_status == "accepted"


async def test_the_wait_stops_growing_at_fifteen_minutes(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    order = await add_order(
        db,
        account_scope=scope_of(connection),
        broker_order_id="flaky",
        retry_count=40,
        last_reconciled_at=datetime.now(UTC) - timedelta(seconds=901),
    )
    broker = brokers.for_user(connection.user_id)
    broker.responses["flaky"] = T212APIError(503, "unavailable")

    await run_task()

    assert broker.order_reads == ["flaky"], "an order at the ceiling is still retried"
    events = await _backoff_events(db, order.id)
    assert dict(events[0].payload or {})["wait_seconds"] == 900


async def test_an_answer_from_the_broker_ends_the_wait(db: AsyncSession, brokers: Brokers) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    order = await add_order(
        db,
        account_scope=scope_of(connection),
        broker_order_id="recovered",
        retry_count=3,
        last_reconciled_at=datetime.now(UTC) - timedelta(seconds=121),
    )

    await run_task()
    await run_task()

    assert brokers.all_order_reads == ["recovered", "recovered"]
    assert (await stored(db, order.id)).retry_count == 0


async def test_a_fill_is_recorded_and_the_order_leaves_the_queue(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    order = await add_order(db, account_scope=scope_of(connection), broker_order_id="done")
    brokers.for_user(connection.user_id).responses["done"] = filled()

    first = await run_task()
    second = await run_task()

    assert (await stored(db, order.id)).status == "filled"
    assert brokers.all_order_reads == ["done"]
    assert first["reconciled"] == 1
    assert second == {"reconciled": 0}


async def test_a_rate_limit_stops_that_account_for_the_run_and_the_wait_covers_retry_after(
    db: AsyncSession, brokers: Brokers
) -> None:
    limited = await add_connection(db, account_id="ACC-1", created_at=T0)
    other = await add_connection(db, account_id="ACC-2", created_at=T0 + timedelta(minutes=1))
    first = await add_order(
        db, account_scope=scope_of(limited), broker_order_id="limited-1", created_at=T0
    )
    second = await add_order(
        db,
        account_scope=scope_of(limited),
        broker_order_id="limited-2",
        created_at=T0 + timedelta(seconds=1),
    )
    await add_order(
        db,
        account_scope=scope_of(other),
        broker_order_id="other-1",
        created_at=T0 + timedelta(seconds=2),
    )
    brokers.for_user(limited.user_id).responses["limited-1"] = T212RateLimitError(100.0)

    summary = await run_task()

    assert brokers.for_user(limited.user_id).order_reads == ["limited-1"]
    assert brokers.for_user(other.user_id).order_reads == ["other-1"]
    waiting = await stored(db, first.id)
    assert waiting.retry_count == 3, "100 seconds needs the 120 second step"
    untouched = await stored(db, second.id)
    assert untouched.retry_count == 0
    assert untouched.last_reconciled_at is None
    assert summary["rate_limited"] == 1
    assert summary["deferred"] == 1
    assert summary["reconciled"] == 1


@pytest.mark.parametrize(
    "retry_after", [float("inf"), float("nan"), -5.0, 0.0, 10_000_000.0, "soon", None]
)
async def test_an_unusable_retry_after_neither_removes_nor_unbounds_the_wait(
    db: AsyncSession, brokers: Brokers, retry_after: object
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    order = await add_order(db, account_scope=scope_of(connection), broker_order_id="limited")
    error = T212RateLimitError(60.0)
    error.retry_after = retry_after  # type: ignore[assignment]
    brokers.for_user(connection.user_id).responses["limited"] = error

    await run_task()

    waiting = await stored(db, order.id)
    assert 1 <= waiting.retry_count <= 6
    payload = dict((await _backoff_events(db, order.id))[0].payload or {})
    assert 30 <= payload["wait_seconds"] <= 900
    if retry_after == 10_000_000.0:
        assert payload["wait_seconds"] == 900
    else:
        assert payload["wait_seconds"] == 30


async def test_rejected_credentials_stop_that_account_for_the_run(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    for index in range(4):
        await add_order(
            db,
            account_scope=scope,
            broker_order_id=f"o-{index}",
            created_at=T0 + timedelta(seconds=index),
        )
    brokers.for_user(connection.user_id).default_response = T212AuthError(401, "nope")

    summary = await run_task()

    assert brokers.all_order_reads == ["o-0"]
    assert summary["stopped"] == {scope: "broker_auth_failed"}
    assert summary["deferred"] == 3
    assert await connection_is_active(db, connection), "one refusal must not disconnect it"


async def test_repeated_broker_failures_stop_that_account_for_the_run(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    for index in range(8):
        await add_order(
            db,
            account_scope=scope,
            broker_order_id=f"o-{index}",
            created_at=T0 + timedelta(seconds=index),
        )
    brokers.for_user(connection.user_id).default_response = T212APIError(503, "down")

    summary = await run_task()

    assert brokers.all_order_reads == ["o-0", "o-1", "o-2"]
    assert summary["stopped"] == {scope: "broker_unavailable"}
    assert summary["failed"] == 3
    assert summary["deferred"] == 5


async def test_an_account_in_trouble_does_not_take_the_places_of_another_account(
    db: AsyncSession, brokers: Brokers
) -> None:
    troubled = await add_connection(db, account_id="ACC-1", created_at=T0)
    healthy = await add_connection(db, account_id="ACC-2", created_at=T0 + timedelta(minutes=1))
    for index in range(BATCH + 10):
        await add_order(
            db,
            account_scope=scope_of(troubled),
            broker_order_id=f"troubled-{index:02d}",
            created_at=T0 + timedelta(seconds=index),
            commit=False,
        )
    for index in range(10):
        await add_order(
            db,
            account_scope=scope_of(healthy),
            broker_order_id=f"healthy-{index}",
            created_at=T0 + timedelta(hours=1, seconds=index),
            commit=False,
        )
    await db.commit()
    brokers.for_user(troubled.user_id).default_response = T212AuthError(401, "revoked")

    summary = await run_task()

    assert brokers.for_user(troubled.user_id).order_reads == ["troubled-00"]
    assert brokers.for_user(healthy.user_id).order_reads == [f"healthy-{i}" for i in range(10)]
    assert summary["reconciled"] == 10
    assert summary["deferred"] == BATCH - 1
    assert summary["left_for_next_run"] == 10


async def test_two_accounts_with_backlogs_share_a_run_in_turn(
    db: AsyncSession, brokers: Brokers
) -> None:
    first = await add_connection(db, account_id="ACC-1", created_at=T0)
    second = await add_connection(db, account_id="ACC-2", created_at=T0 + timedelta(minutes=1))
    for index in range(40):
        for name, connection in (("a", first), ("b", second)):
            await add_order(
                db,
                account_scope=scope_of(connection),
                broker_order_id=f"{name}-{index:02d}",
                created_at=T0 + timedelta(seconds=index),
                commit=False,
            )
    await db.commit()

    summary = await run_task()

    assert brokers.for_user(first.user_id).order_reads == [f"a-{i:02d}" for i in range(25)]
    assert brokers.for_user(second.user_id).order_reads == [f"b-{i:02d}" for i in range(25)]
    assert summary["reconciled"] == BATCH
    assert summary["left_for_next_run"] == 30


async def test_orders_the_broker_does_not_know_wait_but_do_not_stop_the_account(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    gone = [
        await add_order(
            db,
            account_scope=scope,
            broker_order_id=f"gone-{index}",
            created_at=T0 + timedelta(seconds=index),
        )
        for index in range(5)
    ]
    await add_order(
        db, account_scope=scope, broker_order_id="working", created_at=T0 + timedelta(minutes=1)
    )
    broker = brokers.for_user(connection.user_id)
    for index in range(5):
        broker.responses[f"gone-{index}"] = T212APIError(404, "not found")

    summary = await run_task()

    assert broker.order_reads == [*(f"gone-{index}" for index in range(5)), "working"]
    assert "stopped" not in summary
    # "No such open order" is an answer, not a failed read: the history is asked next,
    # and an order found in neither is counted as having no record, not as a failure.
    assert "failed" not in summary
    assert summary["no_broker_record"] == 5
    assert summary["reconciled"] == 1
    assert (await stored(db, gone[0].id)).retry_count == 1
    payload = dict((await _backoff_events(db, gone[0].id))[0].payload or {})
    assert payload["outcome"] == "no_broker_record"
    assert payload["error_type"] is None


@pytest.mark.parametrize(
    ("level", "wait"), [(1, 30), (2, 60), (3, 120), (4, 240), (5, 480), (6, 900), (9, 900)]
)
async def test_each_level_waits_its_own_time_and_no_longer(
    db: AsyncSession, brokers: Brokers, level: int, wait: int
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    now = datetime.now(UTC)
    await add_order(
        db,
        account_scope=scope,
        broker_order_id="still-waiting",
        retry_count=level,
        last_reconciled_at=now - timedelta(seconds=wait - 10),
    )
    await add_order(
        db,
        account_scope=scope,
        broker_order_id="wait-over",
        retry_count=level,
        last_reconciled_at=now - timedelta(seconds=wait + 1),
    )

    await run_task()

    assert brokers.all_order_reads == ["wait-over"]


async def test_a_count_with_no_recorded_attempt_does_not_hold_an_order_back(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    await add_order(
        db, account_scope=scope_of(connection), broker_order_id="no-time", retry_count=4
    )

    await run_task()

    assert brokers.all_order_reads == ["no-time"]


async def test_an_attempt_dated_in_the_future_cannot_hold_an_order_back_indefinitely(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    now = datetime.now(UTC)
    far = await add_order(
        db,
        account_scope=scope,
        broker_order_id="far-future",
        retry_count=3,
        last_reconciled_at=now + timedelta(days=30),
    )
    await add_order(
        db,
        account_scope=scope,
        broker_order_id="two-minutes-ahead",
        retry_count=3,
        last_reconciled_at=now + timedelta(minutes=2),
    )
    await add_order(
        db,
        account_scope=scope,
        broker_order_id="clock-a-little-ahead",
        retry_count=3,
        last_reconciled_at=now + timedelta(seconds=10),
    )

    await run_task()

    assert sorted(brokers.all_order_reads) == ["far-future", "two-minutes-ahead"]
    repaired = (await stored(db, far.id)).last_reconciled_at
    assert repaired is not None
    assert repaired.replace(tzinfo=UTC) <= datetime.now(UTC), "still dated in the future"


async def test_a_too_recent_unknown_submission_is_not_counted_as_looked_at(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    await add_order(
        db,
        account_scope=scope_of(connection),
        status="submission_unknown",
        created_at=datetime.now(UTC),
    )

    assert await run_task() == {"reconciled": 0}


async def test_an_unresolved_unknown_submission_waits_and_is_never_sent_again(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    unknown = await add_order(db, account_scope=scope_of(connection), status="submission_unknown")
    broker = brokers.for_user(connection.user_id)

    first = await run_task()
    await run_task()

    assert broker.history_reads == ["AAPL_US_EQ"], "asked again before the wait was over"
    assert broker.write_calls == []
    after = await stored(db, unknown.id)
    assert after.status == "submission_unknown"
    assert after.broker_order_id is None
    assert after.retry_count == 1
    assert first["unknown_submissions"] == 1
    assert first["unknown_submissions_resolved"] == 0

    await rewind_last_attempt(db, unknown.id, 31)
    await run_task()
    assert len(broker.history_reads) == 2
    assert (await stored(db, unknown.id)).status == "submission_unknown"


async def test_an_unknown_submission_too_recent_to_check_is_left_exactly_as_it_is(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    recent = await add_order(
        db,
        account_scope=scope_of(connection),
        status="submission_unknown",
        created_at=datetime.now(UTC),
    )

    await run_task()

    after = await stored(db, recent.id)
    assert brokers.for_user(connection.user_id).history_reads == []
    assert after.retry_count == 0
    assert after.last_reconciled_at is None


async def test_each_order_is_saved_before_the_next_one_is_read(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    first = await add_order(db, account_scope=scope, broker_order_id="first", created_at=T0)
    second = await add_order(
        db, account_scope=scope, broker_order_id="second", created_at=T0 + timedelta(seconds=1)
    )
    broker = brokers.for_user(connection.user_id)
    broker.responses["first"] = filled()
    broker.responses["second"] = SimulatedProcessDeath()

    with pytest.raises(SimulatedProcessDeath):
        await run_task()

    assert (await stored(db, first.id)).status == "filled"
    interrupted = await stored(db, second.id)
    assert interrupted.status == "accepted"
    assert interrupted.retry_count == 0


async def test_a_run_reports_what_it_selected_and_what_it_left(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    for index in range(BATCH + 7):
        await add_order(
            db,
            account_scope=scope,
            broker_order_id=f"o-{index:02d}",
            created_at=T0 + timedelta(seconds=index),
            commit=False,
        )
    await db.commit()

    summary = await run_task()

    assert summary["reconciled"] == BATCH
    assert summary["left_for_next_run"] == 7
