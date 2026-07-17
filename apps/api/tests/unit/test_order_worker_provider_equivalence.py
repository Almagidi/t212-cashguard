from __future__ import annotations

import ast
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from app.broker.provider import (
    BrokerProviderCredentials,
    BrokerProviderRequest,
    BrokerProviderValidationError,
    BrokerRuntimeEnvironment,
    create_trading212_provider_adapter,
)
from app.core.config import settings
from app.core.security import CredentialDecryptionError
from app.services.safety_policy import SafetyPolicyViolation
from app.workers import tasks

API_ROOT = Path(__file__).resolve().parents[2]
TASKS_PATH = API_ROOT / "app" / "workers" / "tasks.py"


class ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def all(self) -> Any:
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value


class ExecuteResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalars(self) -> ScalarResult:
        return ScalarResult(self.value)

    def scalar_one_or_none(self) -> Any:
        return self.value


class FakeSession:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.executed: list[Any] = []
        self.commits = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, statement: Any) -> ExecuteResult:
        self.executed.append(statement)
        if not self.results:
            raise AssertionError("unexpected execute call")
        return ExecuteResult(self.results.pop(0))

    async def commit(self) -> None:
        self.commits += 1


@dataclass(frozen=True)
class FakeOrder:
    id: uuid.UUID
    ticker: str = "AAPL"
    side: str = "buy"
    order_type: str = "limit"
    broker_order_id: str = "t212-order-1"


@dataclass(frozen=True)
class FakeConnection:
    id: uuid.UUID
    user_id: uuid.UUID
    environment: str
    api_key_encrypted: str
    api_secret_encrypted: str


class RecordingTrading212Adapter:
    constructed: ClassVar[list[tuple[str, str, str]]] = []
    entered: ClassVar[int] = 0
    exited: ClassVar[int] = 0
    write_calls: ClassVar[list[str]] = []

    def __init__(self, api_key: str, api_secret: str, environment: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.environment = environment
        self.constructed.append((api_key, api_secret, environment))

    async def __aenter__(self) -> RecordingTrading212Adapter:
        type(self).entered += 1
        return self

    async def __aexit__(self, *_args: Any) -> None:
        type(self).exited += 1

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("place_", "cancel_", "modify_", "submit_")):
            self.write_calls.append(name)
            raise AssertionError(f"worker fake broker write method called directly: {name}")
        raise AttributeError(name)


class RecordingExecutionEngine:
    brokers: ClassVar[list[Any]] = []
    reconcile_calls: ClassVar[list[FakeOrder]] = []
    cancel_calls: ClassVar[list[FakeOrder]] = []

    def __init__(self, _db: FakeSession, broker: Any) -> None:
        self.broker = broker
        self.brokers.append(broker)

    async def reconcile_order(self, order: FakeOrder) -> None:
        self.reconcile_calls.append(order)

    async def cancel_order(self, order: FakeOrder) -> None:
        self.cancel_calls.append(order)


@asynccontextmanager
async def _acquired_task_lock(*_args: Any, **_kwargs: Any) -> Any:
    yield True


@asynccontextmanager
async def _denied_task_lock(*_args: Any, **_kwargs: Any) -> Any:
    yield False


@pytest.fixture(autouse=True)
def _reset_worker_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingTrading212Adapter.constructed.clear()
    RecordingTrading212Adapter.entered = 0
    RecordingTrading212Adapter.exited = 0
    RecordingTrading212Adapter.write_calls.clear()
    RecordingExecutionEngine.brokers.clear()
    RecordingExecutionEngine.reconcile_calls.clear()
    RecordingExecutionEngine.cancel_calls.clear()
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(tasks, "_LOOP", None)
    monkeypatch.setattr("app.core.redis.task_lock", _acquired_task_lock)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", RecordingTrading212Adapter)
    monkeypatch.setattr("app.execution.engine.ExecutionEngine", RecordingExecutionEngine)


def test_order_worker_tasks_define_soft_time_limits() -> None:
    assert tasks.reconcile_pending_orders.time_limit == 60
    assert tasks.reconcile_pending_orders.soft_time_limit == 45
    assert tasks.cancel_timed_out_orders.time_limit == 60
    assert tasks.cancel_timed_out_orders.soft_time_limit == 45


