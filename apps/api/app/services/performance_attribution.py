"""
Performance attribution service.
Tracks execution quality, slippage, and P&L by strategy/symbol/time.

Answers:
- Which symbols are actually profitable?
- What time of day produces best results?
- Are we getting good fills vs expected prices?
- Are stops being hit too early (MAE analysis)?
- Are we leaving money on the table (MFE analysis)?
- What's our actual vs theoretical R:R achieved?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from app.db.models import Order, Signal, Trade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class SlippageRecord:
    order_id: str
    ticker: str
    side: str
    expected_price: Decimal  # Signal entry price
    actual_price: Decimal  # Actual fill price
    slippage_pct: Decimal  # (actual - expected) / expected * 100
    slippage_dollars: Decimal  # slippage_pct * position_value
    timestamp: datetime


@dataclass
class SymbolAttribution:
    ticker: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    avg_win: float
    avg_loss: float
    avg_slippage_pct: float
    total_slippage_cost: float
    contribution_pct: float  # % of total portfolio P&L from this symbol


@dataclass
class TimeAttribution:
    hour_et: int
    trades: int
    win_rate: float
    avg_pnl: float
    best_period: bool


@dataclass
class StrategyAttribution:
    strategy_name: str
    strategy_type: str
    total_signals: int
    signals_traded: int
    signals_filtered: int  # Blocked by risk engine
    win_rate: float
    total_pnl: float
    sharpe_ratio: float | None
    avg_confidence_correct: float  # Avg confidence of winning signals
    avg_confidence_wrong: float  # Avg confidence of losing signals


class PerformanceAttributor:
    """
    Generates detailed performance attribution reports from the database.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def slippage_report(self, days: int = 30) -> list[SlippageRecord]:
        """
        Compare expected fill price (from signal) to actual fill price (from order).
        Identifies systematic fill quality issues.
        """
        from datetime import timedelta

        since = datetime.now(UTC) - timedelta(days=days)

        # Get filled orders with their associated signals
        result = await self.db.execute(
            select(Order, Signal)
            .join(Signal, Order.signal_id == Signal.id, isouter=True)
            .where(
                Order.status == "filled",
                Order.created_at >= since,
                Order.avg_fill_price.isnot(None),
                Order.is_dry_run == False,  # noqa: E712
            )
            .order_by(desc(Order.created_at))
        )
        rows = result.all()

        records = []
        for order, signal in rows:
            if signal is None or signal.entry_price is None:
                continue
            if order.avg_fill_price is None:
                continue

            expected = signal.entry_price
            actual = order.avg_fill_price
            position_value = actual * abs(order.quantity)

            if expected <= 0:
                continue

            slip_pct = (actual - expected) / expected * 100
            # For sells, slippage is when we get less than expected
            if order.side == "sell":
                slip_pct = (expected - actual) / expected * 100

            slip_dollars = abs(slip_pct / 100) * position_value

            records.append(
                SlippageRecord(
                    order_id=str(order.id),
                    ticker=order.ticker,
                    side=order.side,
                    expected_price=expected,
                    actual_price=actual,
                    slippage_pct=slip_pct.quantize(Decimal("0.001")),
                    slippage_dollars=slip_dollars.quantize(Decimal("0.01")),
                    timestamp=order.created_at,
                )
            )

        return records

    async def symbol_attribution(self, days: int = 90) -> list[SymbolAttribution]:
        """
        P&L and execution quality breakdown by symbol.
        """
        from datetime import timedelta

        since = datetime.now(UTC) - timedelta(days=days)

        result = await self.db.execute(
            select(Trade).where(
                Trade.closed_at >= since,
                Trade.is_dry_run == False,  # noqa: E712
                Trade.realized_pnl.isnot(None),
            )
        )
        trades = result.scalars().all()

        if not trades:
            return []

        # Group by ticker
        by_ticker: dict[str, list[Trade]] = {}
        for t in trades:
            by_ticker.setdefault(t.ticker, []).append(t)

        total_pnl = sum(float(t.realized_pnl or 0) for t in trades)
        attrs = []

        for ticker, ticker_trades in by_ticker.items():
            pnls = [float(t.realized_pnl or 0) for t in ticker_trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            sym_pnl = sum(pnls)

            attrs.append(
                SymbolAttribution(
                    ticker=ticker,
                    total_trades=len(ticker_trades),
                    winning_trades=len(wins),
                    losing_trades=len(losses),
                    win_rate=len(wins) / len(ticker_trades),
                    total_pnl=round(sym_pnl, 2),
                    avg_pnl=round(sym_pnl / len(ticker_trades), 2),
                    avg_win=round(sum(wins) / len(wins), 2) if wins else 0.0,
                    avg_loss=round(sum(losses) / len(losses), 2) if losses else 0.0,
                    avg_slippage_pct=0.0,  # TODO: join with slippage records
                    total_slippage_cost=0.0,
                    contribution_pct=round(sym_pnl / total_pnl * 100, 1) if total_pnl != 0 else 0.0,
                )
            )

        attrs.sort(key=lambda a: a.total_pnl, reverse=True)
        return attrs

    async def time_of_day_attribution(self, days: int = 90) -> list[TimeAttribution]:
        """
        Win rate and avg P&L by hour of day (ET).
        Reveals which sessions are profitable (morning vs afternoon).
        """
        from datetime import timedelta

        import pytz

        since = datetime.now(UTC) - timedelta(days=days)
        et_tz = pytz.timezone("America/New_York")

        result = await self.db.execute(
            select(Trade).where(
                Trade.opened_at >= since,
                Trade.is_dry_run == False,  # noqa: E712
                Trade.realized_pnl.isnot(None),
            )
        )
        trades = result.scalars().all()

        by_hour: dict[int, list[float]] = {}
        for t in trades:
            et_time = t.opened_at.astimezone(et_tz)
            hour = et_time.hour
            by_hour.setdefault(hour, []).append(float(t.realized_pnl or 0))

        if not by_hour:
            return []

        all_avg_pnls = [sum(pnls) / len(pnls) for pnls in by_hour.values()]
        best_avg = max(all_avg_pnls) if all_avg_pnls else 0

        attrs = []
        for hour in sorted(by_hour.keys()):
            pnls = by_hour[hour]
            wins = [p for p in pnls if p > 0]
            avg_pnl = sum(pnls) / len(pnls)
            attrs.append(
                TimeAttribution(
                    hour_et=hour,
                    trades=len(pnls),
                    win_rate=round(len(wins) / len(pnls), 3),
                    avg_pnl=round(avg_pnl, 2),
                    best_period=(avg_pnl == best_avg),
                )
            )

        return attrs

    async def mfe_mae_analysis(self, days: int = 90) -> dict[str, Any]:
        """
        MFE (Maximum Favourable Excursion) vs MAE (Maximum Adverse Excursion).

        Key insight: If avg MFE >> avg winning trade P&L, we're exiting too early.
        If avg MAE >> stop distance, our stops are consistently wrong.
        """
        from datetime import timedelta

        since = datetime.now(UTC) - timedelta(days=days)

        result = await self.db.execute(
            select(Trade).where(
                Trade.closed_at >= since,
                Trade.is_dry_run == False,  # noqa: E712
            )
        )
        trades = result.scalars().all()

        # We'd need MFE/MAE stored on trades for this.
        # For now, compute a proxy from order data.
        return {
            "message": "MFE/MAE tracking requires position-level OHLC data",
            "trades_analysed": len(trades),
            "recommendation": (
                "Enable trade-level MFE/MAE tracking by storing "
                "the highest/lowest price seen while position is open."
            ),
        }

    async def full_report(self, days: int = 30) -> dict[str, Any]:
        """Generate the complete attribution report."""
        slippage = await self.slippage_report(days)
        symbols = await self.symbol_attribution(days)
        time_of_day = await self.time_of_day_attribution(days)
        mfe_mae = await self.mfe_mae_analysis(days)

        total_slippage_cost = sum(float(s.slippage_dollars) for s in slippage)
        avg_slippage_pct = (
            sum(float(s.slippage_pct) for s in slippage) / len(slippage) if slippage else 0.0
        )

        return {
            "period_days": days,
            "generated_at": datetime.now(UTC).isoformat(),
            "execution_quality": {
                "total_orders_analysed": len(slippage),
                "total_slippage_cost": round(total_slippage_cost, 2),
                "avg_slippage_pct": round(avg_slippage_pct, 4),
                "worst_fills": [
                    {
                        "ticker": s.ticker,
                        "slippage_pct": float(s.slippage_pct),
                        "slippage_dollars": float(s.slippage_dollars),
                        "timestamp": s.timestamp.isoformat(),
                    }
                    for s in sorted(
                        slippage, key=lambda x: float(x.slippage_dollars), reverse=True
                    )[:5]
                ],
            },
            "symbol_attribution": [
                {
                    "ticker": a.ticker,
                    "trades": a.total_trades,
                    "win_rate_pct": round(a.win_rate * 100, 1),
                    "total_pnl": a.total_pnl,
                    "avg_pnl": a.avg_pnl,
                    "contribution_pct": a.contribution_pct,
                }
                for a in symbols
            ],
            "time_of_day": [
                {
                    "hour_et": a.hour_et,
                    "session": _hour_to_session(a.hour_et),
                    "trades": a.trades,
                    "win_rate_pct": round(a.win_rate * 100, 1),
                    "avg_pnl": a.avg_pnl,
                    "best_period": a.best_period,
                }
                for a in time_of_day
            ],
            "mfe_mae": mfe_mae,
        }


def _hour_to_session(hour_et: int) -> str:
    if 9 <= hour_et < 10:
        return "Opening (09:00-10:00)"
    if 10 <= hour_et < 12:
        return "Morning (10:00-12:00)"
    if 12 <= hour_et < 14:
        return "Lunch (12:00-14:00)"
    if 14 <= hour_et < 16:
        return "Afternoon (14:00-16:00)"
    return "Extended hours"
