"""Exercise real Alembic commands only in newly created disposable databases."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

API_ROOT = Path(__file__).resolve().parents[2]


async def _migration(url, direction, target):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        direction,
        target,
        cwd=API_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=40)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return process.returncode, output.decode()


@pytest_asyncio.fixture
async def migrated_db():
    source = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if not source:
        pytest.skip("POSTGRES_TEST_DATABASE_URL required for real migration proof")
    # Never migrate the supplied database. Only create/drop this random test database.
    database = f"eod_migration_test_{uuid.uuid4().hex}"
    admin = create_async_engine(source, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    url = make_url(source).set(database=database).render_as_string(hide_password=False)
    engine = create_async_engine(url, poolclass=NullPool)
    created = False
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
        created = True
        code, output = await _migration(url, "upgrade", "head")
        assert code == 0, output
        yield url, engine
    finally:
        await engine.dispose()
        if created:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE "{database}"'))
        await admin.dispose()


async def _seed(connection, status, *, order_status=None, scoped_order=False):
    strategy_id, order_id, operation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await connection.execute(
        text("INSERT INTO strategies (id, name, type) VALUES (:id, 'synthetic', 'orb')"),
        {"id": strategy_id},
    )
    if order_status:
        await connection.execute(
            text(
                "INSERT INTO orders (id, client_order_key, ticker, side, order_type, "
                "quantity, status, broker_account_scope) "
                "VALUES (:id, :key, 'TEST', 'sell', 'market', 2, :status, :scope)"
            ),
            {
                "id": order_id,
                "key": str(order_id),
                "status": order_status,
                "scope": "synthetic-account" if scoped_order else None,
            },
        )
    if status:
        await connection.execute(
            text(
                "INSERT INTO eod_flatten_operations "
                "(id, strategy_id, venue, exchange, execution_environment, "
                "broker_account_scope, exchange_session_date, ticker, "
                "attributable_quantity, order_id, status, requires_manual_reconciliation, "
                "details) VALUES (:id, :strategy, 'test', 'TEST', 'paper', "
                "'synthetic-account', '2026-10-09', 'TEST', 2, :order, :status, :manual, "
                '\'{"recovery_evidence": "preserve verbatim"}\')'
            ),
            {
                "id": operation_id,
                "strategy": strategy_id,
                "order": order_id if order_status else None,
                "status": status,
                "manual": status == "manual_reconciliation_required",
            },
        )


async def _snapshot(engine):
    async with engine.connect() as connection:
        state = {
            table: (await connection.execute(text(f"SELECT to_jsonb(t) FROM {table} t")))
            .scalars()
            .all()
            for table in (
                "alembic_version",
                "orders",
                "eod_flatten_operations",
                "order_submission_attempts",
            )
        }
        state["columns"] = (
            (
                await connection.execute(
                    text(
                        "SELECT table_name, column_name, data_type, is_nullable "
                        "FROM information_schema.columns WHERE table_schema = 'public' "
                        "ORDER BY table_name, ordinal_position"
                    )
                )
            )
            .tuples()
            .all()
        )
        state["indexes"] = (
            (
                await connection.execute(
                    text(
                        "SELECT tablename, indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = 'public' ORDER BY tablename, indexname"
                    )
                )
            )
            .tuples()
            .all()
        )
        return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "order_status", "scoped_order"),
    [
        ("claimed", None, False),
        ("intent_persisted", "accepted", False),
        ("submission_pending", "cancelled", False),
        ("manual_reconciliation_required", "error", False),
        ("future_unknown_state", None, False),
        ("completed", "filled", False),
        (None, "filled", True),
    ],
)
async def test_downgrade_preserves_eod_history_and_account_identity(
    migrated_db, status, order_status, scoped_order
):
    url, engine = migrated_db
    async with engine.begin() as connection:
        await _seed(connection, status, order_status=order_status, scoped_order=scoped_order)
    before = await _snapshot(engine)
    code, output = await _migration(url, "downgrade", "0019_strategy_version")
    assert code != 0, "downgrade discarded durable financial evidence"
    assert "Refusing to downgrade" in output, output
    assert await _snapshot(engine) == before


@pytest.mark.asyncio
async def test_empty_database_can_round_trip(migrated_db):
    url, engine = migrated_db
    for direction, target in (("downgrade", "base"), ("upgrade", "head")):
        code, output = await _migration(url, direction, target)
        assert code == 0, output
    state = await _snapshot(engine)
    assert state["alembic_version"] == [{"version_num": "0021_order_submission_attempts"}]
    assert state["orders"] == state["eod_flatten_operations"] == []


@pytest.mark.asyncio
async def test_downgrade_observes_in_flight_eod_record_before_dropping(migrated_db):
    url, engine = migrated_db
    async with engine.connect() as writer:
        transaction = await writer.begin()
        await _seed(writer, "submission_pending")
        writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(_migration(url, "downgrade", "0019_strategy_version"))
        try:
            async with engine.connect() as observer:
                observer = await observer.execution_options(isolation_level="AUTOCOMMIT")
                for _ in range(200):
                    blocked = await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE :writer = ANY(pg_blocking_pids(pid)))"
                        ),
                        {"writer": writer_pid},
                    )
                    if blocked:
                        break
                    assert not task.done(), "downgrade did not wait for pending evidence"
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("did not observe PostgreSQL lock contention")
            await transaction.commit()
            code, output = await task
            assert code != 0, "downgrade dropped an EOD record committed while it waited"
            assert "Refusing to downgrade" in output, output
            state = await _snapshot(engine)
            assert state["eod_flatten_operations"][0]["status"] == "submission_pending"
            assert state["alembic_version"] == [{"version_num": "0021_order_submission_attempts"}]
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await task
