"""DCA config repository.

Provides access to persistent paper-only DCA plan configuration.
One row per (ticker, venue) pair holds the policy inputs for KrakenDCAPlanner.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import DcaConfig

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.strategies.kraken_dca_planner import DCAConfig as PlannerDCAConfig


class DcaConfigRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, config_id: uuid.UUID) -> DcaConfig | None:
        result = await self.db.execute(
            select(DcaConfig).where(DcaConfig.id == config_id)
        )
        return result.scalar_one_or_none()

    async def get_by_ticker_venue(self, ticker: str, venue: str) -> DcaConfig | None:
        result = await self.db.execute(
            select(DcaConfig).where(
                DcaConfig.ticker == ticker,
                DcaConfig.venue == venue,
            )
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> Sequence[DcaConfig]:
        result = await self.db.execute(
            select(DcaConfig).order_by(DcaConfig.ticker, DcaConfig.venue)
        )
        rows: Sequence[DcaConfig] = result.scalars().all()
        return rows

    async def list_enabled(self) -> Sequence[DcaConfig]:
        result = await self.db.execute(
            select(DcaConfig)
            .where(
                DcaConfig.enabled == True,  # noqa: E712
                DcaConfig.paper_only == True,  # noqa: E712
            )
            .order_by(DcaConfig.ticker, DcaConfig.venue)
        )
        rows: Sequence[DcaConfig] = result.scalars().all()
        return rows

    async def create(self, config: DcaConfig) -> DcaConfig:
        self.db.add(config)
        await self.db.flush()
        return config

    async def update(self, config: DcaConfig, updates: dict[str, Any]) -> DcaConfig:
        for key, value in updates.items():
            setattr(config, key, value)
        await self.db.flush()
        return config


def dca_config_from_row(row: DcaConfig) -> PlannerDCAConfig:
    """Convert a DcaConfig ORM row to the planner's DCAConfig dataclass."""
    from app.strategies.kraken_dca_planner import DCAConfig

    return DCAConfig(
        ticker=row.ticker,
        cadence_days=row.cadence_days,
        base_allocation_usd=row.fixed_cash_amount,
        enable_dip_enhancement=row.dip_buy_enabled,
        dip_threshold_pct=float(row.dip_threshold_pct),
        dip_multiplier=float(row.dip_buy_multiplier),
        dip_ema_period=row.dip_ema_period,
        min_cash_reserve_usd=row.min_cash_reserve,
        max_position_pct=float(row.max_position_percent),
        paper_only=row.paper_only,
        enabled=row.enabled,
        venue=row.venue,
    )
