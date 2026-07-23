from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from app.core import security as security_module
from app.core.config import settings
from app.core.security import CredentialDecryptionError
from app.services import strategy_runner
from app.services.safety_policy import SafetyPolicyViolation
from app.services.strategy_runner import StrategyRunner
from app.strategies.indicators import Bar
from tests.unit import test_trading212_construction_inventory as construction_inventory

API_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_RUNNER_PATH = API_ROOT / "app" / "services" / "strategy_runner.py"


class ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def all(self) -> Any:
        if not isinstance(self.value, list):
            raise AssertionError(f"expected list result for scalars().all(), got {self.value!r}")
        return self.value


class ExecuteResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        if isinstance(self.value, list):
            raise AssertionError(
                "scalar_one_or_none() received a list result; fake query ordering is wrong"
            )
        return self.value

    def scalar_one(self) -> Any:
        if isinstance(self.value, list):
            raise AssertionError(
                "scalar_one() received a list result; fake query ordering is wrong"
            )
        return self.value

    def scalars(self) -> ScalarResult:
        return ScalarResult(self.value)


class FakeSession:
    def __init__(self, results: list[Any], events: list[str] | None = None) -> None:
        self.results = results
        self.events = events
        self.executed: list[Any] = []
        self.added: list[Any] = []
        self.flushed = 0

    async def execute(self, statement: Any) -> ExecuteResult:
        if self.events is not None:
            self.events.append("db_execute")
        self.executed.append(statement)
        if not self.results:
            raise AssertionError("unexpected execute call")
        return ExecuteResult(self.results.pop(0))

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushed += 1


@dataclass
class FakeAppSettings:
    auto_trading_enabled: bool = True
    kill_switch_active: bool = False
    live_trading_unlocked: bool = True


@dataclass(frozen=True)
class FakeConnection:
    id: uuid.UUID
    user_id: uuid.UUID
    environment: str
    api_key_encrypted: str
    api_secret_encrypted: str


@dataclass
class FakeSubmittedOrder:
    id: uuid.UUID
    ticker: str
    side: str
    is_dry_run: bool


class RecordingTrading212Adapter:
    constructed: ClassVar[list[tuple[str, str, str]]] = []
    entered: ClassVar[int] = 0
    exited: ClassVar[int] = 0

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

    async def get_account_summary(self) -> dict[str, Decimal]:
        return {"total": Decimal("1000"), "free": Decimal("1000")}

    async def get_positions(self) -> list[dict[str, Any]]:
        return []


