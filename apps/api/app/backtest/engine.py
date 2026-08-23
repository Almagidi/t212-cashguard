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
import math
import random
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from app.backtest.data_contract import validate_bar_series
from app.execution.paper_policy import PaperFillDecision, evaluate_paper_fill

if TYPE_CHECKING:
    from app.strategies.indicators import Bar

log = structlog.get_logger()


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class BacktestOrder:
    id: str
    ticker: str
    side: str  # buy | sell
    order_type: str  # market | limit
    quantity: Decimal
    limit_price: Decimal | None
    submitted_bar_idx: int
    fill_bar_idx: int | None = None
    fill_price: Decimal | None = None
    slippage: Decimal = Decimal("0")
    status: str = "pending"  # pending | filled | cancelled | expired


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
    exit_reason: str  # stop | take_profit | partial | eod | signal
    slippage_cost: Decimal
    holding_bars: int
    commission_cost: Decimal = Decimal("0")
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
    completed_positions: int = 0
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


@dataclass(frozen=True)
class ParameterSelectionResult:
    """Auditable outcome of a train/validation parameter search."""

    params: dict[str, Any] | None
    combinations_tested: int
    eligible_candidates: int
    validation_sharpe: float | None


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

    selected = [item for item in results if item.get("selection_status") == "selected"]
    profitable = sum(1 for item in selected if item["oos_return_pct"] > 0)
    positive_sharpe = sum(
        1 for item in selected if item["oos_sharpe"] is not None and item["oos_sharpe"] > 0.5
    )
    controlled_drawdown = sum(1 for item in selected if item["oos_max_dd"] <= 15.0)
    selected_count = len(selected)
    robustness_score = (
        round(
            (
                0.5 * (profitable / selected_count)
                + 0.3 * (positive_sharpe / selected_count)
                + 0.2 * (controlled_drawdown / selected_count)
            )
            * 100,
            1,
        )
        if selected
        else 0.0
    )

    performance_assessment = "unassessed" if not selected else "fragile"
    if selected and robustness_score >= 75:
        performance_assessment = "favourable"
    elif selected and robustness_score >= 40:
        performance_assessment = "mixed"

    oos_returns = [item["oos_return_pct"] for item in selected]
    oos_drawdowns = [item["oos_max_dd"] for item in selected]
    oos_sharpes = [item["oos_sharpe"] for item in selected if item["oos_sharpe"] is not None]
    total_oos_positions = sum(int(item["oos_positions"]) for item in selected)
    minimum_window_positions_met = bool(selected) and all(
        int(item["oos_positions"]) >= 10 for item in selected
    )
    evidence_reasons = []
    if selected_count < 3:
        evidence_reasons.append("need at least 3 selected held-out windows")
    if not minimum_window_positions_met:
        evidence_reasons.append("need at least 10 independent positions in every held-out window")
    if total_oos_positions < 30:
        evidence_reasons.append("need at least 30 independent held-out positions in total")

    verdict = "insufficient_evidence" if evidence_reasons else "research_only"
    message = (
        "Insufficient evidence: " + "; ".join(evidence_reasons) + "."
        if evidence_reasons
        else "Evidence gates passed for research comparison only; this is not a promotion decision."
    )

    return {
        "windows": len(results),
        "selected_windows": selected_count,
        "selection_failures": len(results) - selected_count,
        "total_oos_positions": total_oos_positions,
        "minimum_oos_positions_per_window": 10,
        "minimum_selected_windows": 3,
        "parameter_combinations_tested": max(
            (int(item.get("parameter_combinations_tested", 0)) for item in results),
            default=0,
        ),
        "candidate_evaluations": sum(
            int(item.get("parameter_combinations_tested", 0)) for item in results
        ),
        "eligible_candidate_evaluations": sum(
            int(item.get("eligible_candidates", 0)) for item in results
        ),
        "profitable_windows": profitable,
        "positive_sharpe_windows": positive_sharpe,
        "controlled_drawdown_windows": controlled_drawdown,
        "avg_oos_return_pct": round(statistics.mean(oos_returns), 2) if oos_returns else None,
        "median_oos_return_pct": (
            round(statistics.median(oos_returns), 2) if oos_returns else None
        ),
        "avg_oos_sharpe": round(statistics.mean(oos_sharpes), 3) if oos_sharpes else None,
        "median_oos_sharpe": (round(statistics.median(oos_sharpes), 3) if oos_sharpes else None),
        "worst_oos_max_dd": round(max(oos_drawdowns), 2) if oos_drawdowns else None,
        "robustness_score": robustness_score,
        "performance_assessment": performance_assessment,
        "verdict": verdict,
        "message": message,
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
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
        "median_consecutive_losses": round(statistics.median(max_consecutive_losses)),
        "p95_consecutive_losses": round(
            _percentile([float(item) for item in max_consecutive_losses], 0.95)
        ),
        "probability_drawdown_gt_10pct": round(
            sum(dd >= 10 for dd in max_drawdowns) / iterations * 100, 1
        ),
        "probability_drawdown_gt_20pct": round(
            sum(dd >= 20 for dd in max_drawdowns) / iterations * 100, 1
        ),
    }