def test_cancel_timed_out_orders_uses_task_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    lock_calls: list[tuple[str, int]] = []

    @asynccontextmanager
    async def recording_task_lock(name: str, *, ttl_seconds: int) -> Any:
        lock_calls.append((name, ttl_seconds))
        yield True

    summaries: list[tuple[str, dict[str, Any]]] = []
    fake_db = FakeSession(results=[[]])
    _install_session(monkeypatch, fake_db, summaries)
    monkeypatch.setattr("app.core.redis.task_lock", recording_task_lock)

    assert tasks.cancel_timed_out_orders.run() == {"cancelled": 0}

    assert lock_calls == [("cancel_timed_out_orders", 90)]
    assert summaries == [("cancel_timed_out_orders", {"cancelled": 0})]


def test_cancel_timed_out_orders_skips_when_task_lock_not_acquired_without_provider_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr("app.core.redis.task_lock", _denied_task_lock)
    _install_adapter_sentinel(monkeypatch)
    _install_provider_sentinel(monkeypatch, provider_calls)

    assert tasks.cancel_timed_out_orders.run() == {
        "skipped": True,
        "reason": "already_running",
    }

    assert provider_calls == []
    assert RecordingTrading212Adapter.constructed == []
    assert RecordingTrading212Adapter.write_calls == []
    assert RecordingExecutionEngine.brokers == []
    assert RecordingExecutionEngine.cancel_calls == []


def _order(**overrides: Any) -> FakeOrder:
    order = FakeOrder(
        id=overrides.pop("id", uuid.uuid4()),
        ticker=overrides.pop("ticker", "AAPL"),
        side=overrides.pop("side", "buy"),
        order_type=overrides.pop("order_type", "limit"),
        broker_order_id=overrides.pop("broker_order_id", "t212-order-1"),
    )
    if overrides:
        raise AssertionError(f"unknown FakeOrder overrides: {sorted(overrides)}")
    return order


def _active_conn(*, environment: str = "live") -> FakeConnection:
    return FakeConnection(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        environment=environment,
        api_key_encrypted=f"encrypted-{environment}-active-key",
        api_secret_encrypted=f"encrypted-{environment}-active-secret",
    )


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    fake_db: FakeSession,
    summaries: list[tuple[str, dict[str, Any]]],
) -> None:
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: fake_db)

    async def complete_task(
        _db: FakeSession, task_name: str, summary: dict[str, Any]
    ) -> dict[str, Any]:
        summaries.append((task_name, summary))
        await _db.commit()
        return summary

    monkeypatch.setattr(tasks, "_complete_task", complete_task)


def _install_decrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = {
        "encrypted-live-active-key": "decrypted-live-key",
        "encrypted-live-active-secret": "decrypted-live-secret",
        "encrypted-demo-active-key": "decrypted-demo-key",
        "encrypted-demo-active-secret": "decrypted-demo-secret",
    }

    def decrypt(value: str) -> str:
        if value not in lookup:
            raise AssertionError(f"unexpected decrypt call: {value!r}")
        return lookup[value]

    monkeypatch.setattr("app.core.security.decrypt_field", decrypt)


def _raise_adapter_sentinel(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("Trading212Adapter must not be constructed in this path")


def _install_adapter_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _raise_adapter_sentinel)


