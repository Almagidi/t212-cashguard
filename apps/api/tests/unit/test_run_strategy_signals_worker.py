"""Scheduled strategy-signals task-path dry-run validation.

Proves the Celery entry point that Celery beat fires every 5 minutes —
``app.workers.tasks.run_strategy_signals`` — is safe to invoke directly in
``APP_MODE=mock`` and wires correctly to its collaborators:

    Celery beat (production only, not exercised here)
    -> run_strategy_signals task (invoked directly via ``.run()``, no broker
       needed — mirrors the existing pattern in
       ``test_order_worker_provider_equivalence.py``)
    -> app.core.redis.task_lock (distributed lock; skip if already running)
    -> app.db.session.AsyncSessionLocal (DB session for the tick)
    -> app.services.strategy_runner.StrategyRunner.run_all_enabled()
    -> heartbeat recording / summary return

``StrategyRunner.run_all_enabled()`` itself is stubbed at its constructor
boundary here — its real kill-switch short-circuit, no-broker-in-mock-mode,
and safe-no-op-with-no-enabled-strategies behaviour is already proven against
a real database in ``tests/integration/test_paper_dry_run_validation.py``
(``test_kill_switch_skips_automated_strategy_runner_before_broker_lookup``)
and ``tests/unit/test_strategy_runner_provider_equivalence.py``
(``test_get_broker_mock_mode_returns_mock_adapter_without_trading212_construction``,
``test_run_all_enabled_safety_gates_skip_before_broker_lookup``). This module
adds the one thing those do not cover: the task wrapper itself — task_lock
acquisition/denial, session lifecycle, and that the wrapper faithfully
propagates the runner's documented safety contracts without adding any
broker access of its own.

No real Trading 212 or Kraken adapter is ever constructed by any test here;
Trading212Adapter/KrakenAdapter constructors are monkeypatched to raise if
touched.
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.core.config import settings
from app.workers import tasks

API_ROOT = Path(__file__).resolve().parents[2]
TASKS_PATH = API_ROOT / "app" / "workers" / "tasks.py"


class _RaisingTrading212Adapter:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("Trading212Adapter must not be constructed for run_strategy_signals")


class _RaisingKrakenAdapter:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("KrakenAdapter must not be constructed for run_strategy_signals")


def _raise_if_provider_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("broker provider factory must not be called for run_strategy_signals")


class FakeSession:
    """Minimal async-context-manager stand-in for an ``AsyncSessionLocal()`` result."""

    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class FakeStrategyRunner:
    """Stand-in for ``StrategyRunner`` — records construction/invocation only."""

    constructed_with: ClassVar[list[Any]] = []
    run_all_enabled_calls: ClassVar[int] = 0
    summary: ClassVar[dict[str, Any]] = {
        "strategies_run": 0,
        "signals_generated": 0,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
    }

    def __init__(self, db: Any) -> None:
        type(self).constructed_with.append(db)

    async def run_all_enabled(self) -> dict[str, Any]:
        type(self).run_all_enabled_calls += 1
        return dict(type(self).summary)


class _RaisingStrategyRunner:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("StrategyRunner must not be constructed when the task lock is denied")


def _raise_if_session_opened(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("AsyncSessionLocal must not be opened when the task lock is denied")


@asynccontextmanager
async def _acquired_task_lock(*_args: Any, **_kwargs: Any) -> Any:
    yield True


@asynccontextmanager
async def _denied_task_lock(*_args: Any, **_kwargs: Any) -> Any:
    yield False


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeStrategyRunner.constructed_with = []
    FakeStrategyRunner.run_all_enabled_calls = 0
    FakeStrategyRunner.summary = {
        "strategies_run": 0,
        "signals_generated": 0,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
    }
    monkeypatch.setattr(tasks, "_LOOP", None)
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _RaisingTrading212Adapter)
    monkeypatch.setattr("app.broker.kraken.KrakenAdapter", _RaisingKrakenAdapter)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter", _raise_if_provider_called
    )


# ─── Beat schedule + task shape ───────────────────────────────────────────────


def test_strategy_signals_task_registered_on_five_minute_cadence() -> None:
    from app.workers.celery_app import celery_app

    cfg = celery_app.conf.beat_schedule["strategy-signals"]
    assert cfg["task"] == "app.workers.tasks.run_strategy_signals"
    assert cfg["schedule"] == 300.0


def test_run_strategy_signals_defines_bounded_retries_and_time_limits() -> None:
    assert tasks.run_strategy_signals.max_retries == 0
    assert tasks.run_strategy_signals.time_limit == 240
    assert tasks.run_strategy_signals.soft_time_limit == 180


def test_run_strategy_signals_never_references_broker_adapters_directly() -> None:
    """Static guard: the task delegates all broker access to StrategyRunner.

    If this ever fails, someone added a direct broker/provider reference to
    the task wrapper itself, bypassing the safety gates that live inside
    ``StrategyRunner.run_all_enabled()``.
    """
    tree = ast.parse(TASKS_PATH.read_text())
    node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_strategy_signals"
    )
    source = ast.unparse(node)
    assert "Trading212Adapter" not in source
    assert "KrakenAdapter" not in source
    assert "create_trading212_provider_adapter" not in source
    assert "StrategyRunner" in source
    assert "task_lock" in source


# ─── Task-lock + session wiring (direct invocation, no broker/Redis needed) ──


def test_run_strategy_signals_uses_task_lock_and_invokes_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_calls: list[tuple[str, int]] = []

    @asynccontextmanager
    async def recording_task_lock(name: str, *, ttl_seconds: int) -> Any:
        lock_calls.append((name, ttl_seconds))
        yield True

    fake_db = FakeSession()
    summaries: list[tuple[str, dict[str, Any]]] = []
    FakeStrategyRunner.summary = {
        "strategies_run": 2,
        "signals_generated": 1,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
    }

    async def complete_task(
        _db: FakeSession, task_name: str, summary: dict[str, Any]
    ) -> dict[str, Any]:
        summaries.append((task_name, summary))
        await _db.commit()
        return summary

    monkeypatch.setattr("app.core.redis.task_lock", recording_task_lock)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: fake_db)
    monkeypatch.setattr("app.services.strategy_runner.StrategyRunner", FakeStrategyRunner)
    monkeypatch.setattr(tasks, "_complete_task", complete_task)

    result = tasks.run_strategy_signals.run()

    assert lock_calls == [("run_strategy_signals", 270)]
    assert FakeStrategyRunner.constructed_with == [fake_db]
    assert FakeStrategyRunner.run_all_enabled_calls == 1
    assert result == FakeStrategyRunner.summary
    assert summaries == [("run_strategy_signals", FakeStrategyRunner.summary)]
    assert fake_db.commits == 1


def test_run_strategy_signals_skips_when_lock_not_acquired_without_touching_session_or_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.core.redis.task_lock", _denied_task_lock)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", _raise_if_session_opened)
    monkeypatch.setattr("app.services.strategy_runner.StrategyRunner", _RaisingStrategyRunner)

    result = tasks.run_strategy_signals.run()

    assert result == {"skipped": True, "reason": "already_running"}
    assert FakeStrategyRunner.constructed_with == []
    assert FakeStrategyRunner.run_all_enabled_calls == 0


# ─── Runner safety contracts propagate through the real task entrypoint ──────


def test_run_strategy_signals_propagates_kill_switch_contract_without_broker_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task wrapper must pass the kill-switch skip straight through.

    ``StrategyRunner.run_all_enabled()`` returning ``skipped="kill_switch"``
    before any broker lookup is proven against a real database in
    ``test_kill_switch_skips_automated_strategy_runner_before_broker_lookup``
    (tests/integration/test_paper_dry_run_validation.py). Here we prove the
    *task* does not add any broker access of its own around that contract.
    """
    FakeStrategyRunner.summary = {
        "strategies_run": 0,
        "signals_generated": 0,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
        "skipped": "kill_switch",
    }
    fake_db = FakeSession()

    async def complete_task(
        _db: FakeSession, task_name: str, summary: dict[str, Any]
    ) -> dict[str, Any]:
        return summary

    monkeypatch.setattr("app.core.redis.task_lock", _acquired_task_lock)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: fake_db)
    monkeypatch.setattr("app.services.strategy_runner.StrategyRunner", FakeStrategyRunner)
    monkeypatch.setattr(tasks, "_complete_task", complete_task)

    result = tasks.run_strategy_signals.run()

    assert result == FakeStrategyRunner.summary
    assert result["skipped"] == "kill_switch"
    assert result["orders_submitted"] == 0


def test_run_strategy_signals_propagates_safe_noop_when_no_enabled_strategies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No enabled strategies -> bare zero-summary, no ``skipped`` key.

    Mirrors the real ``run_all_enabled`` contract at
    ``app/services/strategy_runner.py`` (``if not strategies: return
    summary``), which never adds a ``skipped`` reason for this case.
    """
    FakeStrategyRunner.summary = {
        "strategies_run": 0,
        "signals_generated": 0,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
    }
    fake_db = FakeSession()

    async def complete_task(
        _db: FakeSession, task_name: str, summary: dict[str, Any]
    ) -> dict[str, Any]:
        return summary

    monkeypatch.setattr("app.core.redis.task_lock", _acquired_task_lock)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: fake_db)
    monkeypatch.setattr("app.services.strategy_runner.StrategyRunner", FakeStrategyRunner)
    monkeypatch.setattr(tasks, "_complete_task", complete_task)

    result = tasks.run_strategy_signals.run()

    assert result == FakeStrategyRunner.summary
    assert "skipped" not in result
