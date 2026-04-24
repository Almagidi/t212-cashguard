"""Reports routes — performance, trade history, exports."""
from __future__ import annotations

import csv
import io
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import OrderOut, PerformanceReport
from app.db.models import Order, Signal, Strategy, Trade, User
from app.db.session import get_db

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/performance", response_model=PerformanceReport)
async def get_performance(
    days: int = Query(30, ge=1, le=365),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PerformanceReport:
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Select only the columns used by this report so it still works against
    # older databases where journal fields may not have been migrated yet.
    trades_result = await db.execute(
        select(Trade.closed_at, Trade.realized_pnl)
        .where(Trade.closed_at >= since, Trade.is_dry_run == False)  # noqa: E712
        .order_by(Trade.closed_at)
    )
    trades = trades_result.all()

    if not trades:
        return PerformanceReport(
            total_trades=0, winning_trades=0, losing_trades=0,
            win_rate=0.0, total_pnl=0.0, avg_win=0.0, avg_loss=0.0,
            profit_factor=0.0, max_drawdown=0.0, sharpe_ratio=None,
            daily_pnl=[],
        )

    pnls = [float(realized_pnl or 0) for _, realized_pnl in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0

    # Daily P&L bucketing
    daily: dict[str, float] = {}
    for closed_at, realized_pnl in trades:
        if closed_at:
            day = closed_at.strftime("%Y-%m-%d")
            daily[day] = daily.get(day, 0.0) + float(realized_pnl or 0)

    # Max drawdown from equity curve
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    # Sharpe ratio (annualised, simplified — assumes 252 trading days)
    import statistics
    sharpe = None
    if len(pnls) > 1 and statistics.stdev(pnls) > 0:
        mean_pnl = statistics.mean(pnls)
        std_pnl = statistics.stdev(pnls)
        sharpe = round((mean_pnl / std_pnl) * (252 ** 0.5), 3)

    return PerformanceReport(
        total_trades=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=round(len(wins) / len(trades), 4) if trades else 0.0,
        total_pnl=round(total_pnl, 2),
        avg_win=round(sum(wins) / len(wins), 2) if wins else 0.0,
        avg_loss=round(sum(losses) / len(losses), 2) if losses else 0.0,
        profit_factor=round(gross_profit / gross_loss, 3) if gross_loss > 0 else 0.0,
        max_drawdown=round(max_dd, 2),
        sharpe_ratio=sharpe,
        daily_pnl=[{"date": d, "pnl": round(p, 2)} for d, p in sorted(daily.items())],
    )


@router.get("/trades", response_model=list[OrderOut])
async def get_trades_report(
    limit: int = Query(100, ge=1, le=1000),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Order]:
    result = await db.execute(
        select(Order)
        .where(Order.status == "filled")
        .order_by(desc(Order.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())


# ── Per-strategy performance breakdown ───────────────────────────────────────

@router.get("/performance/by-strategy")
async def get_performance_by_strategy(
    days: int = Query(30, ge=1, le=365),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """
    Per-strategy P&L breakdown: win rate, profit factor, Sharpe, Sortino.
    Only includes closed (non-dry-run) trades joined to their originating strategy.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    trades_result = await db.execute(
        select(Trade.strategy_id, Trade.realized_pnl, Strategy.name, Strategy.type, Trade.closed_at)
        .join(Strategy, Trade.strategy_id == Strategy.id, isouter=True)
        .where(Trade.closed_at >= since, Trade.is_dry_run == False)  # noqa: E712
        .order_by(Trade.closed_at)
    )
    rows = trades_result.all()

    # Group trades by strategy name
    by_strategy: dict[str, list[float]] = {}
    for strategy_id, realized_pnl, strat_name, _strat_type, _closed_at in rows:
        key = strat_name or f"strategy:{strategy_id}"
        by_strategy.setdefault(key, []).append(float(realized_pnl or 0))

    results: list[dict[str, Any]] = []
    for name, pnls in by_strategy.items():
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_profit = sum(wins)
        gross_loss   = abs(sum(losses)) if losses else 0.0
        total_pnl    = sum(pnls)

        # Sharpe (annualised, simplified)
        sharpe = None
        if len(pnls) > 1 and statistics.stdev(pnls) > 0:
            sharpe = round(statistics.mean(pnls) / statistics.stdev(pnls) * (252 ** 0.5), 3)

        # Sortino (downside deviation only)
        sortino = None
        neg_pnls = [p for p in pnls if p < 0]
        if len(neg_pnls) > 1:
            downside_std = statistics.stdev(neg_pnls)
            if downside_std > 0:
                sortino = round(statistics.mean(pnls) / downside_std * (252 ** 0.5), 3)

        # Max consecutive losses
        max_consec = 0
        consec = 0
        for p in pnls:
            if p < 0:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 0

        results.append({
            "strategy": name,
            "total_trades": len(pnls),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(len(wins) / len(pnls), 4) if pnls else 0.0,
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else 0.0,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "max_consecutive_losses": max_consec,
        })

    # Sort by total P&L descending
    results.sort(key=lambda r: r["total_pnl"], reverse=True)
    return results


# ── CSV trade export ──────────────────────────────────────────────────────────

@router.get("/export/trades.csv")
async def export_trades_csv(
    days: int = Query(90, ge=1, le=730),
    include_dry_run: bool = Query(False),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """
    Download a CSV of all closed trades.
    Columns: date, ticker, side, qty, open_price, close_price, pnl, strategy, dry_run
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(
        Trade.closed_at,
        Trade.ticker,
        Trade.side,
        Trade.quantity,
        Trade.open_price,
        Trade.close_price,
        Trade.realized_pnl,
        Trade.is_dry_run,
        Strategy.name,
    ).join(
        Strategy, Trade.strategy_id == Strategy.id, isouter=True
    ).where(Trade.closed_at >= since)
    if not include_dry_run:
        q = q.where(Trade.is_dry_run == False)  # noqa: E712
    q = q.order_by(Trade.closed_at)

    rows = (await db.execute(q)).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "date", "ticker", "side", "quantity",
        "open_price", "close_price", "realized_pnl",
        "strategy", "is_dry_run",
    ])
    for closed_at, ticker, side, quantity, open_price, close_price, realized_pnl, is_dry_run, strat_name in rows:
        writer.writerow([
            closed_at.strftime("%Y-%m-%d %H:%M:%S") if closed_at else "",
            ticker,
            side,
            float(quantity),
            float(open_price),
            float(close_price or 0),
            float(realized_pnl or 0),
            strat_name or "",
            is_dry_run,
        ])

    output.seek(0)
    filename = f"trades_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export/audit.csv")
async def export_audit_csv(
    days: int = Query(30, ge=1, le=365),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Download AuditLog as CSV for compliance review."""
    from app.db.models import AuditLog
    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.occurred_at >= since)
        .order_by(AuditLog.occurred_at)
    )
    logs = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "action", "entity_type", "entity_id", "actor", "payload"])
    for entry in logs:
        writer.writerow([
            entry.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
            entry.action,
            entry.entity_type or "",
            entry.entity_id or "",
            entry.actor,
            str(entry.payload or ""),
        ])

    output.seek(0)
    filename = f"audit_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