class RecordingBroker:
    environment = "demo"

    def __init__(
        self,
        *,
        account_summary: dict[str, Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.account_summary = account_summary or {
            "total": Decimal("1000"),
            "free": Decimal("1000"),
        }
        self.positions = positions or []
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> RecordingBroker:
        self.entered += 1
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.exited += 1

    async def get_account_summary(self) -> dict[str, Any]:
        self.read_calls.append("get_account_summary")
        return self.account_summary

    async def get_positions(self) -> list[dict[str, Any]]:
        self.read_calls.append("get_positions")
        return self.positions

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("place_", "cancel_", "modify_", "submit_")):
            self.write_calls.append(name)
            raise AssertionError(f"unexpected direct broker write method: {name}")
        raise AttributeError(name)


class RecordingExecutionEngine:
    brokers: ClassVar[list[RecordingBroker]] = []
    order_intents: ClassVar[list[dict[str, Any]]] = []
    submitted_orders: ClassVar[list[FakeSubmittedOrder]] = []

    def __init__(self, _db: FakeSession, broker: RecordingBroker) -> None:
        self.broker = broker
        self.brokers.append(broker)

    async def create_order_intent(self, **kwargs: Any) -> FakeSubmittedOrder:
        self.order_intents.append(kwargs)
        return FakeSubmittedOrder(
            id=uuid.uuid4(),
            ticker=str(kwargs["ticker"]),
            side=str(kwargs["side"]),
            is_dry_run=bool(kwargs.get("is_dry_run", False)),
        )

    async def submit_order(self, order: FakeSubmittedOrder) -> FakeSubmittedOrder:
        self.submitted_orders.append(order)
        return order


class AllowingRiskEngine:
    def __init__(self) -> None:
        self.market_condition_calls: list[dict[str, Any]] = []
        self.run_all_calls: list[dict[str, Any]] = []
        self.sector_calls: list[dict[str, Any]] = []
        self.kill_switch_checks = 0

    async def check_market_conditions(self, **kwargs: Any) -> None:
        self.market_condition_calls.append(kwargs)

    async def run_all_checks(self, **kwargs: Any) -> None:
        self.run_all_calls.append(kwargs)

    async def check_sector_and_correlation(self, **kwargs: Any) -> None:
        self.sector_calls.append(kwargs)

    async def check_kill_switch(self) -> None:
        self.kill_switch_checks += 1


@dataclass
class FakeAllocationDecision:
    status: str = "allocated"
    reason: str = "allocated"
    score: Decimal = Decimal("1")

    def to_payload(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


class AllowingSignalAllocator:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def new_state(self) -> object:
        return object()

    def allocate_one(self, *args: Any, **kwargs: Any) -> FakeAllocationDecision:
        self.calls.append((args, kwargs))
        return FakeAllocationDecision()


class FakeMarketIntelligenceMonitor:
    def __init__(self, _db: FakeSession) -> None:
        self.db = _db

    async def evaluate_and_alert(self) -> dict[str, Any]:
        return {"regime": {"regime": "test"}}


class FakeExitEngine:
    def __init__(self, _params: dict[str, Any]) -> None:
        self.params = _params

    def check_exit_conditions(
        self,
        _ticker: str,
        _state: Any,
        current_price: Decimal,
        _bars: list[Bar],
    ) -> SimpleNamespace:
        return SimpleNamespace(
            signal_type="take_profit",
            suggested_quantity=Decimal("-1"),
            stop_price=current_price - Decimal("5"),
            take_profit_price=current_price + Decimal("5"),
            confidence=Decimal("0.75"),
            reason="fake exit",
        )


@pytest.fixture(autouse=True)
def _reset_strategy_runner_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingTrading212Adapter.constructed.clear()
    RecordingTrading212Adapter.entered = 0
    RecordingTrading212Adapter.exited = 0
    RecordingExecutionEngine.brokers.clear()
    RecordingExecutionEngine.order_intents.clear()
    RecordingExecutionEngine.submitted_orders.clear()
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", RecordingTrading212Adapter)
    monkeypatch.setattr(strategy_runner, "ExecutionEngine", RecordingExecutionEngine)


def _connection(*, environment: str = "demo") -> FakeConnection:
    return FakeConnection(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        environment=environment,
        api_key_encrypted=f"encrypted-{environment}-key",
        api_secret_encrypted=f"encrypted-{environment}-secret",
    )


def _decrypt(value: str) -> str:
    return {
        "encrypted-demo-key": "decrypted-demo-key",
        "encrypted-demo-secret": "decrypted-demo-secret",
        "encrypted-live-key": "decrypted-live-key",
        "encrypted-live-secret": "decrypted-live-secret",
    }[value]


def _adapter_sentinel(*_args: Any, **_kwargs: Any) -> object:
    raise AssertionError("Trading212Adapter must not be constructed")


def _parse_strategy_runner() -> ast.Module:
    return ast.parse(STRATEGY_RUNNER_PATH.read_text(), filename=str(STRATEGY_RUNNER_PATH))


def _service_class() -> ast.ClassDef:
    return next(
        node
        for node in _parse_strategy_runner().body
        if isinstance(node, ast.ClassDef) and node.name == "StrategyRunner"
    )


def _method_node(name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    service = _service_class()
    return next(
        node
        for node in service.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    )


def _call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


def _adapter_counts(node: ast.AST) -> dict[str, int]:
    imports = 0
    constructs = 0
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom) and child.module == "app.broker.trading212":
            imports += sum(alias.name == "Trading212Adapter" for alias in child.names)
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "Trading212Adapter"
        ):
            constructs += 1
    return {"construct": constructs, "import": imports}


def _strategy(*, is_live: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Strategy Runner Provider Equivalence",
        type="orb",
        is_enabled=True,
        is_live=is_live,
        params={"session_open_utc": "14:30"},
        allowed_tickers=["AAPL"],
        venue="t212",
        last_signal_at=None,
    )


def _bars() -> list[Bar]:
    return [
        Bar(
            open=Decimal("100"),
            high=Decimal("106"),
            low=Decimal("99"),
            close=Decimal("105"),
            volume=Decimal("1000000"),
        )
        for _ in range(16)
    ]


def _bar_times() -> list[datetime]:
    return [datetime(2026, 1, 2, 14, minute, tzinfo=UTC) for minute in range(16)]


def _entry_signal() -> SimpleNamespace:
    return SimpleNamespace(
        side="buy",
        signal_type="entry",
        entry_price=Decimal("105"),
        stop_price=Decimal("100"),
        take_profit_price=Decimal("115"),
        suggested_quantity=Decimal("2"),
        confidence=Decimal("0.80"),
        reason="fake entry",
        params_snapshot={},
    )


class FakeEntryEngine:
    params: ClassVar[dict[str, str]] = {"session_open_utc": "14:30"}
    history_days: ClassVar[int] = 5
    max_history_bars: ClassVar[int] = 180
    required_bars: ClassVar[int] = 4
    DATA_PROVIDER_TYPE: ClassVar[str] = "equity"
    BAR_INTERVAL_MINUTES: ClassVar[int] = 5

    def __init__(self) -> None:
        self.generate_signal_calls: list[dict[str, Any]] = []

    def generate_signal(self, **kwargs: Any) -> SimpleNamespace:
        self.generate_signal_calls.append(kwargs)
        return _entry_signal()


async def _market_context(
    *_args: Any, **_kwargs: Any
) -> tuple[
    list[Bar],
    list[datetime],
    list[Bar],
    list[datetime],
    Decimal | None,
    str,
]:
    bars = _bars()
    times = _bar_times()
    return bars, times, bars, times, Decimal("99"), "14:45"


def test_strategy_runner_source_is_provider_backed_and_mixed_write_capable() -> None:
    tree = _parse_strategy_runner()
    service = _service_class()
    get_broker = _method_node("_get_broker")
    run_all_enabled = _method_node("run_all_enabled")
    process_ticker = _method_node("_process_ticker")
    check_exit = _method_node("_check_exit")
    source = STRATEGY_RUNNER_PATH.read_text()

    assert _adapter_counts(get_broker) == {"construct": 0, "import": 0}
    assert _adapter_counts(tree) == {"construct": 0, "import": 0}
    assert construction_inventory._trading212_adapter_references().get(
        "app/services/strategy_runner.py",
        {"construct": 0, "import": 0},
    ) == {"construct": 0, "import": 0}
    assert "create_trading212_provider_adapter" in source
    assert "app.broker.provider" in source
    assert "BrokerProviderRequest" in source
    assert "BrokerProviderCredentials" in source
    assert "worker_strategy_runner" in ast.unparse(get_broker)

    assert {"get_account_summary", "get_positions"} <= _call_names(run_all_enabled)
    assert {"create_order_intent", "submit_order"} <= _call_names(process_ticker)
    assert {"create_order_intent", "submit_order"} <= _call_names(check_exit)
    assert "strategy_order_placed" in ast.unparse(process_ticker)
    assert "strategy_exit_placed" in ast.unparse(check_exit)
    assert "ExecutionEngine" in ast.unparse(service)


@pytest.mark.asyncio
async def test_get_broker_mock_mode_returns_mock_adapter_without_trading212_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    db = FakeSession(results=[])
    service = StrategyRunner(db)

    broker = await service._get_broker()

    assert type(broker).__name__ == "MockBrokerAdapter"
    assert db.executed == []
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_preserves_no_active_connection_as_no_broker_without_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    service = StrategyRunner(FakeSession(results=[None]))

    broker = await service._get_broker()

    assert broker is None
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_marks_reconnect_required_on_decryption_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    db = FakeSession(results=[conn])
    reconnect_calls: list[tuple[FakeSession, FakeConnection, str, str]] = []
    service = StrategyRunner(db)

    def fail_decrypt(_value: str) -> str:
        raise CredentialDecryptionError("cannot decrypt strategy credentials")

    async def mark_reconnect(
        marked_db: FakeSession,
        marked_conn: FakeConnection,
        reason: str,
        *,
        actor: str,
    ) -> None:
        reconnect_calls.append((marked_db, marked_conn, reason, actor))

    # decrypt_field is imported locally inside StrategyRunner._get_broker, so patching
    # the source module works at call time. If it is refactored to a module-level
    # import, patch strategy_runner.decrypt_field instead.
    monkeypatch.setattr(security_module, "decrypt_field", fail_decrypt)
    monkeypatch.setattr(
        strategy_runner,
        "mark_broker_connection_reconnect_required",
        mark_reconnect,
    )
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)

    broker = await service._get_broker()

    assert broker is None
    assert reconnect_calls == [
        (
            db,
            conn,
            "cannot decrypt strategy credentials",
            "strategy_runner",
        )
    ]
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_policy_rejection_happens_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection(environment="live")
    db = FakeSession(results=[conn])
    service = StrategyRunner(db)
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(security_module, "decrypt_field", _decrypt)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    monkeypatch.setattr(
        strategy_runner,
        "create_trading212_provider_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not reach provider on policy rejection")
        ),
    )
    gate_calls: list[tuple[str, str]] = []

    def rejecting_gate(environment: str, *, action: str) -> None:
        gate_calls.append((environment, action))
        raise SafetyPolicyViolation("blocked by strategy test gate")

    monkeypatch.setattr(strategy_runner, "require_broker_environment", rejecting_gate)

    broker = await service._get_broker()

    assert broker is None
    assert gate_calls == [("live", "strategy runner broker access")]
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_calls_provider_after_lookup_decrypt_and_environment_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    conn = _connection(environment="live")
    db = FakeSession(results=[conn], events=events)
    service = StrategyRunner(db)
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)

    def decrypt(value: str) -> str:
        events.append(f"decrypt:{value}")
        return _decrypt(value)

    def gate(environment: str, *, action: str) -> None:
        events.append(f"gate:{environment}:{action}")

    provider_calls: list[dict[str, Any]] = []

    def provider_adapter(
        request: Any, credentials: Any, **kwargs: Any
    ) -> RecordingTrading212Adapter:
        events.append(f"provider:{request.environment}:{request.purpose}")
        provider_calls.append(
            {
                "request": request,
                "credentials": credentials,
                "kwargs": kwargs,
            }
        )
        return RecordingTrading212Adapter(
            credentials.api_key,
            credentials.api_secret,
            request.environment,
        )

    monkeypatch.setattr(security_module, "decrypt_field", decrypt)
    monkeypatch.setattr(strategy_runner, "require_broker_environment", gate)
    monkeypatch.setattr(strategy_runner, "create_trading212_provider_adapter", provider_adapter)

    broker = await service._get_broker()

    assert isinstance(broker, RecordingTrading212Adapter)
    assert events == [
        "db_execute",
        "decrypt:encrypted-live-key",
        "decrypt:encrypted-live-secret",
        "gate:live:strategy runner broker access",
        "provider:live:worker_strategy_runner",
    ]
    assert provider_calls[0]["request"].broker_id == "trading212"
    assert provider_calls[0]["request"].environment == "live"
    assert provider_calls[0]["request"].purpose == "worker_strategy_runner"
    assert provider_calls[0]["request"].user_id == conn.user_id
    assert provider_calls[0]["credentials"].api_key == "decrypted-live-key"
    assert provider_calls[0]["credentials"].api_secret == "decrypted-live-secret"
    assert provider_calls[0]["kwargs"] == {
        "app_mode": "live",
        "live_trading_enabled": True,
    }
    assert RecordingTrading212Adapter.constructed == [
        ("decrypted-live-key", "decrypted-live-secret", "live")
    ]


