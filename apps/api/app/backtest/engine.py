"""
Event-driven backtesting engine.

Design principles:
- Walk through bars chronologically — no look-ahead bias
- Realistic execution: limit orders fill at next bar open + slippage
- Track all costs: spread, slippage, opportunity cost
- Full trade log for attribution analysis
- Walk-forward validation support
"""
from __future__ import annotations

import inspect
import random
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import structlog

if TYPE_CHECKING:
    from app.strategies.indicators import Bar

log = structlog.get_logger()


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class BacktestOrder:
    id: str
    ticker: str
    side: str                    # buy | sell
    order_type: str              # market | limit
    quantity: Decimal
    limit_price: Decimal | None
    submitted_bar_idx: int
    fill_bar_idx: int | None = None
    fill_price: Decimal | None = None
    slippage: Decimal = Decimal("0")
    status: str = "pending"     # pending | filled | cancelled | expired


@dataclass
class BacktestTrade:
    id: str
    ticker: str
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    side: str
    pnl: Decimal
    pnl_pct: Decimal
    entry_bar_idx: int
    exit_bar_idx: int
    entry_time: datetime
    exit_time: datetime
    exit_reason: str            # stop | take_profit | partial | eod | signal
    slippage_cost: Decimal
    holding_bars: int
    mfe: Decimal = Decimal("0")  # Maximum Favourable Excursion
    mae: Decimal = Decimal("0")  # Maximum Adverse Excursion


@dataclass
class BacktestResult:
    """Complete result from one backtest run."""
    strategy_name: str
    ticker: str
    start_date: date
    end_date: date
    initial_capital: Decimal
    final_capital: Decimal

    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    monte_carlo: dict[str, Any] = field(default_factory=dict)

    # Computed metrics
    gross_pnl: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    gross_return_pct: Decimal = Decimal("0")
    total_return_pct: Decimal = Decimal("0")
    annualised_return_pct: Decimal = Decimal("0")
    sharpe_ratio: Decimal | None = None
    sortino_ratio: Decimal | None = None
    calmar_ratio: Decimal | None = None
    max_drawdown_pct: Decimal = Decimal("0")
    max_drawdown_duration_days: int = 0
    benchmark_return_pct: Decimal = Decimal("0")
    alpha_vs_benchmark_pct: Decimal = Decimal("0")
    win_rate: Decimal = Decimal("0")
    profit_factor: Decimal = Decimal("0")
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    expectancy: Decimal = Decimal("0")
    expectancy_pct: Decimal = Decimal("0")
    avg_rr_achieved: Decimal = Decimal("0")
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_holding_bars: Decimal = Decimal("0")
    total_slippage_cost: Decimal = Decimal("0")
    total_commission_cost: Decimal = Decimal("0")
    turnover_pct: Decimal = Decimal("0")
    exposure_pct: Decimal = Decimal("0")
    avg_mfe: Decimal = Decimal("0")
    avg_mae: Decimal = Decimal("0")
    consecutive_losses_max: int = 0


class StrategyProtocol(Protocol):
    """Interface any strategy must implement for backtesting."""

    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None,
    ) -> Any | None: ...


def generate_strategy_signal(
    strategy: Any,
    *,
    ticker: str,
    bars: list[Bar],
    bar_times: list[datetime] | None = None,
    history_bars: list[Bar] | None = None,
    history_bar_times: list[datetime] | None = None,
    account_value: Decimal,
    available_cash: Decimal,
    current_time_utc: str,
    prev_close: Decimal | None,
) -> Any | None:
    """Invoke a strategy with only the kwargs it actually supports."""
    signature = inspect.signature(strategy.generate_signal)
    kwargs: dict[str, Any] = {
        "ticker": ticker,
        "bars": bars,
        "account_value": account_value,
        "available_cash": available_cash,
        "current_time_utc": current_time_utc,
    }
    if "prev_close" in signature.parameters:
        kwargs["prev_close"] = prev_close
    if "session_open" in signature.parameters and bars:
        kwargs["session_open"] = bars[0].open
    if "bar_times" in signature.parameters:
        kwargs["bar_times"] = bar_times
    if "history_bars" in signature.parameters:
        kwargs["history_bars"] = history_bars
    if "history_bar_times" in signature.parameters:
        kwargs["history_bar_times"] = history_bar_times
    return strategy.generate_signal(**kwargs)