def _install_provider_sentinel(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[tuple[Any, ...], dict[str, Any]]]
) -> None:
    def provider_sentinel(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        raise AssertionError("provider must not be called in this path")

    monkeypatch.setattr("app.broker.provider.create_trading212_provider_adapter", provider_sentinel)


def _assert_no_adapter_context_entered() -> None:
    assert RecordingTrading212Adapter.entered == 0
    assert RecordingTrading212Adapter.exited == 0


@pytest.mark.parametrize(
    ("task_name", "run_task", "results", "expected"),
    [
        (
            "reconcile_pending_orders",
            lambda: tasks.reconcile_pending_orders.run(),
            [[], [_order()]],
            {"reconciled": 0},
        ),
        (
            "cancel_timed_out_orders",
            lambda: tasks.cancel_timed_out_orders.run(),
            [[_order()]],
            {"cancelled": 0},
        ),
    ],
)
def test_order_workers_skip_safely_in_mock_mode_without_constructing_adapter(
    monkeypatch: pytest.MonkeyPatch,
    task_name: str,
    run_task: Any,
    results: list[Any],
    expected: dict[str, Any],
) -> None:
    summaries: list[tuple[str, dict[str, Any]]] = []
    provider_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    fake_db = FakeSession(results=results)
    _install_session(monkeypatch, fake_db, summaries)
    _install_adapter_sentinel(monkeypatch)
    _install_provider_sentinel(monkeypatch, provider_calls)
    monkeypatch.setattr(settings, "APP_MODE", "mock")

    assert run_task() == expected

    # Mock mode skips adapter construction after the existing query sequence.
    assert fake_db.results == []
    assert summaries == [(task_name, expected)]
    assert provider_calls == []
    assert RecordingTrading212Adapter.constructed == []
    assert RecordingExecutionEngine.brokers == []
    _assert_no_adapter_context_entered()


@pytest.mark.parametrize(
    ("task_name", "run_task", "results", "expected"),
    [
        (
            "reconcile_pending_orders",
            lambda: tasks.reconcile_pending_orders.run(),
            [[], []],
            {"reconciled": 0},
        ),
        (
            "cancel_timed_out_orders",
            lambda: tasks.cancel_timed_out_orders.run(),
            [[]],
            {"cancelled": 0},
        ),
    ],
)
def test_order_workers_skip_safely_when_no_candidate_orders_exist(
    monkeypatch: pytest.MonkeyPatch,
    task_name: str,
    run_task: Any,
    results: list[Any],
    expected: dict[str, Any],
) -> None:
    summaries: list[tuple[str, dict[str, Any]]] = []
    provider_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    fake_db = FakeSession(results=results)
    _install_session(monkeypatch, fake_db, summaries)
    _install_adapter_sentinel(monkeypatch)
    _install_provider_sentinel(monkeypatch, provider_calls)

    assert run_task() == expected

    assert summaries == [(task_name, expected)]
    assert fake_db.results == []
    assert provider_calls == []
    assert RecordingTrading212Adapter.constructed == []
    _assert_no_adapter_context_entered()


@pytest.mark.parametrize(
    ("task_name", "run_task", "results", "expected"),
    [
        (
            "reconcile_pending_orders",
            lambda: tasks.reconcile_pending_orders.run(),
            [[], [_order()], None],
            {"reconciled": 0, "skipped": "no_connection"},
        ),
        (
            "cancel_timed_out_orders",
            lambda: tasks.cancel_timed_out_orders.run(),
            [[_order()], None],
            {"cancelled": 0, "skipped": "no_connection"},
        ),
    ],
)
def test_order_workers_skip_safely_when_no_active_connection_exists(
    monkeypatch: pytest.MonkeyPatch,
    task_name: str,
    run_task: Any,
    results: list[Any],
    expected: dict[str, Any],
) -> None:
    summaries: list[tuple[str, dict[str, Any]]] = []
    provider_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    fake_db = FakeSession(results=results)
    _install_session(monkeypatch, fake_db, summaries)
    _install_adapter_sentinel(monkeypatch)
    _install_provider_sentinel(monkeypatch, provider_calls)

    assert run_task() == expected

    assert fake_db.results == []
    assert summaries == [(task_name, expected)]
    assert provider_calls == []
    assert RecordingTrading212Adapter.constructed == []
    assert RecordingExecutionEngine.brokers == []
    _assert_no_adapter_context_entered()


@pytest.mark.parametrize(
    ("task_name", "run_task", "results", "actor", "expected"),
    [
        (
            "reconcile_pending_orders",
            lambda: tasks.reconcile_pending_orders.run(),
            [[], [_order()], _active_conn()],
            "worker:reconcile_pending_orders",
            {"reconciled": 0, "skipped": "credential_error"},
        ),
        (
            "cancel_timed_out_orders",
            lambda: tasks.cancel_timed_out_orders.run(),
            [[_order()], _active_conn()],
            "worker:cancel_timed_out_orders",
            {"cancelled": 0, "skipped": "credential_error"},
        ),
    ],
)
def test_order_workers_mark_reconnect_required_when_credential_decryption_fails(
    monkeypatch: pytest.MonkeyPatch,
    task_name: str,
    run_task: Any,
    results: list[Any],
    actor: str,
    expected: dict[str, Any],
) -> None:
    summaries: list[tuple[str, dict[str, Any]]] = []
    provider_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    reconnect_calls: list[tuple[FakeSession, Any, str, str]] = []
    fake_db = FakeSession(results=results)
    expected_conn = results[-1]
    _install_session(monkeypatch, fake_db, summaries)
    _install_adapter_sentinel(monkeypatch)
    _install_provider_sentinel(monkeypatch, provider_calls)

    def fail_decrypt(_value: str) -> str:
        raise CredentialDecryptionError("cannot decrypt active worker credentials")

    async def mark_reconnect(db: FakeSession, conn: Any, reason: str, *, actor: str) -> None:
        reconnect_calls.append((db, conn, reason, actor))

    monkeypatch.setattr("app.core.security.decrypt_field", fail_decrypt)
    monkeypatch.setattr(tasks, "_mark_connection_reconnect_required", mark_reconnect)

    assert run_task() == expected

    assert fake_db.results == []
    assert summaries == [(task_name, expected)]
    assert reconnect_calls == [
        (fake_db, expected_conn, "cannot decrypt active worker credentials", actor)
    ]
    assert provider_calls == []
    assert RecordingTrading212Adapter.constructed == []
    _assert_no_adapter_context_entered()


@pytest.mark.parametrize(
    ("task_name", "run_task", "results", "action", "expected"),
    [
        (
            "reconcile_pending_orders",
            lambda: tasks.reconcile_pending_orders.run(),
            [[], [_order()], _active_conn()],
            "worker reconcile",
            {
                "reconciled": 0,
                "skipped": "test_environment_block",
                "reason": "blocked before construction",
            },
        ),
        (
            "cancel_timed_out_orders",
            lambda: tasks.cancel_timed_out_orders.run(),
            [[_order()], _active_conn()],
            "worker timeout cancel",
            {
                "cancelled": 0,
                "skipped": "test_environment_block",
                "reason": "blocked before construction",
            },
        ),
    ],
)
def test_order_workers_do_not_construct_adapter_when_environment_gate_rejects(
    monkeypatch: pytest.MonkeyPatch,
    task_name: str,
    run_task: Any,
    results: list[Any],
    action: str,
    expected: dict[str, Any],
) -> None:
    summaries: list[tuple[str, dict[str, Any]]] = []
    provider_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    gate_calls: list[tuple[str, str]] = []
    fake_db = FakeSession(results=results)
    _install_session(monkeypatch, fake_db, summaries)
    _install_adapter_sentinel(monkeypatch)
    _install_provider_sentinel(monkeypatch, provider_calls)

    def reject_environment(environment: str, *, action: str) -> None:
        gate_calls.append((environment, action))
        raise SafetyPolicyViolation(
            "blocked before construction",
            decision_code="test_environment_block",
        )

    monkeypatch.setattr("app.services.safety_policy.require_broker_environment", reject_environment)

    assert run_task() == expected

    assert fake_db.results == []
    assert gate_calls == [("live", action)]
    assert summaries == [(task_name, expected)]
    assert provider_calls == []
    assert RecordingTrading212Adapter.constructed == []
    _assert_no_adapter_context_entered()


@pytest.mark.parametrize(
    ("task_name", "run_task", "results", "expected"),
    [
        (
            "reconcile_pending_orders",
            lambda: tasks.reconcile_pending_orders.run(),
            [[], [_order()], _active_conn(environment="live")],
            {
                "reconciled": 0,
                "skipped": "live_flag_disabled",
                "reason": (
                    "worker reconcile blocked: LIVE_TRADING_ENABLED must be true before "
                    "live broker calls."
                ),
            },
        ),
        (
            "cancel_timed_out_orders",
            lambda: tasks.cancel_timed_out_orders.run(),
            [[_order()], _active_conn(environment="live")],
            {
                "cancelled": 0,
                "skipped": "live_flag_disabled",
                "reason": (
                    "worker timeout cancel blocked: LIVE_TRADING_ENABLED must be true before "
                    "live broker calls."
                ),
            },
        ),
    ],
)
def test_order_workers_do_not_construct_adapter_on_live_disabled_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    task_name: str,
    run_task: Any,
    results: list[Any],
    expected: dict[str, Any],
) -> None:
    summaries: list[tuple[str, dict[str, Any]]] = []
    provider_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    fake_db = FakeSession(results=results)
    _install_session(monkeypatch, fake_db, summaries)
    _install_adapter_sentinel(monkeypatch)
    _install_provider_sentinel(monkeypatch, provider_calls)
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    assert run_task() == expected

    assert fake_db.results == []
    assert summaries == [(task_name, expected)]
    assert provider_calls == []
    assert RecordingTrading212Adapter.constructed == []
    assert RecordingExecutionEngine.brokers == []
    _assert_no_adapter_context_entered()