@pytest.mark.asyncio
async def test_get_broker_provider_validation_error_returns_none_and_logs_safe_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection(environment="demo")
    db = FakeSession(results=[conn])
    service = StrategyRunner(db)
    logged: list[tuple[str, dict[str, Any]]] = []

    class FakeLogger:
        def error(self, event: str, **kwargs: Any) -> None:
            logged.append((event, kwargs))

    def rejecting_provider(*_args: Any, **_kwargs: Any) -> object:
        raise strategy_runner.BrokerProviderValidationError("demo mode may only request demo")

    monkeypatch.setattr(security_module, "decrypt_field", _decrypt)
    monkeypatch.setattr(strategy_runner, "create_trading212_provider_adapter", rejecting_provider)
    monkeypatch.setattr(strategy_runner, "log", FakeLogger())

    broker = await service._get_broker()

    assert broker is None
    assert logged == [
        (
            "strategy_runner.provider_validation_error",
            {
                "reason": "demo mode may only request demo",
                "broker_id": "trading212",
                "environment": "demo",
                "purpose": "worker_strategy_runner",
                "user_id": str(conn.user_id),
            },
        )
    ]
    rendered_log = repr(logged)
    assert "decrypted-demo-key" not in rendered_log
    assert "decrypted-demo-secret" not in rendered_log
    assert conn.api_key_encrypted not in rendered_log
    assert conn.api_secret_encrypted not in rendered_log


