"""
Portfolio rebalance attribution service.

Builds sleeve-level PnL and attribution directly from recorded rebalance orders
so portfolio automation can be reviewed with the same evidence trail used for
execution.
"""
from __future__ import annotations

import inspect
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.api.schemas import (
    PORTFOLIO_ATTRIBUTION_COVERAGE_CAVEATS,
    PortfolioRebalanceEventOut,
    PortfolioRebalanceWeightChangeOut,
    PortfolioStrategyAttributionOut,
    PortfolioStrategyAttributionSummaryOut,
    PortfolioTickerAttributionOut,
    PortfolioTimelinePointOut,
)
from app.db.models import Order, Signal, Strategy
from app.market_data import get_live_provider
from app.strategies.indicators import Bar

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


ZERO = Decimal("0")
QUANTITY_EPSILON = Decimal("0.0000001")


@dataclass
class RebalanceFill:
    ticker: str
    side: str
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    target_weight: Decimal | None = None


@dataclass
class PositionLedger:
    quantity: Decimal = ZERO
    avg_cost: Decimal = ZERO
    realized_pnl: Decimal = ZERO


class PortfolioAttributionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_strategy_attribution(
        self,
        strategy: Strategy,
    ) -> PortfolioStrategyAttributionOut:
        computed_at = datetime.now(UTC)
        fills = await self._load_rebalance_fills(strategy.id)
        if not fills:
            return PortfolioStrategyAttributionOut(
                strategy_id=strategy.id,
                strategy_name=strategy.name,
                strategy_type=strategy.type,
                computed_at=computed_at,
                benchmark_name=self._benchmark_name(strategy),
                total_return_pct=0.0,
                benchmark_return_pct=0.0,
                alpha_vs_benchmark_pct=0.0,
                max_drawdown_pct=0.0,
                benchmark_max_drawdown_pct=0.0,
                total_pnl=0.0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                cash_balance=0.0,
                current_market_value=0.0,
                turnover_notional=0.0,
                buys_notional=0.0,
                sells_notional=0.0,
                rebalance_days=0,
                order_count=0,
                recent_timeline=[],
                timeline=[],
                ticker_attribution=[],
                rebalance_events=[],
                coverage_caveats=PORTFOLIO_ATTRIBUTION_COVERAGE_CAVEATS,
            )

        fill_dates = sorted({fill.occurred_at.date() for fill in fills})
        strategy_tickers = sorted({ticker.upper() for ticker in strategy.allowed_tickers})
        tickers = sorted({fill.ticker for fill in fills} | set(strategy_tickers))
        price_history = await self._load_price_history(tickers, fill_dates[0])
        timeline_dates = sorted(
            {
                current_date
                for history in price_history.values()
                for current_date in history
                if current_date >= fill_dates[0]
            }
            | set(fill_dates)
        )

        fills_by_date: dict[date, list[RebalanceFill]] = defaultdict(list)
        for fill in fills:
            fills_by_date[fill.occurred_at.date()].append(fill)

        positions: dict[str, PositionLedger] = {ticker: PositionLedger() for ticker in tickers}
        last_prices: dict[str, Decimal] = {}
        timeline: list[PortfolioTimelinePointOut] = []
        rebalance_events: list[PortfolioRebalanceEventOut] = []
        cash_balance = ZERO
        turnover_notional = ZERO
        buys_notional = ZERO
        sells_notional = ZERO
        total_order_count = 0
        capital_base = self._infer_capital_base(fills)
        benchmark_positions, benchmark_cash = self._build_benchmark_positions(
            tickers=strategy_tickers or tickers,
            price_history=price_history,
            start_date=fill_dates[0],
            capital_base=capital_base,
        )
        sleeve_peak_value = capital_base
        benchmark_peak_value = capital_base

        for current_date in timeline_dates:
            for ticker, history in price_history.items():
                maybe_price = history.get(current_date)
                if maybe_price is not None and maybe_price > 0:
                    last_prices[ticker] = maybe_price

            order_count = 0
            turnover_today = ZERO
            fills_today = fills_by_date.get(current_date, [])
            before_weights = self._snapshot_weights(positions, last_prices)
            for fill in fills_today:
                ledger = positions.setdefault(fill.ticker, PositionLedger())
                if fill.price > 0:
                    last_prices.setdefault(fill.ticker, fill.price)

                executed_quantity = fill.quantity
                if fill.side == "buy":
                    prior_cost = ledger.avg_cost * ledger.quantity
                    new_quantity = ledger.quantity + executed_quantity
                    if new_quantity > QUANTITY_EPSILON:
                        ledger.avg_cost = (prior_cost + (executed_quantity * fill.price)) / new_quantity
                    ledger.quantity = new_quantity
                    notional = executed_quantity * fill.price
                    cash_balance -= notional
                    buys_notional += notional
                else:
                    if ledger.quantity <= QUANTITY_EPSILON:
                        continue
                    executed_quantity = min(executed_quantity, ledger.quantity)
                    if executed_quantity <= QUANTITY_EPSILON:
                        continue
                    notional = executed_quantity * fill.price
                    realized = (fill.price - ledger.avg_cost) * executed_quantity
                    ledger.realized_pnl += realized
                    ledger.quantity -= executed_quantity
                    cash_balance += notional
                    sells_notional += notional
                    if ledger.quantity <= QUANTITY_EPSILON:
                        ledger.quantity = ZERO
                        ledger.avg_cost = ZERO

                turnover_notional += notional
                turnover_today += notional
                order_count += 1
                total_order_count += 1

            after_weights = self._snapshot_weights(positions, last_prices)
            if fills_today:
                rebalance_events.append(
                    PortfolioRebalanceEventOut(
                        date=current_date.isoformat(),
                        order_count=order_count,
                        turnover_notional=self._to_float(turnover_today),
                        total_pnl_after=0.0,
                        weights=self._build_weight_changes(before_weights, after_weights, fills_today),
                    )
                )

            current_market_value = ZERO
            unrealized_pnl = ZERO
            realized_pnl = ZERO
            for ticker, ledger in positions.items():
                realized_pnl += ledger.realized_pnl
                if ledger.quantity <= QUANTITY_EPSILON:
                    continue
                market_price = last_prices.get(ticker, ledger.avg_cost)
                market_value = ledger.quantity * market_price
                current_market_value += market_value
                unrealized_pnl += (market_price - ledger.avg_cost) * ledger.quantity

            total_pnl = cash_balance + current_market_value
            sleeve_value = capital_base + total_pnl
            if sleeve_value > sleeve_peak_value:
                sleeve_peak_value = sleeve_value
            drawdown_pct = ZERO
            if sleeve_peak_value > ZERO and sleeve_value < sleeve_peak_value:
                drawdown_pct = ((sleeve_peak_value - sleeve_value) / sleeve_peak_value) * Decimal("100")

            benchmark_value = benchmark_cash
            for ticker, shares in benchmark_positions.items():
                benchmark_value += shares * last_prices.get(ticker, ZERO)
            if benchmark_value > benchmark_peak_value:
                benchmark_peak_value = benchmark_value
            benchmark_drawdown_pct = ZERO
            if benchmark_peak_value > ZERO and benchmark_value < benchmark_peak_value:
                benchmark_drawdown_pct = ((benchmark_peak_value - benchmark_value) / benchmark_peak_value) * Decimal("100")
            benchmark_pnl = benchmark_value - capital_base
            timeline.append(
                PortfolioTimelinePointOut(
                    date=current_date.isoformat(),
                    equity_pnl=self._to_float(total_pnl),
                    benchmark_pnl=self._to_float(benchmark_pnl),
                    realized_pnl=self._to_float(realized_pnl),
                    unrealized_pnl=self._to_float(unrealized_pnl),
                    cash_balance=self._to_float(cash_balance),
                    gross_exposure=self._to_float(current_market_value),
                    drawdown_pct=self._to_float(drawdown_pct),
                    benchmark_drawdown_pct=self._to_float(benchmark_drawdown_pct),
                    turnover_notional=self._to_float(turnover_today),
                    order_count=order_count,
                )
            )
            if rebalance_events and rebalance_events[-1].date == current_date.isoformat():
                rebalance_events[-1].total_pnl_after = self._to_float(total_pnl)

        latest_market_value = ZERO
        latest_unrealized = ZERO
        latest_realized = ZERO
        ticker_rows: list[PortfolioTickerAttributionOut] = []
        for ticker, ledger in positions.items():
            if ledger.quantity <= QUANTITY_EPSILON and ledger.realized_pnl == ZERO:
                continue
            market_price = last_prices.get(ticker, ledger.avg_cost)
            market_value = ledger.quantity * market_price
            unrealized = (market_price - ledger.avg_cost) * ledger.quantity
            total_ticker_pnl = ledger.realized_pnl + unrealized
            latest_market_value += market_value
            latest_unrealized += unrealized
            latest_realized += ledger.realized_pnl
            ticker_rows.append(
                PortfolioTickerAttributionOut(
                    ticker=ticker,
                    quantity=self._to_float(ledger.quantity, places="0.0001"),
                    avg_cost=self._to_float(ledger.avg_cost),
                    market_price=self._to_float(market_price),
                    market_value=self._to_float(market_value),
                    realized_pnl=self._to_float(ledger.realized_pnl),
                    unrealized_pnl=self._to_float(unrealized),
                    total_pnl=self._to_float(total_ticker_pnl),
                    weight_pct=0.0,
                )
            )

        for row in ticker_rows:
            if latest_market_value > ZERO:
                row.weight_pct = self._to_float((Decimal(str(row.market_value)) / latest_market_value) * Decimal("100"))

        ticker_rows.sort(key=lambda row: row.total_pnl, reverse=True)
        total_pnl = cash_balance + latest_market_value
        benchmark_final_value = benchmark_cash
        for ticker, shares in benchmark_positions.items():
            benchmark_final_value += shares * last_prices.get(ticker, ZERO)
        benchmark_pnl = benchmark_final_value - capital_base
        total_return_pct = ZERO
        benchmark_return_pct = ZERO
        if capital_base > ZERO:
            total_return_pct = (total_pnl / capital_base) * Decimal("100")
            benchmark_return_pct = (benchmark_pnl / capital_base) * Decimal("100")
        max_drawdown_pct = max((Decimal(str(point.drawdown_pct)) for point in timeline), default=ZERO)
        benchmark_max_drawdown_pct = max((Decimal(str(point.benchmark_drawdown_pct)) for point in timeline), default=ZERO)
        return PortfolioStrategyAttributionOut(
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            strategy_type=strategy.type,
            computed_at=computed_at,
            benchmark_name=self._benchmark_name(strategy),
            total_return_pct=self._to_float(total_return_pct),
            benchmark_return_pct=self._to_float(benchmark_return_pct),
            alpha_vs_benchmark_pct=self._to_float(total_return_pct - benchmark_return_pct),
            max_drawdown_pct=self._to_float(max_drawdown_pct),
            benchmark_max_drawdown_pct=self._to_float(benchmark_max_drawdown_pct),
            total_pnl=self._to_float(total_pnl),
            realized_pnl=self._to_float(latest_realized),
            unrealized_pnl=self._to_float(latest_unrealized),
            cash_balance=self._to_float(cash_balance),
            current_market_value=self._to_float(latest_market_value),
            turnover_notional=self._to_float(turnover_notional),
            buys_notional=self._to_float(buys_notional),
            sells_notional=self._to_float(sells_notional),
            rebalance_days=len(fill_dates),
            order_count=total_order_count,
            recent_timeline=timeline[-30:],
            timeline=timeline,
            ticker_attribution=ticker_rows,
            rebalance_events=rebalance_events,
            coverage_caveats=PORTFOLIO_ATTRIBUTION_COVERAGE_CAVEATS,
        )

    async def build_summary(
        self,
        strategy: Strategy,
    ) -> PortfolioStrategyAttributionSummaryOut:
        detail = await self.build_strategy_attribution(strategy)
        return PortfolioStrategyAttributionSummaryOut(
            strategy_id=detail.strategy_id,
            strategy_name=detail.strategy_name,
            strategy_type=detail.strategy_type,
            computed_at=detail.computed_at,
            benchmark_name=detail.benchmark_name,
            total_return_pct=detail.total_return_pct,
            benchmark_return_pct=detail.benchmark_return_pct,
            alpha_vs_benchmark_pct=detail.alpha_vs_benchmark_pct,
            max_drawdown_pct=detail.max_drawdown_pct,
            benchmark_max_drawdown_pct=detail.benchmark_max_drawdown_pct,
            total_pnl=detail.total_pnl,
            realized_pnl=detail.realized_pnl,
            unrealized_pnl=detail.unrealized_pnl,
            cash_balance=detail.cash_balance,
            current_market_value=detail.current_market_value,
            turnover_notional=detail.turnover_notional,
            buys_notional=detail.buys_notional,
            sells_notional=detail.sells_notional,
            rebalance_days=detail.rebalance_days,
            order_count=detail.order_count,
            recent_timeline=detail.recent_timeline,
            coverage_caveats=detail.coverage_caveats,
        )

    async def _load_rebalance_fills(self, strategy_id: uuid.UUID) -> list[RebalanceFill]:
        signal_alias = aliased(Signal)
        result = await self.db.execute(
            select(Order, signal_alias)
            .join(signal_alias, Order.signal_id == signal_alias.id)
            .where(
                signal_alias.strategy_id == strategy_id,
                signal_alias.signal_type == "portfolio_rebalance",
                Order.status == "filled",
            )
            .order_by(Order.created_at.asc(), Order.id.asc())
        )
        rows = result.all()
        fills: list[RebalanceFill] = []
        for order, signal in rows:
            quantity = self._positive_decimal(order.filled_quantity or order.quantity)
            price = self._effective_price(order, signal)
            if quantity <= ZERO or price <= ZERO:
                continue
            target_weight_raw = (signal.params_snapshot or {}).get("target_weight")
            target_weight = None
            if target_weight_raw is not None:
                target_weight = Decimal(str(target_weight_raw))
            fills.append(
                RebalanceFill(
                    ticker=order.ticker.upper(),
                    side=order.side,
                    quantity=quantity,
                    price=price,
                    occurred_at=order.updated_at or order.created_at,
                    target_weight=target_weight,
                )
            )
        return fills

    async def _load_price_history(
        self,
        tickers: list[str],
        start_date: date,
    ) -> dict[str, dict[date, Decimal]]:
        limit = max(90, (datetime.now(UTC).date() - start_date).days + 5)
        provider = get_live_provider()
        if hasattr(provider, "__aenter__"):
            async with provider as active_provider:
                return await self._price_history_from_provider(active_provider, tickers, limit)
        return await self._price_history_from_provider(provider, tickers, limit)

    async def _price_history_from_provider(
        self,
        provider: Any,
        tickers: list[str],
        limit: int,
    ) -> dict[str, dict[date, Decimal]]:
        history: dict[str, dict[date, Decimal]] = {}
        for ticker in tickers:
            bars, bar_times = await self._fetch_daily_bars(provider, ticker, limit)
            if not bars:
                continue
            history[ticker] = {
                bar_time.date(): bar.close
                for bar, bar_time in zip(bars, bar_times, strict=True)
            }
        return history

    async def _fetch_daily_bars(
        self,
        provider: Any,
        ticker: str,
        limit: int,
    ) -> tuple[list[Bar], list[datetime]]:
        if hasattr(provider, "get_bars"):
            raw_bars = await self._maybe_await(
                provider.get_bars(ticker, multiplier=1, timespan="day", limit=limit)
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
            bar_times = [
                getattr(bar, "timestamp", datetime.now(UTC))
                for bar in raw_bars
            ]
            return bars, bar_times

        raw = provider.get_ohlcv(ticker, interval_minutes=1440, bars=limit)
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
        bar_times = [datetime.fromisoformat(str(bar["timestamp"])) for bar in raw]
        return bars, bar_times

    @staticmethod
    def _effective_price(order: Order, signal: Signal) -> Decimal:
        if order.avg_fill_price is not None and order.avg_fill_price > ZERO:
            return Decimal(str(order.avg_fill_price))
        if order.cash_used is not None and order.quantity and order.quantity > ZERO:
            return Decimal(str(order.cash_used)) / Decimal(str(order.quantity))
        if signal.entry_price is not None and signal.entry_price > ZERO:
            return Decimal(str(signal.entry_price))
        return ZERO

    @staticmethod
    def _positive_decimal(value: Any) -> Decimal:
        amount = Decimal(str(value or 0))
        return abs(amount)

    @staticmethod
    def _infer_capital_base(fills: list[RebalanceFill]) -> Decimal:
        if not fills:
            return ZERO
        first_date = fills[0].occurred_at.date()
        initial_buys = sum(
            (fill.quantity * fill.price)
            for fill in fills
            if fill.occurred_at.date() == first_date and fill.side == "buy"
        )
        initial_sells = sum(
            (fill.quantity * fill.price)
            for fill in fills
            if fill.occurred_at.date() == first_date and fill.side == "sell"
        )
        capital_base = initial_buys - initial_sells
        if capital_base <= ZERO:
            capital_base = initial_buys
        return capital_base if capital_base > ZERO else ZERO

    @staticmethod
    def _build_benchmark_positions(
        *,
        tickers: list[str],
        price_history: dict[str, dict[date, Decimal]],
        start_date: date,
        capital_base: Decimal,
    ) -> tuple[dict[str, Decimal], Decimal]:
        if capital_base <= ZERO or not tickers:
            return {}, ZERO
        eligible: list[tuple[str, Decimal]] = []
        for ticker in tickers:
            price = price_history.get(ticker, {}).get(start_date)
            if price is not None and price > ZERO:
                eligible.append((ticker, price))
        if not eligible:
            return {}, capital_base
        allocation = capital_base / Decimal(str(len(eligible)))
        positions: dict[str, Decimal] = {}
        invested = ZERO
        for ticker, price in eligible:
            shares = allocation / price
            positions[ticker] = shares
            invested += shares * price
        return positions, capital_base - invested

    @staticmethod
    def _snapshot_weights(
        positions: dict[str, PositionLedger],
        prices: dict[str, Decimal],
    ) -> dict[str, Decimal]:
        values: dict[str, Decimal] = {}
        total_value = ZERO
        for ticker, ledger in positions.items():
            if ledger.quantity <= QUANTITY_EPSILON:
                continue
            price = prices.get(ticker)
            if price is None or price <= ZERO:
                continue
            market_value = ledger.quantity * price
            if market_value <= ZERO:
                continue
            values[ticker] = market_value
            total_value += market_value
        if total_value <= ZERO:
            return {}
        return {ticker: value / total_value for ticker, value in values.items()}

    @staticmethod
    def _build_weight_changes(
        before_weights: dict[str, Decimal],
        after_weights: dict[str, Decimal],
        fills: list[RebalanceFill],
    ) -> list[PortfolioRebalanceWeightChangeOut]:
        target_weights = {
            fill.ticker: fill.target_weight
            for fill in fills
            if fill.target_weight is not None
        }
        tickers = sorted(set(before_weights) | set(after_weights) | set(target_weights))
        rows: list[PortfolioRebalanceWeightChangeOut] = []
        for ticker in tickers:
            before_weight = before_weights.get(ticker)
            after_weight = after_weights.get(ticker)
            target_weight = target_weights.get(ticker)
            before_gap = None
            after_gap = None
            if target_weight is not None:
                before_gap = target_weight - (before_weight or ZERO)
                after_gap = target_weight - (after_weight or ZERO)
            rows.append(
                PortfolioRebalanceWeightChangeOut(
                    ticker=ticker,
                    target_weight=PortfolioAttributionService._to_optional_float(target_weight),
                    before_weight=PortfolioAttributionService._to_optional_float(before_weight),
                    after_weight=PortfolioAttributionService._to_optional_float(after_weight),
                    before_gap=PortfolioAttributionService._to_optional_float(before_gap),
                    after_gap=PortfolioAttributionService._to_optional_float(after_gap),
                )
            )
        return rows

    @staticmethod
    def _benchmark_name(strategy: Strategy) -> str:
        universe = sorted({ticker.upper() for ticker in strategy.allowed_tickers})
        if universe:
            return f"Equal-weight {'/'.join(universe[:3])}{'…' if len(universe) > 3 else ''}"
        return "Equal-weight sleeve universe"

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _to_float(value: Decimal, *, places: str = "0.01") -> float:
        return float(value.quantize(Decimal(places)))

    @staticmethod
    def _to_optional_float(value: Decimal | None, *, places: str = "0.0001") -> float | None:
        if value is None:
            return None
        return float(value.quantize(Decimal(places)))