@pytest.mark.parametrize(
    (
        "environment",
        "app_mode",
        "live_trading_enabled",
        "expected_key",
        "expected_secret",
    ),
    [
        ("live", "live", True, "decrypted-live-key", "decrypted-live-secret"),
        ("demo", "demo", False, "decrypted-demo-key", "decrypted-demo-secret"),
    ],
)
def test_reconcile_pending_orders_calls_provider_after_all_gates_and_reconciles_orders(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    app_mode: str,
    live_trading_enabled: bool,
    expected_key: str,
    expected_secret: str,
) -> None:
    selected_orders = [
        _order(broker_order_id="broker-1"),
        _order(broker_order_id="broker-2"),
    ]
    conn = _active_conn(environment=environment)
    summaries: list[tuple[str, dict[str, Any]]] = []
    fake_db = FakeSession(results=[[], selected_orders, conn])
    events: list[str] = []
    provider_calls: list[dict[str, Any]] = []
    _install_session(monkeypatch, fake_db, summaries)
    _install_decrypt(monkeypatch)
    monkeypatch.setattr(settings, "APP_MODE", app_mode)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", live_trading_enabled)

    def gate(environment: str, *, action: str) -> None:
        events.append(f"gate:{environment}:{action}")

    def recording_provider(
        request: BrokerProviderRequest,
        credentials: BrokerProviderCredentials,
        *,
        app_mode: str,
        live_trading_enabled: bool,
    ) -> RecordingTrading212Adapter:
        events.append(f"provider:{request.environment}")
        provider_calls.append(
            {
                "request": request,
                "credentials": credentials,
                "app_mode": app_mode,
                "live_trading_enabled": live_trading_enabled,
            }
        )
        return cast(
            "RecordingTrading212Adapter",
            create_trading212_provider_adapter(
                request,
                credentials,
                app_mode=app_mode,
                live_trading_enabled=live_trading_enabled,
            ),
        )

    monkeypatch.setattr("app.services.safety_policy.require_broker_environment", gate)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", RecordingTrading212Adapter)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        recording_provider,
    )

    assert tasks.reconcile_pending_orders.run() == {"reconciled": 2}

    assert fake_db.results == []
    assert events == [f"gate:{environment}:worker reconcile", f"provider:{environment}"]
    assert provider_calls == [
        {
            "request": BrokerProviderRequest(
                broker_id="trading212",
                environment=cast("BrokerRuntimeEnvironment", environment),
                purpose="worker_reconcile",
                user_id=conn.user_id,
            ),
            "credentials": BrokerProviderCredentials(
                api_key=expected_key,
                api_secret=expected_secret,
            ),
            "app_mode": app_mode,
            "live_trading_enabled": live_trading_enabled,
        }
    ]
    assert RecordingTrading212Adapter.constructed == [(expected_key, expected_secret, environment)]
    assert RecordingTrading212Adapter.entered == 1
    assert RecordingTrading212Adapter.exited == 1
    assert len(RecordingExecutionEngine.brokers) == 1
    assert RecordingExecutionEngine.brokers[0].environment == environment
    assert RecordingExecutionEngine.reconcile_calls == selected_orders
    assert RecordingExecutionEngine.cancel_calls == []
    assert RecordingTrading212Adapter.write_calls == []
    assert summaries == [("reconcile_pending_orders", {"reconciled": 2})]


