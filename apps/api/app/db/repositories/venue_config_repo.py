"""Venue config repository — reads venue-level safety controls."""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VenueConfig


class VenueConfigRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_venue(self, venue: str) -> VenueConfig | None:
        result = await self.db.execute(
            select(VenueConfig).where(VenueConfig.venue == venue)
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> Sequence[VenueConfig]:
        result = await self.db.execute(select(VenueConfig))
        return result.scalars().all()