@pytest.mark.asyncio
async def test_run_all_enabled_reads_account_and_positions_before_strategy_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _strategy()
    broker = RecordingBroker(
        account_summary={"total": Decimal("1200.25"), "free": Decimal("900.50")},
        positions=[{"ticker": "AAPL", "quantity": "1"}],
    )
    db = FakeSession(results=[FakeAppSettings(), [strategy]])
    service = StrategyRunner(db)
    observed: list[dict[str, Any]] = []

    async def get_broker() -> RecordingBroker:
        return broker

    async def run_strategy(**kwargs: Any) -> tuple[int, int, int]:
        observed.append(kwargs)
        return 1, 0, 0

    monkeypatch.setattr(service, "_get_broker", get_broker)
    monkeypatch.setattr(service, "_run_strategy", run_strategy)
    monkeypatch.setattr(strategy_runner, "MarketIntelligenceMonitor", FakeMarketIntelligenceMonitor)
    monkeypatch.setattr(strategy_runner, "alert_daily_summary", lambda *_args, **_kwargs: None)

    summary = await service.run_all_enabled()

    assert broker.read_calls == ["get_account_summary", "get_positions"]
    assert broker.write_calls == []
    assert observed[0]["broker"] is broker
    assert observed[0]["cash"] == Decimal("900.50")
    assert observed[0]["total"] == Decimal("1200.25")
    assert observed[0]["pos_map"] == {"AAPL": {"ticker": "AAPL", "quantity": "1"}}
    assert summary == {
        "strategies_run": 1,
        "signals_generated": 1,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_settings", "expected_reason"),
    [
        (FakeAppSettings(kill_switch_active=True), "kill_switch"),
        (FakeAppSettings(auto_trading_enabled=False), "auto_trading_off"),
    ],
)
async def test_run_all_enabled_safety_gates_skip_before_broker_lookup(
    monkeypatch: pytest.MonkeyPatch,
    app_settings: FakeAppSettings,
    expected_reason: str,
) -> None:
    db = FakeSession(results=[app_settings])
    service = StrategyRunner(db)

    async def get_broker() -> RecordingBroker:
        raise AssertionError("run_all_enabled safety gate must skip before broker lookup")

    monkeypatch.setattr(service, "_get_broker", get_broker)

    summary = await service.run_all_enabled()

    assert summary == {
        "strategies_run": 0,
        "signals_generated": 0,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
        "skipped": expected_reason,
    }
    assert RecordingTrading212Adapter.constructed == []
    assert RecordingExecutionEngine.order_intents == []
    assert RecordingExecutionEngine.submitted_orders == []