# ── Execution simulation ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SimulatedFill:
    price: Decimal
    quantity: Decimal
    slippage_cost: Decimal
    fee: Decimal


class ExecutionSimulator:
    """
    Realistic fill simulation.

    Market fills use the same deterministic standard profile as the paper
    execution engine so research and paper results share one cost model.
    """

    def simulate_fill(
        self,
        order: BacktestOrder,
        next_bar: Bar,
        side: str,
    ) -> SimulatedFill:
        """Fill a market order at the next bar open."""
        return self.simulate_at_quote(order=order, quote_price=next_bar.open, side=side)

    def simulate_at_quote(
        self,
        *,
        order: BacktestOrder,
        quote_price: Decimal,
        side: str,
    ) -> SimulatedFill:
        if side not in {"buy", "sell"}:
            raise ValueError(f"Unsupported execution side: {side}")
        decision: PaperFillDecision = evaluate_paper_fill(
            side=side,
            quantity=order.quantity,
            quote_price=quote_price,
            profile="standard",
        )
        if decision.outcome != "filled" or decision.fill_price is None:
            raise ValueError(f"Paper execution rejected backtest fill: {decision.rejection_code}")
        slippage_cost = abs(decision.fill_price - decision.quote_price) * decision.filled_quantity
        return SimulatedFill(
            price=decision.fill_price,
            quantity=decision.filled_quantity,
            slippage_cost=slippage_cost.quantize(Decimal("0.01")),
            fee=decision.fee_amount,
        )

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
        commission_per_trade: Decimal | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("Backtest initial capital must be positive")
        if risk_per_trade_pct <= 0 or risk_per_trade_pct > 100:
            raise ValueError("risk_per_trade_pct must be greater than 0 and at most 100")
        if max_position_pct <= 0 or max_position_pct > 100:
            raise ValueError("max_position_pct must be greater than 0 and at most 100")
        if commission_per_trade is not None and commission_per_trade < 0:
            raise ValueError("commission_per_trade cannot be negative")
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

    def _fill_fee(self, fill: SimulatedFill) -> Decimal:
        """Use paper-policy fees by default while preserving explicit legacy overrides."""
        if self.commission_per_trade is not None:
            return self.commission_per_trade
        return fill.fee

    def _cap_entry_fill(
        self,
        *,
        order: BacktestOrder,
        bar: Bar,
        available_cash: Decimal,
        account_equity: Decimal,
        stop_price: Decimal,
    ) -> SimulatedFill | None:
        """Cap a long entry by position size, stop risk, and settled cash."""
        if order.quantity <= 0:
            raise ValueError("Backtest entry quantity must be positive")

        initial_fill = self.executor.simulate_fill(order, bar, "buy")
        fill_price = initial_fill.price
        quantity = initial_fill.quantity
        position_budget = account_equity * self.max_position_pct / Decimal("100")
        quantity = min(quantity, position_budget / fill_price)

        if self.stop_loss_required and (stop_price <= 0 or stop_price >= fill_price):
            raise ValueError("Long-only backtest entry requires a stop below its fill price")
        risk_per_share = fill_price - stop_price
        if risk_per_share > 0:
            risk_budget = account_equity * self.risk_per_trade_pct / Decimal("100")
            quantity = min(quantity, risk_budget / risk_per_share)

        quantity = quantity.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if quantity <= 0:
            return None

        capped_order = BacktestOrder(
            id=order.id,
            ticker=order.ticker,
            side=order.side,
            order_type=order.order_type,
            quantity=quantity,
            limit_price=order.limit_price,
            submitted_bar_idx=order.submitted_bar_idx,
        )
        fill = self.executor.simulate_fill(capped_order, bar, "buy")
        fee = self._fill_fee(fill)
        if fill.price * fill.quantity + fee > available_cash:
            spendable = available_cash - fee
            if spendable <= 0:
                return None
            capped_order.quantity = min(
                fill.quantity,
                (spendable / fill.price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN),
            )
            if capped_order.quantity <= 0:
                return None
            fill = self.executor.simulate_fill(capped_order, bar, "buy")
            fee = self._fill_fee(fill)
            if fill.price * fill.quantity + fee > available_cash:
                return None

        return SimulatedFill(
            price=fill.price,
            quantity=fill.quantity,
            slippage_cost=fill.slippage_cost,
            fee=fee,
        )

    def run(self, bars: list[Bar], bar_times: list[datetime]) -> BacktestResult:
        """
        Run full backtest over the provided bar series.

        bars: list of Bar namedtuples in chronological order
        bar_times: matching list of datetime for each bar
        """
        validate_bar_series(bars, bar_times)

        capital = self.initial_capital
        available_cash = capital
        position_qty = Decimal("0")
        position_entry_price = Decimal("0")
        position_entry_idx = 0
        position_stop = Decimal("0")
        position_tp = Decimal("0")
        position_tp1 = Decimal("0")
        remaining_entry_slippage = Decimal("0")
        remaining_entry_fee = Decimal("0")
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
                        if order.side != "buy":
                            raise ValueError(
                                "The single-symbol backtester is long-only; "
                                f"unsupported entry side: {order.side}"
                            )
                        account_equity = available_cash + position_qty * bar.open
                        fill = self._cap_entry_fill(
                            order=order,
                            bar=bar,
                            available_cash=available_cash,
                            account_equity=account_equity,
                            stop_price=position_stop,
                        )
                        if fill is None:
                            order.status = "cancelled"
                            continue
                        order.quantity = fill.quantity
                        order.fill_price = fill.price
                        order.fill_bar_idx = i
                        order.slippage = fill.slippage_cost
                        order.status = "filled"
                        cost = fill.price * fill.quantity + fill.fee
                        available_cash -= cost
                        total_commission_cost += fill.fee
                        position_qty += fill.quantity
                        position_entry_price = fill.price
                        position_entry_idx = i
                        remaining_entry_slippage = fill.slippage_cost
                        remaining_entry_fee = fill.fee
                        mfe_high = fill.price
                        mae_low = fill.price
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
                            fee = self.commission_per_trade or Decimal("0")
                            cost = order.limit_price * order.quantity + fee
                            available_cash -= cost
                            total_commission_cost += fee
                            position_qty += order.quantity
                            position_entry_price = order.limit_price
                            position_entry_idx = i
                            remaining_entry_slippage = Decimal("0")
                            remaining_entry_fee = fee
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
                mae_low = min(mae_low, bar.low)

            # Check exit conditions for open position
            if position_qty > 0 and position_stop > 0:
                exit_reason = None
                exit_quote = None
                exit_qty = position_qty

                # Stop loss
                if bar.low <= position_stop:
                    exit_reason = "stop"
                    exit_quote = min(bar.open, position_stop)  # Worst case: gap through stop

                # Take profit full
                elif bar.high >= position_tp and not partial_done:
                    exit_reason = "take_profit"
                    exit_quote = position_tp

                # Partial exit at 1R
                elif bar.high >= position_tp1 and not partial_done:
                    exit_reason = "partial"
                    exit_qty = (position_qty * Decimal("0.5")).quantize(
                        Decimal("0.00000001"), rounding=ROUND_DOWN
                    )
                    exit_quote = position_tp1
                    partial_done = True

                # Max holding time
                elif i - position_entry_idx >= self.max_holding_bars:
                    exit_reason = "eod"
                    exit_quote = bar.open

                if exit_reason and exit_quote and exit_qty > 0:
                    exit_fill = self.executor.simulate_at_quote(
                        order=BacktestOrder(
                            id=f"exit-{i}",
                            ticker=self.ticker,
                            side="sell",
                            order_type="market",
                            quantity=exit_qty,
                            limit_price=None,
                            submitted_bar_idx=i,
                        ),
                        quote_price=exit_quote,
                        side="sell",
                    )
                    exit_price = exit_fill.price
                    exit_fee = self._fill_fee(exit_fill)
                    open_qty_before_exit = position_qty
                    raw_pnl = (exit_price - position_entry_price) * exit_qty
                    entry_slippage_alloc = (
                        remaining_entry_slippage * exit_qty / open_qty_before_exit
                        if open_qty_before_exit > 0
                        else Decimal("0")
                    )
                    entry_fee_alloc = (
                        remaining_entry_fee * exit_qty / open_qty_before_exit
                        if open_qty_before_exit > 0
                        else Decimal("0")
                    )
                    commission_cost = entry_fee_alloc + exit_fee
                    net_pnl = raw_pnl - commission_cost
                    slippage_cost = entry_slippage_alloc + exit_fill.slippage_cost

                    trade = BacktestTrade(
                        id=str(uuid.uuid4())[:8],
                        ticker=self.ticker,
                        entry_price=position_entry_price,
                        exit_price=exit_price,
                        quantity=exit_qty,
                        side="buy",
                        pnl=net_pnl,
                        pnl_pct=(net_pnl / (position_entry_price * exit_qty) * 100).quantize(
                            Decimal("0.01")
                        ),
                        entry_bar_idx=position_entry_idx,
                        exit_bar_idx=i,
                        entry_time=bar_times[position_entry_idx],
                        exit_time=ts,
                        exit_reason=exit_reason,
                        slippage_cost=slippage_cost,
                        holding_bars=i - position_entry_idx,
                        commission_cost=commission_cost,
                        mfe=(mfe_high - position_entry_price) * exit_qty,
                        mae=(position_entry_price - mae_low) * exit_qty,
                    )
                    trades.append(trade)

                    proceeds = exit_price * exit_qty - exit_fee
                    available_cash += proceeds
                    total_commission_cost += exit_fee
                    position_qty -= exit_qty
                    remaining_entry_slippage -= entry_slippage_alloc
                    remaining_entry_fee -= entry_fee_alloc

                    if position_qty <= 0:
                        position_qty = Decimal("0")
                        position_stop = Decimal("0")
                        position_tp = Decimal("0")
                        position_tp1 = Decimal("0")
                        remaining_entry_slippage = Decimal("0")
                        remaining_entry_fee = Decimal("0")
                        partial_done = False

                    capital = available_cash + position_qty * bar.close

            # Only generate entry signals when flat
            if position_qty <= 0 and not pending_orders:
                signal = generate_strategy_signal(
                    self.strategy,
                    ticker=self.ticker,
                    bars=session_bars,
                    bar_times=filtered_times[-len(session_bars) :],
                    history_bars=filtered_bars,
                    history_bar_times=filtered_times,
                    account_value=capital,
                    available_cash=available_cash,
                    current_time_utc=current_time_utc,
                    prev_close=previous_session_close,
                )

                if signal:
                    if signal.side != "buy":
                        raise ValueError(
                            "The single-symbol backtester is long-only; "
                            f"unsupported entry side: {signal.side}"
                        )
                    if signal.suggested_quantity <= 0:
                        raise ValueError("Backtest entry quantity must be positive")
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

            if position_qty > 0:
                exposure_bars += 1

            # Update equity
            current_equity = available_cash + position_qty * bar.close
            equity_curve.append(
                {
                    "time": ts.isoformat(),
                    "equity": float(current_equity),
                    "cash": float(available_cash),
                    "position_value": float(position_qty * bar.close),
                    "bar_idx": i,
                }
            )

        # Close any remaining open position
        if position_qty > 0 and filtered_bars and filtered_times and filtered_indices:
            last_bar = filtered_bars[-1]
            last_time = filtered_times[-1]
            last_idx = filtered_indices[-1]
            final_qty = position_qty
            final_fill = self.executor.simulate_at_quote(
                order=BacktestOrder(
                    id="final",
                    ticker=self.ticker,
                    side="sell",
                    order_type="market",
                    quantity=final_qty,
                    limit_price=None,
                    submitted_bar_idx=last_idx,
                ),
                quote_price=last_bar.close,
                side="sell",
            )
            final_fee = self._fill_fee(final_fill)
            raw_pnl = (final_fill.price - position_entry_price) * final_qty
            commission_cost = remaining_entry_fee + final_fee
            net_pnl = raw_pnl - commission_cost
            trades.append(
                BacktestTrade(
                    id=str(uuid.uuid4())[:8],
                    ticker=self.ticker,
                    entry_price=position_entry_price,
                    exit_price=final_fill.price,
                    quantity=final_qty,
                    side="buy",
                    pnl=net_pnl,
                    pnl_pct=(net_pnl / (position_entry_price * final_qty) * 100).quantize(
                        Decimal("0.01")
                    ),
                    entry_bar_idx=position_entry_idx,
                    exit_bar_idx=last_idx,
                    entry_time=bar_times[position_entry_idx],
                    exit_time=last_time,
                    exit_reason="backtest_end",
                    slippage_cost=remaining_entry_slippage + final_fill.slippage_cost,
                    holding_bars=last_idx - position_entry_idx,
                    commission_cost=commission_cost,
                    mfe=(mfe_high - position_entry_price) * final_qty,
                    mae=(position_entry_price - mae_low) * final_qty,
                )
            )
            available_cash += final_fill.price * final_qty - final_fee
            total_commission_cost += final_fee
            position_qty = Decimal("0")
            if equity_curve:
                equity_curve[-1]["equity"] = float(available_cash)
                equity_curve[-1]["cash"] = float(available_cash)
                equity_curve[-1]["position_value"] = 0.0

        final_capital = available_cash
        result = BacktestResult(
            strategy_name=type(self.strategy).__name__,
            ticker=self.ticker,
            start_date=self.start_date
            or (filtered_times[0].date() if filtered_times else date.today()),
            end_date=self.end_date
            or (filtered_times[-1].date() if filtered_times else date.today()),
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
            result.exposure_pct = Decimal(str(round(exposure_bars / len(filtered_times) * 100, 2)))
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
        ann = (
            (float(result.final_capital) / float(result.initial_capital)) ** (1 / years) - 1
        ) * 100
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

    # Risk-adjusted ratios use the marked-to-market equity return path. Trade
    # P&L observations are not time-series returns and change when identical
    # fills are split into multiple trade records.
    equity_returns: list[float] = []
    for previous, current in pairwise(result.equity_curve):
        previous_equity = float(previous["equity"])
        if previous_equity > 0:
            equity_returns.append((float(current["equity"]) / previous_equity) - 1)

    periods_per_year = 252.0
    equity_times: list[datetime] = []
    for point in result.equity_curve:
        raw_time = point.get("time")
        if isinstance(raw_time, str):
            try:
                equity_times.append(datetime.fromisoformat(raw_time))
            except ValueError:
                equity_times = []
                break
    if len(equity_times) > 1:
        intervals = [
            (current - previous).total_seconds()
            for previous, current in pairwise(equity_times)
            if current > previous
        ]
        if intervals:
            median_interval = statistics.median(intervals)
            if median_interval < 12 * 60 * 60:
                periods_per_trading_day = min((6.5 * 60 * 60) / median_interval, 390.0)
                periods_per_year = 252.0 * periods_per_trading_day

    if len(equity_returns) > 1:
        mean_return = statistics.mean(equity_returns)
        std_return = statistics.stdev(equity_returns)
        if std_return > 0:
            sharpe = (mean_return / std_return) * math.sqrt(periods_per_year)
            result.sharpe_ratio = Decimal(str(round(sharpe, 3)))

        downside_deviation = math.sqrt(
            statistics.mean(min(period_return, 0.0) ** 2 for period_return in equity_returns)
        )
        if downside_deviation > 0:
            sortino = (mean_return / downside_deviation) * math.sqrt(periods_per_year)
            result.sortino_ratio = Decimal(str(round(sortino, 3)))

    if not trades:
        return result

    pnls = [float(t.pnl) for t in trades]
    pnl_pcts = [float(t.pnl_pct) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    result.total_trades = len(trades)
    result.completed_positions = len({(trade.ticker, trade.entry_bar_idx) for trade in trades})
    result.winning_trades = len(wins)
    result.losing_trades = len(losses)
    result.win_rate = Decimal(str(round(len(wins) / len(trades), 4)))
    result.avg_win = Decimal(str(round(sum(wins) / len(wins), 2))) if wins else Decimal("0")
    result.avg_loss = Decimal(str(round(sum(losses) / len(losses), 2))) if losses else Decimal("0")
    result.expectancy = Decimal(str(round(statistics.mean(pnls), 2)))
    result.expectancy_pct = Decimal(str(round(statistics.mean(pnl_pcts), 2)))
    result.profit_factor = (
        Decimal(str(round(sum(wins) / abs(sum(losses)), 3)))
        if losses and sum(losses) != 0
        else Decimal("0")
    )
    result.total_slippage_cost = sum(
        (trade.slippage_cost for trade in trades),
        Decimal("0"),
    )
    result.gross_pnl = (
        result.net_pnl + result.total_slippage_cost + result.total_commission_cost
    ).quantize(Decimal("0.01"))
    result.gross_return_pct = Decimal(
        str(round(float(result.gross_pnl / result.initial_capital * 100), 2))
    )
    result.avg_mfe = Decimal(str(round(float(sum(t.mfe for t in trades)) / len(trades), 2)))
    result.avg_mae = Decimal(str(round(float(sum(t.mae for t in trades)) / len(trades), 2)))
    result.avg_holding_bars = Decimal(
        str(round(sum(t.holding_bars for t in trades) / len(trades), 1))
    )
    turnover_notional = sum(
        (trade.entry_price * trade.quantity) + (trade.exit_price * trade.quantity)
        for trade in trades
    )
    result.turnover_pct = Decimal(
        str(round(float(turnover_notional / result.initial_capital * 100), 2))
    )

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
    Nested walk-forward validation with disjoint chronological blocks.

    Every block has train, validation, and held-out test partitions. Candidate
    eligibility is checked on train, selection is performed on validation, and
    the chosen candidate is evaluated once on test. Blocks never overlap.
    """

    def __init__(
        self,
        strategy_class: Any,
        ticker: str,
        initial_capital: Decimal,
        in_sample_bars: int = 2000,  # ~13 months of 5-min bars
        out_sample_bars: int = 500,  # ~3 months
        step_bars: int = 250,
    ) -> None:
        for parameter_name, parameter_value in (
            ("in_sample_bars", in_sample_bars),
            ("out_sample_bars", out_sample_bars),
            ("step_bars", step_bars),
        ):
            if parameter_value <= 0:
                raise ValueError(f"{parameter_name} must be positive")
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

        ``step_bars`` is retained for API compatibility, but the effective
        stride is never smaller than a complete train/validation/test block.
        The returned OOS metrics are exclusively from held-out test partitions.
        """
        validate_bar_series(bars, bar_times, label="walk-forward bar series")
        results = []
        total = len(bars)
        start = 0

        window_num = 0
        block_bars = self.in_sample_bars + (2 * self.out_sample_bars)
        stride_bars = max(self.step_bars, block_bars)
        while start + block_bars <= total:
            window_num += 1
            train_end = start + self.in_sample_bars
            validation_end = train_end + self.out_sample_bars
            test_end = validation_end + self.out_sample_bars

            train_bars = bars[start:train_end]
            train_times = bar_times[start:train_end]
            validation_bars = bars[train_end:validation_end]
            validation_times = bar_times[train_end:validation_end]
            test_bars = bars[validation_end:test_end]
            test_times = bar_times[validation_end:test_end]

            selection = self._optimise(
                train_bars,
                train_times,
                validation_bars,
                validation_times,
                param_grid,
            )

            window_result: dict[str, Any] = {
                "window": window_num,
                "is_start": train_times[0].date().isoformat() if train_times else "",
                "is_end": train_times[-1].date().isoformat() if train_times else "",
                "validation_start": (
                    validation_times[0].date().isoformat() if validation_times else ""
                ),
                "validation_end": (
                    validation_times[-1].date().isoformat() if validation_times else ""
                ),
                "oos_start": test_times[0].date().isoformat() if test_times else "",
                "oos_end": test_times[-1].date().isoformat() if test_times else "",
                "selection_status": (
                    "selected" if selection.params is not None else "no_eligible_candidate"
                ),
                "best_params": selection.params,
                "selection_criterion": "validation_equity_sharpe",
                "validation_sharpe": selection.validation_sharpe,
                "parameter_combinations_tested": selection.combinations_tested,
                "eligible_candidates": selection.eligible_candidates,
                "oos_return_pct": None,
                "oos_sharpe": None,
                "oos_max_dd": None,
                "oos_win_rate": None,
                "oos_profit_factor": None,
                "oos_trades": 0,
                "oos_positions": 0,
            }

            if selection.params is not None:
                strategy = self.strategy_class(selection.params)
                oos_result = Backtester(strategy, self.ticker, self.initial_capital).run(
                    test_bars, test_times
                )
                window_result.update(
                    {
                        "oos_return_pct": float(oos_result.total_return_pct),
                        "oos_sharpe": (
                            float(oos_result.sharpe_ratio)
                            if oos_result.sharpe_ratio is not None
                            and math.isfinite(float(oos_result.sharpe_ratio))
                            else None
                        ),
                        "oos_max_dd": float(oos_result.max_drawdown_pct),
                        "oos_win_rate": float(oos_result.win_rate),
                        "oos_profit_factor": float(oos_result.profit_factor),
                        "oos_trades": oos_result.total_trades,
                        "oos_positions": oos_result.completed_positions,
                    }
                )

            results.append(window_result)

            log.info(
                "walk_forward.window_complete",
                window=window_num,
                selection_status=window_result["selection_status"],
                oos_return=window_result["oos_return_pct"],
                oos_sharpe=window_result["oos_sharpe"],
            )
            start += stride_bars

        return results

    def _optimise(
        self,
        train_bars: list[Bar],
        train_times: list[datetime],
        validation_bars: list[Bar],
        validation_times: list[datetime],
        param_grid: list[dict[str, Any]],
    ) -> ParameterSelectionResult:
        """Select by validation Sharpe without reading held-out test data."""
        best_sharpe = -math.inf
        best_params: dict[str, Any] | None = None
        eligible_candidates = 0

        for params in param_grid:
            try:
                train_strategy = self.strategy_class(params)
                train_result = Backtester(train_strategy, self.ticker, self.initial_capital).run(
                    train_bars, train_times
                )
                if train_result.completed_positions < 10:
                    continue

                validation_strategy = self.strategy_class(params)
                validation_result = Backtester(
                    validation_strategy, self.ticker, self.initial_capital
                ).run(validation_bars, validation_times)
                if validation_result.completed_positions < 10:
                    continue

                if validation_result.sharpe_ratio is None:
                    continue
                sharpe = float(validation_result.sharpe_ratio)
                if not math.isfinite(sharpe):
                    continue
                eligible_candidates += 1
                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = params
            except Exception:
                continue

        return ParameterSelectionResult(
            params=best_params,
            combinations_tested=len(param_grid),
            eligible_candidates=eligible_candidates,
            validation_sharpe=best_sharpe if best_params is not None else None,
        )
