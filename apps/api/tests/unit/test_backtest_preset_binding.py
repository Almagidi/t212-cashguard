from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks
from pydantic import ValidationError

from app.api.v1.routes import backtest as route
from app.backtest.data_fetcher import BacktestDataFetcher
from app.backtest.strategy_registry import get_backtest_strategy
from app.services.strategy_presets import PRESET_CONFIGS
from app.strategies.indicators import Bar


def effective(key):
    return deepcopy(
        get_backtest_strategy(key)["strategy_class"](PRESET_CONFIGS[key].strategy_params).params
    )


def request(key="orb", **changes):
    return {
        "ticker": "AAPL",
        "strategy_type": key,
        "preset_key": key,
        "strategy_params": effective(key),
        "from_date": "2025-01-06",
        "to_date": "2025-01-07",
        **changes,
    }


@pytest.fixture
def synthetic_history(monkeypatch):
    monkeypatch.setattr(route.settings, "POLYGON_API_KEY", uuid4().hex)

    async def fetch(self, ticker, **kwargs):
        self._manifests[ticker] = {"manifest_id": "synthetic-only"}
        start = datetime(2025, 1, 6, 14, 30, tzinfo=UTC)
        return (
            [
                Bar(
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=Decimal("1000"),
                )
                for _ in range(60)
            ],
            [start + timedelta(minutes=5 * index) for index in range(60)],
        )

    monkeypatch.setattr(BacktestDataFetcher, "fetch_bars", fetch)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", PRESET_CONFIGS)
async def test_catalog_exposes_exact_effective_preset(client, auth_headers, key):
    response = await client.get("/v1/backtest/strategies", headers=auth_headers)
    assert response.status_code == 200
    item = next(item for item in response.json() if item["type"] == key)
    assert item["preset_key"] == key
    assert item["label"] == PRESET_CONFIGS[key].label
    assert item["description"] == PRESET_CONFIGS[key].description
    assert item["strategy_params"] == effective(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", PRESET_CONFIGS)
async def test_preset_run_records_and_executes_exact_configuration(
    client, auth_headers, synthetic_history, monkeypatch, key
):
    from app.backtest import engine

    original = engine.Backtester
    observed = []

    class RecordingBacktester(original):
        def __init__(self, *, strategy, **kwargs):
            observed.append(deepcopy(strategy.params))
            super().__init__(strategy=strategy, **kwargs)

    monkeypatch.setattr(engine, "Backtester", RecordingBacktester)
    response = await client.post("/v1/backtest/run", headers=auth_headers, json=request(key))
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    job = (await client.get(f"/v1/backtest/result/{job_id}", headers=auth_headers)).json()
    assert job["status"] == "complete", job.get("error")
    assert job["configuration"]["preset_key"] == key
    assert job["configuration"]["source"] == "preset"
    assert job["configuration"]["strategy_params"] == effective(key)
    assert observed == [effective(key)]
    assert job["result"]["strategy"] == PRESET_CONFIGS[key].label
    route._jobs.pop(job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"preset_key": "not-a-preset"},
        {"preset_key": "vwap_reclaim"},
        {"preset_id": "orb"},
        {"strategy_params": {}},
        {"strategy_params": {"min_rvol": 99}},
        {"strategy_params": {**effective("orb"), "min_rvol": 1.5}},
        {"strategy_params": {**effective("orb"), "allow_short": 0}},
        {"strategy_params": {**effective("orb"), "unknown": 1}},
    ],
)
async def test_inconsistent_preset_request_rejected_before_job_or_fetch(
    client, auth_headers, synthetic_history, changes
):
    before = set(route._jobs)
    response = await client.post("/v1/backtest/run", headers=auth_headers, json=request(**changes))
    assert response.status_code == 422
    assert set(route._jobs) == before


@pytest.mark.asyncio
async def test_legacy_custom_parameters_remain_explicit_custom(
    client, auth_headers, synthetic_history
):
    body = request(strategy_params={"min_rvol": 2.3})
    del body["preset_key"]
    response = await client.post("/v1/backtest/run", headers=auth_headers, json=body)
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    job = route._jobs.pop(job_id)
    assert job["configuration"]["source"] == "custom"
    assert job["configuration"]["preset_key"] is None
    assert job["configuration"]["strategy_params"]["min_rvol"] == 2.3
    assert job["configuration"]["strategy_params"]["risk_per_trade_pct"] == 0.75


@pytest.mark.parametrize("key", PRESET_CONFIGS)
def test_id_only_request_intentionally_resolves_current_preset(key):
    body = request(key)
    del body["strategy_params"]
    assert route.BacktestRequest(**body).configuration()["strategy_params"] == effective(key)


