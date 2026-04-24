"""
Portfolio sleeve attribution and rebalance timeline service.

Replays filled `portfolio_rebalance` orders for a strategy against daily bar
history so the UI can show how a sleeve's cash, exposure, and mark-to-market
P&L evolved over time.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import Order, Signal, Strategy
from app.market_data import get_live_provider
from app.strategies.indicators import Bar

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class SleeveOrderFill:
    order_id: str
    signal_id: str | None
    occurred_at: datetime
    ticker: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    is_dry_run: bool
    target_weight: Decimal | None


@dataclass
class PositionLedger:
    quantity: Decimal = Decimal("0")
    avg_cost: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")


@dataclass
class TimelinePoint:
    date: date
    equity_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    cash_balance: Decimal
    gross_exposure: Decimal
    turnover_notional: Decimal
    order_count: int


@dataclass
class TickerAttribution:
    ticker: str
    quantity: Decimal
    avg_cost: Decimal
    market_price: Decimal
    market_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    weight_pct: Decimal


@dataclass
class SleeveAttribution:
    strategy_id: str
    strategy_name: str
    strategy_type: str
    computed_at: datetime
    timeline: list[TimelinePoint] = field(default_factory=list)
    ticker_attribution: list[TickerAttribution] = field(default_factory=list)
    total_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    cash_balance: Decimal = Decimal("0")
    current_market_value: Decimal = Decimal("0")
    turnover_notional: Decimal = Decimal("0")
    buys_notional: Decimal = Decimal("0")
    sells_notional: Decimal = Decimal("0")
    rebalance_days: int = 0
    order_count: int = 0


class PortfolioAttributionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_for_strategy(self, strategy: Strategy) -> SleeveAttribution:
        fills = await self._load_order_fills(strategy.id)
        if not fills:
            return SleeveAttribution(
                strategy_id=str(strategy.id),
                strategy_name=strategy.name,
                strategy_type=strategy.type,
                computed_at=datetime.now(UTC),
            )

        histories = await self._load_histories(fills)
        timeline, ledger, latest_prices = self._replay(fills, histories)

        ticker_attribution = self._build_ticker_attribution(ledger, latest_prices)
        last_point = timeline[-1] if timeline else TimelinePoint(
            date=datetime.now(UTC).date(),
            equity_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            cash_balance=Decimal("0"),
            gross_exposure=Decimal("0"),
            turnover_notional=Decimal("0"),
            order_count=0,
        )
        turnover_notional = sum((point.turnover_notional for point in timeline), Decimal("0"))
        buys_notional = sum(
            (fill.quantity * fill.fill_price for fill in fills if fill.side == "buy"),
            Decimal("0"),
        )
        sells_notional = sum(
            (fill.quantity * fill.fill_price for fill in fills if fill.side == "sell"),
            Decimal("0"),
        )

        return SleeveAttribution(
            strategy_id=str(strategy.id),
            strategy_name=strategy.name,
            strategy_type=strategy.type,
            computed_at=datetime.now(UTC),
            timeline=timeline,
            ticker_attribution=ticker_attribution,
            total_pnl=last_point.equity_pnl,
            realized_pnl=last_point.realized_pnl,
            unrealized_pnl=last_point.unrealized_pnl,
            cash_balance=last_point.cash_balance,
            current_market_value=last_point.gross_exposure,
            turnover_notional=turnover_notional,
            buys_notional=buys_notional,
            sells_notional=sells_notional,
            rebalance_days=sum(1 for point in timeline if point.order_count > 0),
            order_count=len(fills),
        )

    async def _load_order_fills(self, strategy_id: Any) -> list[SleeveOrderFill]:
        result = await self.db.execute(
            select(Order, Signal)
            .join(Signal, Order.signal_id == Signal.id)
            .where(
                Signal.strategy_id == strategy_id,
                Signal.signal_type == "portfolio_rebalance",
                Order.status == "filled",
                Order.avg_fill_price.is_not(None),
            )
            .order_by(Order.created_at.asc())
        )

        fills: list[SleeveOrderFill] = []
        for order, signal in result.all():
            quantity = order.filled_quantity or order.quantity
            if quantity is None or order.avg_fill_price is None:
                continue
            params_snapshot = signal.params_snapshot or {}
            target_weight = params_snapshot.get("target_weight")
            fills.append(
                SleeveOrderFill(
                    order_id=str(order.id),
                    signal_id=str(order.signal_id) if order.signal_id else None,
                    occurred_at=order.created_at,
                    ticker=order.ticker.upper(),
                    side=order.side,
                    quantity=Decimal(str(quantity)),
                    fill_price=Decimal(str(order.avg_fill_price)),
                    is_dry_run=order.is_dry_run,
                    target_weight=Decimal(str(target_weight)) if target_weight is not None else None,
                )
            )
        return fills

    async def _load_histories(self, fills: list[SleeveOrderFill]) -> dict[str, dict[date, Decimal]]:
        tickers = sorted({fill.ticker for fill in fills})
        first_date = min(fill.occurred_at.date() for fill in fills) - timedelta(days=10)
        today = datetime.now(UTC).date()
        approx_days = max(45, (today - first_date).days + 15)

        provider = get_live_provider()
        if hasattr(provider, "__aenter__"):
            async with provider as active_provider:
                return await self._fetch_histories(active_provider, tickers, approx_days)
        return await self._fetch_histories(provider, tickers, approx_days)

    async def _fetch_histories(
        self,
        provider: Any,
        tickers: list[str],
        bars_needed: int,
    ) -> dict[str, dict[date, Decimal]]:
        histories: dict[str, dict[date, Decimal]] = {}
        for ticker in tickers:
            bars, bar_times = await self._fetch_daily_bars(provider, ticker, bars_needed)
            histories[ticker] = {
                ts.date(): bar.close
                for bar, ts in zip(bars, bar_times, strict=True)
            }
        return histories

    async def _fetch_daily_bars(
        self,
        provider: Any,
        ticker: str,
        bars_needed: int,
    ) -> tuple[list[Bar], list[datetime]]:
        if hasattr(provider, "get_bars"):
            raw_bars = await self._maybe_await(
                provider.get_bars(
                    ticker,
                    multiplier=1,
                    timespan="day",
                    limit=bars_needed,
                )
            )
            bars = [
                Bar(
                    open=Decimal(str(bar.open)),
                    high=Decimal(str(bar.high)),
                    low=Decimal(str(bar.low)),
                    close=Decimal(str(bar.close)),
                    volume=Decimal(str(bar.volume)),
                )
                for bar in raw_bars
            ]
            times = [getattr(bar, "timestamp", datetime.now(UTC)) for bar in raw_bars]
            return bars, times

        raw = provider.get_ohlcv(ticker, interval_minutes=1440, bars=bars_needed)
        bars = [
            Bar(
                open=Decimal(str(bar["open"])),
                high=Decimal(str(bar["high"])),
                low=Decimal(str(bar["low"])),
                close=Decimal(str(bar["close"])),
                volume=Decimal(str(bar["volume"])),
            )
            for bar in raw
        ]
        times = [datetime.fromisoformat(str(bar["timestamp"])) for bar in raw]
        return bars, times

    def _replay(
        self,
        fills: list[SleeveOrderFill],
        histories: dict[str, dict[date, Decimal]],
    ) -> tuple[list[TimelinePoint], dict[str, PositionLedger], dict[str, Decimal]]:
        dates = sorted(
            set(fill.occurred_at.date() for fill in fills)
            | {bar_date for per_ticker in histories.values() for bar_date in per_ticker}
        )
        latest_prices: dict[str, Decimal] = {}
        ledger: dict[str, PositionLedger] = {}
        timeline: list[TimelinePoint] = []
        sleeve_cash = Decimal("0")
        fills_by_date: dict[date, list[SleeveOrderFill]] = {}
        for fill in fills:
            fills_by_date.setdefault(fill.occurred_at.date(), []).append(fill)

        for current_date in dates:
            for ticker, per_day in histories.items():
                close = per_day.get(current_date)
                if close is not None:
                    latest_prices[ticker] = close

            realized_total = sum((state.realized_pnl for state in ledger.values()), Decimal("0"))
            turnover = Decimal("0")
            order_count = 0

            for fill in fills_by_date.get(current_date, []):
                state = ledger.setdefault(fill.ticker, PositionLedger())
                notional = fill.quantity * fill.fill_price
                turnover += notional
                order_count += 1
                if fill.side == "buy":
                    new_qty = state.quantity + fill.quantity
                    if new_qty > 0:
                        state.avg_cost = (
                            (state.avg_cost * state.quantity) + (fill.fill_price * fill.quantity)
                        ) / new_qty
                    state.quantity = new_qty
                    sleeve_cash -= notional
                else:
                    sell_qty = min(fill.quantity, state.quantity)
                    state.realized_pnl += (fill.fill_price - state.avg_cost) * sell_qty
                    state.quantity = max(Decimal("0"), state.quantity - sell_qty)
                    if state.quantity == 0:
                        state.avg_cost = Decimal("0")
                    sleeve_cash += fill.fill_price * sell_qty
                realized_total = sum((item.realized_pnl for item in ledger.values()), Decimal("0"))

            market_value = Decimal("0")
            unrealized = Decimal("0")
            for ticker, state in ledger.items():
                if state.quantity <= 0:
                    continue
                mark_price = latest_prices.get(ticker, state.avg_cost)
                market_value += state.quantity * mark_price
                unrealized += (mark_price - state.avg_cost) * state.quantity

            timeline.append(
                TimelinePoint(
                    date=current_date,
                    equity_pnl=sleeve_cash + market_value,
                    realized_pnl=realized_total,
                    unrealized_pnl=unrealized,
                    cash_balance=sleeve_cash,
                    gross_exposure=market_value,
                    turnover_notional=turnover,
                    order_count=order_count,
                )
            )

        return timeline, ledger, latest_prices

    def _build_ticker_attribution(
        self,
        ledger: dict[str, PositionLedger],
        latest_prices: dict[str, Decimal],
    ) -> list[TickerAttribution]:
        current_market_value = sum(
            (state.quantity * latest_prices.get(ticker, state.avg_cost))
            for ticker, state in ledger.items()
            if state.quantity > 0
        )
        attributions: list[TickerAttribution] = []
        for ticker, state in sorted(ledger.items()):
            mark_price = latest_prices.get(ticker, state.avg_cost)
            market_value = state.quantity * mark_price
            unrealized = (mark_price - state.avg_cost) * state.quantity
            total_pnl = state.realized_pnl + unrealized
            weight_pct = Decimal("0")
            if current_market_value > 0 and market_value > 0:
                weight_pct = (market_value / current_market_value) * Decimal("100")
            attributions.append(
                TickerAttribution(
                    ticker=ticker,
                    quantity=state.quantity,
                    avg_cost=state.avg_cost,
                    market_price=mark_price,
                    market_value=market_value,
                    realized_pnl=state.realized_pnl,
                    unrealized_pnl=unrealized,
                    total_pnl=total_pnl,
                    weight_pct=weight_pct,
                )
            )
        attributions.sort(key=lambda item: item.total_pnl, reverse=True)
        return attributions

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value
