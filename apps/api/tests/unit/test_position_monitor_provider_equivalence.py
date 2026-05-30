from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from app.core import security as security_module
from app.core.config import settings
from app.core.security import CredentialDecryptionError
from app.services import position_monitor
from app.services.position_monitor import PositionMonitor
from app.services.safety_policy import SafetyPolicyViolation
from app.strategies.indicators import Bar
from tests.unit import test_trading212_construction_inventory as construction_inventory

API_ROOT = Path(__file__).resolve().parents[2]
POSITION_MONITOR_PATH = API_ROOT / "app" / "services" / "position_monitor.py"


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
        if self.value is None:
            raise AssertionError("scalar_one() got None; fake query ordering is wrong")
        if isinstance(self.value, list):
            raise AssertionError("scalar_one() received a list; fake query ordering is wrong")
        return self.value

    def scalars(self) -> ScalarResult:
        return ScalarResult(self.value)


class FakeSession:
    def __init__(self, results: list[Any], events: list[str] | None = None) -> None:
        self.results = results
        self.events = events
        self.executed: list[Any] = []
        self.added: list[Any] = []
        self.committed = 0

    async def execute(self, statement: Any) -> ExecuteResult:
        if self.events is not None:
            self.events.append("db_execute")
        self.executed.append(statement)
        if not self.results:
            raise AssertionError("unexpected execute call")
        return ExecuteResult(self.results.pop(0))

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed += 1


@dataclass
class FakeAppSettings:
    auto_trading_enabled: bool = True
    kill_switch_active: bool = False


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

    # PositionMonitor may enter the same broker sequentially for initial reads,
    # daily-loss checks, and per-position order-routing.
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


class FakeExitEngine:
    def __init__(self, _params: dict[str, Any] | None = None) -> None:
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
            reason="fake monitor exit",
        )


