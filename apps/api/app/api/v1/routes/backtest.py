"""
Backtest and performance attribution routes.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Self, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.deps import get_current_user
from app.backtest.configuration import resolve_backtest_configuration
from app.backtest.gate_floors import (
    MIN_DEFLATED_SHARPE_PROBABILITY,
    MIN_REBALANCES_PER_WINDOW,
    MIN_VALID_WINDOWS,
    benchmark_sanity_text,
    config_floor_violations,
    cost_gate_refusal,
)
from app.backtest.portfolio_costs import TransactionCostModel
from app.backtest.portfolio_engine import (
    BENCHMARK_BROAD_INDEX,
    BENCHMARK_UNIVERSE_EW,
    InsufficientPortfolioEvidence,
)
from app.backtest.portfolio_metrics import TRADING_DAYS_PER_YEAR
from app.backtest.portfolio_strategies import (
    get_portfolio_backtest_strategy,
    list_portfolio_backtest_strategies,
)
from app.backtest.portfolio_walk_forward import (
    HOLDOUT_REUSE_WARNING,
    WalkForwardConfig,
    run_portfolio_walk_forward,
    serialize_walk_forward,
)
from app.backtest.strategy_registry import get_backtest_strategy, list_backtest_strategies
from app.core.config import settings
from app.db.session import get_db
from app.market_data.exchange_calendar import calendar_for_venue
from app.services.strategy_presets import get_strategy_preset

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.requests import Request
    from starlette.responses import Response

    from app.db.models import User


class BacktestValidationRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                if not self.path.endswith("/backtest/run"):
                    raise
                # Do not echo request input: nonfinite JSON numbers cannot themselves
                # be serialized in FastAPI's default validation-error response.
                detail = [
                    {key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()
                ]
                raise HTTPException(status_code=422, detail=detail) from exc

        return handler


router = APIRouter(prefix="/backtest", tags=["backtest"], route_class=BacktestValidationRoute)
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
    model_config = ConfigDict(extra="forbid")

    ticker: str
    strategy_type: BacktestStrategyType = "orb"
    preset_key: BacktestStrategyType | None = None
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    from_date: date
    to_date: date
    initial_capital: float = 10000.0
    run_walk_forward: bool = False

    def configuration(self) -> dict[str, Any]:
        return resolve_backtest_configuration(
            self.strategy_type,
            self.strategy_params,
            self.preset_key,
            parameters_supplied="strategy_params" in self.model_fields_set,
        )

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        self.configuration()
        return self


class BacktestJobResponse(BaseModel):
    job_id: str
    status: str
    message: str
    configuration: dict[str, Any] | None = None


class BacktestStrategyInfo(BaseModel):
    type: BacktestStrategyType
    label: str
    description: str
    preset_key: BacktestStrategyType
    strategy_params: dict[str, Any]


PortfolioBenchmarkKey = Literal[
    "universe_equal_weight_costed", "broad_index", "exposure_matched_blend"
]
# Accepted on requests for compatibility; the benchmark matches exposure, not risk.
_BENCHMARK_ALIASES = {"risk_matched_blend": "exposure_matched_blend"}

SURVIVORSHIP_DISCLOSURE = (
    "Survivorship bias: the default scanner universe is hand-picked winners, there are no "
    "point-in-time index constituents (delisted and failed names are absent), and prices are "
    "provider-adjusted with unqualified corporate-action treatment. Results on such a universe "
    "are upper bounds, not estimates; a positive label here does not survive that."
)

# Always attached to a portfolio result so no reader has to find these in the docs.
PORTFOLIO_DISCLOSURES: dict[str, str] = {
    "currency": (
        "FX moves between the account currency and instrument currencies are NOT modelled. "
        "Returns are computed in each instrument's quote currency (typically USD) and presented "
        "as if they were account-currency returns; only the FX conversion fee is charged."
    ),
    "annualisation": (
        "252 trading sessions per year for annualised return, Sharpe, Sortino and turnover."
    ),
    "turnover": (
        "turnover_pct is annualised one-way turnover: half of (buys + sells) over average equity, "
        "scaled to a 252-session year."
    ),
    "cash_buffer": (
        "Rebalances allocate 99.8% of equity (the rest is a cash buffer); every buy-and-hold "
        "benchmark uses the same 99.8%, so the buffer does not tilt the comparison."
    ),
    "survivorship": SURVIVORSHIP_DISCLOSURE,
    "benchmark_exits": "Benchmarks are bought on day one and marked to market with no exit cost.",
}


class PortfolioCostRequest(BaseModel):
    """Transaction costs for a portfolio backtest (per side, in bps unless stated).

    ``spread_bps`` is the full quoted spread (half is paid per side).  ``fx_fee_rate`` is a
    fraction of notional charged per side on instruments whose currency differs from
    ``account_currency``; an instrument with no known currency is treated as foreign and pays it.
    ``stress_multiplier`` scales every cost.  ``zero_cost`` must be set explicitly to run
    frictionless; the result is then labelled ``zero_cost_explicit``.
    """

    model_config = ConfigDict(extra="forbid")

    spread_bps: float = Field(default=10.0, ge=0, le=500)
    slippage_bps: float = Field(default=5.0, ge=0, le=500)
    commission_bps: float = Field(default=2.0, ge=0, le=500)
    fx_fee_rate: float = Field(default=0.0015, ge=0, le=0.05)
    account_currency: str = Field(default="GBP", min_length=3, max_length=3)
    instrument_currencies: dict[str, str] = Field(default_factory=dict)
    default_instrument_currency: str | None = Field(default=None, min_length=3, max_length=3)
    spread_bps_by_ticker: dict[str, float] = Field(default_factory=dict)
    stress_multiplier: Literal[1, 2, 3] = 1
    zero_cost: bool = False

    def to_model(self) -> TransactionCostModel:
        if self.zero_cost:
            return TransactionCostModel.explicit_zero(account_currency=self.account_currency)
        return TransactionCostModel(
            spread_bps=Decimal(str(self.spread_bps)),
            slippage_bps=Decimal(str(self.slippage_bps)),
            commission_bps=Decimal(str(self.commission_bps)),
            fx_fee_rate=Decimal(str(self.fx_fee_rate)),
            account_currency=self.account_currency,
            instrument_currencies=dict(self.instrument_currencies),
            default_instrument_currency=self.default_instrument_currency,
            spread_bps_by_ticker={k: Decimal(str(v)) for k, v in self.spread_bps_by_ticker.items()},
            stress_multiplier=Decimal(self.stress_multiplier),
        )

    @model_validator(mode="after")
    def validate_model(self) -> Self:
        self.to_model()  # surfaces e.g. "all costs are zero" as a 422 instead of a failed job
        return self


class PortfolioWalkForwardRequest(BaseModel):
    """Optional expanding-window walk-forward validation (off unless supplied).

    ``param_grid`` entries are merged over ``strategy_params``; the grid size (plus
    ``extra_trials`` for ideas already tried elsewhere) is recorded as the number of trials for
    the deflated Sharpe ratio.  The last ``holdout_bars`` sessions are never used for selection.
    """

    model_config = ConfigDict(extra="forbid")

    param_grid: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    initial_train_bars: int = Field(ge=2)
    test_bars: int = Field(ge=2)
    holdout_bars: int = Field(ge=2)
    # Settings below the server floors in ``app.backtest.gate_floors`` are accepted (research
    # runs) but can never pass the gate; the floors cannot be lowered by the request.
    min_rebalances_per_window: int = Field(default=MIN_REBALANCES_PER_WINDOW, ge=1)
    min_valid_windows: int = Field(default=MIN_VALID_WINDOWS, ge=1)
    extra_trials: int = Field(default=0, ge=0)
    # Mandatory: None means the server default 0.95; lower values are enforced at 0.95 anyway.
    min_deflated_sharpe_probability: float | None = Field(
        default=MIN_DEFLATED_SHARPE_PROBABILITY, ge=0, le=1
    )

    def to_config(self) -> WalkForwardConfig:
        return WalkForwardConfig(
            initial_train_bars=self.initial_train_bars,
            test_bars=self.test_bars,
            holdout_bars=self.holdout_bars,
            min_rebalances_per_window=self.min_rebalances_per_window,
            min_valid_windows=self.min_valid_windows,
            extra_trials=self.extra_trials,
            min_deflated_sharpe_probability=self.min_deflated_sharpe_probability,
        )


class PortfolioBacktestRequest(BaseModel):
    # A misspelt or removed field (e.g. a stale ``risk_matched_blend`` option) must be a 422, not
    # silently ignored: an ignored guardrail looks like a run that honoured it.
    model_config = ConfigDict(extra="forbid")

    tickers: list[str] = Field(min_length=1, max_length=20)
    strategy_type: PortfolioBacktestStrategyType = "buy_hold_core"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    from_date: date
    to_date: date
    initial_capital: float = 10000.0
    cost_model: PortfolioCostRequest = Field(default_factory=PortfolioCostRequest)
    # Annual risk-free rate for Sharpe/Sortino.  Default 0.0 (documented, reported in the result).
    risk_free_rate_annual: float = Field(default=0.0, ge=-0.5, le=1.0)
    # A price series already in/fetchable as daily bars (e.g. SPY or a global ETF), used for the
    # 'broad_index' and 'exposure_matched_blend' benchmarks, net of the same costs.
    benchmark_ticker: str | None = Field(default=None, min_length=1, max_length=12)
    primary_benchmark: PortfolioBenchmarkKey = BENCHMARK_UNIVERSE_EW
    walk_forward: PortfolioWalkForwardRequest | None = None
    # Caller-supplied label echoed in the result so repeated runs against one holdout can be found.
    run_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("primary_benchmark", mode="before")
    @classmethod
    def map_benchmark_alias(cls, value: Any) -> Any:
        return _BENCHMARK_ALIASES.get(value, value) if isinstance(value, str) else value

    @field_validator("tickers")
    @classmethod
    def normalize_tickers(cls, value: list[str]) -> list[str]:
        cleaned = [ticker.strip().upper() for ticker in value if ticker.strip()]
        unique = list(dict.fromkeys(cleaned))
        if not unique:
            raise ValueError("Provide at least one ticker.")
        return unique

    @field_validator("benchmark_ticker")
    @classmethod
    def normalize_benchmark(cls, value: str | None) -> str | None:
        return value.strip().upper() if value and value.strip() else None

    @model_validator(mode="after")
    def validate_benchmark(self) -> Self:
        if self.primary_benchmark != BENCHMARK_UNIVERSE_EW and self.benchmark_ticker is None:
            raise ValueError(f"primary_benchmark {self.primary_benchmark!r} needs benchmark_ticker")
        return self


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
    catalog = []
    for item in list_backtest_strategies():
        key = cast("BacktestStrategyType", item["type"])
        configuration = resolve_backtest_configuration(key, {}, key, parameters_supplied=False)
        catalog.append(
            BacktestStrategyInfo(
                type=key,
                label=configuration["label"],
                description=get_strategy_preset(key).description,
                preset_key=key,
                strategy_params=configuration["strategy_params"],
            )
        )
    return catalog


@router.get("/portfolio/strategies", response_model=list[PortfolioBacktestStrategyInfo])
async def get_portfolio_backtest_strategies(
    _: User = Depends(get_current_user),
) -> list[PortfolioBacktestStrategyInfo]:
    return [
        PortfolioBacktestStrategyInfo(**item.__dict__)
        for item in list_portfolio_backtest_strategies()
    ]


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
            ),
        )
    if body.from_date >= body.to_date:
        raise HTTPException(status_code=422, detail="from_date must be before to_date")

    import uuid

    job_id = str(uuid.uuid4())[:12]
    configuration = body.configuration()
    _jobs[job_id] = {
        "status": "running",
        "created_at": datetime.now(UTC).isoformat(),
        "configuration": deepcopy(configuration),
    }

    background_tasks.add_task(
        _run_backtest_job, job_id, body.model_copy(deep=True), deepcopy(configuration)
    )

    return BacktestJobResponse(
        job_id=job_id,
        status="running",
        configuration=deepcopy(configuration),
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


async def _run_backtest_job(
    job_id: str, body: BacktestRequest, configuration: dict[str, Any] | None = None
) -> None:
    """Background task: run backtest and store result."""
    configuration = deepcopy(configuration) if configuration is not None else body.configuration()
    strategy_config = get_backtest_strategy(body.strategy_type)
    param_grid = deepcopy(strategy_config["param_grid"])
    metadata = {
        "configuration": deepcopy(configuration),
        "walk_forward_configuration": {
            "source": "registry_parameter_grid",
            "strategy_type": body.strategy_type,
            "parameter_grid": deepcopy(param_grid),
        }
        if body.run_walk_forward
        else None,
    }
    try:
        from app.backtest.data_fetcher import BacktestDataFetcher
        from app.backtest.engine import (
            Backtester,
            WalkForwardValidator,
            summarise_walk_forward_results,
        )

        fetcher = BacktestDataFetcher(settings.POLYGON_API_KEY)
        calendar = calendar_for_venue("XNYS")
        requested_sessions = calendar.expected_sessions(body.from_date, body.to_date)
        if not requested_sessions:
            raise ValueError("Requested range contains no XNYS regular sessions")
        warmup_from = calendar.previous_session(requested_sessions[0]).local_date
        bars, bar_times = await fetcher.fetch_bars(
            ticker=body.ticker,
            from_date=warmup_from,
            to_date=body.to_date,
        )
        dataset_manifest = fetcher.manifest_for(body.ticker)

        requested_bar_count = sum(
            1
            for bar_time in bar_times
            if (session := calendar.session_for_timestamp(bar_time)) is not None
            and body.from_date <= session.local_date <= body.to_date
        )
        if requested_bar_count < 50:
            _jobs[job_id] = {
                **metadata,
                "status": "error",
                "error": (
                    f"Only {requested_bar_count} requested-period regular bars fetched"
                    " — need at least 50"
                ),
            }
            return

        strategy_class = strategy_config["strategy_class"]

        strategy = strategy_class(deepcopy(configuration["strategy_params"]))
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
        if body.run_walk_forward and requested_bar_count >= 3000:
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
                "verdict": "insufficient_evidence",
                "message": (
                    "Walk-forward requires more history; only "
                    f"{requested_bar_count} requested-period regular bars were available."
                ),
            }

        _jobs[job_id] = {
            **metadata,
            "status": "complete",
            "ticker": body.ticker,
            "strategy_type": body.strategy_type,
            "bars_used": requested_bar_count,
            "datasets": [dataset_manifest],
            "result": _serialize_backtest_result(
                result=result,
                strategy_type=body.strategy_type,
                strategy_label=configuration["label"],
            ),
            "walk_forward": wf_results,
            "walk_forward_summary": wf_summary,
            "interpretation": _interpret_results(result),
        }

    except Exception as exc:
        import traceback

        _jobs[job_id] = {
            **metadata,
            "status": "error",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


async def _run_portfolio_backtest_job(job_id: str, body: PortfolioBacktestRequest) -> None:
    dataset_manifests: list[dict[str, Any]] = []
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
                membership_source="research_only_survivor_biased",
                universe=tuple(body.tickers),
            )
            dataset_manifests.append(fetcher.manifest_for(ticker))

        benchmark_history = None
        if body.benchmark_ticker is not None:
            benchmark_history = histories.get(body.benchmark_ticker)
            if benchmark_history is None:
                benchmark_history = await fetcher.fetch_bars(
                    ticker=body.benchmark_ticker,
                    from_date=body.from_date,
                    to_date=body.to_date,
                    multiplier=1,
                    timespan="day",
                    membership_source="benchmark_series",
                    universe=(body.benchmark_ticker,),
                )
                dataset_manifests.append(fetcher.manifest_for(body.benchmark_ticker))

        strategy_config = get_portfolio_backtest_strategy(body.strategy_type)
        strategy_class = strategy_config["strategy_class"]
        strategy = strategy_class(body.strategy_params or None)

        backtester = PortfolioBacktester(
            strategy=strategy,
            universe=body.tickers,
            initial_capital=Decimal(str(body.initial_capital)),
            start_date=body.from_date,
            end_date=body.to_date,
            cost_model=body.cost_model.to_model(),
            risk_free_rate=Decimal(str(body.risk_free_rate_annual)),
            benchmark_history=benchmark_history,
            benchmark_ticker=body.benchmark_ticker or "BROAD_INDEX",
            primary_benchmark=body.primary_benchmark,
        )
        result = backtester.run(histories)

        walk_forward = None
        if body.walk_forward is not None:
            import asyncio

            walk_forward = await asyncio.to_thread(
                run_portfolio_walk_forward,
                strategy_class=strategy_class,
                base_params=body.strategy_params or None,
                param_grid=body.walk_forward.param_grid,
                histories=histories,
                universe=body.tickers,
                start_date=body.from_date,
                end_date=body.to_date,
                config=body.walk_forward.to_config(),
                initial_capital=Decimal(str(body.initial_capital)),
                cost_model=body.cost_model.to_model(),
                risk_free_rate=Decimal(str(body.risk_free_rate_annual)),
                benchmark_history=benchmark_history,
                benchmark_ticker=body.benchmark_ticker or "BROAD_INDEX",
            )

        _portfolio_jobs[job_id] = {
            "status": "complete",
            "tickers": body.tickers,
            "strategy_type": body.strategy_type,
            "bars_used": len(result.coverage_report.retained_session_ids),
            "datasets": dataset_manifests,
            "holdout_reuse_warning": HOLDOUT_REUSE_WARNING,
            "holdout_reuse": (
                _holdout_reuse_record(
                    job_id,
                    body,
                    dataset_manifests,
                    walk_forward.holdout.test_end
                    if walk_forward is not None and walk_forward.holdout
                    else None,
                )
                if body.walk_forward is not None
                else None
            ),
            "result": _serialize_portfolio_backtest_result(
                result=result,
                strategy_type=body.strategy_type,
                strategy_label=str(strategy_config["label"]),
                rationale=str(strategy_config["rationale"]),
            ),
            "walk_forward": serialize_walk_forward(walk_forward) if walk_forward else None,
            "interpretation": _interpret_portfolio_results(result, walk_forward),
        }
    except InsufficientPortfolioEvidence as exc:
        _portfolio_jobs[job_id] = {
            "status": "complete",
            "tickers": body.tickers,
            "strategy_type": body.strategy_type,
            "bars_used": len(exc.report.retained_session_ids),
            "datasets": dataset_manifests,
            "verdict": exc.verdict,
            "evidence_reasons": list(exc.reasons),
            "coverage": _serialize_portfolio_coverage(exc.report),
            "result": None,
        }
    except Exception as exc:
        import traceback

        _portfolio_jobs[job_id] = {
            "status": "error",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def _fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _holdout_reuse_record(
    job_id: str,
    body: PortfolioBacktestRequest,
    dataset_manifests: list[dict[str, Any]],
    holdout_end: Any = None,
) -> dict[str, Any]:
    """Identify this run so repeated use of one holdout can be spotted (advisory only).

    ``family_fingerprint`` covers the strategy type and the holdout END date (the last aligned
    session when known, else ``to_date``).  It deliberately ignores the universe and start date,
    so changing either cannot hide a second look at the same holdout; ``grid_fingerprint``
    covers the initial parameter grid.  ``data_fingerprint`` covers the universe, dates,
    benchmark and dataset manifests; ``run_fingerprint`` adds the strategy, grid, costs and
    walk-forward settings.  There is no durable store: runs are compared only against jobs still
    held in this process's memory, so this is a prompt to keep your own log, not enforcement.
    """
    end = holdout_end.isoformat() if holdout_end is not None else body.to_date.isoformat()
    family_fingerprint = _fingerprint({"strategy_type": body.strategy_type, "holdout_end": end})
    grid_fingerprint = _fingerprint(
        {"family": family_fingerprint, "param_grid": body.walk_forward.param_grid}
        if body.walk_forward
        else {"family": family_fingerprint}
    )
    data_fingerprint = _fingerprint(
        {
            "tickers": sorted(body.tickers),
            "benchmark": body.benchmark_ticker,
            "from": body.from_date,
            "to": body.to_date,
            "datasets": dataset_manifests,
        }
    )
    run_fingerprint = _fingerprint(
        {
            "data": data_fingerprint,
            "strategy_type": body.strategy_type,
            "strategy_params": body.strategy_params,
            "cost_model": body.cost_model.model_dump(mode="json"),
            "risk_free_rate_annual": body.risk_free_rate_annual,
            "walk_forward": body.walk_forward.model_dump(mode="json")
            if body.walk_forward
            else None,
        }
    )
    seen_family = [
        other
        for other, job in _portfolio_jobs.items()
        if other != job_id
        and (job.get("holdout_reuse") or {}).get("family_fingerprint") == family_fingerprint
    ]
    seen_data = [
        other
        for other, job in _portfolio_jobs.items()
        if other != job_id
        and (job.get("holdout_reuse") or {}).get("data_fingerprint") == data_fingerprint
    ]
    seen_run = [
        other
        for other in seen_data
        if _portfolio_jobs[other]["holdout_reuse"]["run_fingerprint"] == run_fingerprint
    ]
    return {
        "run_id": body.run_id,
        "data_fingerprint": data_fingerprint,
        "run_fingerprint": run_fingerprint,
        "family_fingerprint": family_fingerprint,
        "grid_fingerprint": grid_fingerprint,
        "tracking": "in_process_memory_only",
        "enforcement": "advisory_only",
        "earlier_jobs_same_strategy_and_holdout_end": seen_family,
        "earlier_jobs_on_same_data": seen_data,
        "earlier_jobs_identical_run": seen_run,
        "reused": bool(seen_family or seen_data),
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
        "commission_cost": float(trade.commission_cost),
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
        "primary_benchmark": result.primary_benchmark,
        "benchmarks": {key: _serialize_benchmark(item) for key, item in result.benchmarks.items()},
        "risk_free_rate_annual": float(result.risk_free_rate_annual),
        "cost_model": result.cost_model,
        "metrics_start_date": (
            result.metrics_start_date.isoformat() if result.metrics_start_date else None
        ),
        "warmup_bars_excluded": result.warmup_bars_excluded,
        "total_fx_fee_cost": float(result.total_fx_fee_cost),
        "sharpe_ratio": float(result.sharpe_ratio) if result.sharpe_ratio is not None else None,
        "sortino_ratio": float(result.sortino_ratio) if result.sortino_ratio is not None else None,
        "calmar_ratio": float(result.calmar_ratio) if result.calmar_ratio is not None else None,
        "max_drawdown_pct": float(result.max_drawdown_pct),
        "total_trades": result.total_trades,
        "rebalance_count": result.rebalance_count,
        "turnover_pct": float(result.turnover_pct),
        "avg_exposure_pct": float(result.avg_exposure_pct),
        "total_slippage_cost": float(result.total_slippage_cost),
        "total_fee_cost": float(result.total_fee_cost),
        "total_execution_cost": float(result.total_execution_cost),
        "coverage": _serialize_portfolio_coverage(result.coverage_report),
        "latest_weights": {
            ticker: float(weight) for ticker, weight in result.latest_weights.items()
        },
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
                "quote_price": float(trade.quote_price),
                "price": float(trade.price),
                "notional": float(trade.notional),
                "slippage_cost": float(trade.slippage_cost),
                "fee_cost": float(trade.fee_cost),
                "fx_fee_cost": float(trade.fx_fee_cost),
                "cost": float(trade.cost),
                "reason": trade.reason,
                "target_weight": float(trade.target_weight),
            }
            for trade in result.trades[-250:]
        ],
        "rationale": rationale,
        "trading_days_per_year": TRADING_DAYS_PER_YEAR,
        "disclosures": dict(PORTFOLIO_DISCLOSURES),
    }


def _serialize_benchmark(item: Any) -> dict[str, Any]:
    def number(value: Any) -> float | None:
        return float(value) if value is not None else None

    return {
        "key": item.key,
        "name": item.name,
        "total_return_pct": number(item.total_return_pct),
        "annualised_return_pct": number(item.annualised_return_pct),
        "sharpe_ratio": number(item.sharpe_ratio),
        "sortino_ratio": number(item.sortino_ratio),
        "max_drawdown_pct": number(item.max_drawdown_pct),
        "avg_exposure_pct": number(item.avg_exposure_pct),
        "total_execution_cost": number(item.total_execution_cost),
        "excess_return_pct": number(item.excess_return_pct),
        "excess_sharpe_ratio": number(item.excess_sharpe_ratio),
        "excess_max_drawdown_pct": number(item.excess_max_drawdown_pct),
    }


def _serialize_portfolio_coverage(report: Any) -> dict[str, Any]:
    return {
        "calendar": report.calendar,
        "exchange_timezone": report.exchange_timezone,
        "requested_from": report.requested_start.isoformat(),
        "requested_to": report.requested_end.isoformat(),
        "minimum_coverage_pct": float(report.minimum_coverage_pct),
        "retained_coverage_pct": float(report.retained_coverage_pct),
        "complete": report.complete,
        "policy": report.policy,
        "policy_id": report.policy_id,
        "eligible": report.eligible,
        "common_from": report.common_start.isoformat() if report.common_start else None,
        "common_to": report.common_end.isoformat() if report.common_end else None,
        "expected_session_ids": list(report.expected_session_ids),
        "retained_session_ids": list(report.retained_session_ids),
        "dropped_session_ids": list(report.dropped_session_ids),
        "symbols": [
            {
                "ticker": item.ticker,
                "expected_session_ids": list(item.expected_session_ids),
                "observed_session_ids": list(item.observed_session_ids),
                "missing_session_ids": list(item.missing_session_ids),
                "extra_session_ids": list(item.extra_session_ids),
                "coverage_pct": float(item.coverage_pct),
                "longest_missing_run": item.longest_missing_run,
                "first_valid_session_id": item.first_valid_session_id,
                "last_valid_session_id": item.last_valid_session_id,
            }
            for item in report.symbols
        ],
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
        warnings.append(
            f"Max drawdown {float(result.max_drawdown_pct):.1f}% is high — review stops"
        )
    if result.consecutive_losses_max >= 5:
        warnings.append(
            f"Max {result.consecutive_losses_max} consecutive losses — ensure daily loss limit covers this"
        )
    friction_cost = float(result.total_slippage_cost + result.total_commission_cost)
    gross_pnl = max(float(result.gross_pnl), 0.0)
    if gross_pnl > 0 and friction_cost > gross_pnl * 0.3:
        warnings.append(
            "Execution friction is >30% of gross profit — tighten entry quality or routing"
        )

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


POSITIVE_VERDICTS = frozenset({"promising", "strong"})


def _classify_portfolio(*, sharpe: float, max_drawdown_pct: float, total_return_pct: float) -> str:
    if total_return_pct <= 0:
        return "losing"
    if sharpe >= 1.0 and max_drawdown_pct <= 20:
        return "strong"
    if sharpe >= 0.6 and max_drawdown_pct <= 25:
        return "promising"
    if sharpe < 0.25:
        return "marginal"
    return "mixed"


def _validated_verdict(walk_forward: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Classify the walk-forward's out-of-sample and holdout evidence with the same thresholds.

    Takes the weaker of the two on every axis (lowest Sharpe and total return, deepest drawdown),
    so neither the stitched out-of-sample series nor the holdout alone can carry a verdict.
    Returns (None, None) when there is no usable evidence.
    """
    if walk_forward is None:
        return None, None
    oos, holdout = walk_forward.oos, walk_forward.holdout_summary
    fields = ("sharpe_ratio", "total_return_pct", "max_drawdown_pct")
    if any(source.get(name) is None for source in (oos, holdout) for name in fields):
        return None, None
    evidence = {
        "out_of_sample": {name: oos[name] for name in fields},
        "holdout": {name: holdout[name] for name in fields},
    }
    verdict = _classify_portfolio(
        sharpe=min(oos["sharpe_ratio"], holdout["sharpe_ratio"]),
        max_drawdown_pct=max(oos["max_drawdown_pct"], holdout["max_drawdown_pct"]),
        total_return_pct=min(oos["total_return_pct"], holdout["total_return_pct"]),
    )
    return verdict, evidence


