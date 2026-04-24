"""
Backtest and performance attribution routes.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.api.deps import get_current_user
from app.backtest.portfolio_strategies import (
    get_portfolio_backtest_strategy,
    list_portfolio_backtest_strategies,
)
from app.backtest.strategy_registry import get_backtest_strategy, list_backtest_strategies
from app.core.config import settings
from app.db.session import get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import User

router = APIRouter(prefix="/backtest", tags=["backtest"])
attribution_router = APIRouter(prefix="/attribution", tags=["attribution"])

# ── In-memory job store (sufficient for local use) ───────────────────────────
_jobs: dict[str, dict[str, Any]] = {}
_portfolio_jobs: dict[str, dict[str, Any]] = {}
BacktestStrategyType = Literal[
    "orb",
    "opening_fade",
    "vwap_reclaim",
    "closing_momentum",
    "intraday_periodicity",
]
PortfolioBacktestStrategyType = Literal[
    "buy_hold_core",
    "equal_weight_rebalance",
    "cross_sectional_momentum",
    "low_volatility_tilt",
    "trend_following_tactical",
]


class BacktestRequest(BaseModel):
    ticker: str
    strategy_type: BacktestStrategyType = "orb"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    from_date: date
    to_date: date
    initial_capital: float = 10000.0
    run_walk_forward: bool = False


class BacktestJobResponse(BaseModel):
    job_id: str
    status: str
    message: str


class BacktestStrategyInfo(BaseModel):
    type: BacktestStrategyType
    label: str
    description: str


class PortfolioBacktestRequest(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=20)
    strategy_type: PortfolioBacktestStrategyType = "buy_hold_core"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    from_date: date
    to_date: date
    initial_capital: float = 10000.0

    @field_validator("tickers")
    @classmethod
    def normalize_tickers(cls, value: list[str]) -> list[str]:
        cleaned = [ticker.strip().upper() for ticker in value if ticker.strip()]
        unique = list(dict.fromkeys(cleaned))
        if not unique:
            raise ValueError("Provide at least one ticker.")
        return unique


class PortfolioBacktestStrategyInfo(BaseModel):
    type: PortfolioBacktestStrategyType
    label: str
    description: str
    rationale: str
    rebalance_frequency: str
    min_history_bars: int


@router.get("/strategies", response_model=list[BacktestStrategyInfo])
async def get_backtest_strategies(
    _: User = Depends(get_current_user),
) -> list[BacktestStrategyInfo]:
    return [BacktestStrategyInfo(**item) for item in list_backtest_strategies()]


@router.get("/portfolio/strategies", response_model=list[PortfolioBacktestStrategyInfo])
async def get_portfolio_backtest_strategies(
    _: User = Depends(get_current_user),
) -> list[PortfolioBacktestStrategyInfo]:
    return [PortfolioBacktestStrategyInfo(**item.__dict__) for item in list_portfolio_backtest_strategies()]


@router.post("/run", response_model=BacktestJobResponse)
async def run_backtest(
    body: BacktestRequest,
    background_tasks: BackgroundTasks,
    _: User = Depends(get_current_user),
) -> BacktestJobResponse:
    """
    Kick off an asynchronous backtest.
    Returns a job_id to poll for results.
    """
    if not settings.POLYGON_API_KEY:
        raise HTTPException(
            status_code=400,
            detail=(
                "POLYGON_API_KEY required for backtesting. "
                "Get a free key at https://polygon.io — "
                "Note: Alpaca is used for live signals, Polygon for historical backtesting. "
                "Both can be set at the same time."
            )
        )
    if body.from_date >= body.to_date:
        raise HTTPException(status_code=422, detail="from_date must be before to_date")

    import uuid
    job_id = str(uuid.uuid4())[:12]
    _jobs[job_id] = {"status": "running", "created_at": datetime.now(UTC).isoformat()}

    background_tasks.add_task(_run_backtest_job, job_id, body)

    return BacktestJobResponse(
        job_id=job_id,
        status="running",
        message=f"Backtest started for {body.ticker} ({body.from_date} to {body.to_date}). "
                f"Poll GET /v1/backtest/result/{job_id}",
    )


@router.post("/portfolio/run", response_model=BacktestJobResponse)
async def run_portfolio_backtest(
    body: PortfolioBacktestRequest,
    background_tasks: BackgroundTasks,
    _: User = Depends(get_current_user),
) -> BacktestJobResponse:
    if not settings.POLYGON_API_KEY:
        raise HTTPException(
            status_code=400,
            detail=(
                "POLYGON_API_KEY required for portfolio backtesting. "
                "The current portfolio research pack uses daily Polygon bars."
            ),
        )
    if body.from_date >= body.to_date:
        raise HTTPException(status_code=422, detail="from_date must be before to_date")

    import uuid

    job_id = f"pf-{str(uuid.uuid4())[:10]}"
    _portfolio_jobs[job_id] = {"status": "running", "created_at": datetime.now(UTC).isoformat()}
    background_tasks.add_task(_run_portfolio_backtest_job, job_id, body)

    return BacktestJobResponse(
        job_id=job_id,
        status="running",
        message=(
            f"Portfolio backtest started for {', '.join(body.tickers)}. "
            f"Poll GET /v1/backtest/portfolio/result/{job_id}"
        ),
    )


@router.get("/result/{job_id}")
async def get_backtest_result(
    job_id: str,
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/portfolio/result/{job_id}")
async def get_portfolio_backtest_result(
    job_id: str,
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    job = _portfolio_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/jobs")
async def list_backtest_jobs(_: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    return [
        {"job_id": jid, **{k: v for k, v in data.items() if k != "result"}}
        for jid, data in _jobs.items()
    ]


@router.get("/portfolio/jobs")
async def list_portfolio_backtest_jobs(_: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    return [
        {"job_id": jid, **{k: v for k, v in data.items() if k != "result"}}
        for jid, data in _portfolio_jobs.items()
    ]


async def _run_backtest_job(job_id: str, body: BacktestRequest) -> None:
    """Background task: run backtest and store result."""
    try:
        from app.backtest.data_fetcher import BacktestDataFetcher
        from app.backtest.engine import (
            Backtester,
            WalkForwardValidator,
            summarise_walk_forward_results,
        )

        fetcher = BacktestDataFetcher(settings.POLYGON_API_KEY)
        bars, bar_times = await fetcher.fetch_bars(
            ticker=body.ticker,
            from_date=body.from_date,
            to_date=body.to_date,
        )

        if len(bars) < 50:
            _jobs[job_id] = {
                "status": "error",
                "error": f"Only {len(bars)} bars fetched — need at least 50",
            }
            return

        strategy_config = get_backtest_strategy(body.strategy_type)
        strategy_class = strategy_config["strategy_class"]
        param_grid = strategy_config["param_grid"]

        strategy = strategy_class(body.strategy_params or None)
        bt = Backtester(
            strategy=strategy,
            ticker=body.ticker,
            initial_capital=Decimal(str(body.initial_capital)),
            start_date=body.from_date,
            end_date=body.to_date,
        )
        result = bt.run(bars, bar_times)

        # Walk-forward if requested
        wf_results = None
        wf_summary = None
        if body.run_walk_forward and len(bars) > 3000:
            validator = WalkForwardValidator(
                strategy_class=strategy_class,
                ticker=body.ticker,
                initial_capital=Decimal(str(body.initial_capital)),
            )
            wf_results = validator.run(bars, bar_times, param_grid)
            wf_summary = summarise_walk_forward_results(wf_results)
        elif body.run_walk_forward:
            wf_summary = {
                "windows": 0,
                "verdict": "insufficient_data",
                "message": f"Walk-forward requires more history; only {len(bars)} bars were available.",
            }

        _jobs[job_id] = {
            "status": "complete",
            "ticker": body.ticker,
            "strategy_type": body.strategy_type,
            "bars_used": len(bars),
            "result": _serialize_backtest_result(
                result=result,
                strategy_type=body.strategy_type,
                strategy_label=str(strategy_config["label"]),
            ),
            "walk_forward": wf_results,
            "walk_forward_summary": wf_summary,
            "interpretation": _interpret_results(result),
        }

    except Exception as exc:
        import traceback
        _jobs[job_id] = {
            "status": "error",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


async def _run_portfolio_backtest_job(job_id: str, body: PortfolioBacktestRequest) -> None:
    try:
        from app.backtest.data_fetcher import BacktestDataFetcher
        from app.backtest.portfolio_engine import PortfolioBacktester

        fetcher = BacktestDataFetcher(settings.POLYGON_API_KEY)
        histories: dict[str, tuple[list[Any], list[datetime]]] = {}
        for ticker in body.tickers:
            histories[ticker] = await fetcher.fetch_bars(
                ticker=ticker,
                from_date=body.from_date,
                to_date=body.to_date,
                multiplier=1,
                timespan="day",
            )

        strategy_config = get_portfolio_backtest_strategy(body.strategy_type)
        strategy_class = strategy_config["strategy_class"]
        strategy = strategy_class(body.strategy_params or None)

        backtester = PortfolioBacktester(
            strategy=strategy,
            universe=body.tickers,
            initial_capital=Decimal(str(body.initial_capital)),
            start_date=body.from_date,
            end_date=body.to_date,
        )
        result = backtester.run(histories)

        _portfolio_jobs[job_id] = {
            "status": "complete",
            "tickers": body.tickers,
            "strategy_type": body.strategy_type,
            "bars_used": min(len(history[0]) for history in histories.values()),
            "result": _serialize_portfolio_backtest_result(
                result=result,
                strategy_type=body.strategy_type,
                strategy_label=str(strategy_config["label"]),
                rationale=str(strategy_config["rationale"]),
            ),
            "interpretation": _interpret_portfolio_results(result),
        }
    except Exception as exc:
        import traceback

        _portfolio_jobs[job_id] = {
            "status": "error",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def _serialize_backtest_trade(trade: Any) -> dict[str, Any]:
    return {
        "id": trade.id,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "entry_price": float(trade.entry_price),
        "exit_price": float(trade.exit_price),
        "quantity": float(trade.quantity),
        "pnl": float(trade.pnl),
        "pnl_pct": float(trade.pnl_pct),
        "exit_reason": trade.exit_reason,
        "holding_bars": trade.holding_bars,
        "slippage": float(trade.slippage_cost),
        "mfe": float(trade.mfe),
        "mae": float(trade.mae),
    }


def _serialize_backtest_result(
    *,
    result: Any,
    strategy_type: BacktestStrategyType,
    strategy_label: str,
) -> dict[str, Any]:
    return {
        "strategy": strategy_label,
        "strategy_name": result.strategy_name,
        "strategy_type": strategy_type,
        "ticker": result.ticker,
        "from": result.start_date.isoformat(),
        "to": result.end_date.isoformat(),
        "initial_capital": float(result.initial_capital),
        "final_capital": float(result.final_capital),
        "gross_pnl": float(result.gross_pnl),
        "net_pnl": float(result.net_pnl),
        "gross_return_pct": float(result.gross_return_pct),
        "total_return_pct": float(result.total_return_pct),
        "annualised_return_pct": float(result.annualised_return_pct),
        "benchmark_return_pct": float(result.benchmark_return_pct),
        "alpha_vs_benchmark_pct": float(result.alpha_vs_benchmark_pct),
        "sharpe_ratio": float(result.sharpe_ratio) if result.sharpe_ratio is not None else None,
        "sortino_ratio": float(result.sortino_ratio) if result.sortino_ratio is not None else None,
        "calmar_ratio": float(result.calmar_ratio) if result.calmar_ratio is not None else None,
        "max_drawdown_pct": float(result.max_drawdown_pct),
        "max_drawdown_duration_days": result.max_drawdown_duration_days,
        "total_trades": result.total_trades,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "win_rate": float(result.win_rate),
        "win_rate_pct": float(result.win_rate * 100),
        "profit_factor": float(result.profit_factor),
        "avg_win": float(result.avg_win),
        "avg_loss": float(result.avg_loss),
        "expectancy": float(result.expectancy),
        "expectancy_pct": float(result.expectancy_pct),
        "avg_rr_achieved": float(result.avg_rr_achieved),
        "consecutive_losses_max": result.consecutive_losses_max,
        "total_slippage_cost": float(result.total_slippage_cost),
        "total_commission_cost": float(result.total_commission_cost),
        "avg_holding_bars": float(result.avg_holding_bars),
        "turnover_pct": float(result.turnover_pct),
        "exposure_pct": float(result.exposure_pct),
        "avg_mfe": float(result.avg_mfe),
        "avg_mae": float(result.avg_mae),
        "monte_carlo": result.monte_carlo,
        "equity_curve": result.equity_curve[-200:],
        "trades": [_serialize_backtest_trade(trade) for trade in result.trades],
    }


def _serialize_portfolio_backtest_result(
    *,
    result: Any,
    strategy_type: PortfolioBacktestStrategyType,
    strategy_label: str,
    rationale: str,
) -> dict[str, Any]:
    return {
        "strategy": strategy_label,
        "strategy_name": result.strategy_name,
        "strategy_type": strategy_type,
        "universe": result.universe,
        "from": result.start_date.isoformat(),
        "to": result.end_date.isoformat(),
        "initial_capital": float(result.initial_capital),
        "final_capital": float(result.final_capital),
        "total_return_pct": float(result.total_return_pct),
        "annualised_return_pct": float(result.annualised_return_pct),
        "benchmark_name": result.benchmark_name,
        "benchmark_return_pct": float(result.benchmark_return_pct),
        "alpha_vs_benchmark_pct": float(result.alpha_vs_benchmark_pct),
        "sharpe_ratio": float(result.sharpe_ratio) if result.sharpe_ratio is not None else None,
        "sortino_ratio": float(result.sortino_ratio) if result.sortino_ratio is not None else None,
        "calmar_ratio": float(result.calmar_ratio) if result.calmar_ratio is not None else None,
        "max_drawdown_pct": float(result.max_drawdown_pct),
        "total_trades": result.total_trades,
        "rebalance_count": result.rebalance_count,
        "turnover_pct": float(result.turnover_pct),
        "avg_exposure_pct": float(result.avg_exposure_pct),
        "latest_weights": {ticker: float(weight) for ticker, weight in result.latest_weights.items()},
        "equity_curve": [
            {
                "date": point.date.isoformat(),
                "equity": float(point.equity),
                "cash": float(point.cash),
                "exposure_pct": float(point.exposure_pct),
                "drawdown_pct": float(point.drawdown_pct),
                "weights": {ticker: float(weight) for ticker, weight in point.weights.items()},
            }
            for point in result.equity_curve[-260:]
        ],
        "trades": [
            {
                "date": trade.date.isoformat(),
                "ticker": trade.ticker,
                "side": trade.side,
                "shares": float(trade.shares),
                "price": float(trade.price),
                "notional": float(trade.notional),
                "cost": float(trade.cost),
                "reason": trade.reason,
                "target_weight": float(trade.target_weight),
            }
            for trade in result.trades[-250:]
        ],
        "rationale": rationale,
    }


def _interpret_results(result: Any) -> dict[str, Any]:
    """Plain-English interpretation of backtest results."""
    verdict = "insufficient_data" if result.total_trades < 10 else ""

    if result.total_trades >= 10:
        sharpe = float(result.sharpe_ratio or 0)
        if sharpe < 0:
            verdict = "losing"
        elif sharpe < 0.5:
            verdict = "marginal"
        elif sharpe < 1.0:
            verdict = "promising"
        elif sharpe >= 1.0 and float(result.profit_factor) >= 1.5:
            verdict = "strong"
        else:
            verdict = "mixed"

    warnings = []
    if result.total_trades < 30:
        warnings.append("Too few trades for statistical significance (need 30+)")
    if float(result.max_drawdown_pct) > 20:
        warnings.append(f"Max drawdown {float(result.max_drawdown_pct):.1f}% is high — review stops")
    if result.consecutive_losses_max >= 5:
        warnings.append(f"Max {result.consecutive_losses_max} consecutive losses — ensure daily loss limit covers this")
    friction_cost = float(result.total_slippage_cost + result.total_commission_cost)
    gross_pnl = max(float(result.gross_pnl), 0.0)
    if gross_pnl > 0 and friction_cost > gross_pnl * 0.3:
        warnings.append("Execution friction is >30% of gross profit — tighten entry quality or routing")

    return {
        "verdict": verdict,
        "summary": {
            "losing": "Strategy is unprofitable over this period. Do not trade live.",
            "marginal": "Marginal edge. Paper trade for 60+ days before going live.",
            "promising": "Promising results. Run walk-forward validation before live trading.",
            "strong": "Strong results. Validate with walk-forward and consider paper trading.",
            "mixed": "Mixed results. Review trade-level data to understand what's driving performance.",
            "insufficient_data": "Not enough trades to draw conclusions. Extend the backtest period.",
        }.get(verdict, ""),
        "warnings": warnings,
    }


def _interpret_portfolio_results(result: Any) -> dict[str, Any]:
    verdict = "mixed"
    sharpe = float(result.sharpe_ratio or 0)
    if float(result.total_return_pct) <= 0:
        verdict = "losing"
    elif sharpe >= 1.0 and float(result.max_drawdown_pct) <= 20:
        verdict = "strong"
    elif sharpe >= 0.6 and float(result.max_drawdown_pct) <= 25:
        verdict = "promising"
    elif sharpe < 0.25:
        verdict = "marginal"

    warnings: list[str] = []
    if result.rebalance_count < 3:
        warnings.append("Very few rebalance decisions were observed — extend the test period.")
    if float(result.max_drawdown_pct) > 25:
        warnings.append(f"Portfolio drawdown reached {float(result.max_drawdown_pct):.1f}% — consider a stronger cash filter.")
    if float(result.turnover_pct) > 250:
        warnings.append("Turnover is high for a retail account — review rebalancing frequency and FX drag.")
    if float(result.avg_exposure_pct) < 40:
        warnings.append("Average exposure stayed low — performance may be driven more by cash timing than asset selection.")

    return {
        "verdict": verdict,
        "summary": {
            "losing": "This portfolio rule set failed to add value over the tested window. Keep it in research only.",
            "marginal": "The edge is weak. Tighten selection rules or broaden the history before treating it as deployable.",
            "promising": "The portfolio logic is promising. Validate it on additional universes before paper trading.",
            "strong": "This is a strong research result for a low-friction retail implementation. Still promote through demo first.",
            "mixed": "Results are mixed. Review turnover, exposure, and benchmark-relative performance before promoting it.",
        }.get(verdict, ""),
        "warnings": warnings,
    }


# ── Attribution routes ────────────────────────────────────────────────────────

@attribution_router.get("/full")
async def get_full_attribution(
    days: int = Query(30, ge=7, le=365),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.services.performance_attribution import PerformanceAttributor
    attr = PerformanceAttributor(db)
    return await attr.full_report(days=days)


@attribution_router.get("/slippage")
async def get_slippage_report(
    days: int = Query(30, ge=7, le=365),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    from app.services.performance_attribution import PerformanceAttributor
    attr = PerformanceAttributor(db)
    records = await attr.slippage_report(days=days)
    return [
        {
            "ticker": r.ticker,
            "side": r.side,
            "expected": float(r.expected_price),
            "actual": float(r.actual_price),
            "slippage_pct": float(r.slippage_pct),
            "slippage_dollars": float(r.slippage_dollars),
            "timestamp": r.timestamp.isoformat(),
        }
        for r in records
    ]


@attribution_router.get("/symbols")
async def get_symbol_attribution(
    days: int = Query(90, ge=7, le=365),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    from app.services.performance_attribution import PerformanceAttributor
    attr = PerformanceAttributor(db)
    symbols = await attr.symbol_attribution(days=days)
    return [
        {
            "ticker": a.ticker,
            "total_trades": a.total_trades,
            "win_rate_pct": round(a.win_rate * 100, 1),
            "total_pnl": a.total_pnl,
            "avg_pnl": a.avg_pnl,
            "contribution_pct": a.contribution_pct,
        }
        for a in symbols
    ]
