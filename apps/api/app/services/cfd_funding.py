"""
CFD Overnight Funding Cost Service.

Computes and persists the daily financing charge for every open CFD position
at end-of-day (22:00 UTC, before EOD flatten decisions).

Formula (Chan 2013; T212 documentation):
  notional     = quantity × price_at_close
  daily_charge = notional × (annual_rate_pct / 100) / 360

The annual_rate_pct is either:
  1. Fetched from broker position data ('overnightFee' field in T212 API)
  2. Derived from app setting 'cfd_default_annual_rate_pct' (default 5.5%)
  3. Falls back to a hard-coded 5.5% (current USD Fed Funds + typical spread)

Cumulative funding costs are subtracted from strategy P&L in the performance
attribution report, giving a realistic net return.

Usage (from Celery task):
    await track_cfd_funding(db, positions, strategy_map)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CFDFundingCost

log = structlog.get_logger()

# Default annual rate used when broker doesn't provide one
DEFAULT_ANNUAL_RATE_PCT = Decimal("5.5")


async def track_cfd_funding(
    db: AsyncSession,
    positions: list[dict[str, Any]],
    strategy_map: dict[str, str] | None = None,
    default_rate_pct: Decimal = DEFAULT_ANNUAL_RATE_PCT,
) -> list[CFDFundingCost]:
    """
    Compute and persist overnight funding costs for all open CFD positions.

    Args:
        positions:    Broker position list. Each dict should contain at least:
                      'ticker', 'quantity', 'currentPrice' (or 'current_price'),
                      and optionally 'overnightFee' (annual % as float).
        strategy_map: Optional {ticker: strategy_id_str} for attribution.
        default_rate_pct: Annual rate to use when broker doesn't provide one.

    Returns:
        List of persisted CFDFundingCost records.
    """
    records: list[CFDFundingCost] = []
    now = datetime.now(timezone.utc)

    for pos in positions:
        ticker = pos.get("ticker") or pos.get("symbol", "")
        if not ticker:
            continue

        # Extract quantity and price
        try:
            qty = Decimal(str(pos.get("quantity") or pos.get("qty") or "0"))
            price = Decimal(str(
                pos.get("currentPrice") or pos.get("current_price") or
                pos.get("averagePrice") or pos.get("avg_price") or "0"
            ))
        except Exception:
            continue

        if qty <= 0 or price <= 0:
            continue

        # Determine if this is a CFD (T212 uses isCfd flag or instrument type)
        # If broker data doesn't tag it, we treat all positions as potential CFDs
        # when the task is called (caller is responsible for filtering)
        annual_rate = default_rate_pct
        broker_rate = pos.get("overnightFee") or pos.get("overnight_fee")
        if broker_rate is not None:
            try:
                annual_rate = Decimal(str(broker_rate))
            except Exception:
                pass

        notional = qty * price
        # daily_charge = notional × (rate / 100) / 360
        daily_charge = notional * (annual_rate / 100) / 360

        strategy_id_str = (strategy_map or {}).get(ticker)
        strategy_id = uuid.UUID(strategy_id_str) if strategy_id_str else None

        record = CFDFundingCost(
            id=uuid.uuid4(),
            ticker=ticker,
            strategy_id=strategy_id,
            quantity=qty,
            price_at_close=price,
            notional=notional,
            annual_rate_pct=annual_rate,
            daily_charge=daily_charge,
            currency=pos.get("currency", "USD"),
            recorded_at=now,
        )
        db.add(record)
        records.append(record)

        log.info(
            "cfd_funding.charged",
            ticker=ticker,
            notional=float(notional),
            rate_pct=float(annual_rate),
            daily_charge=float(daily_charge),
        )

    if records:
        await db.flush()

    return records


async def get_funding_costs_summary(
    db: AsyncSession,
    days: int = 30,
) -> dict[str, Any]:
    """
    Returns total and per-ticker funding costs over the last N days.
    Used in the performance attribution report.
    """
    from datetime import timedelta
    from sqlalchemy import func, select

    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(
            CFDFundingCost.ticker,
            func.sum(CFDFundingCost.daily_charge).label("total_charge"),
            func.count(CFDFundingCost.id).label("days"),
        )
        .where(CFDFundingCost.recorded_at >= since)
        .group_by(CFDFundingCost.ticker)
        .order_by(func.sum(CFDFundingCost.daily_charge).desc())
    )
    rows = result.all()

    total = sum(float(r.total_charge) for r in rows)
    by_ticker = [
        {
            "ticker": r.ticker,
            "total_charge": round(float(r.total_charge), 4),
            "days_charged": r.days,
            "avg_daily": round(float(r.total_charge) / max(r.days, 1), 4),
        }
        for r in rows
    ]

    return {
        "period_days": days,
        "total_funding_cost": round(total, 4),
        "by_ticker": by_ticker,
    }
