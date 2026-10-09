"""Strategy repository."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import RiskProfile, Signal, Strategy

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class StrategyRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, strategy_id: uuid.UUID) -> Strategy | None:
        result = await self.db.execute(
            select(Strategy)
            .where(Strategy.id == strategy_id)
            .options(selectinload(Strategy.risk_profile))
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, strategy_id: uuid.UUID) -> Strategy | None:
        result = await self.db.execute(
            select(Strategy)
            .where(Strategy.id == strategy_id)
            .options(selectinload(Strategy.risk_profile))
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def list_enabled(self) -> Sequence[Strategy]:
        result = await self.db.execute(
            select(Strategy)
            .where(Strategy.is_enabled == True)  # noqa: E712
            .options(selectinload(Strategy.risk_profile))
        )
        return result.scalars().all()

    async def list_all(self) -> Sequence[Strategy]:
        result = await self.db.execute(
            select(Strategy)
            .options(selectinload(Strategy.risk_profile))
            .order_by(Strategy.created_at.desc())
        )
        return result.scalars().all()

    async def create(self, strategy: Strategy) -> Strategy:
        self.db.add(strategy)
        await self.db.flush()
        return strategy

    async def get_signals(self, strategy_id: uuid.UUID, limit: int = 50) -> Sequence[Signal]:
        result = await self.db.execute(
            select(Signal)
            .where(Signal.strategy_id == strategy_id)
            .order_by(Signal.generated_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def get_default_risk_profile(self) -> RiskProfile | None:
        result = await self.db.execute(
            select(RiskProfile).where(RiskProfile.is_default == True)  # noqa: E712
        )
        return result.scalar_one_or_none()