@pytest.mark.parametrize("params", [{}, {"min_rvol": 2.3}])
def test_unbound_legacy_requests_never_infer_preset_identity(params):
    body = request(strategy_params=params)
    del body["preset_key"]
    configuration = route.BacktestRequest(**body).configuration()
    assert configuration["preset_key"] is None
    assert configuration["source"] == ("custom" if params else "class_defaults")
    assert configuration["label"] == get_backtest_strategy("orb")["label"]


@pytest.mark.parametrize(
    "params",
    [
        {"unknown": 1},
        {"min_rvol": "1.6"},
        {"min_rvol": True},
        {"min_rvol": None},
        {"min_rvol": []},
        {"min_rvol": {}},
        {"min_rvol": float("nan")},
        {"min_rvol": float("inf")},
        {"allow_short": 0},
        {"orb_minutes": True},
        {"orb_minutes": 1.5},
        {"orb_minutes": 0},
        {"candle_interval_minutes": 0},
        {"orb_minutes": 1},
        {"candle_interval_minutes": 20},
        {"session_open_utc": "24:00"},
        {"session_open_utc": "9:30"},
        {"avoid_first_minutes": -1},
    ],
)
def test_invalid_custom_parameters_are_rejected_without_silent_coercion(params):
    body = request(strategy_params=params)
    del body["preset_key"]
    with pytest.raises(ValidationError):
        route.BacktestRequest(**body)


@pytest.mark.asyncio
async def test_queue_snapshot_has_no_alias_to_request_catalog_or_job_metadata(synthetic_history):
    original = deepcopy(PRESET_CONFIGS["orb"].strategy_params)
    body = route.BacktestRequest(**request())
    tasks = BackgroundTasks()
    response = await route.run_backtest(body, tasks, None)
    body.strategy_params["min_rvol"] = 99
    response.configuration["strategy_params"]["min_rvol"] = 98
    route._jobs[response.job_id]["configuration"]["strategy_params"]["min_rvol"] = 97
    await tasks()
    job = route._jobs.pop(response.job_id)
    assert job["status"] == "complete", job.get("error")
    assert job["configuration"]["strategy_params"] == effective("orb")
    assert PRESET_CONFIGS["orb"].strategy_params == original


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["short", "exception"])
async def test_error_paths_preserve_parameter_intent(monkeypatch, failure):
    async def fetch(self, ticker, **kwargs):
        self._manifests[ticker] = {"manifest_id": "synthetic-only"}
        if failure == "exception":
            raise ValueError("synthetic data unavailable")
        return [], []

    monkeypatch.setattr(BacktestDataFetcher, "fetch_bars", fetch)
    body = route.BacktestRequest(**request("vwap_reclaim"))
    await route._run_backtest_job("synthetic-error", body)
    job = route._jobs.pop("synthetic-error")
    assert job["status"] == "error"
    assert job["configuration"]["strategy_params"] == effective("vwap_reclaim")
    assert job["configuration"]["preset_key"] == "vwap_reclaim"


@pytest.mark.asyncio
async def test_walk_forward_is_explicitly_separate_registry_search(synthetic_history):
    body = route.BacktestRequest(**request("orb", run_walk_forward=True))
    await route._run_backtest_job("synthetic-walk-forward", body)
    job = route._jobs.pop("synthetic-walk-forward")
    assert job["configuration"]["source"] == "preset"
    assert job["walk_forward_configuration"]["source"] == "registry_parameter_grid"
    assert (
        job["walk_forward_configuration"]["parameter_grid"]
        == get_backtest_strategy("orb")["param_grid"]
    )
    assert job["walk_forward_summary"]["verdict"] == "insufficient_evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["NaN", "Infinity", "1e999"])
async def test_http_nonfinite_input_returns_controlled_422(client, auth_headers, number):
    body = request(strategy_params={"min_rvol": "NUMBER"})
    del body["preset_key"]
    raw = json.dumps(body).replace('"NUMBER"', number)
    before = set(route._jobs)
    response = await client.post(
        "/v1/backtest/run",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=raw,
    )
    assert response.status_code == 422
    assert set(route._jobs) == before
    assert all(set(error) == {"loc", "msg", "type"} for error in response.json()["detail"])


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["{", "{}"])
async def test_invalid_json_and_missing_fields_remain_422(client, auth_headers, body):
    response = await client.post(
        "/v1/backtest/run",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=body,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_other_route_keeps_its_standard_validation_response(client, auth_headers):
    response = await client.post("/v1/backtest/portfolio/run", headers=auth_headers, json={})
    assert response.status_code == 422
    assert all("input" in error for error in response.json()["detail"])
