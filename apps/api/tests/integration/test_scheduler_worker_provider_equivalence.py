from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from app.core.config import settings
from app.services import demo_reconciliation_scheduler as scheduler_module
from app.services import demo_reconciliation_worker as worker_module


class RecordingAdapter:
    calls: ClassVar[list[tuple[str, str, str]]] = []
    write_calls: ClassVar[list[str]] = []

    def __init__(self, api_key: str, api_secret: str, environment: str = "demo") -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.environment = environment
        self.calls.append((api_key, api_secret, environment))

    async def __aenter__(self) -> RecordingAdapter:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def get_historical_orders(self, **_kwargs: Any) -> dict[str, Any]:
        return {"items": []}

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("place_", "cancel_", "modify_", "submit_")):
            self.write_calls.append(name)
            raise AssertionError(f"test adapter write method must not be called: {name}")
        raise AttributeError(name)


class FakeSession:
    commits: ClassVar[int] = 0
    adds: ClassVar[list[Any]] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def add(self, value: Any) -> None:
        self.adds.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class RecordingScheduler:
    constructed_with: ClassVar[list[tuple[FakeSession, RecordingAdapter, str]]] = []
    tick_calls: ClassVar[int] = 0

    def __init__(self, db: FakeSession, broker: RecordingAdapter, *, actor: str) -> None:
        self.db = db
        self.broker = broker
        self.actor = actor
        self.constructed_with.append((db, broker, actor))

    async def tick(self) -> None:
        type(self).tick_calls += 1

    async def _audit(self, _action: str, _payload: dict[str, Any]) -> str:
        return "audit-id"

    def _base_payload(self) -> dict[str, Any]:
        return {
            "broker_environment": getattr(self.broker, "environment", settings.T212_ENVIRONMENT),
            "no_broker_order_sent": True,
            "read_only_broker_calls": True,
        }


@pytest.fixture(autouse=True)
def _safe_demo_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingAdapter.calls.clear()
    RecordingAdapter.write_calls.clear()
    FakeSession.commits = 0
    FakeSession.adds.clear()
    RecordingScheduler.constructed_with.clear()
    RecordingScheduler.tick_calls = 0
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(settings, "T212_API_KEY", "")
    monkeypatch.setattr(settings, "T212_API_SECRET", "")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "")
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_WORKER_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_ENABLED", True, raising=False)
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS", 0, raising=False
    )
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS", 60, raising=False
    )
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP", True, raising=False
    )
    scheduler_module._BACKGROUND_TASK = None


@pytest.fixture
def scheduler_construction_fakes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    provider_calls: list[str] = []

    def forbidden_provider(*_args: Any, **_kwargs: Any) -> Any:
        provider_calls.append("create_trading212_provider_adapter")
        raise AssertionError("scheduler/worker construction must not use provider helper yet")

    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        forbidden_provider,
    )
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", RecordingAdapter)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(scheduler_module, "DemoReconciliationScheduler", RecordingScheduler)
    return provider_calls


async def _start_scheduler_and_cancel() -> None:
    task = await scheduler_module.start_global_demo_reconciliation_scheduler()
    assert task is not None
    for _ in range(10):
        await scheduler_module.asyncio.sleep(0)
    await scheduler_module.stop_global_demo_reconciliation_scheduler()


@pytest.mark.asyncio
async def test_scheduler_startup_constructs_demo_adapter_after_demo_gates(
    monkeypatch: pytest.MonkeyPatch,
    scheduler_construction_fakes: list[str],
) -> None:
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "demo-key")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setattr(settings, "T212_API_KEY", "generic-key-must-not-win")
    monkeypatch.setattr(settings, "T212_API_SECRET", "generic-secret-must-not-win")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key-must-not-be-used")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret-must-not-be-used")

    await _start_scheduler_and_cancel()

    assert RecordingAdapter.calls == [("demo-key", "demo-secret", "demo")]
    assert RecordingScheduler.tick_calls == 1
    adapter_construction_actors = [
        actor
        for _db, broker, actor in RecordingScheduler.constructed_with
        if isinstance(broker, RecordingAdapter)
    ]
    assert adapter_construction_actors == ["background:demo_reconciliation_scheduler"]
    assert scheduler_construction_fakes == []
    assert RecordingAdapter.write_calls == []


@pytest.mark.asyncio
async def test_scheduler_startup_preserves_current_generic_demo_credential_fallback(
    monkeypatch: pytest.MonkeyPatch,
    scheduler_construction_fakes: list[str],
) -> None:
    monkeypatch.setattr(settings, "T212_API_KEY", "generic-demo-key")
    monkeypatch.setattr(settings, "T212_API_SECRET", "generic-demo-secret")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key-must-not-be-used")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret-must-not-be-used")

    await _start_scheduler_and_cancel()

    assert RecordingAdapter.calls == [("generic-demo-key", "generic-demo-secret", "demo")]
    assert scheduler_construction_fakes == []
    assert RecordingAdapter.write_calls == []


