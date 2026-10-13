"""Shared pieces for the cash reservation tests: an isolated database, a scripted broker and
helpers that admit orders through the real execution engine.

Each test gets its own PostgreSQL schema, so no other test's orders can count against it.
Nothing leaves this machine; the broker is an in-memory double.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.models import AppSettings, Order, OrderSubmissionAttempt, RiskEvent, RiskProfile, User
from app.db.session import Base
from app.execution.engine import ExecutionEngine

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")
WAIT = 10.0
WORKING = {"status": "WORKING"}
PRICE = Decimal("100")


class SimulatedProcessDeath(BaseException):
    """Stands in for the process being killed: nothing may catch it and carry on."""


class Account:
    """One broker account: reports a free-cash figure and answers order requests from a script."""

    environment = "demo"

    def __init__(self, name: str, cash: str = "1000") -> None:
        self.account_scope: str | None = f"trading212:demo:account:{name}"
        self.cash = Decimal(cash)
        self.answers: dict[str, Any] = {}
        self.by_id: dict[str, Any] = {}
        self.posts: list[tuple[str, Decimal]] = []
        self.cancels: list[str] = []
        self.summaries = 0
        self.summary_times: list[datetime] = []
        self.cancel_failure: Exception | None = None
        self.read_together: asyncio.Barrier | None = None

    async def __aenter__(self) -> Account:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get_account_summary(self) -> dict[str, Any]:
        self.summaries += 1
        self.summary_times.append(datetime.now(UTC))
        summary = {
            "cash": {
                "availableToTrade": str(self.cash),
                "total": str(self.cash),
                "blockedForPendingOrders": "0",
            },
            "total": "10000",
            "invested": "9000",
            "currency": "USD",
        }
        if self.read_together is not None:
            await asyncio.wait_for(self.read_together.wait(), WAIT)
        return summary

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    async def _answer(self, ticker: str, quantity: Decimal) -> Any:
        self.posts.append((ticker, quantity))
        answer = self.answers.get(ticker, WORKING)
        if isinstance(answer, BaseException):
            raise answer
        return {"id": f"B-{ticker}", **answer}

    async def place_limit_order(
        self, ticker: str, quantity: Decimal, _limit_price: Decimal, **_kwargs: Any
    ) -> Any:
        return await self._answer(ticker, quantity)

    async def place_market_order(self, ticker: str, quantity: Decimal, **_kwargs: Any) -> Any:
        return await self._answer(ticker, quantity)

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancels.append(broker_order_id)
        if self.cancel_failure is not None:
            raise self.cancel_failure

    async def get_order_by_id(self, broker_order_id: str) -> Any:
        return dict(self.by_id[broker_order_id])

    async def get_historical_orders(self, **_kwargs: Any) -> Any:
        return {"items": [], "nextPagePath": None}


def filled(quantity: Decimal | int | str, price: Decimal | int | str = PRICE) -> dict[str, Any]:
    return {"status": "FILLED", "filledQuantity": str(quantity), "filledPrice": str(price)}


class World:
    """One test's database."""

    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions
        self.user_id: uuid.UUID

    async def admit(
        self,
        account: Account,
        ticker: str,
        quantity: int | str,
        *,
        cash: Decimal | int | str | None = None,
        cash_read_at: datetime | None = None,
        send: bool = True,
        **intent: Any,
    ) -> Order:
        """One process: create the intent against a cash figure and, by default, send it."""
        figure = account.cash if cash is None else Decimal(str(cash))
        async with self.sessions() as session:
            engine = ExecutionEngine(session, account)
            values: dict[str, Any] = {
                "ticker": ticker,
                "side": "buy",
                "order_type": "limit",
                "quantity": Decimal(str(quantity)),
                "limit_price": PRICE,
                "estimated_price": PRICE,
                "is_dry_run": False,
                "available_cash": figure,
                "cash_read_at": cash_read_at or datetime.now(UTC),
                **intent,
            }
            order = await engine.create_order_intent(**values)
            if send and order.status == "pending_intent":
                order = await engine.submit_order(order)
            await session.commit()
            return order

    async def orders(self) -> list[Order]:
        async with self.sessions() as session:
            return list(
                (await session.execute(select(Order).order_by(Order.created_at))).scalars().all()
            )

    async def order(self, ticker: str) -> Order:
        async with self.sessions() as session:
            return (await session.execute(select(Order).where(Order.ticker == ticker))).scalar_one()

    async def attempts(self) -> list[OrderSubmissionAttempt]:
        async with self.sessions() as session:
            return list((await session.execute(select(OrderSubmissionAttempt))).scalars().all())

    async def refusals(self) -> list[RiskEvent]:
        async with self.sessions() as session:
            return list(
                (
                    await session.execute(
                        select(RiskEvent).where(RiskEvent.event_type == "cash_reservation_block")
                    )
                )
                .scalars()
                .all()
            )

    async def make_older(self, ticker: str, by: timedelta) -> None:
        """Move every time recorded on the order back, as if it had all happened earlier."""
        async with self.sessions() as session:
            await session.execute(
                update(Order)
                .where(Order.ticker == ticker)
                .values(
                    updated_at=Order.updated_at - by,
                    created_at=Order.created_at - by,
                    filled_at=Order.filled_at - by,
                    cancelled_at=Order.cancelled_at - by,
                    rejected_at=Order.rejected_at - by,
                )
            )
            await session.commit()

    async def set_kill_switch(self, active: bool) -> None:
        async with self.sessions() as session:
            app_settings = await session.get(AppSettings, 1)
            app_settings.kill_switch_active = active
            await session.commit()

    async def is_kept_waiting(
        self, admission: asyncio.Task[Any], *, for_seconds: float = 0.5
    ) -> bool:
        """True when the admission has neither been admitted nor refused after that long."""
        await asyncio.sleep(for_seconds)
        return not admission.done()


@asynccontextmanager
async def open_world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[World, None]:
    """A fresh schema with one admin, open trading gates and the default risk limits."""
    assert POSTGRES_TEST_DATABASE_URL is not None
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_ORDER_ENABLED", True)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    schema = f"cash_reservation_{uuid.uuid4().hex}"
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    scoped = engine.execution_options(schema_translate_map={None: schema})
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with scoped.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        made = World(async_sessionmaker(scoped, expire_on_commit=False))
        async with made.sessions() as setup:
            user = User(
                id=uuid.uuid4(),
                email=f"{uuid.uuid4().hex}@example.test",
                hashed_password="x",
                is_active=True,
                is_admin=True,
            )
            setup.add(user)
            setup.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False))
            setup.add(RiskProfile(name="Default limits", is_default=True))
            await setup.commit()
            made.user_id = user.id
        yield made
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