def test_reconcile_pending_orders_provider_validation_error_uses_skipped_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_orders = [_order(broker_order_id="broker-1")]
    summaries: list[tuple[str, dict[str, Any]]] = []
    fake_db = FakeSession(results=[[], selected_orders, _active_conn(environment="live")])
    _install_session(monkeypatch, fake_db, summaries)
    _install_decrypt(monkeypatch)
    _install_adapter_sentinel(monkeypatch)

    # This covers the worker's provider-validation failure summary when the
    # provider itself rejects construction after all worker-owned gates pass.
    def fail_provider(*_args: Any, **_kwargs: Any) -> RecordingTrading212Adapter:
        raise BrokerProviderValidationError("provider refused worker reconcile")

    monkeypatch.setattr("app.broker.provider.create_trading212_provider_adapter", fail_provider)

    assert tasks.reconcile_pending_orders.run() == {
        "reconciled": 0,
        "skipped": "provider_validation_error",
        "reason": "provider refused worker reconcile",
    }

    assert fake_db.results == []
    assert RecordingExecutionEngine.reconcile_calls == []
    assert RecordingExecutionEngine.brokers == []
    assert RecordingTrading212Adapter.constructed == []
    assert summaries == [
        (
            "reconcile_pending_orders",
            {
                "reconciled": 0,
                "skipped": "provider_validation_error",
                "reason": "provider refused worker reconcile",
            },
        )
    ]


