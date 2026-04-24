"""Market intelligence routes: regime and watchlist catalyst context."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.deps import get_current_user
from app.api.schemas import MarketRegimeOut, WatchlistIntelligenceOut
from app.db.models import Strategy, User
from app.db.session import get_db
from app.services.market_regime import MarketRegimeService
from app.services.news_intelligence import NewsIntelligenceService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/intelligence", tags=["intelligence"])
regime_router = APIRouter(prefix="/regime", tags=["regime"])


async def _market_regime_payload() -> dict[str, Any]:
    return await MarketRegimeService().evaluate()


@regime_router.get("", response_model=MarketRegimeOut)
async def get_market_regime(_: User = Depends(get_current_user)) -> MarketRegimeOut:
    return MarketRegimeOut(**(await _market_regime_payload()))


@router.get("/regime", response_model=MarketRegimeOut)
async def get_market_regime_intelligence(_: User = Depends(get_current_user)) -> MarketRegimeOut:
    return MarketRegimeOut(**(await _market_regime_payload()))


@router.get("/watchlist", response_model=WatchlistIntelligenceOut)
async def get_watchlist_intelligence(
    limit: int = Query(default=8, ge=1, le=20),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WatchlistIntelligenceOut:
    result = await db.execute(
        select(Strategy).where(Strategy.is_enabled == True)  # noqa: E712
    )
    strategies = result.scalars().all()

    watchlist: list[str] = []
    for strategy in strategies:
        params = strategy.params or {}
        todays = params.get("todays_watchlist")
        if isinstance(todays, list) and todays:
            watchlist.extend(str(item).upper() for item in todays)
        else:
            watchlist.extend(str(item).upper() for item in strategy.allowed_tickers)

    deduped_watchlist = list(dict.fromkeys(watchlist))[:20]
    news = await NewsIntelligenceService().get_watchlist_intelligence(deduped_watchlist, limit=limit)
    return WatchlistIntelligenceOut(
        watchlist=deduped_watchlist,
        news=news,
        count=len(deduped_watchlist),
    )