def _interpret_portfolio_results(result: Any, walk_forward: Any = None) -> dict[str, Any]:
    """Plain-English verdict for a portfolio run.

    'promising' and 'strong' are in-sample labels that are refused ('unvalidated' instead)
    unless a walk-forward with out-of-sample windows and an untouched holdout exists, passed
    its gate, and beat the costed benchmark(s).  ``walk_forward`` is a WalkForwardResult.
    """
    if not result.coverage_report.complete:
        return {
            "verdict": "insufficient_evidence",
            "summary": (
                "Incomplete session coverage prevents a positive evidence classification. "
                "Review the disclosed missing sessions before further research use."
            ),
            "warnings": [
                f"Only {result.coverage_report.retained_coverage_pct}% of expected sessions "
                "were retained across the full universe."
            ],
        }

    # Two verdicts, always shown together.  The in-sample one comes from the full-period run with
    # ``strategy_params``; the validated one comes from the walk-forward's stitched out-of-sample
    # and holdout evidence for the grid-selected procedure.  Those are different procedures and
    # can disagree: a positive verdict is emitted only when both agree on the same label.
    in_sample_verdict = _classify_portfolio(
        sharpe=float(result.sharpe_ratio or 0),
        max_drawdown_pct=float(result.max_drawdown_pct),
        total_return_pct=float(result.total_return_pct),
    )
    validated_verdict, validated_evidence = _validated_verdict(walk_forward)
    verdict = in_sample_verdict

    warnings: list[str] = []
    validation: dict[str, Any] = {
        "walk_forward_run": walk_forward is not None,
        "passes_gate": bool(walk_forward.passes_gate) if walk_forward is not None else False,
        "n_trials": walk_forward.n_trials if walk_forward is not None else None,
        "in_sample_verdict": in_sample_verdict,
        "validated_verdict": validated_verdict,
        "verdicts_agree": validated_verdict == in_sample_verdict
        if validated_verdict is not None
        else None,
        "validated_evidence": validated_evidence,
    }
    sanity = getattr(walk_forward, "benchmark_sanity", None) if walk_forward is not None else None
    validation["benchmark_sanity"] = sanity
    if sanity is not None:
        warnings.append(
            "Benchmark sanity: " + benchmark_sanity_text(sanity) + f"; passes: {sanity['passes']}."
        )
    claimed = next(
        (v for v in (in_sample_verdict, validated_verdict) if v in POSITIVE_VERDICTS), None
    )
    if claimed is not None:
        refusal = None
        if walk_forward is None:
            refusal = "no out-of-sample or holdout walk-forward was run"
        elif not walk_forward.passes_gate:
            refusal = "; ".join(walk_forward.gate_reasons) or "walk-forward gate not passed"
        elif floor_reasons := config_floor_violations(
            holdout_bars=walk_forward.config.holdout_bars,
            test_bars=walk_forward.config.test_bars,
            min_valid_windows=walk_forward.config.min_valid_windows,
            min_rebalances_per_window=walk_forward.config.min_rebalances_per_window,
            min_deflated_sharpe_probability=walk_forward.config.min_deflated_sharpe_probability,
        ):
            # Defence in depth: the harness already enforces the floors; do not trust a
            # walk-forward object that claims a pass on settings below them.
            refusal = "; ".join(floor_reasons)
        elif cost_reason := cost_gate_refusal(result.cost_model):
            refusal = cost_reason
        elif BENCHMARK_BROAD_INDEX not in walk_forward.benchmarks_evaluated:
            refusal = "no broad-index benchmark series was supplied; best verdict is 'unvalidated'"
        elif sanity is None or not sanity.get("passes"):
            # Defence in depth: the harness folds these into gate_reasons, but a missing or
            # failing sanity record must never let a caller-chosen benchmark anchor a verdict.
            refusal = (
                "; ".join(sanity["reasons"])
                if sanity and sanity.get("reasons")
                else "the broad-index benchmark was not sanity-checked"
            )
        elif validated_verdict != in_sample_verdict:
            refusal = (
                f"the in-sample verdict '{in_sample_verdict}' (strategy_params, full period) and "
                f"the validated out-of-sample/holdout verdict '{validated_verdict}' "
                "(grid-selected procedure) disagree"
            )
        if refusal is not None:
            validation["refused_verdict"] = claimed
            validation["refusal_reason"] = refusal
            warnings.append(
                f"Verdict '{claimed}' withheld: {refusal}. "
                "Promising/strong needs out-of-sample and holdout results that beat the costed benchmark."
            )
            verdict = "unvalidated"
    # Shown beside any positive claim or passed gate: the labels are only as good as the universe.
    if (
        verdict in POSITIVE_VERDICTS
        or claimed is not None
        or (walk_forward is not None and walk_forward.passes_gate)
    ):
        warnings.append(SURVIVORSHIP_DISCLOSURE)
    if walk_forward is not None:
        warnings.extend(walk_forward.gate_warnings)
        dropped_ids = walk_forward.oos.get("dropped_window_ids") or []
        validation["dropped_window_ids"] = list(dropped_ids)
        if dropped_ids:
            warnings.append(
                f"{len(dropped_ids)} walk-forward window(s) {list(dropped_ids)} had too few "
                "rebalances and were dropped; the out-of-sample statistics exclude them."
            )
    if result.rebalance_count < 3:
        warnings.append("Very few rebalance decisions were observed — extend the test period.")
    if float(result.max_drawdown_pct) > 25:
        warnings.append(
            f"Portfolio drawdown reached {float(result.max_drawdown_pct):.1f}% — consider a stronger cash filter."
        )
    if float(result.turnover_pct) > 250:
        warnings.append(
            "Turnover is high for a retail account — review rebalancing frequency and FX drag."
        )
    if float(result.avg_exposure_pct) < 40:
        warnings.append(
            "Average exposure stayed low — performance may be driven more by cash timing than asset selection."
        )

    return {
        "verdict": verdict,
        "in_sample_verdict": in_sample_verdict,
        "validated_verdict": validated_verdict,
        "validation": validation,
        "summary": {
            "unvalidated": (
                "In-sample numbers look favourable but are not evidence of an edge: there are no "
                "out-of-sample and holdout results that beat the costed benchmark. Keep it in research only."
            ),
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