class RecordingAlertService:
    sent: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, _db: FakeSession) -> None:
        self.db = _db

    async def send(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


@pytest.fixture(autouse=True)
def _reset_position_monitor_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingTrading212Adapter.constructed.clear()
    RecordingTrading212Adapter.entered = 0
    RecordingTrading212Adapter.exited = 0
    RecordingExecutionEngine.brokers.clear()
    RecordingExecutionEngine.order_intents.clear()
    RecordingExecutionEngine.submitted_orders.clear()
    RecordingAlertService.sent.clear()
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", RecordingTrading212Adapter)
    monkeypatch.setattr(position_monitor, "ExecutionEngine", RecordingExecutionEngine)
    monkeypatch.setattr(position_monitor, "AlertService", RecordingAlertService)


def _connection(*, environment: str = "demo") -> FakeConnection:
    return FakeConnection(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        environment=environment,
        api_key_encrypted=f"encrypted-{environment}-key",
        api_secret_encrypted=f"encrypted-{environment}-secret",
    )


def _decrypt(value: str) -> str:
    values = {
        "encrypted-demo-key": "decrypted-demo-key",
        "encrypted-demo-secret": "decrypted-demo-secret",
        "encrypted-live-key": "decrypted-live-key",
        "encrypted-live-secret": "decrypted-live-secret",
    }
    if value not in values:
        raise AssertionError(f"unexpected fake ciphertext: {value}")
    return values[value]


def _adapter_sentinel(*_args: Any, **_kwargs: Any) -> object:
    raise AssertionError("Trading212Adapter must not be constructed")


def _parse_position_monitor() -> ast.Module:
    return ast.parse(POSITION_MONITOR_PATH.read_text(), filename=str(POSITION_MONITOR_PATH))


def _service_class() -> ast.ClassDef:
    return next(
        node
        for node in _parse_position_monitor().body
        if isinstance(node, ast.ClassDef) and node.name == "PositionMonitor"
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


def _last_entry_signal(strategy_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        strategy_id=strategy_id or uuid.uuid4(),
        stop_price=Decimal("95"),
        take_profit_price=Decimal("110"),
    )


def _strategy(strategy_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(id=strategy_id, is_enabled=True, is_live=True, params={})


def test_position_monitor_source_remains_direct_provider_unwired_and_write_capable() -> None:
    tree = _parse_position_monitor()
    service = _service_class()
    get_broker = _method_node("_get_broker")
    run = _method_node("run")
    check_daily_loss = _method_node("_check_daily_loss_with_unrealized")
    monitor_position = _method_node("_monitor_position")
    eod_flatten = _method_node("eod_flatten")
    source = POSITION_MONITOR_PATH.read_text()

    assert _adapter_counts(get_broker) == {"construct": 1, "import": 1}
    assert _adapter_counts(tree) == {"construct": 1, "import": 1}
    assert construction_inventory._trading212_adapter_references()[
        "app/services/position_monitor.py"
    ] == {"construct": 1, "import": 1}
    assert "create_trading212_provider_adapter" not in source
    assert "app.broker.provider" not in source
    assert "BrokerProviderRequest" not in source
    assert "BrokerProviderCredentials" not in source

    assert {"get_positions", "get_account_summary"} <= _call_names(run)
    assert "get_positions" in _call_names(check_daily_loss)
    assert {"create_order_intent", "submit_order"} <= _call_names(monitor_position)
    assert {"get_positions", "create_order_intent", "submit_order"} <= _call_names(eod_flatten)
    assert "position_exit_automated" in ast.unparse(monitor_position)
    assert "eod_flatten_executed" in ast.unparse(eod_flatten)
    assert "ExecutionEngine" in ast.unparse(service)


@pytest.mark.asyncio
async def test_get_broker_mock_mode_returns_mock_adapter_without_trading212_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    db = FakeSession(results=[])
    service = PositionMonitor(db)

    broker = await service._get_broker()

    assert type(broker).__name__ == "MockBrokerAdapter"
    assert db.executed == []
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_preserves_no_active_connection_as_no_broker_without_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    service = PositionMonitor(FakeSession(results=[None]))

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
    service = PositionMonitor(db)

    def fail_decrypt(_value: str) -> str:
        raise CredentialDecryptionError("cannot decrypt position monitor credentials")

    async def mark_reconnect(
        marked_db: FakeSession,
        marked_conn: FakeConnection,
        reason: str,
        *,
        actor: str,
    ) -> None:
        reconnect_calls.append((marked_db, marked_conn, reason, actor))

    # decrypt_field is imported inside PositionMonitor._get_broker, so each call
    # re-fetches the function from the source security module.
    monkeypatch.setattr(security_module, "decrypt_field", fail_decrypt)
    monkeypatch.setattr(
        position_monitor,
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
            "cannot decrypt position monitor credentials",
            "position_monitor",
        )
    ]
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_policy_rejection_happens_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection(environment="live")
    db = FakeSession(results=[conn])
    service = PositionMonitor(db)
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(security_module, "decrypt_field", _decrypt)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    gate_calls: list[tuple[str, str]] = []
    reconnect_calls: list[str] = []

    def rejecting_gate(environment: str, *, action: str) -> None:
        gate_calls.append((environment, action))
        raise SafetyPolicyViolation("blocked by position monitor test gate")

    async def mark_reconnect(*_args: Any, **_kwargs: Any) -> None:
        reconnect_calls.append("called")

    monkeypatch.setattr(position_monitor, "require_broker_environment", rejecting_gate)
    monkeypatch.setattr(
        position_monitor,
        "mark_broker_connection_reconnect_required",
        mark_reconnect,
    )

    broker = await service._get_broker()

    assert broker is None
    assert gate_calls == [("live", "position monitor broker access")]
    assert RecordingTrading212Adapter.constructed == []
    assert reconnect_calls == []
    assert "create_trading212_provider_adapter" not in POSITION_MONITOR_PATH.read_text()


@pytest.mark.asyncio
async def test_get_broker_constructs_direct_adapter_after_lookup_decrypt_and_environment_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    conn = _connection(environment="live")
    db = FakeSession(results=[conn], events=events)
    service = PositionMonitor(db)
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)

    def decrypt(value: str) -> str:
        events.append(f"decrypt:{value}")
        return _decrypt(value)

    def gate(environment: str, *, action: str) -> None:
        events.append(f"gate:{environment}:{action}")

    original_init = RecordingTrading212Adapter.__init__

    def adapter_init(
        self: RecordingTrading212Adapter,
        api_key: str,
        api_secret: str,
        environment: str,
    ) -> None:
        events.append(f"adapter:{environment}")
        original_init(self, api_key, api_secret, environment)

    monkeypatch.setattr(security_module, "decrypt_field", decrypt)
    monkeypatch.setattr(position_monitor, "require_broker_environment", gate)
    monkeypatch.setattr(RecordingTrading212Adapter, "__init__", adapter_init)

    broker = await service._get_broker()

    assert isinstance(broker, RecordingTrading212Adapter)
    assert events == [
        "db_execute",
        "decrypt:encrypted-live-key",
        "decrypt:encrypted-live-secret",
        "gate:live:position monitor broker access",
        "adapter:live",
    ]
    assert RecordingTrading212Adapter.constructed == [
        ("decrypted-live-key", "decrypted-live-secret", "live")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_settings", "expected_reason"),
    [
        (FakeAppSettings(kill_switch_active=True), "kill_switch_active"),
        (FakeAppSettings(auto_trading_enabled=False), "auto_trading_disabled"),
    ],
)
async def test_run_safety_gates_skip_before_broker_lookup(
    monkeypatch: pytest.MonkeyPatch,
    app_settings: FakeAppSettings,
    expected_reason: str,
) -> None:
    db = FakeSession(results=[app_settings])
    service = PositionMonitor(db)

    async def get_broker() -> RecordingBroker:
        raise AssertionError("run safety gate must skip before broker lookup")

    monkeypatch.setattr(service, "_get_broker", get_broker)

    summary = await service.run()

    assert summary == {
        "positions_checked": 0,
        "exits_submitted": 0,
        "partial_exits": 0,
        "stops_hit": 0,
        "take_profits": 0,
        "errors": [],
        "skipped": expected_reason,
    }
    assert RecordingExecutionEngine.order_intents == []
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_run_reads_positions_and_account_then_routes_exit_through_execution_engine_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_id = uuid.uuid4()
    position = {
        "ticker": "AAPL",
        "quantity": "2",
        "maxSell": "1",
        "averagePrice": "100",
        "currentPrice": "105",
    }
    broker = RecordingBroker(
        account_summary={"total": Decimal("2500"), "free": Decimal("1500")},
        positions=[position],
    )
    last_signal = _last_entry_signal(strategy_id)
    db = FakeSession(
        results=[
            FakeAppSettings(),  # 1. _get_settings()
            None,  # 2. RiskProfile query; None skips daily-loss realized-P&L query
            [_strategy(strategy_id)],  # 3. live strategies for AAPL
            last_signal,  # 4. last executed entry signal
            None,  # 5. partial-exit signal
        ]
    )
    service = PositionMonitor(db)

    async def get_broker() -> RecordingBroker:
        return broker

    async def market_data(_ticker: str) -> tuple[list[Bar], Decimal]:
        return _bars(), Decimal("105")

    monkeypatch.setattr(service, "_get_broker", get_broker)
    monkeypatch.setattr(service, "_get_market_data", market_data)
    monkeypatch.setattr(position_monitor, "OpeningRangeBreakoutStrategy", FakeExitEngine)

    summary = await service.run()

    assert broker.read_calls == ["get_positions", "get_account_summary"]
    assert broker.write_calls == []
    assert RecordingExecutionEngine.brokers == [broker]
    assert RecordingExecutionEngine.order_intents == [
        {
            "ticker": "AAPL",
            "side": "sell",
            "order_type": "market",
            "quantity": Decimal("1"),
            "signal_id": last_signal.id,
            "is_dry_run": False,
            "estimated_price": Decimal("105"),
        }
    ]
    assert RecordingExecutionEngine.submitted_orders[0].side == "sell"
    assert [entry.action for entry in db.added if hasattr(entry, "action")] == [
        "position_exit_automated"
    ]
    assert RecordingAlertService.sent[0]["alert_type"] == "take_profit"
    assert db.committed == 1
    assert summary == {
        "positions_checked": 1,
        "exits_submitted": 1,
        "partial_exits": 0,
        "stops_hit": 0,
        "take_profits": 1,
        "errors": [],
    }


@pytest.mark.asyncio
async def test_monitor_position_dry_run_flag_is_currently_app_mode_mock_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_id = uuid.uuid4()
    last_signal = _last_entry_signal(strategy_id)
    db = FakeSession(results=[last_signal, None])
    service = PositionMonitor(db)
    broker = RecordingBroker()
    monkeypatch.setattr(settings, "APP_MODE", "mock")

    async def market_data(_ticker: str) -> tuple[list[Bar], Decimal]:
        return _bars(), Decimal("105")

    monkeypatch.setattr(service, "_get_market_data", market_data)
    monkeypatch.setattr(position_monitor, "OpeningRangeBreakoutStrategy", FakeExitEngine)

    result = await service._monitor_position(
        ticker="AAPL",
        pos_qty=Decimal("2"),
        pos_data={"averagePrice": "100", "maxSell": "1"},
        broker=broker,
        strategies=[_strategy(strategy_id)],
        account_value=Decimal("2500"),
    )

    assert result == {"exits": 1, "partial": 0, "stops": 0, "tps": 1}
    assert broker.read_calls == []
    assert broker.write_calls == []
    assert RecordingExecutionEngine.order_intents[0]["is_dry_run"] is True
    assert RecordingExecutionEngine.submitted_orders[0].is_dry_run is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_mode", "expected_dry_run"),
    [("demo", False), ("mock", True)],
)
async def test_eod_flatten_reads_positions_and_routes_sell_orders_through_execution_engine_only(
    monkeypatch: pytest.MonkeyPatch,
    app_mode: str,
    expected_dry_run: bool,
) -> None:
    positions = [
        {"ticker": "AAPL", "quantity": "2", "currentPrice": "170.25"},
        {"ticker": "MSFT", "quantity": "0", "currentPrice": "300"},
        {"ticker": "TSLA", "quantity": "-1", "currentPrice": "250"},
    ]
    broker = RecordingBroker(positions=positions)
    db = FakeSession(results=[])
    service = PositionMonitor(db)
    monkeypatch.setattr(settings, "APP_MODE", app_mode)

    async def get_broker() -> RecordingBroker:
        return broker

    monkeypatch.setattr(service, "_get_broker", get_broker)

    summary = await service.eod_flatten()

    assert summary == {"flattened": 1}
    assert broker.read_calls == ["get_positions"]
    assert broker.write_calls == []
    assert RecordingExecutionEngine.brokers == [broker]
    assert RecordingExecutionEngine.order_intents == [
        {
            "ticker": "AAPL",
            "side": "sell",
            "order_type": "market",
            "quantity": Decimal("2"),
            "is_dry_run": expected_dry_run,
            "estimated_price": Decimal("170.25"),
        }
    ]
    assert RecordingExecutionEngine.submitted_orders[0].side == "sell"
    assert RecordingExecutionEngine.submitted_orders[0].is_dry_run is expected_dry_run
    assert [entry.action for entry in db.added if hasattr(entry, "action")] == [
        "eod_flatten_executed"
    ]
    # Runtime commits the audit record after routing flatten orders through the engine.
    assert db.committed == 1