@pytest.mark.parametrize(
    (
        "environment",
        "app_mode",
        "live_trading_enabled",
        "expected_key",
        "expected_secret",
    ),
    [
        ("live", "live", True, "decrypted-live-key", "decrypted-live-secret"),
        ("demo", "demo", False, "decrypted-demo-key", "decrypted-demo-secret"),
    ],
)
def test_cancel_timed_out_orders_calls_provider_after_all_gates_and_cancels_candidates(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    app_mode: str,
    live_trading_enabled: bool,
    expected_key: str,
    expected_secret: str,
) -> None:
    selected_orders = [
        _order(order_type="limit", broker_order_id="broker-limit"),
        _order(order_type="stop_limit", broker_order_id="broker-stop-limit"),
    ]
    conn = _active_conn(environment=environment)
    summaries: list[tuple[str, dict[str, Any]]] = []
    fake_db = FakeSession(results=[selected_orders, conn])
    events: list[str] = []
    provider_calls: list[dict[str, Any]] = []
    _install_session(monkeypatch, fake_db, summaries)
    _install_decrypt(monkeypatch)
    monkeypatch.setattr(settings, "APP_MODE", app_mode)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", live_trading_enabled)

    def gate(environment: str, *, action: str) -> None:
        events.append(f"gate:{environment}:{action}")

    def recording_provider(
        request: BrokerProviderRequest,
        credentials: BrokerProviderCredentials,
        *,
        app_mode: str,
        live_trading_enabled: bool,
    ) -> RecordingTrading212Adapter:
        events.append(f"provider:{request.environment}")
        provider_calls.append(
            {
                "request": request,
                "credentials": credentials,
                "app_mode": app_mode,
                "live_trading_enabled": live_trading_enabled,
            }
        )
        return cast(
            "RecordingTrading212Adapter",
            create_trading212_provider_adapter(
                request,
                credentials,
                app_mode=app_mode,
                live_trading_enabled=live_trading_enabled,
            ),
        )

    monkeypatch.setattr("app.services.safety_policy.require_broker_environment", gate)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        recording_provider,
    )

    assert tasks.cancel_timed_out_orders.run() == {"cancelled": 2}

    assert fake_db.results == []
    assert events == [
        f"gate:{environment}:worker timeout cancel",
        f"provider:{environment}",
    ]
    assert provider_calls == [
        {
            "request": BrokerProviderRequest(
                broker_id="trading212",
                environment=cast("BrokerRuntimeEnvironment", environment),
                purpose="worker_cancel_timed_out_orders",
                user_id=conn.user_id,
            ),
            "credentials": BrokerProviderCredentials(
                api_key=expected_key,
                api_secret=expected_secret,
            ),
            "app_mode": app_mode,
            "live_trading_enabled": live_trading_enabled,
        }
    ]
    assert RecordingTrading212Adapter.constructed == [(expected_key, expected_secret, environment)]
    assert RecordingTrading212Adapter.entered == 1
    assert RecordingTrading212Adapter.exited == 1
    assert len(RecordingExecutionEngine.brokers) == 1
    assert RecordingExecutionEngine.brokers[0].environment == environment
    assert RecordingExecutionEngine.cancel_calls == selected_orders
    assert RecordingExecutionEngine.reconcile_calls == []
    assert RecordingTrading212Adapter.write_calls == []
    assert summaries == [("cancel_timed_out_orders", {"cancelled": 2})]


