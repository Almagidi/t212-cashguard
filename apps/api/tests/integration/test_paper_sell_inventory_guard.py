"""Paper sell fills must consume current holdings, never fabricate proceeds."""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import (
    AuditLog,
    BrokerAccountSnapshot,
    Order,
    OrderEvent,
    PositionSnapshot,
    Trade,
    User,
)
from app.db.session import Base
from app.execution.paper_engine import PaperExecutionEngine, PaperExecutionError
from tests.integration.test_paper_execution_realism import _body, _seed_user_and_settings


@pytest_asyncio.fixture
async def postgres_sessions():
    url = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL required for real PostgreSQL inventory proof")
    # A unique, test-owned schema preserves all other suites' data.
    schema = f"paper_sell_{uuid.uuid4().hex}"
    engine = create_async_engine(url, poolclass=NullPool)
    scoped = engine.execution_options(schema_translate_map={None: schema})
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with scoped.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(scoped, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def ledger(request, db):
    if request.param == "sqlite":
        yield db
    else:
        # Requesting the async fixture dynamically is unsupported; use a local
        # transaction on the migrated, explicitly supplied synthetic database.
        url = os.getenv("POSTGRES_TEST_DATABASE_URL")
        if not url:
            pytest.skip("POSTGRES_TEST_DATABASE_URL required for PostgreSQL inventory proof")
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                yield session
                await session.rollback()
        finally:
            await engine.dispose()


async def _financial_state(db, order):
    await db.flush()
    await db.refresh(order)
    counts = tuple(
        [
            await db.scalar(select(func.count()).select_from(model))
            for model in (Order, Trade, PositionSnapshot, BrokerAccountSnapshot, OrderEvent)
        ]
    )
    accounts = (
        (await db.execute(select(BrokerAccountSnapshot).order_by(BrokerAccountSnapshot.id)))
        .scalars()
        .all()
    )
    positions = (
        (await db.execute(select(PositionSnapshot).order_by(PositionSnapshot.id))).scalars().all()
    )
    return (
        counts,
        tuple((a.id, a.cash, a.total_value) for a in accounts),
        tuple((p.id, p.quantity, p.quantity_available) for p in positions),
        order.status,
        order.filled_quantity,
        order.avg_fill_price,
        order.fee_amount,
        order.filled_at,
        order.broker_response,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("depletion", [Decimal("1"), Decimal("2")])
async def test_follow_up_sell_rejects_depleted_inventory_without_financial_effects(
    ledger, depletion
):
    user = await _seed_user_and_settings(ledger)
    engine = PaperExecutionEngine(ledger)
    await engine.execute(_body(quantity=Decimal("4")), user=user)
    partial = await engine.execute(
        _body(side="sell", quantity=Decimal("4"), simulation_profile="partial_fill"), user=user
    )
    # Synthetic recovered/depleted ledger state; a second execute is correctly
    # blocked by the existing duplicate-order gate and is not this reproduction.
    position = (await engine._latest_paper_positions(user))["REALISM"]
    position.quantity = Decimal("2") - depletion
    position.quantity_available = position.quantity
    await ledger.flush()
    before = await _financial_state(ledger, partial)

    with pytest.raises(PaperExecutionError, match="exceeds available paper quantity"):
        await engine.fill_partial_order(
            partial, quantity=Decimal("2"), estimated_price=Decimal("100"), user=user
        )

    assert await _financial_state(ledger, partial) == before
    assert partial.status == "partially_filled" and partial.remaining_quantity == Decimal("2")
    blocked = (
        await ledger.execute(select(AuditLog).where(AuditLog.action == "paper_sell_fill_blocked"))
    ).scalar_one()
    assert Decimal(blocked.payload["available_quantity"]) == Decimal("2") - depletion
    assert blocked.entity_id == str(partial.id)


@pytest.mark.asyncio
async def test_valid_fractional_sell_follow_ups_conserve_cash_and_inventory(ledger):
    user = await _seed_user_and_settings(ledger)
    engine = PaperExecutionEngine(ledger)
    buy = await engine.execute(_body(quantity=Decimal("4")), user=user)
    partial = await engine.execute(
        _body(side="sell", quantity=Decimal("4"), simulation_profile="partial_fill"), user=user
    )
    for quantity in (Decimal("0.5"), Decimal("1.5")):
        await engine.fill_partial_order(
            partial, quantity=quantity, estimated_price=Decimal("100"), user=user
        )
    account, positions = await engine.portfolio_state(user)
    assert positions == []
    assert partial.status == "filled" and partial.filled_quantity == Decimal("4")
    trades = (await ledger.execute(select(Trade))).scalars().all()
    assert sum(t.quantity for t in trades) == Decimal("4")
    assert (
        account["free"]
        == Decimal("100000")
        - buy.cash_used
        + partial.avg_fill_price * Decimal("4")
        - partial.fee_amount
    )
    assert account["total"] == account["free"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "available,sell_quantity", [(Decimal("0"), Decimal("1")), (Decimal("9"), Decimal("5"))]
)
async def test_new_sell_honors_zero_availability_and_owned_quantity_cap(
    ledger, available, sell_quantity
):
    user = await _seed_user_and_settings(ledger)
    engine = PaperExecutionEngine(ledger)
    buy = await engine.execute(_body(quantity=Decimal("4")), user=user)
    position = (await ledger.execute(select(PositionSnapshot))).scalar_one()
    position.quantity_available = available
    await ledger.flush()
    before = await _financial_state(ledger, buy)
    with pytest.raises(PaperExecutionError, match="exceeds available paper quantity"):
        await engine.execute(_body(side="sell", quantity=sell_quantity), user=user)
    assert await _financial_state(ledger, buy) == before
    _, positions = await engine.portfolio_state(user)
    assert positions[0]["maxSell"] == min(available, Decimal("4"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owned,available,blocked",
    [
        (Decimal("2"), None, False),
        (Decimal("2"), Decimal("0"), True),
        (Decimal("2"), Decimal("1"), True),
        (Decimal("2"), Decimal("9"), False),
        (Decimal("1"), Decimal("9"), True),
    ],
)
async def test_follow_up_uses_explicit_available_quantity_with_owned_cap(
    ledger, owned, available, blocked
):
    user = await _seed_user_and_settings(ledger)
    engine = PaperExecutionEngine(ledger)
    await engine.execute(_body(quantity=Decimal("4")), user=user)
    partial = await engine.execute(
        _body(side="sell", quantity=Decimal("4"), simulation_profile="partial_fill"), user=user
    )
    position = (await engine._latest_paper_positions(user))["REALISM"]
    position.quantity = owned
    position.quantity_available = available
    await ledger.flush()
    before = await _financial_state(ledger, partial)
    if blocked:
        with pytest.raises(PaperExecutionError, match="exceeds available paper quantity"):
            await engine.fill_partial_order(
                partial, quantity=Decimal("2"), estimated_price=Decimal("100"), user=user
            )
        assert await _financial_state(ledger, partial) == before
    else:
        await engine.fill_partial_order(
            partial, quantity=Decimal("2"), estimated_price=Decimal("100"), user=user
        )
        assert partial.status == "filled"


@pytest.mark.asyncio
async def test_supported_sell_does_not_release_unavailable_holdings(ledger):
    user = await _seed_user_and_settings(ledger)
    engine = PaperExecutionEngine(ledger)
    await engine.execute(_body(quantity=Decimal("4")), user=user)
    partial = await engine.execute(
        _body(side="sell", quantity=Decimal("4"), simulation_profile="partial_fill"), user=user
    )
    position = (await engine._latest_paper_positions(user))["REALISM"]
    position.quantity_available = Decimal("1")
    await ledger.flush()

    await engine.fill_partial_order(
        partial, quantity=Decimal("1"), estimated_price=Decimal("100"), user=user
    )
    position = (await engine._latest_paper_positions(user))["REALISM"]
    assert position.quantity == Decimal("1")
    assert position.quantity_available == Decimal("0")
    before = await _financial_state(ledger, partial)
    with pytest.raises(PaperExecutionError, match="exceeds available paper quantity"):
        await engine.fill_partial_order(
            partial, quantity=Decimal("1"), estimated_price=Decimal("100"), user=user
        )
    assert await _financial_state(ledger, partial) == before


@pytest.mark.asyncio
async def test_buy_does_not_release_previously_unavailable_holdings(ledger):
    user = await _seed_user_and_settings(ledger)
    engine = PaperExecutionEngine(ledger)
    await engine.execute(_body(quantity=Decimal("4")), user=user)
    position = (await engine._latest_paper_positions(user))["REALISM"]
    position.quantity_available = Decimal("1")
    await ledger.flush()

    await engine.execute(_body(quantity=Decimal("2")), user=user)
    position = (await engine._latest_paper_positions(user))["REALISM"]
    assert position.quantity == Decimal("6")
    assert position.quantity_available == Decimal("3")
    _, positions = await engine.portfolio_state(user)
    assert positions[0]["maxSell"] == Decimal("3")


@pytest.mark.asyncio
async def test_postgres_competing_partial_sells_cannot_consume_same_inventory(postgres_sessions):
    async with postgres_sessions() as setup:
        user = await _seed_user_and_settings(setup)
        engine = PaperExecutionEngine(setup)
        await engine.execute(_body(quantity=Decimal("4")), user=user)
        first = await engine.execute(
            _body(side="sell", quantity=Decimal("4"), simulation_profile="partial_fill"), user=user
        )
        # Synthetic recovery fixture, NOT two orders admitted by execute():
        # the duplicate gate correctly prevents that normal submission sequence.
        second = Order(
            id=uuid.uuid4(),
            client_order_key=f"recovered-{uuid.uuid4().hex}",
            ticker=first.ticker,
            side="sell",
            order_type="market",
            quantity=Decimal("2"),
            filled_quantity=Decimal("1"),
            avg_fill_price=first.avg_fill_price,
            fee_amount=Decimal("0"),
            status="partially_filled",
            execution_environment=first.execution_environment,
            broker_account_scope=first.broker_account_scope,
            is_dry_run=True,
            venue="paper",
            broker_request=dict(first.broker_request),
        )
        setup.add(second)
        position = (await engine._latest_paper_positions(user))["REALISM"]
        position.quantity = position.quantity_available = Decimal("1")
        await setup.flush()
        user_id, first_id, second_id = user.id, first.id, second.id
        initial_cash = (await engine._latest_paper_account(user)).cash
        initial_trades = await setup.scalar(select(func.sum(Trade.quantity)))
        initial_filled = first.filled_quantity + second.filled_quantity
        await setup.commit()

    contender_pid = asyncio.get_running_loop().create_future()

    async def contend():
        async with postgres_sessions() as session:
            user = await session.get(User, user_id)
            order = await session.get(Order, second_id)
            contender_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
            try:
                await PaperExecutionEngine(session).fill_partial_order(
                    order, quantity=Decimal("1"), estimated_price=Decimal("100"), user=user
                )
            except PaperExecutionError as exc:
                assert "exceeds available paper quantity" in exc.reason
                await session.commit()  # Persist rejection; rollback must not hide effects.
                return "blocked"
            await session.commit()
            return "filled"

    async with postgres_sessions() as holder:
        user = await holder.get(User, user_id)
        first = await holder.get(Order, first_id)
        holder_engine = PaperExecutionEngine(holder)
        await holder_engine._lock_paper_connection(user)
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        competing = asyncio.create_task(contend())
        try:
            pid = await asyncio.wait_for(contender_pid, 5)
            # Observe an actual database lock wait, not merely task timing.
            async with postgres_sessions() as observer:
                async with asyncio.timeout(5):
                    while True:
                        blockers = await observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
                        )
                        if holder_pid in blockers:
                            break
                        assert not competing.done(), "contender bypassed the account lock"
                        await asyncio.sleep(0.01)
            await holder_engine.fill_partial_order(
                first, quantity=Decimal("1"), estimated_price=Decimal("100"), user=user
            )
            await holder.commit()
            assert await asyncio.wait_for(competing, 5) == "blocked"
        finally:
            if not competing.done():
                competing.cancel()
            await asyncio.gather(competing, return_exceptions=True)

    async with postgres_sessions() as verify:
        user = await verify.get(User, user_id)
        account, positions = await PaperExecutionEngine(verify).portfolio_state(user)
        assert positions == []
        assert await verify.scalar(select(func.sum(Trade.quantity))) == initial_trades + Decimal(
            "1"
        )
        sells = (await verify.execute(select(Order).where(Order.side == "sell"))).scalars().all()
        assert sum(o.filled_quantity for o in sells) == initial_filled + Decimal("1")
        assert all(o.status == "partially_filled" for o in sells)
        assert account["free"] == initial_cash + Decimal("99.88002000")
        assert (
            await verify.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "paper_sell_fill_blocked")
            )
            == 1
        )
