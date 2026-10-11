"""
CFD Overnight Funding Cost Service.

Computes and persists the daily financing charge for every open CFD position
at end-of-day (22:00 UTC, before EOD flatten decisions).

This application is cash-only by default (`CASH_ONLY_MODE`): it holds shares, not CFDs, and
shares carry no overnight financing. In cash-only mode nothing is recorded. Outside it, only
a position the broker explicitly marks as a CFD is charged; an unmarked position never is.

Nothing here prevents a second run on the same day from recording a marked CFD position
twice. The table has no account, environment or instrument-identity column, so a correct
uniqueness rule cannot be expressed yet; it must be added before any CFD mode is used.

Formula (Chan 2013; T212 documentation):
  notional     = quantity x price_at_close
  daily_charge = notional x (annual_rate_pct / 100) / 360

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

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import CFDFundingCost

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

# Default annual rate used when broker doesn't provide one
DEFAULT_ANNUAL_RATE_PCT = Decimal("5.5")
_CFD_INSTRUMENT_TYPE = "CFD"
_CFD_FLAG_KEYS = ("isCfd", "is_cfd")
_INSTRUMENT_TYPE_KEYS = ("instrumentType", "instrument_type")


def _is_cfd_position(position: dict[str, Any]) -> bool:
    """True only when a recognised field says the position is a CFD and none says otherwise.

    The recognised fields are the flags ``isCfd`` / ``is_cfd`` and the instrument types
    ``instrumentType`` / ``instrument_type``; no other field is read. A flag counts only when
    it is the boolean ``True``. Any other flag value, or any instrument type other than "CFD"
    (an empty one included), means the position is not charged; a field that is absent or
    ``None`` says nothing. No broker adapter in this application sets these fields today, so
    with the shipped adapters nothing is charged.
    """
    flags = [position[key] for key in _CFD_FLAG_KEYS if position.get(key) is not None]
    types = [
        str(position[key]).strip().upper()
        for key in _INSTRUMENT_TYPE_KEYS
        if position.get(key) is not None
    ]
    says_cfd = any(flag is True for flag in flags) or _CFD_INSTRUMENT_TYPE in types
    says_otherwise = any(flag is not True for flag in flags) or any(
        instrument_type != _CFD_INSTRUMENT_TYPE for instrument_type in types
    )
    return says_cfd and not says_otherwise


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
                      and optionally 'overnightFee' (annual % as float). Only a
                      position marked as a CFD ('isCfd' is True or 'instrumentType'
                      is "CFD") is charged; in cash-only mode none is.
        strategy_map: Optional {ticker: strategy_id_str} for attribution.
        default_rate_pct: Annual rate to use when broker doesn't provide one.

    Returns:
        List of persisted CFDFundingCost records.
    """
    records: list[CFDFundingCost] = []
    if settings.CASH_ONLY_MODE:
        # A cash-only account holds no CFDs, so there is no financing to record.
        log.info("cfd_funding.skipped_cash_only", positions=len(positions))
        return records
    now = datetime.now(UTC)

    for pos in positions:
        if not _is_cfd_position(pos):
            continue
        ticker = pos.get("ticker") or pos.get("symbol", "")
        if not ticker:
            continue

        # Extract quantity and price
        try:
            qty = Decimal(str(pos.get("quantity") or pos.get("qty") or "0"))
            price = Decimal(
                str(
                    pos.get("currentPrice")
                    or pos.get("current_price")
                    or pos.get("averagePrice")
                    or pos.get("avg_price")
                    or "0"
                )
            )
        except Exception:
            continue

        if qty <= 0 or price <= 0:
            continue

        annual_rate = default_rate_pct
        broker_rate = pos.get("overnightFee") or pos.get("overnight_fee")
        if broker_rate is not None:
            with contextlib.suppress(Exception):
                annual_rate = Decimal(str(broker_rate))

        notional = qty * price
        # daily_charge = notional x (rate / 100) / 360
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
    since = datetime.now(UTC) - timedelta(days=days)
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
