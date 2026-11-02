"""
Portfolio-level event-driven backtester for lower-turnover long-only strategies.

This complements the intraday single-symbol engine by supporting basket-based
allocation research that more closely matches Trading 212 Pie workflows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.backtest.data_contract import validate_bar_series
from app.backtest.portfolio_costs import TransactionCostModel
from app.backtest.portfolio_metrics import (
    annualised_one_way_turnover_pct,
    annualised_return_pct_from_periods,
    max_drawdown_pct_from_equity,
    returns_from_equity,
    sharpe_ratio,
    sortino_ratio,
)
from app.market_data.exchange_calendar import TradingSession, calendar_for_venue

if TYPE_CHECKING:
    from app.strategies.indicators import Bar


SHARE_QUANT = Decimal("0.0001")
MIN_PORTFOLIO_COVERAGE_PCT = Decimal("95")

BENCHMARK_UNIVERSE_EW = "universe_equal_weight_costed"
BENCHMARK_BROAD_INDEX = "broad_index"
BENCHMARK_RISK_MATCHED = "risk_matched_blend"
BENCHMARK_KEYS = (BENCHMARK_UNIVERSE_EW, BENCHMARK_BROAD_INDEX, BENCHMARK_RISK_MATCHED)


@dataclass
class PortfolioTrade:
    date: date
    ticker: str
    side: str
    shares: Decimal
    quote_price: Decimal
    price: Decimal
    notional: Decimal
    slippage_cost: Decimal
    fee_cost: Decimal
    cost: Decimal
    reason: str
    target_weight: Decimal
    fx_fee_cost: Decimal = Decimal("0")  # included in fee_cost


@dataclass(frozen=True)
class PortfolioExecutionFill:
    price: Decimal
    shares: Decimal
    fee: Decimal
    fx_fee: Decimal = Decimal("0")


@dataclass
class PortfolioAllocationPoint:
    date: date
    equity: Decimal
    cash: Decimal
    exposure_pct: Decimal
    drawdown_pct: Decimal
    weights: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class BenchmarkComparison:
    """A costed benchmark measured over the same window and with the same metrics as the strategy.

    ``excess_*`` fields are strategy minus benchmark.  For drawdown a positive excess means the
    strategy drew down *more* than the benchmark.
    """

    key: str
    name: str
    total_return_pct: Decimal
    annualised_return_pct: Decimal
    sharpe_ratio: Decimal | None
    sortino_ratio: Decimal | None
    max_drawdown_pct: Decimal
    avg_exposure_pct: Decimal
    total_execution_cost: Decimal
    excess_return_pct: Decimal
    excess_sharpe_ratio: Decimal | None
    excess_max_drawdown_pct: Decimal
    daily_returns: list[float] = field(default_factory=list, repr=False)


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
    total_slippage_cost: Decimal
    total_fee_cost: Decimal
    total_execution_cost: Decimal
    coverage_report: PortfolioSessionCoverageReport
    equity_curve: list[PortfolioAllocationPoint] = field(default_factory=list)
    trades: list[PortfolioTrade] = field(default_factory=list)
    latest_weights: dict[str, Decimal] = field(default_factory=dict)
    # Risk metrics, annualised return, exposure and the benchmark all start at this
    # date (the first allocation); warm-up days at 100% cash are excluded.
    metrics_start_date: date | None = None
    warmup_bars_excluded: int = 0
    total_fx_fee_cost: Decimal = Decimal("0")
    cost_model: dict[str, Any] = field(default_factory=dict)
    # Annual risk-free rate used for Sharpe/Sortino (default 0.0; cash in the simulation earns 0).
    risk_free_rate_annual: Decimal = Decimal("0")
    primary_benchmark: str = BENCHMARK_UNIVERSE_EW
    benchmarks: dict[str, BenchmarkComparison] = field(default_factory=dict)
    daily_returns: list[float] = field(default_factory=list, repr=False)


@dataclass(frozen=True, slots=True)
class PortfolioCoveragePolicy:
    """Explicit calendar and minimum completeness required for portfolio research."""

    calendar_venue: str = "XNYS"
    minimum_coverage_pct: Decimal = Decimal("100")

    def __post_init__(self) -> None:
        if not MIN_PORTFOLIO_COVERAGE_PCT <= self.minimum_coverage_pct <= Decimal("100"):
            raise ValueError(
                f"minimum_coverage_pct must be between {MIN_PORTFOLIO_COVERAGE_PCT} and 100"
            )

    @property
    def name(self) -> str:
        if self.minimum_coverage_pct == Decimal("100"):
            return "strict_complete"
        return "intersection_with_disclosure"

    @property
    def policy_id(self) -> str:
        return f"portfolio_session_coverage:{self.name}:v1"


@dataclass(frozen=True, slots=True)
class SymbolSessionCoverage:
    ticker: str
    expected_session_ids: tuple[str, ...]
    observed_session_ids: tuple[str, ...]
    missing_session_ids: tuple[str, ...]
    extra_session_ids: tuple[str, ...]
    coverage_pct: Decimal
    longest_missing_run: int
    first_valid_session_id: str | None
    last_valid_session_id: str | None


@dataclass(frozen=True, slots=True)
class PortfolioSessionCoverageReport:
    calendar: str
    exchange_timezone: str
    requested_start: date
    requested_end: date
    minimum_coverage_pct: Decimal
    retained_coverage_pct: Decimal
    complete: bool
    policy: str
    policy_id: str
    eligible: bool
    common_start: date | None
    common_end: date | None
    expected_session_ids: tuple[str, ...]
    retained_session_ids: tuple[str, ...]
    dropped_session_ids: tuple[str, ...]
    symbols: tuple[SymbolSessionCoverage, ...]


class InsufficientPortfolioEvidence(ValueError):
    """Raised before strategy invocation when session coverage is ineligible."""

    verdict = "insufficient_evidence"

    def __init__(self, report: PortfolioSessionCoverageReport, reasons: list[str]) -> None:
        self.report = report
        self.reasons = tuple(reasons)
        super().__init__(f"{self.verdict}: {'; '.join(reasons)}")


def _decimal_mean(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(str(len(values)))


def _latest_completed_session(*, venue: str, as_of: datetime) -> TradingSession:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    calendar = calendar_for_venue(venue)
    exchange_timezone = ZoneInfo(calendar.exchange_timezone)
    local_date = as_of.astimezone(exchange_timezone).date()
    sessions = calendar.expected_sessions(local_date - timedelta(days=14), local_date)
    completed = [session for session in sessions if calendar.session_close(session) <= as_of]
    if not completed:
        raise ValueError("No completed exchange session is available for operational history.")
    return completed[-1]


def _ineligible_operational_report(
    *,
    venue: str,
    latest_completed_date: date,
) -> PortfolioSessionCoverageReport:
    calendar = calendar_for_venue(venue)
    session = calendar.expected_sessions(latest_completed_date, latest_completed_date)[0]
    return PortfolioSessionCoverageReport(
        calendar=calendar.venue,
        exchange_timezone=calendar.exchange_timezone,
        requested_start=latest_completed_date,
        requested_end=latest_completed_date,
        minimum_coverage_pct=Decimal("100"),
        retained_coverage_pct=Decimal("0"),
        complete=False,
        policy="strict_complete",
        policy_id="portfolio_session_coverage:strict_complete:v1",
        eligible=False,
        common_start=None,
        common_end=None,
        expected_session_ids=(session.session_id,),
        retained_session_ids=(),
        dropped_session_ids=(session.session_id,),
        symbols=(),
    )


def _longest_missing_run(expected_dates: tuple[date, ...], observed_dates: set[date]) -> int:
    longest = 0
    current = 0
    for session_date in expected_dates:
        if session_date in observed_dates:
            current = 0
            continue
        current += 1
        longest = max(longest, current)
    return longest


def _align_operational_histories(
    histories: dict[str, tuple[list[Bar], list[datetime]]],
    *,
    universe: list[str],
    as_of: datetime,
    calendar_venue: str = "XNYS",
) -> tuple[list[date], dict[str, list[Bar]], PortfolioSessionCoverageReport]:
    """Build strict complete-session history for one operational decision.

    Daily provider bars are labelled by their exchange-local date. A bar for the
    current not-yet-complete session is expected from some providers and is
    excluded explicitly; every earlier expected session must then be present for
    every requested symbol.
    """

    calendar = calendar_for_venue(calendar_venue)
    latest_completed = _latest_completed_session(venue=calendar_venue, as_of=as_of)
    latest_completed_date = latest_completed.local_date
    expected_symbols = {ticker.upper() for ticker in universe}
    observed_symbols = {ticker.upper() for ticker in histories}
    if observed_symbols != expected_symbols:
        report = _ineligible_operational_report(
            venue=calendar_venue,
            latest_completed_date=latest_completed_date,
        )
        raise InsufficientPortfolioEvidence(
            report,
            [
                "operational portfolio history symbols must exactly match the requested universe: "
                f"expected {sorted(expected_symbols)}, got {sorted(observed_symbols)}"
            ],
        )

    exchange_timezone = ZoneInfo(calendar.exchange_timezone)
    local_date = as_of.astimezone(exchange_timezone).date()
    local_sessions = calendar.expected_sessions(local_date, local_date)
    incomplete_session_date = None
    if local_sessions and calendar.session_close(local_sessions[0]) > as_of:
        incomplete_session_date = local_sessions[0].local_date

    filtered_histories: dict[str, tuple[list[Bar], list[datetime]]] = {}
    retained_dates: list[date] = []
    for raw_ticker, (bars, bar_times) in histories.items():
        ticker = raw_ticker.upper()
        validate_bar_series(bars, bar_times, label=f"portfolio bar series for {ticker}")
        filtered_bars: list[Bar] = []
        filtered_times: list[datetime] = []
        seen_dates: set[date] = set()
        for bar, bar_time in zip(bars, bar_times, strict=True):
            bar_date = bar_time.astimezone(exchange_timezone).date()
            if bar_date in seen_dates:
                report = _ineligible_operational_report(
                    venue=calendar_venue,
                    latest_completed_date=latest_completed_date,
                )
                raise InsufficientPortfolioEvidence(
                    report,
                    [f"{ticker} has duplicate operational daily date {bar_date.isoformat()}"],
                )
            seen_dates.add(bar_date)
            if bar_date == incomplete_session_date:
                continue
            filtered_bars.append(bar)
            filtered_times.append(bar_time)
            retained_dates.append(bar_date)
        filtered_histories[ticker] = (filtered_bars, filtered_times)

    if not retained_dates:
        report = _ineligible_operational_report(
            venue=calendar_venue,
            latest_completed_date=latest_completed_date,
        )
        raise InsufficientPortfolioEvidence(
            report,
            ["no completed operational portfolio sessions were supplied"],
        )

    requested_start = min(retained_dates)
    if requested_start > latest_completed_date:
        report = _ineligible_operational_report(
            venue=calendar_venue,
            latest_completed_date=latest_completed_date,
        )
        raise InsufficientPortfolioEvidence(
            report,
            [
                "operational portfolio history contains no bar on or before the latest completed session"
            ],
        )

    return _align_research_histories(
        filtered_histories,
        universe=sorted(expected_symbols),
        start_date=requested_start,
        end_date=latest_completed_date,
        coverage_policy=PortfolioCoveragePolicy(
            calendar_venue=calendar_venue,
            minimum_coverage_pct=Decimal("100"),
        ),
    )


def _align_research_histories(
    histories: dict[str, tuple[list[Bar], list[datetime]]],
    *,
    universe: list[str],
    start_date: date,
    end_date: date,
    coverage_policy: PortfolioCoveragePolicy,
) -> tuple[list[date], dict[str, list[Bar]], PortfolioSessionCoverageReport]:
    if set(histories) != set(universe):
        raise ValueError(
            "Portfolio history symbols must exactly match the requested universe: "
            f"expected {sorted(universe)}, got {sorted(histories)}"
        )

    calendar = calendar_for_venue(coverage_policy.calendar_venue)
    exchange_timezone = ZoneInfo(calendar.exchange_timezone)
    expected_sessions = calendar.expected_sessions(start_date, end_date)
    if not expected_sessions:
        raise ValueError("No expected exchange sessions in the requested portfolio range.")

    expected_dates = tuple(session.local_date for session in expected_sessions)
    expected_date_set = set(expected_dates)
    expected_session_ids = tuple(session.session_id for session in expected_sessions)
    date_maps: dict[str, dict[date, Bar]] = {}
    common_dates: set[date] | None = None
    symbol_reports: list[SymbolSessionCoverage] = []
    reasons: list[str] = []

    for ticker in sorted(histories):
        bars, bar_times = histories[ticker]
        validate_bar_series(bars, bar_times, label=f"portfolio bar series for {ticker}")
        per_day: dict[date, Bar] = {}
        for bar_time, bar in zip(bar_times, bars, strict=True):
            bar_date = bar_time.astimezone(exchange_timezone).date()
            if bar_date in per_day:
                raise ValueError(
                    f"portfolio bar series for {ticker}: duplicate date {bar_date.isoformat()}"
                )
            per_day[bar_date] = bar
        date_maps[ticker] = per_day
        observed_dates = set(per_day)
        covered_dates = observed_dates & expected_date_set
        missing_dates = expected_date_set - observed_dates
        extra_dates = observed_dates - expected_date_set
        raw_coverage_pct = (
            Decimal(len(covered_dates)) * Decimal("100") / Decimal(len(expected_dates))
        )
        coverage_pct = raw_coverage_pct.quantize(Decimal("0.01"))
        ordered_covered_dates = sorted(covered_dates)
        symbol_reports.append(
            SymbolSessionCoverage(
                ticker=ticker,
                expected_session_ids=expected_session_ids,
                observed_session_ids=tuple(
                    f"{calendar.venue}:{item.isoformat()}" for item in sorted(observed_dates)
                ),
                missing_session_ids=tuple(
                    f"{calendar.venue}:{item.isoformat()}" for item in sorted(missing_dates)
                ),
                extra_session_ids=tuple(
                    f"{calendar.venue}:{item.isoformat()}" for item in sorted(extra_dates)
                ),
                coverage_pct=coverage_pct,
                longest_missing_run=_longest_missing_run(expected_dates, observed_dates),
                first_valid_session_id=(
                    f"{calendar.venue}:{ordered_covered_dates[0].isoformat()}"
                    if ordered_covered_dates
                    else None
                ),
                last_valid_session_id=(
                    f"{calendar.venue}:{ordered_covered_dates[-1].isoformat()}"
                    if ordered_covered_dates
                    else None
                ),
            )
        )
        if extra_dates:
            reasons.append(f"{ticker} has extra session dates outside the expected calendar/range")
        if raw_coverage_pct < coverage_policy.minimum_coverage_pct:
            reasons.append(
                f"{ticker} coverage {coverage_pct}% is below "
                f"{coverage_policy.minimum_coverage_pct}%"
            )
        ticker_dates = covered_dates
        common_dates = ticker_dates if common_dates is None else common_dates & ticker_dates

    ordered_dates = sorted(common_dates or set())
    raw_retained_coverage_pct = (
        Decimal(len(ordered_dates)) * Decimal("100") / Decimal(len(expected_dates))
    )
    retained_coverage_pct = raw_retained_coverage_pct.quantize(Decimal("0.01"))
    if raw_retained_coverage_pct < coverage_policy.minimum_coverage_pct:
        reasons.append(
            f"retained intersection coverage {retained_coverage_pct}% is below "
            f"{coverage_policy.minimum_coverage_pct}%"
        )
    if not ordered_dates:
        reasons.append("no common expected sessions across the requested universe")
    retained_session_ids = tuple(f"{calendar.venue}:{item.isoformat()}" for item in ordered_dates)
    dropped_session_ids = tuple(
        f"{calendar.venue}:{item.isoformat()}"
        for item in expected_dates
        if item not in (common_dates or set())
    )
    report = PortfolioSessionCoverageReport(
        calendar=calendar.venue,
        exchange_timezone=calendar.exchange_timezone,
        requested_start=start_date,
        requested_end=end_date,
        minimum_coverage_pct=coverage_policy.minimum_coverage_pct,
        retained_coverage_pct=retained_coverage_pct,
        complete=not reasons and not dropped_session_ids,
        policy=coverage_policy.name,
        policy_id=coverage_policy.policy_id,
        eligible=not reasons,
        common_start=ordered_dates[0] if ordered_dates else None,
        common_end=ordered_dates[-1] if ordered_dates else None,
        expected_session_ids=expected_session_ids,
        retained_session_ids=retained_session_ids,
        dropped_session_ids=dropped_session_ids,
        symbols=tuple(symbol_reports),
    )
    if reasons:
        raise InsufficientPortfolioEvidence(report, reasons)

    aligned = {
        ticker: [date_maps[ticker][current_date] for current_date in ordered_dates]
        for ticker in sorted(date_maps)
    }
    return ordered_dates, aligned, report


def _round_ratio(value: float | None) -> Decimal | None:
    return Decimal(str(round(value, 3))) if value is not None else None


def _align_benchmark_bars(
    benchmark: tuple[list[Bar], list[datetime]],
    dates: list[date],
    *,
    ticker: str,
    calendar_venue: str,
) -> list[Bar]:
    bars, bar_times = benchmark
    validate_bar_series(bars, bar_times, label=f"benchmark bar series for {ticker}")
    exchange_timezone = ZoneInfo(calendar_for_venue(calendar_venue).exchange_timezone)
    per_day: dict[date, Bar] = {}
    for bar_time, bar in zip(bar_times, bars, strict=True):
        bar_date = bar_time.astimezone(exchange_timezone).date()
        if bar_date in per_day:
            raise ValueError(f"benchmark {ticker}: duplicate date {bar_date.isoformat()}")
        per_day[bar_date] = bar
    missing = [d for d in dates if d not in per_day]
    if missing:
        raise ValueError(
            f"benchmark {ticker} is missing {len(missing)} session(s) of the portfolio window "
            f"(first missing {missing[0].isoformat()}); a benchmark must cover every date"
        )
    return [per_day[d] for d in dates]


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
        transaction_cost_bps: Decimal | None = None,
        coverage_policy: PortfolioCoveragePolicy | None = None,
        cost_model: TransactionCostModel | None = None,
        risk_free_rate: Decimal = Decimal("0"),
        benchmark_history: tuple[list[Bar], list[datetime]] | None = None,
        benchmark_ticker: str = "BROAD_INDEX",
        primary_benchmark: str = BENCHMARK_UNIVERSE_EW,
        trade_start_date: date | None = None,
    ) -> None:
        self.strategy = strategy
        self.universe = sorted(dict.fromkeys(universe))
        self.initial_capital = initial_capital
        self.start_date = start_date
        self.end_date = end_date
        self.coverage_policy = coverage_policy or PortfolioCoveragePolicy()
        if transaction_cost_bps is not None and transaction_cost_bps < 0:
            raise ValueError("transaction_cost_bps cannot be negative")
        self.transaction_cost_rate = (
            transaction_cost_bps / Decimal("10000") if transaction_cost_bps is not None else None
        )
        if transaction_cost_bps is not None and cost_model is not None:
            raise ValueError("pass either transaction_cost_bps (legacy flat fee) or cost_model")
        self.cost_model = cost_model or TransactionCostModel()
        if risk_free_rate <= Decimal("-1"):
            raise ValueError("risk_free_rate must be greater than -1")
        self.risk_free_rate = risk_free_rate
        if primary_benchmark not in BENCHMARK_KEYS:
            raise ValueError(f"primary_benchmark must be one of {', '.join(BENCHMARK_KEYS)}")
        if primary_benchmark != BENCHMARK_UNIVERSE_EW and benchmark_history is None:
            raise ValueError(f"primary_benchmark {primary_benchmark!r} needs benchmark_history")
        self.benchmark_history = benchmark_history
        self.benchmark_ticker = benchmark_ticker.strip().upper()
        self.primary_benchmark = primary_benchmark
        # Strategy history before this date is visible (warm-up) but no trading, and no metrics,
        # happen before it.  Used by the walk-forward harness to evaluate a test window.
        self.trade_start_date = trade_start_date

    def _execute(
        self,
        *,
        side: str,
        shares: Decimal,
        quote_price: Decimal,
        ticker: str,
    ) -> PortfolioExecutionFill:
        """Apply the cost model unless a legacy flat-bps override is explicit."""
        if self.transaction_cost_rate is not None:
            return PortfolioExecutionFill(
                price=quote_price,
                shares=shares,
                fee=(shares * quote_price * self.transaction_cost_rate).quantize(Decimal("0.01")),
            )
        if shares <= 0 or quote_price <= 0:
            raise ValueError("Portfolio fill rejected: non-positive quantity or price")
        cost_fill = self.cost_model.fill(
            side=side, quantity=shares, quote_price=quote_price, ticker=ticker
        )
        return PortfolioExecutionFill(
            price=cost_fill.price, shares=shares, fee=cost_fill.fee, fx_fee=cost_fill.fx_fee
        )

    def _cost_description(self) -> dict[str, Any]:
        if self.transaction_cost_rate is not None:
            return {
                "label": "legacy_flat_bps",
                "zero_cost": self.transaction_cost_rate == 0,
                "transaction_cost_bps": float(self.transaction_cost_rate * Decimal("10000")),
                # Flat fee only: no spread, slippage or FX; never eligible for a gated verdict.
                "gated": False,
            }
        return self.cost_model.describe(tickers=self.universe)

    def _buy_and_hold_equities(
        self,
        *,
        bars_by_ticker: dict[str, list[Bar]],
        weights: dict[str, Decimal],
        first_index: int,
        length: int,
    ) -> tuple[list[Decimal], Decimal]:
        """Costed buy-and-hold from ``first_index`` open; remaining capital stays in cash.

        Positions are marked to market at each close (no liquidation cost at the end,
        exactly like the strategy's final equity).  Returns (equity per index from
        ``first_index``, total execution cost of the entry).
        """
        cash = self.initial_capital
        shares: dict[str, Decimal] = {}
        entry_cost = Decimal("0")
        for ticker, weight in weights.items():
            open_price = bars_by_ticker[ticker][first_index].open
            if open_price <= 0 or weight <= 0:
                continue
            budget = self.initial_capital * weight
            quantity = (budget / open_price).quantize(SHARE_QUANT, rounding=ROUND_DOWN)
            while quantity > 0:
                fill = self._execute(
                    side="buy", shares=quantity, quote_price=open_price, ticker=ticker
                )
                outlay = fill.price * fill.shares + fill.fee
                if outlay <= budget:
                    shares[ticker] = fill.shares
                    cash -= outlay
                    entry_cost += abs(fill.price - open_price) * fill.shares + fill.fee
                    break
                quantity = (quantity * budget / outlay).quantize(SHARE_QUANT, rounding=ROUND_DOWN)
        equities: list[Decimal] = []
        for index in range(first_index, length):
            equity = cash
            for ticker, quantity in shares.items():
                equity += quantity * bars_by_ticker[ticker][index].close
            equities.append(equity.quantize(Decimal("0.01")))
        return equities, entry_cost

    def _comparison(
        self,
        *,
        key: str,
        name: str,
        equities: list[Decimal],
        entry_cost: Decimal,
        avg_exposure_pct: Decimal,
        strategy_total_return_pct: Decimal,
        strategy_sharpe_raw: float | None,
        strategy_max_drawdown_pct: Decimal,
    ) -> BenchmarkComparison:
        rf = float(self.risk_free_rate)
        returns = returns_from_equity(equities, self.initial_capital)
        final = equities[-1]
        total_return_pct = (
            ((final / self.initial_capital) - Decimal("1")) * Decimal("100")
            if self.initial_capital > 0
            else Decimal("0")
        )
        sharpe_raw = sharpe_ratio(returns, rf)
        sharpe = _round_ratio(sharpe_raw)
        max_dd = Decimal(
            str(round(max_drawdown_pct_from_equity(equities, self.initial_capital), 6))
        )
        return BenchmarkComparison(
            key=key,
            name=name,
            total_return_pct=total_return_pct.quantize(Decimal("0.01")),
            annualised_return_pct=annualised_return_pct_from_periods(
                final, self.initial_capital, len(equities)
            ).quantize(Decimal("0.01")),
            sharpe_ratio=sharpe,
            sortino_ratio=_round_ratio(sortino_ratio(returns, rf)),
            max_drawdown_pct=max_dd.quantize(Decimal("0.01")),
            avg_exposure_pct=avg_exposure_pct.quantize(Decimal("0.01")),
            total_execution_cost=entry_cost.quantize(Decimal("0.01")),
            excess_return_pct=strategy_total_return_pct.quantize(Decimal("0.01"))
            - total_return_pct.quantize(Decimal("0.01")),
            # From the unrounded Sharpe ratios, rounded once.
            excess_sharpe_ratio=(
                _round_ratio(strategy_sharpe_raw - sharpe_raw)
                if strategy_sharpe_raw is not None and sharpe_raw is not None
                else None
            ),
            excess_max_drawdown_pct=strategy_max_drawdown_pct - max_dd.quantize(Decimal("0.01")),
            daily_returns=returns,
        )

    def run(
        self, histories: dict[str, tuple[list[Bar], list[datetime]]]
    ) -> PortfolioBacktestResult:
        dates, history, coverage_report = _align_research_histories(
            histories,
            universe=self.universe,
            start_date=self.start_date,
            end_date=self.end_date,
            coverage_policy=self.coverage_policy,
        )
        benchmark_bars = None
        if self.benchmark_history is not None:
            benchmark_bars = _align_benchmark_bars(
                self.benchmark_history,
                dates,
                ticker=self.benchmark_ticker,
                calendar_venue=self.coverage_policy.calendar_venue,
            )
        return self.run_aligned(dates, history, coverage_report, benchmark_bars=benchmark_bars)

    def run_aligned(
        self,
        dates: list[date],
        history: dict[str, list[Bar]],
        coverage_report: PortfolioSessionCoverageReport,
        *,
        benchmark_bars: list[Bar] | None = None,
    ) -> PortfolioBacktestResult:
        """Run on already-aligned, equal-length per-symbol bars (used by walk-forward slices)."""
        min_history = int(getattr(self.strategy, "min_history_bars", 0))
        if len(dates) <= min_history:
            raise ValueError(
                f"Need more history for {self.strategy.label}. "
                f"Required at least {min_history + 1} aligned bars, got {len(dates)}."
            )

        first_trade_index = max(0, min_history)
        if self.trade_start_date is not None:
            start_index = next(
                (i for i, current in enumerate(dates) if current >= self.trade_start_date), None
            )
            if start_index is None:
                raise ValueError("trade_start_date is after the last aligned session")
            first_trade_index = max(first_trade_index, start_index)
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
                observed_history = {
                    ticker: ticker_bars[:index] for ticker, ticker_bars in history.items()
                }
                target_weights = self.strategy.target_weights(
                    observed_history,
                    as_of_index=index - 1,
                )
                if sum(target_weights.values(), Decimal("0")) > Decimal("1.0001"):
                    total_weight = sum(target_weights.values(), Decimal("0"))
                    target_weights = {
                        ticker: weight / total_weight for ticker, weight in target_weights.items()
                    }
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
            total_return_pct = ((final_equity / self.initial_capital) - Decimal("1")) * Decimal(
                "100"
            )
        periods = len(equity_curve) - first_trade_index
        annualised_return_pct = annualised_return_pct_from_periods(
            final_equity, self.initial_capital, periods
        )

        # Everything below is measured from the first allocation index so warm-up days
        # spent 100% in cash do not dilute Sharpe/Sortino/drawdown/exposure.  The base of
        # the first return is the initial capital (all cash before the first trade).
        metrics_curve = equity_curve[first_trade_index:]
        metrics_equities = [point.equity for point in metrics_curve]
        daily_returns = returns_from_equity(metrics_equities, self.initial_capital)
        rf = float(self.risk_free_rate)
        sharpe_raw = sharpe_ratio(daily_returns, rf)
        sharpe = _round_ratio(sharpe_raw)
        sortino = _round_ratio(sortino_ratio(daily_returns, rf))

        max_drawdown_pct = Decimal(
            str(round(max_drawdown_pct_from_equity(metrics_equities, self.initial_capital), 6))
        )
        calmar = None
        if max_drawdown_pct > 0:
            calmar = Decimal(str(round(float(annualised_return_pct / max_drawdown_pct), 3)))

        avg_exposure_pct = _decimal_mean([point.exposure_pct for point in metrics_curve]).quantize(
            Decimal("0.01")
        )
        avg_equity = _decimal_mean(metrics_equities)
        turnover_pct = annualised_one_way_turnover_pct(
            total_turnover, avg_equity, len(metrics_equities)
        ).quantize(Decimal("0.01"))
        total_slippage_cost = sum((trade.slippage_cost for trade in trades), Decimal("0"))
        total_fee_cost = sum((trade.fee_cost for trade in trades), Decimal("0"))

        strategy_max_dd = max_drawdown_pct.quantize(Decimal("0.01"))
        comparison_args = {
            "strategy_total_return_pct": total_return_pct,
            "strategy_sharpe_raw": sharpe_raw,
            "strategy_max_drawdown_pct": strategy_max_dd,
        }
        length = len(dates)
        benchmarks: dict[str, BenchmarkComparison] = {}
        equal_weight = Decimal("1") / Decimal(str(len(history)))
        ew_equities, ew_cost = self._buy_and_hold_equities(
            bars_by_ticker=history,
            weights=dict.fromkeys(history, equal_weight),
            first_index=first_trade_index,
            length=length,
        )
        benchmarks[BENCHMARK_UNIVERSE_EW] = self._comparison(
            key=BENCHMARK_UNIVERSE_EW,
            name="Universe equal-weight buy & hold (costed)",
            equities=ew_equities,
            entry_cost=ew_cost,
            avg_exposure_pct=Decimal("100"),
            **comparison_args,
        )
        if benchmark_bars is not None:
            if len(benchmark_bars) != length:
                raise ValueError("benchmark bars must align one-to-one with portfolio dates")
            index_series = {self.benchmark_ticker: benchmark_bars}
            index_equities, index_cost = self._buy_and_hold_equities(
                bars_by_ticker=index_series,
                weights={self.benchmark_ticker: Decimal("1")},
                first_index=first_trade_index,
                length=length,
            )
            benchmarks[BENCHMARK_BROAD_INDEX] = self._comparison(
                key=BENCHMARK_BROAD_INDEX,
                name=f"Broad index {self.benchmark_ticker} buy & hold (costed)",
                equities=index_equities,
                entry_cost=index_cost,
                avg_exposure_pct=Decimal("100"),
                **comparison_args,
            )
            blend_weight = min(max(avg_exposure_pct / Decimal("100"), Decimal("0")), Decimal("1"))
            blend_equities, blend_cost = self._buy_and_hold_equities(
                bars_by_ticker=index_series,
                weights={self.benchmark_ticker: blend_weight},
                first_index=first_trade_index,
                length=length,
            )
            blend_pct = (blend_weight * 100).quantize(Decimal("0.1"))
            benchmarks[BENCHMARK_RISK_MATCHED] = self._comparison(
                key=BENCHMARK_RISK_MATCHED,
                name=f"Risk-matched blend: {blend_pct}% {self.benchmark_ticker} / rest cash (costed)",
                equities=blend_equities,
                entry_cost=blend_cost,
                avg_exposure_pct=blend_weight * Decimal("100"),
                **comparison_args,
            )
        primary = benchmarks[self.primary_benchmark]

        return PortfolioBacktestResult(
            strategy_name=self.strategy.label,
            universe=self.universe,
            start_date=dates[0],
            end_date=dates[-1],
            initial_capital=self.initial_capital.quantize(Decimal("0.01")),
            final_capital=final_equity.quantize(Decimal("0.01")),
            benchmark_name=primary.name,
            benchmark_return_pct=primary.total_return_pct,
            alpha_vs_benchmark_pct=primary.excess_return_pct,
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
            total_slippage_cost=total_slippage_cost,
            total_fee_cost=total_fee_cost,
            total_execution_cost=total_slippage_cost + total_fee_cost,
            total_fx_fee_cost=sum((trade.fx_fee_cost for trade in trades), Decimal("0")),
            cost_model=self._cost_description(),
            coverage_report=coverage_report,
            equity_curve=equity_curve,
            trades=trades,
            latest_weights=equity_curve[-1].weights if equity_curve else {},
            metrics_start_date=dates[first_trade_index],
            warmup_bars_excluded=first_trade_index,
            risk_free_rate_annual=self.risk_free_rate,
            primary_benchmark=self.primary_benchmark,
            benchmarks=benchmarks,
            daily_returns=daily_returns,
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
        target_values = {
            ticker: allocatable_equity * target_weights.get(ticker, Decimal("0"))
            for ticker in history
        }

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
            fill = self._execute(
                side="sell", shares=shares_to_sell, quote_price=open_price, ticker=ticker
            )
            notional = fill.shares * fill.price
            slippage_cost = abs(fill.price - open_price) * fill.shares
            updated_holdings[ticker] -= fill.shares
            updated_cash += notional - fill.fee
            turnover += notional
            trades.append(
                PortfolioTrade(
                    date=current_date,
                    ticker=ticker,
                    side="sell",
                    shares=fill.shares,
                    quote_price=open_price,
                    price=fill.price,
                    notional=notional.quantize(Decimal("0.01")),
                    slippage_cost=slippage_cost.quantize(Decimal("0.00000001")),
                    fee_cost=fill.fee,
                    fx_fee_cost=fill.fx_fee,
                    cost=(slippage_cost + fill.fee).quantize(Decimal("0.00000001")),
                    reason=reason,
                    target_weight=target_weights.get(ticker, Decimal("0")).quantize(
                        Decimal("0.0001")
                    ),
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
            desired_shares = (delta_value / open_price).quantize(SHARE_QUANT, rounding=ROUND_DOWN)
            shares_to_buy = min(
                desired_shares,
                (updated_cash / open_price).quantize(SHARE_QUANT, rounding=ROUND_DOWN),
            )
            if shares_to_buy <= 0:
                continue
            fill = self._execute(
                side="buy", shares=shares_to_buy, quote_price=open_price, ticker=ticker
            )
            if fill.price * fill.shares + fill.fee > updated_cash:
                spendable = updated_cash - fill.fee
                if spendable <= 0:
                    continue
                shares_to_buy = (spendable / fill.price).quantize(SHARE_QUANT, rounding=ROUND_DOWN)
                if shares_to_buy <= 0:
                    continue
                fill = self._execute(
                    side="buy", shares=shares_to_buy, quote_price=open_price, ticker=ticker
                )
                if fill.price * fill.shares + fill.fee > updated_cash:
                    continue
            notional = fill.shares * fill.price
            slippage_cost = abs(fill.price - open_price) * fill.shares
            updated_holdings[ticker] += fill.shares
            updated_cash -= notional + fill.fee
            turnover += notional
            trades.append(
                PortfolioTrade(
                    date=current_date,
                    ticker=ticker,
                    side="buy",
                    shares=fill.shares,
                    quote_price=open_price,
                    price=fill.price,
                    notional=notional.quantize(Decimal("0.01")),
                    slippage_cost=slippage_cost.quantize(Decimal("0.00000001")),
                    fee_cost=fill.fee,
                    fx_fee_cost=fill.fx_fee,
                    cost=(slippage_cost + fill.fee).quantize(Decimal("0.00000001")),
                    reason=reason,
                    target_weight=target_weights.get(ticker, Decimal("0")).quantize(
                        Decimal("0.0001")
                    ),
                )
            )

        return {
            "cash": updated_cash.quantize(Decimal("0.01")),
            "holdings": updated_holdings,
            "turnover": turnover.quantize(Decimal("0.01")),
            "trades": trades,
        }
