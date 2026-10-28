"""Real PostgreSQL operation ownership, transaction and retained-result proofs."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from app.api.schemas import PaperOrderCreate
from app.db.models import (
    AppSettings,
    AuditLog,
    BrokerAccountSnapshot,
    BrokerConnection,
    Order,
    OrderEvent,
    PaperOperationReceipt,
    PositionSnapshot,
    RiskEvent,
    RiskProfile,
    Trade,
    User,
)
from app.execution.paper_engine import PaperExecutionEngine, PaperExecutionError
from app.execution.paper_operations import PaperOperationService
from tests.integration.test_paper_follow_up_halt import _wait_for_blocker
from tests.integration.test_paper_follow_up_halt import sessions as sessions


async def seed(sessions):
    async with sessions() as db:
        owner = User(
            id=uuid.uuid4(),
            email="operation@test.invalid",
            hashed_password="not-a-login",
            is_active=True,
            is_admin=True,
        )
        db.add_all(
            [
                owner,
                AppSettings(
                    id=1,
                    auto_trading_enabled=False,
                    kill_switch_active=False,
                    live_trading_unlocked=False,
                ),
                RiskProfile(id=uuid.uuid4(), name="Shipped defaults", is_default=True),
            ]
        )
        await db.commit()
        return owner.id


def body(operation=None, **changes):
    values = {
        "operation_id": operation or uuid.uuid4(),
        "ticker": "IDENTITY",
        "side": "buy",
        "quantity": "1",
        "estimated_price": "100",
        "source": "manual_demo_ui",
        "paper_only": True,
    }
    return PaperOrderCreate(**{**values, **changes})


async def submit(sessions, owner, request):
    async with sessions() as db:
        user = await db.get(User, owner)
        return await PaperOperationService(db).submit(request, user=user)


async def snapshot(sessions):
    async with sessions() as db:
        return {
            model.__tablename__: [
                dict(row)
                for row in (await db.execute(select(model.__table__).order_by(model.id))).mappings()
            ]
            for model in (
                Order,
                OrderEvent,
                PositionSnapshot,
                BrokerAccountSnapshot,
                AuditLog,
                RiskEvent,
                RiskProfile,
                Trade,
                BrokerConnection,
                AppSettings,
                PaperOperationReceipt,
            )
        }


@pytest.mark.asyncio
async def test_transaction_prepares_a_valid_result_before_commit(sessions):
    owner = await seed(sessions)
    async with sessions() as db:
        result = await PaperOperationService(db)._submit(body(), owner)
    assert result.status_code == 201


@pytest.mark.asyncio
async def test_committed_response_loss_same_id_concurrency_and_exact_decimal_effect(sessions):
    owner = await seed(sessions)
    request = body()
    results = await asyncio.gather(*(submit(sessions, owner, request) for _ in range(4)))
    assert sorted(row.status_code for row in results) == [200, 200, 200, 201]
    assert len({row.payload["id"] for row in results}) == 1
    before = await snapshot(sessions)
    assert len(before["orders"]) == 1
    assert before["positions_snapshots"][0]["quantity"] == Decimal("1.00000000")
    assert before["broker_accounts_snapshots"][0]["cash"] == Decimal("99899.87998000")
    # Treat every successful response above as lost; retry through a fresh session.
    again = await submit(sessions, owner, request)
    assert again.status_code == 200 and again.payload["replayed"] is True
    assert await snapshot(sessions) == before


@pytest.mark.asyncio
async def test_mismatch_refused_and_receipt_get_is_owner_only_without_writes(sessions):
    owner = await seed(sessions)
    request = body()
    await submit(sessions, owner, request)
    before = await snapshot(sessions)
    assert (
        await submit(sessions, owner, body(request.operation_id, quantity="2"))
    ).status_code == 409
    async with sessions() as db:
        service = PaperOperationService(db)
        assert (await service.lookup(uuid.uuid4(), request.operation_id)).status_code == 404
        found = await service.lookup(owner, request.operation_id)
        assert found.status_code == 200 and found.payload["order"]["status"] == "filled"
        assert not db.new and not db.dirty and not db.deleted
    assert await snapshot(sessions) == before


@pytest.mark.asyncio
async def test_kill_after_commit_allows_readonly_replay_but_new_intent_gets_durable_refusal(
    sessions,
):
    owner = await seed(sessions)
    request = body()
    first = await submit(sessions, owner, request)
    async with sessions() as db:
        await db.execute(update(AppSettings).values(kill_switch_active=True))
        await db.commit()
    before = await snapshot(sessions)
    replay = await submit(sessions, owner, request)
    assert replay.status_code == 200 and replay.payload["id"] == first.payload["id"]
    assert await snapshot(sessions) == before
    blocked = body()
    refused = await submit(sessions, owner, blocked)
    assert refused.status_code == 422 and refused.payload["decision_code"] == "kill_switch_block"
    assert refused.payload["operation_state"] == "recorded"
    after = await snapshot(sessions)
    assert len(after["orders"]) == 1 and len(after["paper_operation_receipts"]) == 2
    again = await submit(sessions, owner, blocked)
    assert again.status_code == 422 and again.payload["replayed"] is True
    assert await snapshot(sessions) == after


@pytest.mark.asyncio
async def test_late_error_after_staged_order_rolls_back_every_caller_effect(sessions, monkeypatch):
    owner = await seed(sessions)
    before = await snapshot(sessions)
    original = PaperExecutionEngine._apply_fill_effects

    async def late_error(self, **kwargs):
        await original(self, **kwargs)
        raise PaperExecutionError("synthetic late failure")

    with monkeypatch.context() as patch:
        patch.setattr(PaperExecutionEngine, "_apply_fill_effects", late_error)
        result = await submit(sessions, owner, body())
    assert result.status_code == 503
    assert await snapshot(sessions) == before


@pytest.mark.asyncio
async def test_commit_acknowledgement_loss_reconciles_without_execution(sessions, monkeypatch):
    owner = await seed(sessions)
    async with sessions() as db:
        user = await db.get(User, owner)
        commit = db.commit

        async def lost_ack():
            await commit()
            raise ConnectionError("synthetic acknowledgement loss")

        monkeypatch.setattr(db, "commit", lost_ack)
        result = await PaperOperationService(db).submit(body(), user=user)
    assert result.status_code == 200 and result.payload["replayed"] is True
    state = await snapshot(sessions)
    assert len(state["orders"]) == len(state["paper_operation_receipts"]) == 1


@pytest.mark.asyncio
async def test_cancel_before_commit_has_no_receipt_or_effect_then_same_identity_can_retry(
    sessions, monkeypatch
):
    owner = await seed(sessions)
    request = body()
    entered = asyncio.Event()
    async with sessions() as db:
        user = await db.get(User, owner)

        async def interrupted_commit():
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(db, "commit", interrupted_commit)
        pending = asyncio.create_task(PaperOperationService(db).submit(request, user=user))
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    state = await snapshot(sessions)
    assert not state["orders"] and not state["paper_operation_receipts"]
    assert (await submit(sessions, owner, request)).status_code == 201


@pytest.mark.asyncio
async def test_legacy_connection_then_user_audit_fk_does_not_deadlock_identified_mutex(sessions):
    owner = await seed(sessions)
    async with sessions() as setup:
        await PaperExecutionEngine(setup)._paper_connection(await setup.get(User, owner))
        await setup.commit()
    contender_pid = asyncio.get_running_loop().create_future()

    async def identified():
        async with sessions() as db:
            user = await db.get(User, owner)
            contender_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            return await PaperOperationService(db).submit(body(), user=user)

    async with sessions() as legacy:
        await PaperExecutionEngine(legacy)._lock_paper_connection(await legacy.get(User, owner))
        blocker = await legacy.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(identified())
        try:
            await _wait_for_blocker(sessions, await contender_pid, blocker, pending)
            legacy.add(AuditLog(user_id=owner, action="synthetic_legacy_audit", actor="synthetic"))
            async with asyncio.timeout(1):
                await legacy.flush()  # KEY SHARE must be compatible with identified User mutex
                await legacy.commit()
            assert (await pending).status_code == 201
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_halt_committed_first_blocks_waiting_identified_command(sessions):
    owner = await seed(sessions)
    contender_pid = asyncio.get_running_loop().create_future()

    async def identified():
        async with sessions() as db:
            user = await db.get(User, owner)
            contender_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            return await PaperOperationService(db).submit(body(), user=user)

    async with sessions() as halt:
        await halt.execute(update(AppSettings).values(kill_switch_active=True))
        blocker = await halt.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(identified())
        try:
            await _wait_for_blocker(sessions, await contender_pid, blocker, pending)
            await halt.commit()
            assert (await pending).status_code == 422
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    state = await snapshot(sessions)
    assert not state["orders"] and len(state["paper_operation_receipts"]) == 1


@pytest.mark.asyncio
async def test_retention_restricts_order_and_user_deletion(sessions):
    owner = await seed(sessions)
    result = await submit(sessions, owner, body())
    for model, identity in [(Order, uuid.UUID(result.payload["id"])), (User, owner)]:
        async with sessions() as db:
            with pytest.raises(IntegrityError):
                await db.execute(delete(model).where(model.id == identity))
                await db.commit()
            await db.rollback()
    state = await snapshot(sessions)
    assert len(state["orders"]) == len(state["paper_operation_receipts"]) == 1


@pytest.mark.asyncio
async def test_same_uuid_for_two_authenticated_owners_has_separate_receipts(sessions):
    owner = await seed(sessions)
    async with sessions() as db:
        other = User(
            id=uuid.uuid4(),
            email="second@test.invalid",
            hashed_password="not-a-login",
            is_active=True,
            is_admin=True,
        )
        db.add(other)
        await db.commit()
        other_id = other.id
    request = body()
    first = await submit(sessions, owner, request)
    second = await submit(sessions, other_id, request)
    assert first.status_code == second.status_code == 201
    assert first.payload["id"] != second.payload["id"]
    async with sessions() as db:
        found = await PaperOperationService(db).lookup(other_id, request.operation_id)
        assert found.payload["order"]["id"] == second.payload["id"]


@pytest.mark.asyncio
async def test_identified_safety_lock_finishes_before_waiting_halt(sessions, monkeypatch):
    owner = await seed(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    original = PaperExecutionEngine._apply_fill_effects
    submit_pid = asyncio.get_running_loop().create_future()
    halt_pid = asyncio.get_running_loop().create_future()

    async def hold_effects(self, **kwargs):
        await original(self, **kwargs)
        entered.set()
        await release.wait()

    async def identified():
        async with sessions() as db:
            user = await db.get(User, owner)
            submit_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            return await PaperOperationService(db).submit(body(), user=user)

    async def halt():
        async with sessions() as db:
            halt_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            await db.execute(update(AppSettings).values(kill_switch_active=True))
            await db.commit()

    monkeypatch.setattr(PaperExecutionEngine, "_apply_fill_effects", hold_effects)
    pending = asyncio.create_task(identified())
    await asyncio.wait_for(entered.wait(), 5)
    waiting = asyncio.create_task(halt())
    try:
        await _wait_for_blocker(sessions, await halt_pid, await submit_pid, waiting)
        release.set()
        assert (await pending).status_code == 201
        await waiting
    finally:
        release.set()
        for task in (pending, waiting):
            if not task.done():
                task.cancel()
        await asyncio.gather(pending, waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_lock_wait_timeout_rolls_back_and_same_identity_later_succeeds(sessions):
    owner = await seed(sessions)
    request = body()
    async with sessions() as holder:
        await holder.execute(select(User).where(User.id == owner).with_for_update())
        result = await submit(sessions, owner, request)
        assert result.status_code == 503
        assert not (await snapshot(sessions))["orders"]
        await holder.rollback()
    assert (await submit(sessions, owner, request)).status_code == 201


@pytest.mark.asyncio
async def test_real_deadlock_victim_rolls_back_and_retry_has_one_effect(sessions):
    owner = await seed(sessions)
    request = body()
    async with sessions() as setup:
        await PaperExecutionEngine(setup)._paper_connection(await setup.get(User, owner))
        await setup.commit()
    contender_pid = asyncio.get_running_loop().create_future()

    async def identified():
        async with sessions() as db:
            user = await db.get(User, owner)
            contender_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            return await PaperOperationService(db).submit(request, user=user)

    async with sessions() as incompatible_writer:
        await PaperExecutionEngine(incompatible_writer)._lock_paper_connection(
            await incompatible_writer.get(User, owner)
        )
        blocker = await incompatible_writer.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(identified())
        try:
            await _wait_for_blocker(sessions, await contender_pid, blocker, pending)
            # Deliberately incompatible external writer, unlike the compatible
            # legacy audit path above. PostgreSQL must break this real lock cycle.
            try:
                await incompatible_writer.execute(
                    select(User).where(User.id == owner).with_for_update()
                )
            except Exception as error:
                assert "deadlock" in str(error).lower()
            finally:
                await incompatible_writer.rollback()
            result = await pending
            assert result.status_code in (201, 503)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert (await submit(sessions, owner, request)).status_code in (200, 201)
    state = await snapshot(sessions)
    assert len(state["orders"]) == len(state["paper_operation_receipts"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_downgrade_preserves_nonempty_order_or_blocked_receipt(
    sessions, monkeypatch, blocked
):
    owner = await seed(sessions)
    if blocked:
        async with sessions() as db:
            await db.execute(update(AppSettings).values(kill_switch_active=True))
            await db.commit()
    assert (await submit(sessions, owner, body())).status_code == (422 if blocked else 201)
    before = await snapshot(sessions)
    migration = importlib.import_module("app.db.migrations.versions.0022_paper_operation_receipts")
    async with sessions() as db:
        connection = await db.connection()
        schema = connection.sync_connection.get_execution_options()["schema_translate_map"][None]
        await db.execute(text(f'SET LOCAL search_path TO "{schema}"'))

        def downgrade(sync):
            monkeypatch.setattr(migration.op, "get_bind", lambda: sync)
            migration.downgrade()

        with pytest.raises(RuntimeError, match="retained paper operation"):
            await connection.run_sync(downgrade)
        await db.rollback()
    assert await snapshot(sessions) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile,status", [("partial_fill", "partially_filled"), ("no_liquidity", "rejected")]
)
async def test_replay_preserves_actual_partial_or_rejected_result_without_new_effects(
    sessions, profile, status
):
    owner = await seed(sessions)
    request = body(quantity="2", simulation_profile=profile)
    first = await submit(sessions, owner, request)
    assert first.status_code == 201 and first.payload["status"] == status
    before = await snapshot(sessions)
    replay = await submit(sessions, owner, request)
    assert replay.status_code == 200 and replay.payload["status"] == status
    assert replay.payload["id"] == first.payload["id"]
    assert await snapshot(sessions) == before


@pytest.mark.asyncio
async def test_first_use_portfolio_and_different_identified_operations_share_one_account(sessions):
    owner = await seed(sessions)

    async def portfolio():
        async with sessions() as db:
            await PaperExecutionEngine(db).portfolio_state(await db.get(User, owner))
            await db.commit()

    first, second, _ = await asyncio.gather(
        submit(sessions, owner, body()), submit(sessions, owner, body()), portfolio()
    )
    assert first.status_code == second.status_code == 201
    assert first.payload["id"] != second.payload["id"]
    state = await snapshot(sessions)
    assert len(state["broker_connections"]) == 1
    assert len(state["orders"]) == len(state["paper_operation_receipts"]) == 2
    latest = sorted(state["positions_snapshots"], key=lambda row: row["snapshotted_at"])[-1]
    assert latest["quantity"] == Decimal("2.00000000")


@pytest.mark.asyncio
async def test_downgrade_waits_for_inflight_receipt_then_refuses(sessions, monkeypatch):
    owner = await seed(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    original_pid = asyncio.get_running_loop().create_future()
    down_pid = asyncio.get_running_loop().create_future()
    migration = importlib.import_module("app.db.migrations.versions.0022_paper_operation_receipts")

    async def identified():
        async with sessions() as db:
            user = await db.get(User, owner)
            original_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            commit = db.commit

            async def paused_commit():
                entered.set()
                await release.wait()
                await commit()

            monkeypatch.setattr(db, "commit", paused_commit)
            return await PaperOperationService(db).submit(body(), user=user)

    async def downgrade():
        async with sessions() as db:
            connection = await db.connection()
            schema = connection.sync_connection.get_execution_options()["schema_translate_map"][
                None
            ]
            await db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            down_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))

            def attempt(sync):
                monkeypatch.setattr(migration.op, "get_bind", lambda: sync)
                migration.downgrade()

            with pytest.raises(RuntimeError, match="retained paper operation"):
                await connection.run_sync(attempt)
            await db.rollback()

    pending = asyncio.create_task(identified())
    await asyncio.wait_for(entered.wait(), 5)
    dropping = asyncio.create_task(downgrade())
    try:
        await _wait_for_blocker(sessions, await down_pid, await original_pid, dropping)
        release.set()
        assert (await pending).status_code == 201
        await dropping
    finally:
        release.set()
        for task in (pending, dropping):
            if not task.done():
                task.cancel()
        await asyncio.gather(pending, dropping, return_exceptions=True)
    assert len((await snapshot(sessions))["paper_operation_receipts"]) == 1


@pytest.mark.asyncio
async def test_process_crash_before_commit_rolls_back_all_effects(sessions):
    owner = await seed(sessions)
    request = body()
    async with sessions() as db:
        schema = (await db.connection()).sync_connection.get_execution_options()[
            "schema_translate_map"
        ][None]
    code = """
