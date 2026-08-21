"""Deterministic paper-fill economics and effect consistency."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.schemas import OrderOut, PaperOrderCreate
from app.core.security import hash_password
from app.db.models import (
    AppSettings,
    AuditLog,
    BrokerAccountSnapshot,
    BrokerConnection,
    Order,
    OrderEvent,
    PositionSnapshot,
    RiskEvent,
    Trade,
    User,
)
from app.execution.paper_engine import PaperExecutionEngine, PaperExecutionError
from app.execution.paper_policy import evaluate_paper_fill

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")


async def _seed_user_and_settings(db: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="paper-realism@test.com",
        hashed_password=hash_password("testpassword123"),
        is_active=True,
        is_admin=True,
    )
    db.add(user)
    db.add(
        AppSettings(
            id=1,
            auto_trading_enabled=True,
            kill_switch_active=False,
            live_trading_unlocked=False,
        )
    )
    await db.flush()
    return user


def _body(**overrides: object) -> PaperOrderCreate:
    values: dict[str, object] = {
        "ticker": "REALISM",
        "side": "buy",
        "quantity": Decimal("2"),
        "estimated_price": Decimal("100"),
        "source": "paper_realism_test",
        "strategy": "paper-realism",
        "venue": "paper",
        "paper_only": True,
    }
    values.update(overrides)
    return PaperOrderCreate(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_standard_fill_applies_non_zero_costs_and_consistent_effects(
    db: AsyncSession,
) -> None:
    user = await _seed_user_and_settings(db)

    order = await PaperExecutionEngine(db).execute(_body(), user=user)

    assert order.status == "filled"
    assert order.avg_fill_price == Decimal("100.10000000")
    assert order.filled_quantity == Decimal("2")
    assert order.fee_amount == Decimal("0.04004000")
    assert order.slippage_pct == Decimal("0.1000")
    assert order.slippage_value == Decimal("0.2000")
    assert order.cash_used == Decimal("200.24004000")
    assert order.fill_latency_ms == 25
    assert order.broker_response["spread_bps"] == "10"
    assert order.broker_response["slippage_bps"] == "5"

    account = (await db.execute(select(BrokerAccountSnapshot))).scalar_one()
    assert account.cash == Decimal("99799.75996000")
    assert account.free_funds == account.cash
    assert account.invested == Decimal("200.00000000")
    assert account.total_value == Decimal("99999.75996000")

    position = (await db.execute(select(PositionSnapshot))).scalar_one()
    assert position.quantity == Decimal("2")
    assert position.avg_price == Decimal("100.12002000")
    assert position.current_price == Decimal("100")
    assert position.unrealized_pnl == Decimal("-0.24004000")
    assert await db.scalar(select(func.count()).select_from(Trade)) == 0


@pytest.mark.asyncio
async def test_notional_order_uses_one_canonical_quantity_for_order_and_effects(
    db: AsyncSession,
) -> None:
    user = await _seed_user_and_settings(db)

    order = await PaperExecutionEngine(db).execute(
        _body(quantity=None, notional=Decimal("2"), estimated_price=Decimal("3")), user=user
    )

    assert order.status == "filled"
    assert order.quantity == order.filled_quantity == Decimal("0.66666666")
    position = (await db.execute(select(PositionSnapshot))).scalar_one()
    assert position.quantity == order.filled_quantity
    assert order.broker_request["quantity"] == "0.66666666"

    with pytest.raises(PaperExecutionError, match="outside persistence range"):
        await PaperExecutionEngine(db).execute(
            _body(
                ticker="TOO-LARGE",
                quantity=None,
                notional=Decimal("1000000"),
                estimated_price=Decimal("0.00000001"),
            ),
            user=user,
        )


@pytest.mark.asyncio
async def test_partial_fill_never_claims_full_fill_and_applies_proportional_effects(
    db: AsyncSession,
) -> None:
    user = await _seed_user_and_settings(db)

    order = await PaperExecutionEngine(db).execute(
        _body(quantity=Decimal("4"), simulation_profile="partial_fill"),
        user=user,
    )

    assert order.status == "partially_filled"
    assert order.quantity == Decimal("4")
    assert order.filled_quantity == Decimal("2")
    assert order.avg_fill_price == Decimal("100.10000000")
    assert order.fee_amount == Decimal("0.04004000")
    assert order.filled_at is None
    events = (
        (
            await db.execute(
                select(OrderEvent)
                .where(OrderEvent.order_id == order.id)
                .order_by(OrderEvent.occurred_at, OrderEvent.id)
            )
        )
        .scalars()
        .all()
    )
    assert events[-1].to_status == "partially_filled"
    position = (await db.execute(select(PositionSnapshot))).scalar_one()
    assert position.quantity == Decimal("2")
    account = (await db.execute(select(BrokerAccountSnapshot))).scalar_one()
    assert account.cash == Decimal("99799.75996000")


@pytest.mark.asyncio
async def test_partial_follow_up_fills_are_cumulative_and_cannot_exceed_remainder(
    db: AsyncSession,
) -> None:
    user = await _seed_user_and_settings(db)
    engine = PaperExecutionEngine(db)
    order = await engine.execute(
        _body(quantity=Decimal("4"), simulation_profile="partial_fill"),
        user=user,
    )

    assert order.remaining_quantity == Decimal("2")
    await db.refresh(order)
    assert OrderOut.model_validate(order).remaining_quantity == Decimal("2")
    order = await engine.fill_partial_order(
        order,
        quantity=Decimal("1"),
        estimated_price=Decimal("100"),
        user=user,
    )
    assert order.status == "partially_filled"
    assert order.filled_quantity == Decimal("3")
    assert order.remaining_quantity == Decimal("1")
    assert order.fee_amount == Decimal("0.06006000")

    with pytest.raises(PaperExecutionError, match="remaining quantity"):
        await engine.fill_partial_order(
            order,
            quantity=Decimal("2"),
            estimated_price=Decimal("100"),
            user=user,
        )
    assert order.filled_quantity == Decimal("3")
    assert order.remaining_quantity == Decimal("1")

    order = await engine.fill_partial_order(
        order,
        quantity=Decimal("1"),
        estimated_price=Decimal("100"),
        user=user,
    )
    assert order.status == "filled"
    assert order.filled_quantity == order.quantity == Decimal("4")
    assert order.remaining_quantity == Decimal("0")
    assert order.fee_amount == Decimal("0.08008000")
    assert order.filled_at is not None

    positions = (
        (await db.execute(select(PositionSnapshot).order_by(PositionSnapshot.snapshotted_at)))
        .scalars()
        .all()
    )
    accounts = (
        (
            await db.execute(
                select(BrokerAccountSnapshot).order_by(BrokerAccountSnapshot.snapshotted_at)
            )
        )
        .scalars()
        .all()
    )
    assert [position.quantity for position in positions] == [
        Decimal("2"),
        Decimal("3"),
        Decimal("4"),
    ]
    assert accounts[-1].cash == Decimal("99599.51992000")
    assert await db.scalar(select(func.count()).select_from(Trade)) == 0

    fill_events = (
        (
            await db.execute(
                select(OrderEvent)
                .where(OrderEvent.order_id == order.id)
                .where(OrderEvent.event_type == "paper_follow_up_fill")
            )
        )
        .scalars()
        .all()
    )
    assert len(fill_events) == 2
    assert fill_events[-1].to_status == "filled"


@pytest.mark.asyncio
async def test_partial_follow_up_buy_rechecks_cash_before_effects(db: AsyncSession) -> None:
    user = await _seed_user_and_settings(db)
    engine = PaperExecutionEngine(db)
    partial = await engine.execute(
        _body(ticker="PARTIAL-CASH", quantity=Decimal("900"), simulation_profile="partial_fill"),
        user=user,
    )
    await engine.execute(
        _body(ticker="OTHER-CASH", quantity=Decimal("500")),
        user=user,
    )

    before = await engine._latest_paper_account(user)
    assert before is not None and before.cash > 0
    position_count = int(await db.scalar(select(func.count()).select_from(PositionSnapshot)) or 0)

    with pytest.raises(PaperExecutionError, match="cash"):
        await engine.fill_partial_order(
            partial,
            quantity=Decimal("450"),
            estimated_price=Decimal("100"),
            user=user,
        )

    after = await engine._latest_paper_account(user)
    assert after is not None and after.cash == before.cash
    assert (
        int(await db.scalar(select(func.count()).select_from(PositionSnapshot)) or 0)
        == position_count
    )
    await db.refresh(partial)
    assert partial.filled_quantity == Decimal("450")


@pytest.mark.asyncio
async def test_same_order_follow_up_preserves_provenance_and_recomputes_quality(
    db: AsyncSession,
) -> None:
    user = await _seed_user_and_settings(db)
    engine = PaperExecutionEngine(db)
    partial = await engine.execute(
        _body(
            ticker="PARTIAL-PROVENANCE", quantity=Decimal("4"), simulation_profile="partial_fill"
        ),
        user=user,
    )
    partial.submitted_at = datetime.now(UTC) - timedelta(seconds=1)

    filled = await engine.fill_partial_order(
        partial,
        quantity=Decimal("2"),
        estimated_price=Decimal("100"),
        user=user,
    )

    assert filled.slippage_pct == Decimal("0.1000")
    assert filled.slippage_value == Decimal("0.4000")
    assert filled.fill_latency_ms is not None and filled.fill_latency_ms >= 1000
    latest_position = (
        await db.execute(
            select(PositionSnapshot)
            .where(PositionSnapshot.ticker == "PARTIAL-PROVENANCE")
            .order_by(PositionSnapshot.snapshotted_at.desc(), PositionSnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert latest_position.raw["position_open_order_id"] == str(partial.id)

    sell = await engine.execute(
        _body(ticker="PARTIAL-PROVENANCE", side="sell", quantity=Decimal("1")),
        user=user,
    )
    trade = (await db.execute(select(Trade).where(Trade.close_order_id == sell.id))).scalar_one()
    assert trade.open_order_id == partial.id


@pytest.mark.asyncio
async def test_partial_follow_up_rejects_a_different_paper_account(db: AsyncSession) -> None:
    owner = await _seed_user_and_settings(db)
    other = User(
        id=uuid.uuid4(),
        email="other-paper-account@test.com",
        hashed_password=hash_password("testpassword123"),
        is_active=True,
        is_admin=True,
    )
    db.add(other)
    await db.flush()
    engine = PaperExecutionEngine(db)
    partial = await engine.execute(
        _body(ticker="ACCOUNT-BOUND", quantity=Decimal("4"), simulation_profile="partial_fill"),
        user=owner,
    )

    with pytest.raises(PaperExecutionError, match="does not belong"):
        await engine.fill_partial_order(
            partial,
            quantity=Decimal("1"),
            estimated_price=Decimal("100"),
            user=other,
        )

    await db.refresh(partial)
    assert partial.filled_quantity == Decimal("2")
    owner_connection = (
        await db.execute(select(BrokerConnection).where(BrokerConnection.user_id == owner.id))
    ).scalar_one()
    positions = (await db.execute(select(PositionSnapshot))).scalars().all()
    assert len(positions) == 1
    assert positions[0].connection_id == owner_connection.id


@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the paper-ledger concurrency proof",
)
@pytest.mark.asyncio
async def test_postgres_first_paper_orders_serialize_on_stable_user_row() -> None:
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    ticker_suffix = user_id.hex[:8].upper()
    tickers = (f"FIRST-A-{ticker_suffix}", f"FIRST-B-{ticker_suffix}")
    settings_created = False

    async with sessions() as setup:
        if await setup.get(AppSettings, 1) is None:
            setup.add(
                AppSettings(
                    id=1,
                    auto_trading_enabled=True,
                    kill_switch_active=False,
                    live_trading_unlocked=False,
                )
            )
            settings_created = True
        setup.add(
            User(
                id=user_id,
                email=f"first-paper-{user_id}@test.com",
                hashed_password=hash_password("testpassword123"),
                is_active=True,
                is_admin=True,
            )
        )
        await setup.commit()

    async def attempt(ticker: str) -> tuple[str, uuid.UUID | None]:
        async with sessions() as session:
            user = await session.get(User, user_id)
            assert user is not None
            try:
                order = await PaperExecutionEngine(session).execute(
                    _body(ticker=ticker, quantity=Decimal("500")),
                    user=user,
                )
                await session.commit()
                return "filled", order.id
            except PaperExecutionError as exc:
                await session.rollback()
                assert "Cash guard" in str(exc) or "cash guard" in str(exc)
                return "cash_blocked", None

    results: list[tuple[str, uuid.UUID | None]] = []
    try:
        results = list(await asyncio.gather(*(attempt(ticker) for ticker in tickers)))
        assert sorted(status for status, _ in results) == ["cash_blocked", "filled"]
        filled_order_id = next(order_id for status, order_id in results if status == "filled")
        assert filled_order_id is not None
        async with sessions() as verify:
            connection_ids = list(
                (
                    await verify.execute(
                        select(BrokerConnection.id).where(BrokerConnection.user_id == user_id)
                    )
                ).scalars()
            )
            assert len(connection_ids) == 1
            filled_order = await verify.get(Order, filled_order_id)
            assert filled_order is not None and filled_order.ticker in tickers
            latest = (
                await verify.execute(
                    select(BrokerAccountSnapshot)
                    .where(BrokerAccountSnapshot.connection_id == connection_ids[0])
                    .order_by(BrokerAccountSnapshot.snapshotted_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            assert latest.cash >= 0
    finally:
        async with sessions() as cleanup:
            order_ids = [order_id for _, order_id in results if order_id is not None]
            if order_ids:
                await cleanup.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
            await cleanup.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await cleanup.execute(delete(RiskEvent).where(RiskEvent.ticker.in_(tickers)))
            if order_ids:
                await cleanup.execute(delete(Order).where(Order.id.in_(order_ids)))
            connection_ids = list(
                (
                    await cleanup.execute(
                        select(BrokerConnection.id).where(BrokerConnection.user_id == user_id)
                    )
                ).scalars()
            )
            if connection_ids:
                await cleanup.execute(
                    delete(PositionSnapshot).where(
                        PositionSnapshot.connection_id.in_(connection_ids)
                    )
                )
                await cleanup.execute(
                    delete(BrokerAccountSnapshot).where(
                        BrokerAccountSnapshot.connection_id.in_(connection_ids)
                    )
                )
                await cleanup.execute(
                    delete(BrokerConnection).where(BrokerConnection.id.in_(connection_ids))
                )
            await cleanup.execute(delete(User).where(User.id == user_id))
            if settings_created:
                await cleanup.execute(delete(AppSettings).where(AppSettings.id == 1))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the paper bootstrap concurrency proof",
)
@pytest.mark.asyncio
async def test_postgres_portfolio_bootstrap_blocks_first_execute_until_commit() -> None:
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    ticker = f"BOOT-{user_id.hex[:8].upper()}"
    settings_created = False
    bootstrap_ready = asyncio.Event()
    release_bootstrap = asyncio.Event()

    async with sessions() as setup:
        if await setup.get(AppSettings, 1) is None:
            setup.add(
                AppSettings(
                    id=1,
                    auto_trading_enabled=True,
                    kill_switch_active=False,
                    live_trading_unlocked=False,
                )
            )
            settings_created = True
        setup.add(
            User(
                id=user_id,
                email=f"paper-bootstrap-{user_id}@test.com",
                hashed_password=hash_password("testpassword123"),
                is_active=True,
                is_admin=True,
            )
        )
        await setup.commit()

    async def hold_portfolio_bootstrap() -> None:
        async with sessions() as session:
            user = await session.get(User, user_id)
            assert user is not None
            account, positions = await PaperExecutionEngine(session).portfolio_state(user)
            assert account["free"] == Decimal("100000") and positions == []
            bootstrap_ready.set()
            await release_bootstrap.wait()
            await session.commit()

    async def execute_after_bootstrap() -> uuid.UUID:
        await bootstrap_ready.wait()
        async with sessions() as session:
            user = await session.get(User, user_id)
            assert user is not None
            order = await PaperExecutionEngine(session).execute(
                _body(ticker=ticker, quantity=Decimal("1")),
                user=user,
            )
            await session.commit()
            return order.id

    bootstrap_task = asyncio.create_task(hold_portfolio_bootstrap())
    execution_task: asyncio.Task[uuid.UUID] | None = None
    order_id: uuid.UUID | None = None
    try:
        await bootstrap_ready.wait()
        execution_task = asyncio.create_task(execute_after_bootstrap())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(execution_task), timeout=0.1)
        release_bootstrap.set()
        await bootstrap_task
        order_id = await execution_task

        async with sessions() as verify:
            assert (
                await verify.scalar(
                    select(func.count())
                    .select_from(BrokerConnection)
                    .where(BrokerConnection.user_id == user_id)
                )
                == 1
            )
            order = await verify.get(Order, order_id)
            assert order is not None and order.status == "filled"
    finally:
        release_bootstrap.set()
        if not bootstrap_task.done():
            await bootstrap_task
        if execution_task is not None and not execution_task.done():
            await execution_task
        async with sessions() as cleanup:
            if order_id is not None:
                await cleanup.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
                await cleanup.execute(delete(Order).where(Order.id == order_id))
            await cleanup.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            connection_ids = list(
                (
                    await cleanup.execute(
                        select(BrokerConnection.id).where(BrokerConnection.user_id == user_id)
                    )
                ).scalars()
            )
            if connection_ids:
                await cleanup.execute(
                    delete(PositionSnapshot).where(
                        PositionSnapshot.connection_id.in_(connection_ids)
                    )
                )
                await cleanup.execute(
                    delete(BrokerAccountSnapshot).where(
                        BrokerAccountSnapshot.connection_id.in_(connection_ids)
                    )
                )
                await cleanup.execute(
                    delete(BrokerConnection).where(BrokerConnection.id.in_(connection_ids))
                )
            await cleanup.execute(delete(User).where(User.id == user_id))
            if settings_created:
                await cleanup.execute(delete(AppSettings).where(AppSettings.id == 1))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_liquidity_rejects_without_fill_or_financial_effects(
    db: AsyncSession,
) -> None:
    user = await _seed_user_and_settings(db)

    order = await PaperExecutionEngine(db).execute(
        _body(simulation_profile="no_liquidity"),
        user=user,
    )

    assert order.status == "rejected"
    assert order.filled_quantity == Decimal("0")
    assert order.avg_fill_price is None
    assert order.fee_amount == Decimal("0")
    assert order.error_message == "paper_no_liquidity"
    assert await db.scalar(select(func.count()).select_from(PositionSnapshot)) == 0
    assert await db.scalar(select(func.count()).select_from(BrokerAccountSnapshot)) == 0
    assert await db.scalar(select(func.count()).select_from(Trade)) == 0


@pytest.mark.asyncio
async def test_fee_is_included_in_cash_guard_before_order_creation(db: AsyncSession) -> None:
    user = await _seed_user_and_settings(db)
    connection = BrokerConnection(
        id=uuid.uuid4(),
        user_id=user.id,
        broker="paper",
        environment="mock",
        api_key_encrypted="paper-only-no-real-credential",
        api_secret_encrypted="paper-only-no-real-credential",
        is_active=True,
    )
    db.add(connection)
    db.add(
        BrokerAccountSnapshot(
            connection_id=connection.id,
            total_value=Decimal("100.1"),
            cash=Decimal("100.1"),
            free_funds=Decimal("100.1"),
            invested=Decimal("0"),
            result=Decimal("-99899.9"),
            currency="USD",
            raw={"paper_only": True},
        )
    )
    await db.flush()

    with pytest.raises(PaperExecutionError, match="Cash guard"):
        await PaperExecutionEngine(db).execute(_body(quantity=Decimal("1")), user=user)

    assert await db.scalar(select(func.count()).select_from(Order)) == 0


def test_unrepresentable_policy_inputs_reject_instead_of_zero_filling() -> None:
    partial = evaluate_paper_fill(
        side="buy",
        quantity=Decimal("0.00000001"),
        quote_price=Decimal("100"),
        profile="partial_fill",
    )
    tiny_quote = evaluate_paper_fill(
        side="buy",
        quantity=Decimal("1"),
        quote_price=Decimal("0.000000001"),
        profile="standard",
    )
    sub_precision = evaluate_paper_fill(
        side="buy",
        quantity=Decimal("0.000000006"),
        quote_price=Decimal("100"),
        profile="standard",
    )

    assert partial.outcome == tiny_quote.outcome == sub_precision.outcome == "rejected"
    assert partial.rejection_code == "paper_partial_fill_below_precision"
    assert tiny_quote.rejection_code == "paper_input_below_precision"
    assert partial.filled_quantity == tiny_quote.filled_quantity == Decimal("0")
    assert sub_precision.filled_quantity == Decimal("0")
    with pytest.raises(ValueError):
        _body(quantity=Decimal("1e30"))


@pytest.mark.asyncio
async def test_buy_then_sell_records_one_closed_trade_and_matching_cash(
    db: AsyncSession,
) -> None:
    user = await _seed_user_and_settings(db)
    engine = PaperExecutionEngine(db)
    buy = await engine.execute(_body(ticker="ROUNDTRIP", quantity=Decimal("4")), user=user)

    sell = await engine.execute(
        _body(ticker="ROUNDTRIP", side="sell", quantity=Decimal("1")),
        user=user,
    )

    assert buy.status == sell.status == "filled"
    assert sell.avg_fill_price == Decimal("99.90000000")
    assert sell.fee_amount == Decimal("0.01998000")
    latest_position = (
        await db.execute(
            select(PositionSnapshot)
            .where(PositionSnapshot.ticker == "ROUNDTRIP")
            .order_by(PositionSnapshot.snapshotted_at.desc(), PositionSnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert latest_position.quantity == Decimal("3")
    assert latest_position.avg_price == Decimal("100.12002000")

    trade = (await db.execute(select(Trade))).scalar_one()
    assert trade.is_dry_run is True
    assert trade.open_order_id == buy.id
    assert trade.close_order_id == sell.id
    assert trade.quantity == Decimal("1")
    assert trade.open_price == Decimal("100.12002000")
    assert trade.close_price == Decimal("99.90000000")
    assert trade.realized_pnl == Decimal("-0.24000000")

    latest_account = (
        await db.execute(
            select(BrokerAccountSnapshot)
            .order_by(BrokerAccountSnapshot.snapshotted_at.desc(), BrokerAccountSnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert latest_account.cash == Decimal("99699.39994000")
    assert latest_account.invested == Decimal("300.00000000")
    assert latest_account.total_value == Decimal("99999.39994000")


@pytest.mark.asyncio
async def test_pooled_buys_and_repeated_sells_do_not_claim_false_open_order(
    db: AsyncSession,
) -> None:
    user = await _seed_user_and_settings(db)
    engine = PaperExecutionEngine(db)
    await engine.execute(_body(ticker="POOLED", quantity=Decimal("1")), user=user)
    await engine.execute(_body(ticker="POOLED", quantity=Decimal("1")), user=user)
    first_sell = await engine.execute(
        _body(ticker="POOLED", side="sell", quantity=Decimal("0.5")), user=user
    )
    second_sell = await engine.execute(
        _body(ticker="POOLED", side="sell", quantity=Decimal("0.5")), user=user
    )

    trades = (await db.execute(select(Trade).order_by(Trade.closed_at, Trade.id))).scalars().all()
    assert len(trades) == 2
    assert all(trade.open_order_id is None for trade in trades)
    assert all(trade.close_order_id in {first_sell.id, second_sell.id} for trade in trades)
    assert all(trade.opened_at == trades[0].opened_at for trade in trades)