@pytest.mark.asyncio
async def test_run_all_enabled_live_not_unlocked_skips_before_broker_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "live")
    db = FakeSession(results=[FakeAppSettings(live_trading_unlocked=False)])
    service = StrategyRunner(db)

    async def get_broker() -> RecordingBroker:
        raise AssertionError("live unlock gate must skip before broker lookup")

    async def run_strategy(**_kwargs: Any) -> tuple[int, int, int]:
        raise AssertionError("live unlock gate must skip before strategy execution")

    monkeypatch.setattr(service, "_get_broker", get_broker)
    monkeypatch.setattr(service, "_run_strategy", run_strategy)

    summary = await service.run_all_enabled()

    assert summary == {
        "strategies_run": 0,
        "signals_generated": 0,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
        "skipped": "live_not_unlocked",
    }
    assert RecordingTrading212Adapter.constructed == []
    assert RecordingExecutionEngine.order_intents == []
    assert RecordingExecutionEngine.submitted_orders == []


@pytest.mark.asyncio
async def test_run_all_enabled_demo_is_not_blocked_by_live_unlock_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    broker = RecordingBroker()
    db = FakeSession(results=[FakeAppSettings(live_trading_unlocked=False), []])
    service = StrategyRunner(db)
    broker_calls = 0

    async def get_broker() -> RecordingBroker:
        nonlocal broker_calls
        broker_calls += 1
        return broker

    monkeypatch.setattr(service, "_get_broker", get_broker)

    summary = await service.run_all_enabled()

    assert summary == {
        "strategies_run": 0,
        "signals_generated": 0,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
    }
    # The enabled strategy list is empty, so broker lookup is unnecessary after
    # demo mode passes the live-unlock gate.
    assert broker_calls == 0


