"""Preserve dispatch history using real Alembic in disposable PostgreSQL databases."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from tests.integration.test_eod_downgrade_preservation import (
    _migration,
    _snapshot,
)
from tests.integration.test_eod_downgrade_preservation import (
    migrated_db as migrated_db,
)


async def _order(connection, status):
    order_id = uuid.uuid4()
    await connection.execute(
        text(
            "INSERT INTO orders (id, client_order_key, ticker, side, order_type, "
            "quantity, status) VALUES (:id, :key, 'HISTORY', 'buy', 'market', 2, :status)"
        ),
        {"id": order_id, "key": str(order_id), "status": status},
    )
    return order_id


async def _attempt(connection, order_id, outcome):
    await connection.execute(
        text(
            "INSERT INTO order_submission_attempts "
            "(id, order_id, request_fingerprint, execution_environment, "
            "broker_environment, broker_account_scope, expected_cost, "
            "dispatch_committed_at, outcome, outcome_recorded_at, outcome_evidence) "
            "VALUES (:id, :order_id, :fingerprint, 'mock', 'mock', 'synthetic-history', "
            "200, CURRENT_TIMESTAMP, :outcome, CURRENT_TIMESTAMP, "
            '\'{"durable_evidence": "preserve verbatim"}\')'
        ),
        {
            "id": uuid.uuid4(),
            "order_id": order_id,
            "fingerprint": "a" * 64,
            "outcome": outcome,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["0020_eod_flatten_operations", "0019_strategy_version"])
@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("accepted", "accepted"),
        ("filled", "accepted"),
        ("cancelled", "accepted"),
        ("rejected", "rejected"),
        ("error", "not_transmitted"),
        ("partially_filled", None),
        ("future_unknown_state", "future_unknown_outcome"),
    ],
)
async def test_downgrade_preserves_all_submission_attempt_history(
    migrated_db, target, status, outcome
):
    url, engine = migrated_db
    async with engine.begin() as connection:
        order_id = await _order(connection, status)
        await _attempt(connection, order_id, outcome)
    before = await _snapshot(engine)

    code, output = await _migration(url, "downgrade", target)

    assert code != 0, "downgrade discarded durable submission history"
    assert "Refusing to downgrade" in output, output
    assert "submission attempt history" in output, output
    assert await _snapshot(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("with_attempt", [False, True])
async def test_unknown_order_still_blocks_downgrade(migrated_db, with_attempt):
    url, engine = migrated_db
    async with engine.begin() as connection:
        order_id = await _order(connection, "submission_unknown")
        if with_attempt:
            await _attempt(connection, order_id, None)
    before = await _snapshot(engine)

    code, output = await _migration(url, "downgrade", "0020_eod_flatten_operations")

    assert code != 0
    assert "still have an unknown submission outcome" in output, output
    assert await _snapshot(engine) == before


@pytest.mark.asyncio
async def test_empty_submission_history_can_round_trip(migrated_db):
    url, engine = migrated_db
    code, output = await _migration(url, "downgrade", "0020_eod_flatten_operations")
    assert code == 0, output
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT to_regclass('order_submission_attempts')")) is None
        )
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0020_eod_flatten_operations"
        )
    code, output = await _migration(url, "upgrade", "head")
    assert code == 0, output
    state = await _snapshot(engine)
    assert state["order_submission_attempts"] == []
    assert state["alembic_version"] == [{"version_num": "0022_paper_operation_receipts"}]


@pytest.mark.asyncio
async def test_downgrade_observes_in_flight_submission_history(migrated_db):
    url, engine = migrated_db
    async with engine.begin() as setup:
        order_id = await _order(setup, "filled")
    expected = await _snapshot(engine)

    async with engine.connect() as writer:
        transaction = await writer.begin()
        await _attempt(writer, order_id, "accepted")
        expected["order_submission_attempts"] = (
            (await writer.execute(text("SELECT to_jsonb(t) FROM order_submission_attempts t")))
            .scalars()
            .all()
        )
        writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(_migration(url, "downgrade", "0020_eod_flatten_operations"))
        try:
            async with engine.connect() as observer:
                observer = await observer.execution_options(isolation_level="AUTOCOMMIT")
                async with asyncio.timeout(10):
                    while True:
                        blocked = await observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE :writer = ANY(pg_blocking_pids(pid)))"
                            ),
                            {"writer": writer_pid},
                        )
                        if blocked:
                            break
                        assert not task.done(), "downgrade bypassed the in-flight writer"
                        await asyncio.sleep(0.05)
            await transaction.commit()
            code, output = await task
            assert code != 0, "downgrade dropped history committed while waiting for the writer"
            assert "submission attempt history" in output, output
            assert await _snapshot(engine) == expected
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await task
