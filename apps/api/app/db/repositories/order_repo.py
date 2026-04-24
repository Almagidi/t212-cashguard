"""
Order repository — all SQL for orders in one place.
Routes and services never touch SQLAlchemy directly.
"""
from __future__ import annotations

import uuid  # noqa: TC003
from collections.abc import Sequence  # noqa: TC003
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002
from sqlalchemy.orm import selectinload

from app.db.models import Order, OrderEvent, Signal


class OrderRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Reads ────────────────────────────────────────────────────────────────

    async def get_by_id(self, order_id: uuid.UUID) -> Order | None:
        result = await self.db.execute(
            select(Order).where(Order.id == order_id)
            .options(
                selectinload(Order.events),
                selectinload(Order.signal).selectinload(Signal.strategy),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_client_key(self, key: str) -> Order | None:
        result = await self.db.execute(
            select(Order).where(Order.client_order_key == key)
        )
        return result.scalar_one_or_none()

    async def get_by_broker_id(self, broker_id: str) -> Order | None:
        result = await self.db.execute(
            select(Order).where(Order.broker_order_id == broker_id)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        status: str | None = None,
        ticker: str | None = None,
        is_dry_run: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Order]:
        q = (
            select(Order)
            .options(selectinload(Order.signal).selectinload(Signal.strategy))
            .order_by(Order.created_at.desc())
        )
        if status:
            q = q.where(Order.status == status)
        if ticker:
            q = q.where(Order.ticker == ticker.upper())
        if is_dry_run is not None:
            q = q.where(Order.is_dry_run == is_dry_run)
        q = q.limit(limit).offset(offset)
        return (await self.db.execute(q)).scalars().all()

    async def list_pending(self) -> Sequence[Order]:
        """All orders in submittable / reconcilable states."""
        return (await self.db.execute(
            select(Order).where(
                Order.status.in_(["pending_intent", "submitted", "accepted"])
            )
        )).scalars().all()

    async def count_today(self, *, dry_run: bool = False) -> int:
        today = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        result = await self.db.execute(
            select(func.count(Order.id)).where(
                Order.created_at >= today,
                Order.is_dry_run == dry_run,
                Order.status.not_in(["rejected", "cancelled"]),
            )
        )
        return result.scalar_one()

    async def has_active_for_ticker_side(
        self, ticker: str, side: str
    ) -> bool:
        result = await self.db.execute(
            select(Order.id).where(
                Order.ticker == ticker,
                Order.side == side,
                Order.status.in_(["pending_intent", "submitted", "accepted"]),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    # ── Writes ───────────────────────────────────────────────────────────────

    async def create(self, order: Order) -> Order:
        self.db.add(order)
        await self.db.flush()
        return order

    async def update_status(
        self,
        order_id: uuid.UUID,
        status: str,
        *,
        version: int | None = None,
    ) -> int:
        """
        Optimistic-locked status update.
        Returns rows affected (0 means version mismatch — retry).
        """
        q = (
            update(Order)
            .where(Order.id == order_id)
            .values(status=status, updated_at=datetime.now(UTC))
        )
        if version is not None:
            q = q.where(Order.version == version)
        result = await self.db.execute(q)
        return result.rowcount

    async def add_event(self, event: OrderEvent) -> None:
        self.db.add(event)
        await self.db.flush()