@pytest.mark.asyncio
async def test_process_ticker_dry_run_persists_signal_without_execution_engine_or_broker_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession(results=[])
    service = StrategyRunner(db)
    strategy = _strategy(is_live=False)
    engine = FakeEntryEngine()
    broker = RecordingBroker()
    risk = AllowingRiskEngine()
    allocator = AllowingSignalAllocator()
    monkeypatch.setattr(service, "_fetch_market_context", _market_context)

    result = await service._process_ticker(
        ticker="AAPL",
        strategy=strategy,
        engine=engine,
        risk=risk,
        broker=broker,
        cash=Decimal("1000"),
        total=Decimal("1500"),
        n_open=0,
        pos_map={},
        all_positions=[],
        intelligence={"regime": {"regime": "test"}},
        allocator=allocator,
        allocation_state=object(),
    )

    assert result == (1, 0, 0)
    assert broker.read_calls == []
    assert broker.write_calls == []
    assert RecordingExecutionEngine.brokers == []
    assert RecordingExecutionEngine.order_intents == []
    assert RecordingExecutionEngine.submitted_orders == []
    assert db.flushed == 1
    assert db.added[0].ticker == "AAPL"
    assert db.added[0].status == "approved"
    assert strategy.last_signal_at is not None


@pytest.mark.asyncio
async def test_process_ticker_live_entry_routes_order_through_execution_engine_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession(results=[Decimal("0")])
    service = StrategyRunner(db)
    strategy = _strategy(is_live=True)
    engine = FakeEntryEngine()
    broker = RecordingBroker()
    risk = AllowingRiskEngine()
    allocator = AllowingSignalAllocator()
    monkeypatch.setattr(service, "_fetch_market_context", _market_context)

    result = await service._process_ticker(
        ticker="AAPL",
        strategy=strategy,
        engine=engine,
        risk=risk,
        broker=broker,
        cash=Decimal("1000"),
        total=Decimal("1500"),
        n_open=0,
        pos_map={},
        all_positions=[],
        intelligence={"regime": {"regime": "test"}},
        allocator=allocator,
        allocation_state=object(),
    )

    assert result == (1, 1, 0)
    assert broker.read_calls == []
    assert broker.write_calls == []
    assert RecordingExecutionEngine.brokers == [broker]
    assert RecordingExecutionEngine.order_intents == [
        {
            "ticker": "AAPL",
            "side": "buy",
            "order_type": "limit",
            "quantity": Decimal("2"),
            "signal_id": db.added[0].id,
            "is_dry_run": False,  # APP_MODE="demo" in this fixture, not "mock"
            "available_cash": Decimal("1000"),
            "estimated_price": Decimal("105"),
            "limit_price": Decimal("105.10"),
            "venue": "t212",
        }
    ]
    assert RecordingExecutionEngine.submitted_orders[0].ticker == "AAPL"
    assert RecordingExecutionEngine.submitted_orders[0].side == "buy"
    assert [entry.action for entry in db.added if hasattr(entry, "action")] == [
        "strategy_order_placed"
    ]
    assert risk.run_all_calls and risk.sector_calls