def test_cancel_timed_out_orders_provider_validation_error_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_orders = [_order(order_type="limit", broker_order_id="broker-limit")]
    summaries: list[tuple[str, dict[str, Any]]] = []
    fake_db = FakeSession(results=[selected_orders, _active_conn(environment="live")])
    _install_session(monkeypatch, fake_db, summaries)
    _install_decrypt(monkeypatch)
    _install_adapter_sentinel(monkeypatch)

    def fail_provider(*_args: Any, **_kwargs: Any) -> RecordingTrading212Adapter:
        raise BrokerProviderValidationError("provider refused timeout cancel")

    monkeypatch.setattr("app.broker.provider.create_trading212_provider_adapter", fail_provider)

    assert tasks.cancel_timed_out_orders.run() == {
        "cancelled": 0,
        "skipped": "provider_validation_error",
        "reason": "provider refused timeout cancel",
    }

    assert fake_db.results == []
    assert RecordingExecutionEngine.cancel_calls == []
    assert RecordingExecutionEngine.brokers == []
    assert RecordingTrading212Adapter.constructed == []
    assert summaries == [
        (
            "cancel_timed_out_orders",
            {
                "cancelled": 0,
                "skipped": "provider_validation_error",
                "reason": "provider refused timeout cancel",
            },
        )
    ]


def _top_level_function(name: str) -> ast.FunctionDef:
    tree = ast.parse(TASKS_PATH.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _adapter_counts(node: ast.AST) -> dict[str, int]:
    imports = 0
    constructs = 0
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom) and child.module == "app.broker.trading212":
            imports += sum(alias.name == "Trading212Adapter" for alias in child.names)
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            constructs += int(child.func.id == "Trading212Adapter")
    return {"construct": constructs, "import": imports}


def test_order_worker_provider_helper_is_wired_and_direct_references_are_localized() -> None:
    tree = ast.parse(TASKS_PATH.read_text())
    reconcile_node = _top_level_function("reconcile_pending_orders")
    cancel_node = _top_level_function("cancel_timed_out_orders")
    migrated_node = _top_level_function("sync_account_snapshot")
    funding_node = _top_level_function("track_cfd_funding")
    provider_helper_functions = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and "create_trading212_provider_adapter" in ast.unparse(node)
    }

    assert _adapter_counts(reconcile_node) == {"construct": 0, "import": 0}
    assert _adapter_counts(cancel_node) == {"construct": 0, "import": 0}
    assert "create_trading212_provider_adapter" in ast.unparse(reconcile_node)
    assert "create_trading212_provider_adapter" in ast.unparse(cancel_node)
    assert "create_trading212_provider_adapter" in ast.unparse(migrated_node)
    assert "create_trading212_provider_adapter" in ast.unparse(funding_node)
    assert provider_helper_functions == {
        "sync_account_snapshot",
        "track_cfd_funding",
        "reconcile_pending_orders",
        "cancel_timed_out_orders",
    }

    assert _adapter_counts(tree) == {"construct": 0, "import": 0}