def summarise_walk_forward_results(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not results:
        return None

    profitable = sum(1 for item in results if item["oos_return_pct"] > 0)
    positive_sharpe = sum(1 for item in results if item["oos_sharpe"] > 0.5)
    controlled_drawdown = sum(1 for item in results if item["oos_max_dd"] <= 15.0)
    robustness_score = round(
        (
            0.5 * (profitable / len(results))
            + 0.3 * (positive_sharpe / len(results))
            + 0.2 * (controlled_drawdown / len(results))
        )
        * 100,
        1,
    )

    verdict = "fragile"
    if robustness_score >= 75:
        verdict = "robust"
    elif robustness_score >= 60:
        verdict = "promising"
    elif robustness_score >= 40:
        verdict = "mixed"

    oos_returns = [item["oos_return_pct"] for item in results]
    oos_drawdowns = [item["oos_max_dd"] for item in results]
    oos_sharpes = [item["oos_sharpe"] for item in results]

    return {
        "windows": len(results),
        "profitable_windows": profitable,
        "positive_sharpe_windows": positive_sharpe,
        "controlled_drawdown_windows": controlled_drawdown,
        "avg_oos_return_pct": round(statistics.mean(oos_returns), 2),
        "median_oos_return_pct": round(statistics.median(oos_returns), 2),
        "avg_oos_sharpe": round(statistics.mean(oos_sharpes), 3),
        "median_oos_sharpe": round(statistics.median(oos_sharpes), 3),
        "worst_oos_max_dd": round(max(oos_drawdowns), 2),
        "robustness_score": robustness_score,
        "verdict": verdict,
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def monte_carlo_trade_sequence(
    trades: list[BacktestTrade],
    initial_capital: Decimal,
    *,
    iterations: int = 500,
) -> dict[str, Any]:
    if len(trades) < 5:
        return {
            "iterations": 0,
            "message": "Need at least 5 trades for Monte Carlo sequence analysis.",
        }

    pnls = [float(trade.pnl) for trade in trades]
    rng = random.Random(42)
    max_drawdowns: list[float] = []
    max_consecutive_losses: list[int] = []

    for _ in range(iterations):
        sequence = pnls[:]
        rng.shuffle(sequence)
        equity = float(initial_capital)
        peak = equity
        worst_drawdown = 0.0
        consecutive_losses = 0
        max_losses = 0

        for pnl in sequence:
            equity += pnl
            peak = max(peak, equity)
            if peak > 0:
                worst_drawdown = max(worst_drawdown, (peak - equity) / peak * 100)
            if pnl <= 0:
                consecutive_losses += 1
                max_losses = max(max_losses, consecutive_losses)
            else:
                consecutive_losses = 0

        max_drawdowns.append(worst_drawdown)
        max_consecutive_losses.append(max_losses)

    return {
        "iterations": iterations,
        "median_max_drawdown_pct": round(statistics.median(max_drawdowns), 2),
        "p95_max_drawdown_pct": round(_percentile(max_drawdowns, 0.95), 2),
        "worst_max_drawdown_pct": round(max(max_drawdowns), 2),
        "median_consecutive_losses": int(round(statistics.median(max_consecutive_losses))),
        "p95_consecutive_losses": int(round(_percentile([float(item) for item in max_consecutive_losses], 0.95))),
        "probability_drawdown_gt_10pct": round(sum(dd >= 10 for dd in max_drawdowns) / iterations * 100, 1),
        "probability_drawdown_gt_20pct": round(sum(dd >= 20 for dd in max_drawdowns) / iterations * 100, 1),
    }


# ── Execution simulation ──────────────────────────────────────────────────────

class ExecutionSimulator:
    """
    Realistic fill simulation.

    Assumptions (conservative):
    - Market orders fill at next bar's open + slippage
    - Limit orders fill if next bar's low <= limit (buys) or high >= limit (sells)
    - Slippage = half_spread + market_impact
    - Half spread = 0.03% of price (conservative for liquid US equities)
    - Market impact = 0.02% (assumes order < 1% of avg volume)
    """
    HALF_SPREAD_PCT = Decimal("0.0003")   # 3 bps per side
    MARKET_IMPACT_PCT = Decimal("0.0002") # 2 bps market impact

    def simulate_fill(
        self,
        order: BacktestOrder,
        next_bar: Bar,
        side: str,
    ) -> tuple[Decimal, Decimal]:
        """
        Returns (fill_price, slippage_cost).
        slippage_cost is always positive (it's a cost).
        """
        raw_price = next_bar.open
        slippage_pct = self.HALF_SPREAD_PCT + self.MARKET_IMPACT_PCT

        fill = raw_price * (1 + slippage_pct) if side == "buy" else raw_price * (1 - slippage_pct)

        slippage_cost = abs(fill - raw_price) * order.quantity
        return fill.quantize(Decimal("0.0001")), slippage_cost.quantize(Decimal("0.01"))

    def can_fill_limit(
        self,
        order: BacktestOrder,
        bar: Bar,
        side: str,
    ) -> bool:
        """Check if a limit order would have filled in this bar."""
        if order.limit_price is None:
            return False
        if side == "buy":
            return bar.low <= order.limit_price
        else:
            return bar.high >= order.limit_price


# ── Main backtester ───────────────────────────────────────────────────────────

class Backtester:
    """
    Single-symbol event-driven backtester.

    Usage:
        backtester = Backtester(
            strategy=ORBStrategy(params),
            ticker="AAPL",
            initial_capital=Decimal("10000"),
        )
        result = backtester.run(bars)
    """

    def __init__(
        self,
        strategy: Any,
        ticker: str,
        initial_capital: Decimal = Decimal("10000"),
        risk_per_trade_pct: Decimal = Decimal("1.0"),
        max_position_pct: Decimal = Decimal("10.0"),
        stop_loss_required: bool = True,
        max_holding_bars: int = 39,  # Full session (~3.25h on 5-min bars)
        commission_per_trade: Decimal = Decimal("0"),  # T212 is zero commission
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> None:
        self.strategy = strategy
        self.ticker = ticker
        self.initial_capital = initial_capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_pct = max_position_pct
        self.stop_loss_required = stop_loss_required
        self.max_holding_bars = max_holding_bars
        self.commission_per_trade = commission_per_trade
        self.start_date = start_date
        self.end_date = end_date
        self.executor = ExecutionSimulator()

    def run(self, bars: list[Bar], bar_times: list[datetime]) -> BacktestResult:
        """
        Run full backtest over the provided bar series.

        bars: list of Bar namedtuples in chronological order
        bar_times: matching list of datetime for each bar
        """
        assert len(bars) == len(bar_times), "bars and bar_times must be same length"

        capital = self.initial_capital
        available_cash = capital
        position_qty = Decimal("0")
        position_entry_price = Decimal("0")
        position_entry_idx = 0
        position_stop = Decimal("0")
        position_tp = Decimal("0")
        position_tp1 = Decimal("0")
        remaining_entry_slippage = Decimal("0")
        partial_done = False

        trades: list[BacktestTrade] = []
        equity_curve: list[dict[str, Any]] = []
        pending_orders: list[BacktestOrder] = []
        filtered_bars: list[Bar] = []
        filtered_times: list[datetime] = []
        filtered_indices: list[int] = []
        session_bars: list[Bar] = []
        current_session_date: date | None = None
        previous_session_close: Decimal | None = None
        exposure_bars = 0
        total_commission_cost = Decimal("0")

        # Track MFE/MAE for open position
        mfe_high = Decimal("0")
        mae_low = Decimal("9999999")

        for i, (bar, ts) in enumerate(zip(bars, bar_times, strict=True)):
            # Filter date range
            if self.start_date and ts.date() < self.start_date:
                continue
            if self.end_date and ts.date() > self.end_date:
                continue

            if current_session_date != ts.date():
                if session_bars:
                    previous_session_close = session_bars[-1].close
                current_session_date = ts.date()
                session_bars = []

            filtered_bars.append(bar)
            filtered_times.append(ts)
            filtered_indices.append(i)
            session_bars.append(bar)

            current_time_utc = ts.strftime("%H:%M")

            # Process pending orders first (fills happen at open of next bar)
            if i > 0 and pending_orders:
                remaining = []
                for order in pending_orders:
                    if order.status != "pending":
                        continue
                    if order.order_type == "market":
                        fp, slip = self.executor.simulate_fill(order, bar, order.side)
                        order.fill_price = fp
                        order.fill_bar_idx = i
                        order.slippage = slip
                        order.status = "filled"

                        if order.side == "buy":
                            cost = fp * order.quantity + self.commission_per_trade
                            available_cash -= cost
                            total_commission_cost += self.commission_per_trade
                            position_qty += order.quantity
                            position_entry_price = fp
                            position_entry_idx = i
                            remaining_entry_slippage = slip
                            mfe_high = fp
                            mae_low = fp
                        else:
                            proceeds = fp * order.quantity - self.commission_per_trade
                            available_cash += proceeds
                            total_commission_cost += self.commission_per_trade
                            position_qty -= order.quantity
                    elif order.order_type == "limit":
                        if self.executor.can_fill_limit(order, bar, order.side):
                            # Defense-in-depth: a limit order must carry a
                            # limit_price when it reaches the fill check. If it
                            # doesn't, skip the fill rather than crash on
                            # Decimal(None) * quantity. Crashing a backtest mid-run
                            # leaves partial state in the results page.
                            if order.limit_price is None:
                                remaining.append(order)
                                continue
                            order.fill_price = order.limit_price
                            order.fill_bar_idx = i
                            order.status = "filled"
                            cost = order.limit_price * order.quantity + self.commission_per_trade
                            available_cash -= cost
                            total_commission_cost += self.commission_per_trade
                            position_qty += order.quantity
                            position_entry_price = order.limit_price
                            position_entry_idx = i
                            remaining_entry_slippage = Decimal("0")
                        else:
                            # Cancel limit if too old (3 bars)
                            if i - order.submitted_bar_idx > 3:
                                order.status = "cancelled"
                                remaining.append(order)
                            else:
                                remaining.append(order)
                            continue
                    remaining.append(order)
                pending_orders = [o for o in remaining if o.status == "pending"]

            # Update MFE/MAE for open position
            if position_qty > 0:
                mfe_high = max(mfe_high, bar.high)
                mae_low  = min(mae_low, bar.low)

            # Check exit conditions for open position
            if position_qty > 0 and position_stop > 0:
                exit_reason = None
                exit_price = None
                exit_qty = position_qty

                # Stop loss
                if bar.low <= position_stop:
                    exit_reason = "stop"
                    exit_price = min(bar.open, position_stop)  # Worst case: gap through stop

                # Take profit full
                elif bar.high >= position_tp and not partial_done:
                    exit_reason = "take_profit"
                    exit_price = position_tp

                # Partial exit at 1R
                elif bar.high >= position_tp1 and not partial_done:
                    exit_reason = "partial"
                    exit_qty = (position_qty * Decimal("0.5")).quantize(Decimal("0.01"))
                    exit_price = position_tp1
                    partial_done = True

                # Max holding time
                elif i - position_entry_idx >= self.max_holding_bars:
                    exit_reason = "eod"
                    exit_price, _ = self.executor.simulate_fill(
                        BacktestOrder(id="eod", ticker=self.ticker, side="sell",
                                      order_type="market", quantity=exit_qty,
                                      limit_price=None, submitted_bar_idx=i),
                        bar, "sell"
                    )

                if exit_reason and exit_price:
                    open_qty_before_exit = position_qty
                    raw_pnl = (exit_price - position_entry_price) * exit_qty
                    exit_slippage_cost = abs(exit_price - bar.open) * exit_qty
                    entry_slippage_alloc = (
                        remaining_entry_slippage * exit_qty / open_qty_before_exit
                        if open_qty_before_exit > 0
                        else Decimal("0")
                    )
                    slippage_cost = entry_slippage_alloc + exit_slippage_cost

                    trade = BacktestTrade(
                        id=str(uuid.uuid4())[:8],
                        ticker=self.ticker,
                        entry_price=position_entry_price,
                        exit_price=exit_price,
                        quantity=exit_qty,
                        side="buy",
                        pnl=raw_pnl,
                        pnl_pct=(raw_pnl / (position_entry_price * exit_qty) * 100).quantize(Decimal("0.01")),
                        entry_bar_idx=position_entry_idx,
                        exit_bar_idx=i,
                        entry_time=bar_times[position_entry_idx],
                        exit_time=ts,
                        exit_reason=exit_reason,
                        slippage_cost=slippage_cost,
                        holding_bars=i - position_entry_idx,
                        mfe=(mfe_high - position_entry_price) * exit_qty,
                        mae=(position_entry_price - mae_low) * exit_qty,
                    )
                    trades.append(trade)

                    proceeds = exit_price * exit_qty - self.commission_per_trade
                    available_cash += proceeds
                    total_commission_cost += self.commission_per_trade
                    position_qty -= exit_qty
                    remaining_entry_slippage -= entry_slippage_alloc

                    if position_qty <= Decimal("0.01"):
                        position_qty = Decimal("0")
                        position_stop = Decimal("0")
                        position_tp = Decimal("0")
                        position_tp1 = Decimal("0")
                        remaining_entry_slippage = Decimal("0")
                        partial_done = False

                    capital = available_cash + position_qty * bar.close

            # Only generate entry signals when flat
            if position_qty <= Decimal("0.01") and not pending_orders:
                signal = generate_strategy_signal(
                    self.strategy,
                    ticker=self.ticker,
                    bars=session_bars,
                    bar_times=filtered_times[-len(session_bars):],
                    history_bars=filtered_bars,
                    history_bar_times=filtered_times,
                    account_value=capital,
                    available_cash=available_cash,
                    current_time_utc=current_time_utc,
                    prev_close=previous_session_close,
                )

                if signal:
                    # Place market order for next bar open
                    order = BacktestOrder(
                        id=str(uuid.uuid4())[:8],
                        ticker=self.ticker,
                        side=signal.side,
                        order_type="market",
                        quantity=signal.suggested_quantity,
                        limit_price=None,
                        submitted_bar_idx=i,
                    )
                    pending_orders.append(order)

                    # Store exit levels from signal
                    position_stop = signal.stop_price
                    position_tp = signal.take_profit_price
                    risk = signal.entry_price - signal.stop_price
                    position_tp1 = signal.entry_price + risk  # 1R target

            if position_qty > Decimal("0.01"):
                exposure_bars += 1

            # Update equity
            current_equity = available_cash + position_qty * bar.close
            equity_curve.append({
                "time": ts.isoformat(),
                "equity": float(current_equity),
                "cash": float(available_cash),
                "position_value": float(position_qty * bar.close),
                "bar_idx": i,
            })

        # Close any remaining open position
        if position_qty > 0 and filtered_bars and filtered_times and filtered_indices:
            last_bar = filtered_bars[-1]
            last_time = filtered_times[-1]
            last_idx = filtered_indices[-1]
            fp, slip = self.executor.simulate_fill(
                BacktestOrder(id="final", ticker=self.ticker, side="sell",
                              order_type="market", quantity=position_qty,
                              limit_price=None, submitted_bar_idx=last_idx),
                last_bar, "sell"
            )
            raw_pnl = (fp - position_entry_price) * position_qty
            trades.append(BacktestTrade(
                id=str(uuid.uuid4())[:8],
                ticker=self.ticker,
                entry_price=position_entry_price,
                exit_price=fp,
                quantity=position_qty,
                side="buy",
                pnl=raw_pnl,
                pnl_pct=(raw_pnl / (position_entry_price * position_qty) * 100).quantize(Decimal("0.01")),
                entry_bar_idx=position_entry_idx,
                exit_bar_idx=last_idx,
                entry_time=bar_times[position_entry_idx],
                exit_time=last_time,
                exit_reason="backtest_end",
                slippage_cost=remaining_entry_slippage + slip,
                holding_bars=last_idx - position_entry_idx,
                mfe=(mfe_high - position_entry_price) * position_qty,
                mae=(position_entry_price - mae_low) * position_qty,
            ))
            available_cash += fp * position_qty - self.commission_per_trade
            total_commission_cost += self.commission_per_trade

        final_capital = available_cash
        result = BacktestResult(
            strategy_name=type(self.strategy).__name__,
            ticker=self.ticker,
            start_date=self.start_date or (filtered_times[0].date() if filtered_times else date.today()),
            end_date=self.end_date or (filtered_times[-1].date() if filtered_times else date.today()),
            initial_capital=self.initial_capital,
            final_capital=final_capital,
            trades=trades,
            equity_curve=equity_curve,
        )
        result.total_commission_cost = total_commission_cost
        if filtered_bars and filtered_times:
            first_close = filtered_bars[0].close
            last_close = filtered_bars[-1].close
            if first_close > 0:
                result.benchmark_return_pct = (
                    (last_close - first_close) / first_close * 100
                ).quantize(Decimal("0.01"))
            result.exposure_pct = Decimal(
                str(round(exposure_bars / len(filtered_times) * 100, 2))
            )
        return _compute_metrics(result)


def _compute_metrics(result: BacktestResult) -> BacktestResult:
    """Compute all performance metrics from raw trade list."""
    trades = result.trades
    result.net_pnl = result.final_capital - result.initial_capital
    result.gross_pnl = (
        result.net_pnl + result.total_slippage_cost + result.total_commission_cost
    ).quantize(Decimal("0.01"))
    result.total_return_pct = Decimal(
        str(round(float(result.net_pnl / result.initial_capital * 100), 2))
    )
    result.gross_return_pct = Decimal(
        str(round(float(result.gross_pnl / result.initial_capital * 100), 2))
    )

    # Annualised return (CAGR)
    days = (result.end_date - result.start_date).days or 1
    if days > 0:
        years = days / 365
        ann = ((float(result.final_capital) / float(result.initial_capital)) ** (1 / years) - 1) * 100
        result.annualised_return_pct = Decimal(str(round(ann, 2)))

    # Drawdown
    equity_values = [e["equity"] for e in result.equity_curve]
    if equity_values:
        peak = equity_values[0]
        max_dd = 0.0
        dd_start = 0
        max_dd_dur = 0
        current_dd_start = 0
        for idx, eq in enumerate(equity_values):
            if eq > peak:
                peak = eq
                current_dd_start = idx
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd
                dd_start = current_dd_start
                max_dd_dur = idx - dd_start
        result.max_drawdown_pct = Decimal(str(round(max_dd, 2)))
        result.max_drawdown_duration_days = max_dd_dur // 78  # ~78 5-min bars per day
        if max_dd > 0:
            result.calmar_ratio = Decimal(
                str(round(float(result.annualised_return_pct) / max_dd, 3))
            )

    result.alpha_vs_benchmark_pct = (
        result.total_return_pct - result.benchmark_return_pct
    ).quantize(Decimal("0.01"))

    if not trades:
        return result

    pnls = [float(t.pnl) for t in trades]
    pnl_pcts = [float(t.pnl_pct) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    result.total_trades = len(trades)
    result.winning_trades = len(wins)
    result.losing_trades = len(losses)
    result.win_rate = Decimal(str(round(len(wins) / len(trades), 4)))
    result.avg_win = Decimal(str(round(sum(wins) / len(wins), 2))) if wins else Decimal("0")
    result.avg_loss = Decimal(str(round(sum(losses) / len(losses), 2))) if losses else Decimal("0")
    result.expectancy = Decimal(str(round(statistics.mean(pnls), 2)))
    result.expectancy_pct = Decimal(str(round(statistics.mean(pnl_pcts), 2)))
    result.profit_factor = Decimal(str(round(sum(wins) / abs(sum(losses)), 3))) if losses and sum(losses) != 0 else Decimal("0")
    result.total_slippage_cost = sum(t.slippage_cost for t in trades)
    result.gross_pnl = (
        result.net_pnl + result.total_slippage_cost + result.total_commission_cost
    ).quantize(Decimal("0.01"))
    result.gross_return_pct = Decimal(
        str(round(float(result.gross_pnl / result.initial_capital * 100), 2))
    )
    result.avg_mfe = Decimal(str(round(float(sum(t.mfe for t in trades)) / len(trades), 2)))
    result.avg_mae = Decimal(str(round(float(sum(t.mae for t in trades)) / len(trades), 2)))
    result.avg_holding_bars = Decimal(str(round(sum(t.holding_bars for t in trades) / len(trades), 1)))
    turnover_notional = sum(
        (trade.entry_price * trade.quantity) + (trade.exit_price * trade.quantity)
        for trade in trades
    )
    result.turnover_pct = Decimal(
        str(round(float(turnover_notional / result.initial_capital * 100), 2))
    )

    # Sharpe ratio (annualised, risk-free = 0 for simplicity)
    if len(pnls) > 1:
        mean_pnl = statistics.mean(pnls)
        std_pnl = statistics.stdev(pnls)
        if std_pnl > 0:
            sharpe = (mean_pnl / std_pnl) * (252 ** 0.5)
            result.sharpe_ratio = Decimal(str(round(sharpe, 3)))

    # Sortino ratio (downside deviation only)
    downside = [p for p in pnls if p < 0]
    if downside and len(downside) > 1:
        downside_std = statistics.stdev(downside)
        if downside_std > 0:
            mean_pnl = statistics.mean(pnls)
            sortino = (mean_pnl / downside_std) * (252 ** 0.5)
            result.sortino_ratio = Decimal(str(round(sortino, 3)))

    # Consecutive losses
    max_consec = 0
    current_consec = 0
    for p in pnls:
        if p <= 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0
    result.consecutive_losses_max = max_consec

    # Average R:R achieved
    rr_achieved = []
    for t in trades:
        if t.pnl > 0 and t.entry_price > 0:
            rr_achieved.append(float(t.pnl_pct))
    if rr_achieved:
        result.avg_rr_achieved = Decimal(str(round(statistics.mean(rr_achieved), 3)))

    result.monte_carlo = monte_carlo_trade_sequence(
        trades,
        result.initial_capital,
    )

    return result


# ── Walk-forward validator ────────────────────────────────────────────────────

class WalkForwardValidator:
    """
    Walk-forward validation (out-of-sample testing).

    Splits data into in-sample (optimisation) and out-of-sample (validation) windows.
    Rolls forward, re-optimising each time.

    Standard: 70% in-sample, 30% out-of-sample, 50% overlap between windows.
    """

    def __init__(
        self,
        strategy_class: Any,
        ticker: str,
        initial_capital: Decimal,
        in_sample_bars: int = 2000,     # ~13 months of 5-min bars
        out_sample_bars: int = 500,     # ~3 months
        step_bars: int = 250,           # Roll forward by ~1.5 months
    ) -> None:
        self.strategy_class = strategy_class
        self.ticker = ticker
        self.initial_capital = initial_capital
        self.in_sample_bars = in_sample_bars
        self.out_sample_bars = out_sample_bars
        self.step_bars = step_bars

    def run(
        self,
        bars: list[Bar],
        bar_times: list[datetime],
        param_grid: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Run walk-forward validation.

        param_grid: list of parameter dicts to try during in-sample optimisation.
        Returns list of out-of-sample results per window.
        """
        results = []
        total = len(bars)
        start = 0

        window_num = 0
        while start + self.in_sample_bars + self.out_sample_bars <= total:
            window_num += 1
            is_end = start + self.in_sample_bars
            oos_end = is_end + self.out_sample_bars

            is_bars   = bars[start:is_end]
            is_times  = bar_times[start:is_end]
            oos_bars  = bars[is_end:oos_end]
            oos_times = bar_times[is_end:oos_end]

            # Optimise on in-sample
            best_params = self._optimise(is_bars, is_times, param_grid)

            # Validate on out-of-sample
            strategy = self.strategy_class(best_params)
            bt = Backtester(strategy, self.ticker, self.initial_capital)
            oos_result = bt.run(oos_bars, oos_times)

            results.append({
                "window": window_num,
                "is_start": is_times[0].date().isoformat() if is_times else "",
                "is_end":   is_times[-1].date().isoformat() if is_times else "",
                "oos_start": oos_times[0].date().isoformat() if oos_times else "",
                "oos_end":   oos_times[-1].date().isoformat() if oos_times else "",
                "best_params": best_params,
                "oos_return_pct": float(oos_result.total_return_pct),
                "oos_sharpe": float(oos_result.sharpe_ratio or 0),
                "oos_max_dd": float(oos_result.max_drawdown_pct),
                "oos_win_rate": float(oos_result.win_rate),
                "oos_profit_factor": float(oos_result.profit_factor),
                "oos_trades": oos_result.total_trades,
            })

            log.info(
                "walk_forward.window_complete",
                window=window_num,
                oos_return=float(oos_result.total_return_pct),
                oos_sharpe=float(oos_result.sharpe_ratio or 0),
            )
            start += self.step_bars

        return results

    def _optimise(
        self,
        bars: list[Bar],
        bar_times: list[datetime],
        param_grid: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Grid search on in-sample data. Returns best params by Sharpe ratio."""
        best_sharpe = -9999.0
        best_params: dict[str, Any] = param_grid[0] if param_grid else {}

        for params in param_grid:
            try:
                strategy = self.strategy_class(params)
                bt = Backtester(strategy, "", self.initial_capital)
                result = bt.run(bars, bar_times)
                sharpe = float(result.sharpe_ratio or -9999)
                # Require minimum trades to avoid over-fitting
                if result.total_trades >= 10 and sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = params
            except Exception:
                continue

        return best_params