@pytest.mark.asyncio
async def test_scheduler_startup_missing_demo_credentials_never_uses_live_credentials(
    monkeypatch: pytest.MonkeyPatch,
    scheduler_construction_fakes: list[str],
) -> None:
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key-must-not-be-used")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret-must-not-be-used")

    await _start_scheduler_and_cancel()

    assert RecordingAdapter.calls == []
    assert RecordingScheduler.tick_calls == 0
    assert scheduler_construction_fakes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_name", "unsafe_value"),
    [
        ("APP_MODE", "mock"),
        ("APP_MODE", "paper"),
        ("APP_MODE", "live"),
        ("T212_ENVIRONMENT", "live"),
        ("LIVE_TRADING_ENABLED", True),
        ("DEMO_RECONCILIATION_WORKER_ENABLED", False),
    ],
)
async def test_scheduler_startup_refuses_unsafe_states_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
    scheduler_construction_fakes: list[str],
    setting_name: str,
    unsafe_value: object,
) -> None:
    monkeypatch.setattr(settings, setting_name, unsafe_value)
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "demo-key")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "demo-secret")

    task = await scheduler_module.start_global_demo_reconciliation_scheduler()

    assert task is None
    assert RecordingAdapter.calls == []
    assert RecordingScheduler.constructed_with == []
    assert scheduler_construction_fakes == []


def test_worker_service_accepts_broker_and_does_not_construct_trading212() -> None:
    # Intentional migration-lock inspection: the worker service must stay broker-injected.
    source = inspect.getsource(worker_module.DemoReconciliationWorker)

    assert "Trading212Adapter" not in source
    assert "create_trading212_provider_adapter" not in source


class RecordingWorker:
    brokers: ClassVar[list[RecordingAdapter]] = []
    calls: ClassVar[int] = 0

    def __init__(self, _db: FakeSession, broker: RecordingAdapter, *, actor: str) -> None:
        assert actor == "script:t212_demo_reconciliation_worker"
        self.broker = broker
        self.brokers.append(broker)

    async def run_once(self) -> Any:
        type(self).calls += 1

        @dataclass(frozen=True)
        class Summary:
            outcome: str = "completed"
            run_id: str = "run-id"
            no_broker_order_sent: bool = True
            read_only_broker_calls: bool = True

        return Summary()


@pytest.fixture
def worker_script_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    import scripts.t212_demo_reconciliation_worker as script

    # RecordingWorker is only exercised through this fixture, so reset its state here.
    RecordingWorker.brokers.clear()
    RecordingWorker.calls = 0
    monkeypatch.setattr(script, "Trading212Adapter", RecordingAdapter)
    monkeypatch.setattr(script, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(script, "DemoReconciliationWorker", RecordingWorker)
    return script


@pytest.mark.asyncio
async def test_worker_script_constructs_demo_adapter_from_current_generic_credentials(
    monkeypatch: pytest.MonkeyPatch,
    worker_script_module: Any,
) -> None:
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setenv("T212_ENVIRONMENT", "demo")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_RECONCILIATION_WORKER_ENABLED", "true")
    monkeypatch.setenv("T212_API_KEY", "script-demo-key")
    monkeypatch.setenv("T212_API_SECRET", "script-demo-secret")
    monkeypatch.setenv("T212_DEMO_API_KEY", "demo-name-key-must-not-be-used")
    monkeypatch.setenv("T212_DEMO_API_SECRET", "demo-name-secret-must-not-be-used")
    monkeypatch.setenv("T212_LIVE_API_KEY", "live-key-must-not-be-used")
    monkeypatch.setenv("T212_LIVE_API_SECRET", "live-secret-must-not-be-used")

    result = await worker_script_module.main()

    assert result == 0
    assert RecordingAdapter.calls == [("script-demo-key", "script-demo-secret", "demo")]
    assert [broker.environment for broker in RecordingWorker.brokers] == ["demo"]
    assert RecordingWorker.calls == 1
    assert RecordingAdapter.write_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("APP_MODE", "mock"),
        ("APP_MODE", "paper"),
        ("APP_MODE", "live"),
        ("T212_ENVIRONMENT", "live"),
        ("LIVE_TRADING_ENABLED", "true"),
        ("DEMO_RECONCILIATION_WORKER_ENABLED", "false"),
    ],
)
async def test_worker_script_refuses_unsafe_states_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
    worker_script_module: Any,
    env_name: str,
    env_value: str,
) -> None:
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setenv("T212_ENVIRONMENT", "demo")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_RECONCILIATION_WORKER_ENABLED", "true")
    monkeypatch.setenv("T212_API_KEY", "script-demo-key")
    monkeypatch.setenv("T212_API_SECRET", "script-demo-secret")
    monkeypatch.setenv(env_name, env_value)

    with pytest.raises(SystemExit):
        await worker_script_module.main()

    assert RecordingAdapter.calls == []
    assert RecordingWorker.calls == 0
    assert RecordingAdapter.write_calls == []


@pytest.mark.asyncio
async def test_worker_script_missing_generic_credentials_do_not_fall_through_to_live(
    monkeypatch: pytest.MonkeyPatch,
    worker_script_module: Any,
) -> None:
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setenv("T212_ENVIRONMENT", "demo")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_RECONCILIATION_WORKER_ENABLED", "true")
    monkeypatch.delenv("T212_API_KEY", raising=False)
    monkeypatch.delenv("T212_API_SECRET", raising=False)
    monkeypatch.setenv("T212_LIVE_API_KEY", "live-key-must-not-be-used")
    monkeypatch.setenv("T212_LIVE_API_SECRET", "live-secret-must-not-be-used")

    with pytest.raises(SystemExit, match="T212_API_KEY"):
        await worker_script_module.main()

    assert RecordingAdapter.calls == []
    assert RecordingWorker.calls == 0
    assert RecordingAdapter.write_calls == []


def test_scheduler_worker_runtime_provider_call_sites_remain_unwired() -> None:
    scheduler_source = inspect.getsource(scheduler_module)
    worker_source = inspect.getsource(worker_module)

    assert "create_trading212_provider_adapter" not in scheduler_source
    assert "app.broker.provider" not in scheduler_source
    assert "create_trading212_provider_adapter" not in worker_source
    assert "app.broker.provider" not in worker_source