@pytest.mark.asyncio
async def test_process_ticker_existing_position_routes_exit_path_without_entry_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession(results=[])
    service = StrategyRunner(db)
    strategy = _strategy(is_live=True)
    engine = FakeEntryEngine()
    broker = RecordingBroker()
    risk = AllowingRiskEngine()
    monkeypatch.setattr(service, "_fetch_market_context", _market_context)
    exit_calls: list[dict[str, Any]] = []

    async def check_exit(**kwargs: Any) -> int:
        exit_calls.append(kwargs)
        return 1

    monkeypatch.setattr(service, "_check_exit", check_exit)

    result = await service._process_ticker(
        ticker="AAPL",
        strategy=strategy,
        engine=engine,
        risk=risk,
        broker=broker,
        cash=Decimal("1000"),
        total=Decimal("1500"),
        n_open=1,
        pos_map={"AAPL": {"ticker": "AAPL", "quantity": "2", "averagePrice": "100"}},
        all_positions=[{"ticker": "AAPL", "quantity": "2", "averagePrice": "100"}],
        intelligence={"regime": {"regime": "test"}},
        allocator=AllowingSignalAllocator(),
        allocation_state=object(),
    )

    assert result == (1, 1, 0)
    assert exit_calls[0]["broker"] is broker
    assert exit_calls[0]["pos_qty"] == Decimal("2")
    assert engine.generate_signal_calls == []
    assert broker.write_calls == []
    assert RecordingExecutionEngine.order_intents == []


@pytest.mark.asyncio
async def test_check_exit_dry_run_preserves_no_submit_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_signal = SimpleNamespace(
        id=uuid.uuid4(),
        stop_price=Decimal("95"),
        take_profit_price=Decimal("110"),
    )
    db = FakeSession(results=[last_signal, None])
    service = StrategyRunner(db)
    broker = RecordingBroker()
    risk = AllowingRiskEngine()
    monkeypatch.setattr(strategy_runner, "OpeningRangeBreakoutStrategy", FakeExitEngine)

    result = await service._check_exit(
        ticker="AAPL",
        strategy=_strategy(is_live=False),
        bars=_bars(),
        pos_qty=Decimal("2"),
        avg_price=Decimal("100"),
        max_sell=Decimal("1"),
        broker=broker,
        risk=risk,
    )

    assert result == 0
    assert broker.read_calls == []
    assert broker.write_calls == []
    assert risk.kill_switch_checks == 0
    assert RecordingExecutionEngine.order_intents == []
    assert RecordingExecutionEngine.submitted_orders == []


@pytest.mark.asyncio
async def test_check_exit_live_routes_sell_order_through_execution_engine_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_signal = SimpleNamespace(
        id=uuid.uuid4(),
        stop_price=Decimal("95"),
        take_profit_price=Decimal("110"),
    )
    db = FakeSession(results=[last_signal, None])
    service = StrategyRunner(db)
    broker = RecordingBroker()
    risk = AllowingRiskEngine()
    monkeypatch.setattr(strategy_runner, "OpeningRangeBreakoutStrategy", FakeExitEngine)

    result = await service._check_exit(
        ticker="AAPL",
        strategy=_strategy(is_live=True),
        bars=_bars(),
        pos_qty=Decimal("2"),
        avg_price=Decimal("100"),
        max_sell=Decimal("1"),
        broker=broker,
        risk=risk,
    )

    assert result == 1
    assert broker.read_calls == []
    assert broker.write_calls == []
    assert risk.kill_switch_checks == 1
    assert RecordingExecutionEngine.brokers == [broker]
    assert RecordingExecutionEngine.order_intents == [
        {
            "ticker": "AAPL",
            "side": "sell",
            "order_type": "market",
            "quantity": Decimal("1"),
            "signal_id": last_signal.id,
            "is_dry_run": False,  # APP_MODE="demo" in this fixture, not "mock"
            "estimated_price": Decimal("105"),
            "venue": "t212",
        }
    ]
    assert RecordingExecutionEngine.submitted_orders[0].side == "sell"
    assert [entry.action for entry in db.added if hasattr(entry, "action")] == [
        "strategy_exit_placed"
    ]