import asyncio, os, uuid
from tests import network_guard
network_guard.install()
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.api.schemas import PaperOrderCreate
from app.db.models import User
from app.execution.paper_operations import PaperOperationService
async def main():
    engine=create_async_engine(os.environ['POSTGRES_TEST_DATABASE_URL']).execution_options(schema_translate_map={None:os.environ['PAPER_TEST_SCHEMA']})
    async with async_sessionmaker(engine,expire_on_commit=False)() as db:
        user=await db.get(User,uuid.UUID(os.environ['PAPER_TEST_OWNER']))
        async def pause():
            print('PAPER_PRECOMMIT_READY',flush=True)
            await asyncio.Event().wait()
        db.commit=pause
        await PaperOperationService(db).submit(PaperOrderCreate(operation_id=uuid.UUID(os.environ['PAPER_TEST_OPERATION']),ticker='IDENTITY',side='buy',quantity='1',estimated_price='100',source='manual_demo_ui'),user=user)
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        env={
            **os.environ,
            "DATABASE_URL": os.environ["POSTGRES_TEST_DATABASE_URL"],
            "PAPER_TEST_SCHEMA": schema,
            "PAPER_TEST_OWNER": str(owner),
            "PAPER_TEST_OPERATION": str(request.operation_id),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 8) == b"PAPER_PRECOMMIT_READY\n"
        child.kill()
        assert await child.wait() == -9
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()
    state = await snapshot(sessions)
    for table in (
        "orders",
        "order_events",
        "positions_snapshots",
        "broker_accounts_snapshots",
        "audit_logs",
        "paper_operation_receipts",
        "broker_connections",
        "risk_events",
    ):
        assert not state[table]
    assert (await submit(sessions, owner, request)).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "pending"},
        {"outcome": "order_recorded", "order_id": None},
        {"decision_code": None},
        {"detail": None},
        {"http_status": 201},
        {"request_version": 2},
        {"user_id": uuid.UUID(int=0)},
    ],
)
async def test_database_rejects_incomplete_or_unowned_terminal_receipts(sessions, changes):
    owner = await seed(sessions)
    async with sessions() as db:
        row = {
            "id": uuid.uuid4(),
            "user_id": owner,
            "operation_id": uuid.uuid4(),
            "account_domain": f"paper:mock:user:{owner}",
            "request_version": 1,
            "canonical_request": {},
            "request_digest": "0" * 64,
            "outcome": "blocked",
            "http_status": 422,
            "decision_code": "synthetic_block",
            "detail": "Synthetic blocked request",
            "order_id": None,
        }
        db.add(PaperOperationReceipt(**{**row, **changes}))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
    assert not (await snapshot(sessions))["paper_operation_receipts"]
