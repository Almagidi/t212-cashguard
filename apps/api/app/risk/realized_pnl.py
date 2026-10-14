"""Today's realised profit or loss of real trades: the figure the daily-loss limit reads.

One query for every caller (the strategy runner and the manual order route), so the limit is
measured the same way however an order is placed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.db.models import Trade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def realized_pnl_today(db: AsyncSession, now: datetime | None = None) -> Decimal:
    """Sum of ``realized_pnl`` of real trades closed since 00:00 UTC today."""
    today_start = (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    result = await db.execute(
        select(func.sum(Trade.realized_pnl)).where(
            Trade.closed_at >= today_start,
            Trade.closed_at < tomorrow_start,
            Trade.is_dry_run == False,  # noqa: E712
            Trade.realized_pnl.isnot(None),
        )
    )
    realized = result.scalar_one()
    return Decimal("0") if realized is None else Decimal(str(realized))
