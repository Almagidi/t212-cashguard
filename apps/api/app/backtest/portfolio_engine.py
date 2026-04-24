"""
Portfolio-level event-driven backtester for lower-turnover long-only strategies.

This complements the intraday single-symbol engine by supporting basket-based
allocation research that more closely matches Trading 212 Pie workflows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any

from app.strategies.indicators import Bar


SHARE_QUANT = Decimal("0.0001")


@dataclass
class PortfolioTrade:
    date: date
    ticker: str
    side: str
    shares: Decimal
    price: Decimal
    notional: Decimal
    cost: Decimal
    reason: str
    target_weight: Decimal


@dataclass
class PortfolioAllocationPoint:
    date: date
    equity: Decimal
    cash: Decimal
    exposure_pct: Decimal
    drawdown_pct: Decimal
    weights: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class PortfolioBacktestResult:
    strategy_name: str
    universe: list[str]
    start_date: date
    end_date: date
    initial_capital: Decimal
    final_capital: Decimal
    benchmark_name: str
    benchmark_return_pct: Decimal
    alpha_vs_benchmark_pct: Decimal
    total_return_pct: Decimal
    annualised_return_pct: Decimal
    sharpe_ratio: Decimal | None
    sortino_ratio: Decimal | None
    calmar_ratio: Decimal | None
    max_drawdown_pct: Decimal
    total_trades: int
    rebalance_count: int
    turnover_pct: Decimal
    avg_exposure_pct: Decimal
    equity_curve: list[PortfolioAllocationPoint] = field(default_factory=list)
    trades: list[PortfolioTrade] = field(default_factory=list)
    latest_weights: dict[str, Decimal] = field(default_factory=dict)


def _decimal_mean(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(str(len(values)))


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return variance ** 0.5


def _align_histories(
    histories: dict[str, tuple[list[Bar], list[datetime]]],
) -> tuple[list[date], dict[str, list[Bar]]]:
    date_maps: dict[str, dict[date, Bar]] = {}
    common_dates: set[date] | None = None

    for ticker, (bars, bar_times) in histories.items():
        per_day = {bar_time.date(): bar for bar_time, bar in zip(bar_times, bars, strict=True)}
        date_maps[ticker] = per_day
        ticker_dates = set(per_day)
        common_dates = ticker_dates if common_dates is None else common_dates & ticker_dates

    if not common_dates:
        raise ValueError("No common dates across the requested universe.")

    ordered_dates = sorted(common_dates)
    aligned = {
        ticker: [date_maps[ticker][current_date] for current_date in ordered_dates]
        for ticker in sorted(date_maps)
    }
    return ordered_dates, aligned


def _rebalance_due(previous: date | None, current: date, frequency: str) -> bool:
    if previous is None:
        return True
    if frequency == "monthly":
        return (previous.year, previous.month) != (current.year, current.month)
    if frequency == "quarterly":
        previous_quarter = (previous.month - 1) // 3
        current_quarter = (current.month - 1) // 3
        return (previous.year, previous_quarter) != (current.year, current_quarter)
    if frequency == "annual":
        return previous.year != current.year
    return False


class PortfolioBacktester:
    def __init__(
        self,
        *,
        strategy: Any,
        universe: list[str],
        initial_capital: Decimal,
        start_date: date,
        end_date: date,
        transaction_cost_bps: Decimal = Decimal("10"),
    ) -> None:
        self.strategy = strategy
        self.universe = sorted(dict.fromkeys(universe))
        self.initial_capital = initial_capital
        self.start_date = start_date
        self.end_date = end_date
        self.transaction_cost_rate = transaction_cost_bps / Decimal("10000")

    def _run_benchmark(
        self,
        *,
        history: dict[str, list[Bar]],
        first_index: int,
    ) -> Decimal:
        open_equity = self.initial_capital
        weight = Decimal("1") / Decimal(str(len(history)))
        shares: dict[str, Decimal] = {}

        for ticker, bars in history.items():
            open_price = bars[first_index].open
            if open_price <= 0:
                continue
            allocation = open_equity * weight
            shares[ticker] = (allocation / open_price).quantize(SHARE_QUANT, rounding=ROUND_DOWN)

        final_equity = Decimal("0")
        for ticker, bars in history.items():
            final_equity += shares.get(ticker, Decimal("0")) * bars[-1].close

        if self.initial_capital <= 0:
            return Decimal("0")
        return ((final_equity / self.initial_capital) - Decimal("1")) * Decimal("100")

    def run(self, histories: dict[str, tuple[list[Bar], list[datetime]]]) -> PortfolioBacktestResult:
        dates, history = _align_histories(histories)
        min_history = int(getattr(self.strategy, "min_history_bars", 0))
        if len(dates) <= min_history:
            raise ValueError(
                f"Need more history for {self.strategy.label}. "
                f"Required at least {min_history + 1} aligned bars, got {len(dates)}."
            )

        first_trade_index = max(0, min_history)
        cash = self.initial_capital
        holdings = {ticker: Decimal("0") for ticker in history}
        equity_curve: list[PortfolioAllocationPoint] = []
        trades: list[PortfolioTrade] = []
        total_turnover = Decimal("0")
        rebalance_count = 0
        peak_equity = self.initial_capital
        previous_rebalance_date: date | None = None

        for index, current_date in enumerate(dates):
            if index >= first_trade_index and _rebalance_due(
                previous_rebalance_date,
                current_date,
                getattr(self.strategy, "rebalance_frequency", "monthly"),
            ):
                target_weights = self.strategy.target_weights(history, as_of_index=index - 1 if index > 0 else 0)
                if sum(target_weights.values(), Decimal("0")) > Decimal("1.0001"):
                    total_weight = sum(target_weights.values(), Decimal("0"))
                    target_weights = {ticker: weight / total_weight for ticker, weight in target_weights.items()}
                traded = self._rebalance_portfolio(
                    current_date=current_date,
                    history=history,
                    index=index,
                    cash=cash,
                    holdings=holdings,
                    target_weights=target_weights,
                    reason=f"{self.strategy.rebalance_frequency}_rebalance",
                )
                cash = traded["cash"]
                holdings = traded["holdings"]
                total_turnover += traded["turnover"]
                trades.extend(traded["trades"])
                if traded["trades"]:
                    rebalance_count += 1
                previous_rebalance_date = current_date

            equity = cash
            invested = Decimal("0")
            weights: dict[str, Decimal] = {}
            for ticker, bars in history.items():
                value = holdings[ticker] * bars[index].close
                invested += value
                equity += value
            if equity > peak_equity:
                peak_equity = equity
            drawdown = Decimal("0")
            if peak_equity > 0:
                drawdown = ((peak_equity - equity) / peak_equity) * Decimal("100")
            for ticker, bars in history.items():
                value = holdings[ticker] * bars[index].close
                if equity > 0 and value > 0:
                    weights[ticker] = (value / equity).quantize(Decimal("0.0001"))
            exposure_pct = Decimal("0")
            if equity > 0:
                exposure_pct = (invested / equity) * Decimal("100")
            equity_curve.append(
                PortfolioAllocationPoint(
                    date=current_date,
                    equity=equity.quantize(Decimal("0.01")),
                    cash=cash.quantize(Decimal("0.01")),
                    exposure_pct=exposure_pct.quantize(Decimal("0.01")),
                    drawdown_pct=drawdown.quantize(Decimal("0.01")),
                    weights=weights,
                )
            )

        final_equity = equity_curve[-1].equity
        total_return_pct = Decimal("0")
        if self.initial_capital > 0:
            total_return_pct = ((final_equity / self.initial_capital) - Decimal("1")) * Decimal("100")
        day_count = max(1, (dates[-1] - dates[first_trade_index]).days)
        annualised_return_pct = Decimal("0")
        if self.initial_capital > 0 and final_equity > 0:
            annualised_return_pct = Decimal(
                str(
                    (
                        (float(final_equity) / float(self.initial_capital)) ** (365.0 / float(day_count))
                        - 1.0
                    )
                    * 100.0
                )
            )

        daily_returns: list[float] = []
        downside_returns: list[float] = []
        for prev, current in zip(equity_curve, equity_curve[1:]):
            if prev.equity <= 0:
                continue
            daily_return = float((current.equity / prev.equity) - Decimal("1"))
            daily_returns.append(daily_return)
            if daily_return < 0:
                downside_returns.append(daily_return)

        sharpe = None
        sortino = None
        if daily_returns:
            daily_mean = sum(daily_returns) / len(daily_returns)
            daily_std = _stddev(daily_returns)
            if daily_std > 0:
                sharpe = Decimal(str(round((daily_mean / daily_std) * (252 ** 0.5), 3)))
            downside_std = _stddev(downside_returns) if downside_returns else 0.0
            if downside_std > 0:
                sortino = Decimal(str(round((daily_mean / downside_std) * (252 ** 0.5), 3)))

        max_drawdown_pct = max((point.drawdown_pct for point in equity_curve), default=Decimal("0"))
        calmar = None
        if max_drawdown_pct > 0:
            calmar = Decimal(str(round(float(annualised_return_pct / max_drawdown_pct), 3)))

        benchmark_return_pct = self._run_benchmark(history=history, first_index=first_trade_index)
        alpha_vs_benchmark_pct = total_return_pct - benchmark_return_pct
        avg_exposure_pct = _decimal_mean([point.exposure_pct for point in equity_curve]).quantize(Decimal("0.01"))
        turnover_pct = Decimal("0")
        avg_equity = _decimal_mean([point.equity for point in equity_curve])
        if avg_equity > 0:
            turnover_pct = ((total_turnover / avg_equity) * Decimal("100")).quantize(Decimal("0.01"))

        return PortfolioBacktestResult(
            strategy_name=self.strategy.label,
            universe=self.universe,
            start_date=dates[0],
            end_date=dates[-1],
            initial_capital=self.initial_capital.quantize(Decimal("0.01")),
            final_capital=final_equity.quantize(Decimal("0.01")),
            benchmark_name="Equal-Weight Buy & Hold",
            benchmark_return_pct=benchmark_return_pct.quantize(Decimal("0.01")),
            alpha_vs_benchmark_pct=alpha_vs_benchmark_pct.quantize(Decimal("0.01")),
            total_return_pct=total_return_pct.quantize(Decimal("0.01")),
            annualised_return_pct=annualised_return_pct.quantize(Decimal("0.01")),
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            max_drawdown_pct=max_drawdown_pct.quantize(Decimal("0.01")),
            total_trades=len(trades),
            rebalance_count=rebalance_count,
            turnover_pct=turnover_pct,
            avg_exposure_pct=avg_exposure_pct,
            equity_curve=equity_curve,
            trades=trades,
            latest_weights=equity_curve[-1].weights if equity_curve else {},
        )

    def _rebalance_portfolio(
        self,
        *,
        current_date: date,
        history: dict[str, list[Bar]],
        index: int,
        cash: Decimal,
        holdings: dict[str, Decimal],
        target_weights: dict[str, Decimal],
        reason: str,
    ) -> dict[str, Any]:
        updated_cash = cash
        updated_holdings = dict(holdings)
        trades: list[PortfolioTrade] = []
        turnover = Decimal("0")

        equity_at_open = updated_cash
        for ticker, bars in history.items():
            equity_at_open += updated_holdings[ticker] * bars[index].open

        allocatable_equity = equity_at_open * Decimal("0.998")
        target_values = {ticker: allocatable_equity * target_weights.get(ticker, Decimal("0")) for ticker in history}

        for ticker, bars in history.items():
            open_price = bars[index].open
            if open_price <= 0:
                continue
            current_value = updated_holdings[ticker] * open_price
            target_value = target_values.get(ticker, Decimal("0"))
            delta_value = target_value - current_value
            if delta_value >= 0:
                continue
            shares_to_sell = min(
                updated_holdings[ticker],
                (abs(delta_value) / open_price).quantize(SHARE_QUANT, rounding=ROUND_DOWN),
            )
            if shares_to_sell <= 0:
                continue
            notional = shares_to_sell * open_price
            cost = (notional * self.transaction_cost_rate).quantize(Decimal("0.01"))
            updated_holdings[ticker] -= shares_to_sell
            updated_cash += notional - cost
            turnover += notional
            trades.append(
                PortfolioTrade(
                    date=current_date,
                    ticker=ticker,
                    side="sell",
                    shares=shares_to_sell,
                    price=open_price,
                    notional=notional.quantize(Decimal("0.01")),
                    cost=cost,
                    reason=reason,
                    target_weight=target_weights.get(ticker, Decimal("0")).quantize(Decimal("0.0001")),
                )
            )

        for ticker, bars in history.items():
            open_price = bars[index].open
            if open_price <= 0:
                continue
            current_value = updated_holdings[ticker] * open_price
            target_value = target_values.get(ticker, Decimal("0"))
            delta_value = target_value - current_value
            if delta_value <= 0:
                continue
            affordable_shares = (updated_cash / (open_price * (Decimal("1") + self.transaction_cost_rate))).quantize(
                SHARE_QUANT,
                rounding=ROUND_DOWN,
            )
            desired_shares = (delta_value / open_price).quantize(SHARE_QUANT, rounding=ROUND_DOWN)
            shares_to_buy = min(desired_shares, affordable_shares)
            if shares_to_buy <= 0:
                continue
            notional = shares_to_buy * open_price
            cost = (notional * self.transaction_cost_rate).quantize(Decimal("0.01"))
            updated_holdings[ticker] += shares_to_buy
            updated_cash -= notional + cost
            turnover += notional
            trades.append(
                PortfolioTrade(
                    date=current_date,
                    ticker=ticker,
                    side="buy",
                    shares=shares_to_buy,
                    price=open_price,
                    notional=notional.quantize(Decimal("0.01")),
                    cost=cost,
                    reason=reason,
                    target_weight=target_weights.get(ticker, Decimal("0")).quantize(Decimal("0.0001")),
                )
            )

        return {
            "cash": updated_cash.quantize(Decimal("0.01")),
            "holdings": updated_holdings,
            "turnover": turnover.quantize(Decimal("0.01")),
            "trades": trades,
        }
