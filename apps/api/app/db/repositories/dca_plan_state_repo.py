"""DCA plan state repository.

Provides CRUD/upsert access to the dca_plan_states table.
One row per (ticker, venue) pair holds the persistent state for KrakenDCAPlanner.

PAPER_ONLY prerequisite layer only — no scheduler, no execution wiring.
The scheduler task (not yet implemented) will call upsert() after each
BUY_DUE evaluation to persist updated state back to the database.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DcaPlanState


class DcaPlanStateRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_ticker_venue(self, ticker: str, venue: str) -> DcaPlanState | None:
        result = await self.db.execute(
            select(DcaPlanState).where(
                DcaPlanState.ticker == ticker,
                DcaPlanState.venue == venue,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, state: DcaPlanState) -> DcaPlanState:
        self.db.add(state)
        await self.db.flush()
        return state

    async def upsert(
        self,
        ticker: str,
        venue: str,
        updates: dict,
    ) -> DcaPlanState:
        """Get-or-create a state row, then apply updates.

        Scheduler contract: after a BUY_DUE decision, pass at minimum:
          updates={
              "last_buy_at": <date>,
              "last_decision_at": <date>,
              "total_allocated_usd": <Decimal>,
              "executions_count": <int>,
              "last_decision_code": "BUY_DUE",
              "last_reason": <str>,
          }
        For non-buying decisions, pass only the fields that changed
        (e.g. last_decision_at, last_decision_code, last_reason).
        """
        result = await self.db.execute(
            select(DcaPlanState).where(
                DcaPlanState.ticker == ticker,
                DcaPlanState.venue == venue,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = DcaPlanState(ticker=ticker, venue=venue, **updates)
            self.db.add(row)
        else:
            for key, value in updates.items():
                setattr(row, key, value)
        await self.db.flush()
        return row

    async def list_all(self) -> Sequence[DcaPlanState]:
        result = await self.db.execute(
            select(DcaPlanState).order_by(DcaPlanState.ticker, DcaPlanState.venue)
        )
        return result.scalars().all()


def dca_state_from_row(row: DcaPlanState):
    """Convert a DcaPlanState ORM row to a DCAState dataclass for the planner.

    The next pass (scheduler task) will call this before handing state into
    KrakenDCAPlanner.evaluate_plan(). Import is deferred to avoid a circular
    dependency between the DB layer and the strategies layer.
    """
    from app.strategies.kraken_dca_planner import DCAState

    return DCAState(
        ticker=row.ticker,
        venue=row.venue,
        last_buy_at=row.last_buy_at,
        last_decision_at=row.last_decision_at,
        total_allocated_usd=row.total_allocated_usd,
        executions_count=row.executions_count,
        last_decision_code=row.last_decision_code,
        last_reason=row.last_reason,
    )
