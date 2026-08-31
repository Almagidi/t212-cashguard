"""PostgreSQL proof that concurrent EOD claimants create one operation and order."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.models import (
    AppSettings,
    AuditLog,
    EodFlattenOperation,
    Order,
    OrderEvent,
    Signal,
    Strategy,
)
from app.services.eod_flatten import EodFlattenService

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")
DUE_AT = datetime(2026, 7, 6, 20, 5, tzinfo=UTC)


class SnapshotOnlyBroker:
    environment = "demo"
    account_scope = "trading212:demo:user:concurrency-test"

    async def get_positions(self) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        return [{"ticker": "EODC", "quantity": "2", "maxSell": "2", "currentPrice": "100"}]

    async def place_market_order(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "status": "FILLED",
            "filledQuantity": "2",
            "filledPrice": "100",
        }


@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the EOD concurrency proof",
)
@pytest.mark.asyncio
async def test_postgres_concurrent_workers_create_one_eod_operation_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    strategy_id = uuid.uuid4()
    signal_id = uuid.uuid4()
    entry_order_id = uuid.uuid4()
    settings_created = False
    prior_kill_switch: bool | None = None

    async with sessions() as setup:
        app_settings = await setup.get(AppSettings, 1)
        if app_settings is None:
            setup.add(
                AppSettings(
                    id=1,
                    auto_trading_enabled=True,
                    kill_switch_active=False,
                    live_trading_unlocked=False,
                )
            )
            settings_created = True
        else:
            prior_kill_switch = app_settings.kill_switch_active
            app_settings.kill_switch_active = False
        setup.add(
            Strategy(
                id=strategy_id,
                name="Concurrent EOD claim",
                type="orb",
                params={},
                venue="t212",
                session_end="16:00",
                eod_flatten=True,
                is_enabled=True,
                is_live=True,
            )
        )
        setup.add(
            Signal(
                id=signal_id,
                strategy_id=strategy_id,
                ticker="EODC",
                side="buy",
                signal_type="entry",
                status="executed",
            )
        )
        setup.add(
            Order(
                id=entry_order_id,
                signal_id=signal_id,
                client_order_key=uuid.uuid4().hex,
                ticker="EODC",
                side="buy",
                order_type="market",
                quantity=Decimal("2"),
                filled_quantity=Decimal("2"),
                status="filled",
                venue="t212",
                execution_environment="demo",
                broker_account_scope=SnapshotOnlyBroker.account_scope,
                is_dry_run=False,
            )
        )
        await setup.commit()

    async def attempt() -> dict[str, Any]:
        async with sessions() as session:
            strategy = await session.get(Strategy, strategy_id)
            assert strategy is not None
            return await EodFlattenService(session, SnapshotOnlyBroker()).run(
                [strategy], now_utc=DUE_AT
            )

    try:
        summaries = await asyncio.wait_for(
            asyncio.gather(attempt(), attempt()),
            timeout=15,
        )
        async with sessions() as verify:
            operation_count = await verify.scalar(
                select(func.count())
                .select_from(EodFlattenOperation)
                .where(EodFlattenOperation.strategy_id == strategy_id)
            )
            eod_order_count = await verify.scalar(
                select(func.count())
                .select_from(Order)
                .join(EodFlattenOperation, EodFlattenOperation.order_id == Order.id)
                .where(EodFlattenOperation.strategy_id == strategy_id)
            )
        assert operation_count == 1
        assert eod_order_count == 1
        assert sum(summary["operations_created"] for summary in summaries) == 1
        assert sum(summary["flattened"] for summary in summaries) == 1
    finally:
        async with sessions() as cleanup:
            eod_order_ids = (
                (
                    await cleanup.execute(
                        select(EodFlattenOperation.order_id).where(
                            EodFlattenOperation.strategy_id == strategy_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            audit_predicates = [
                and_(
                    AuditLog.actor == "eod_flatten_service",
                    AuditLog.payload["broker_account_scope"].as_string()
                    == SnapshotOnlyBroker.account_scope,
                )
            ]
            if eod_order_ids:
                audit_predicates.append(AuditLog.entity_id.in_(map(str, eod_order_ids)))
            await cleanup.execute(delete(AuditLog).where(or_(*audit_predicates)))
            await cleanup.execute(
                delete(EodFlattenOperation).where(EodFlattenOperation.strategy_id == strategy_id)
            )
            if eod_order_ids:
                await cleanup.execute(
                    delete(OrderEvent).where(OrderEvent.order_id.in_(eod_order_ids))
                )
                await cleanup.execute(delete(Order).where(Order.id.in_(eod_order_ids)))
            await cleanup.execute(delete(Order).where(Order.id == entry_order_id))
            await cleanup.execute(delete(Signal).where(Signal.id == signal_id))
            await cleanup.execute(delete(Strategy).where(Strategy.id == strategy_id))
            if settings_created:
                await cleanup.execute(delete(AppSettings).where(AppSettings.id == 1))
            else:
                await cleanup.execute(
                    update(AppSettings)
                    .where(AppSettings.id == 1)
                    .values(kill_switch_active=prior_kill_switch)
                )
            await cleanup.commit()
        await engine.dispose()
