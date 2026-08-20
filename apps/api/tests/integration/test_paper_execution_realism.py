"""Deterministic paper-fill economics and effect consistency."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from app.api.schemas import PaperOrderCreate
from app.core.security import hash_password
from app.db.models import (
    AppSettings,
    BrokerAccountSnapshot,
    BrokerConnection,
    Order,
    OrderEvent,
    PositionSnapshot,
    Trade,
    User,
)
from app.execution.paper_engine import PaperExecutionEngine, PaperExecutionError
from app.execution.paper_policy import evaluate_paper_fill

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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
