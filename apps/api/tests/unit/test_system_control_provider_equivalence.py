from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from sqlalchemy.sql import visitors

from app.core.config import settings
from app.core.security import CredentialDecryptionError
from app.db.models import BrokerConnection
from app.services import system_control
from app.services.system_control import SystemControlError, SystemControlService
from tests.unit import test_trading212_construction_inventory as construction_inventory

API_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_CONTROL_PATH = API_ROOT / "app" / "services" / "system_control.py"


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


@dataclass(frozen=True)
class FakeOrder:
    id: uuid.UUID
    ticker: str = "AAPL"


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


class RecordingBroker:
    environment = "demo"

    def __init__(
        self,
        *,
        account_summary: dict[str, Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
        fail_on_write: bool = True,
    ) -> None:
        self.account_summary = account_summary or {
            "free": Decimal("1000.50"),
            "total": Decimal("1250.75"),
            "invested": Decimal("250.25"),
            "result": Decimal("12.34"),
        }
        self.positions = positions or [{"ticker": "AAPL", "quantity": 2}]
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []
        self.entered = 0
        self.exited = 0
        self.fail_on_write = fail_on_write

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
            if self.fail_on_write:
                raise AssertionError(f"unexpected broker write method: {name}")
        raise AttributeError(name)


class RecordingExecutionEngine:
    # Class-level call records are cleared by the autouse fixture before every test.
    brokers: ClassVar[list[RecordingBroker]] = []
    cancel_calls: ClassVar[list[FakeOrder]] = []
    order_intents: ClassVar[list[dict[str, Any]]] = []
    submitted_orders: ClassVar[list[Any]] = []

    def __init__(self, _db: FakeSession, broker: RecordingBroker) -> None:
        self.broker = broker
        self.brokers.append(broker)

    async def cancel_order(self, order: FakeOrder) -> None:
        self.cancel_calls.append(order)

    async def create_order_intent(self, **kwargs: Any) -> dict[str, Any]:
        self.order_intents.append(kwargs)
        return {"order": kwargs["ticker"]}

    async def submit_order(self, order: Any) -> None:
        self.submitted_orders.append(order)


@pytest.fixture(autouse=True)
def _reset_system_control_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingTrading212Adapter.constructed.clear()
    RecordingTrading212Adapter.entered = 0
    RecordingTrading212Adapter.exited = 0
    RecordingExecutionEngine.brokers.clear()
    RecordingExecutionEngine.cancel_calls.clear()
    RecordingExecutionEngine.order_intents.clear()
    RecordingExecutionEngine.submitted_orders.clear()
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", RecordingTrading212Adapter)
    monkeypatch.setattr(system_control, "ExecutionEngine", RecordingExecutionEngine)


def _connection(*, environment: str = "demo", user_id: uuid.UUID | None = None) -> FakeConnection:
    return FakeConnection(
        id=uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
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


def _broker_provider(broker: RecordingBroker) -> Any:
    # SystemControlService currently awaits _get_broker(), then enters the returned broker.
    async def get_broker() -> RecordingBroker:
        return broker

    return get_broker


def _statement_has_user_id_filter(statement: Any, user_id: uuid.UUID) -> bool:
    found = False

    def visit_binary(binary: Any) -> None:
        nonlocal found
        left = getattr(binary, "left", None)
        right = getattr(binary, "right", None)
        if (
            getattr(getattr(left, "table", None), "name", None) == BrokerConnection.__tablename__
            and getattr(left, "name", None) == "user_id"
            and str(getattr(right, "value", "")) == str(user_id)
        ):
            found = True

    visitors.traverse(statement, {}, {"binary": visit_binary})
    return found


def _parse_system_control() -> ast.Module:
    return ast.parse(SYSTEM_CONTROL_PATH.read_text(), filename=str(SYSTEM_CONTROL_PATH))


def _method_node(name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    tree = _parse_system_control()
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SystemControlService"
    )
    method = next(
        node
        for node in service.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    )
    return method


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


@pytest.mark.asyncio
async def test_get_broker_mock_mode_returns_mock_adapter_without_trading212_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    service = SystemControlService(FakeSession(results=[]))

    broker = await service._get_broker()

    assert type(broker).__name__ == "MockBrokerAdapter"
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_rejects_unsafe_app_mode_before_connection_or_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "paper")
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    db = FakeSession(results=[])
    service = SystemControlService(db)

    with pytest.raises(SystemControlError, match="APP_MODE=paper"):
        await service._get_broker()

    assert db.executed == []
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_snapshot_returns_existing_disconnected_summary_when_no_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMarketRegimeService:
        async def evaluate(self) -> dict[str, str]:
            return {"regime": "quiet"}

    monkeypatch.setattr(system_control, "MarketRegimeService", FakeMarketRegimeService)
    db = FakeSession(results=[FakeAppSettings(), [], None])
    service = SystemControlService(db)

    snapshot = await service.get_snapshot()

    assert snapshot == {
        "mode": "demo",
        "auto_trading_enabled": True,
        "kill_switch_active": False,
        "pending_orders": 0,
        "broker_status": "not_connected",
        "account": None,
        "positions": [],
        "regime": {"regime": "quiet"},
    }
    assert db.results == []
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_marks_reconnect_required_and_commits_on_decrypt_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    db = FakeSession(results=[conn])
    reconnect_calls: list[tuple[FakeSession, FakeConnection, str, str, bool]] = []
    service = SystemControlService(db)

    def fail_decrypt(_value: str) -> str:
        raise CredentialDecryptionError("cannot decrypt system-control credentials")

    async def mark_reconnect(
        marked_db: FakeSession,
        marked_conn: FakeConnection,
        reason: str,
        *,
        actor: str,
        commit: bool,
    ) -> None:
        reconnect_calls.append((marked_db, marked_conn, reason, actor, commit))

    monkeypatch.setattr(system_control, "decrypt_field", fail_decrypt)
    monkeypatch.setattr(system_control, "mark_broker_connection_reconnect_required", mark_reconnect)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)

    with pytest.raises(SystemControlError, match="cannot decrypt system-control credentials"):
        await service._get_broker()

    assert reconnect_calls == [
        (
            db,
            conn,
            "cannot decrypt system-control credentials",
            "system_control",
            True,
        )
    ]
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_constructs_direct_adapter_only_after_all_gates_and_decryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    conn = _connection(environment="live")
    db = FakeSession(results=[conn], events=events)
    service = SystemControlService(db)
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)

    gate_calls = 0

    def gate(environment: str, *, action: str) -> None:
        nonlocal gate_calls
        gate_calls += 1
        events.append(f"gate{gate_calls}:{environment}:{action}")

    def decrypt(value: str) -> str:
        events.append(f"decrypt:{value}")
        return _decrypt(value)

    original_init = RecordingTrading212Adapter.__init__

    def adapter_init(
        self: RecordingTrading212Adapter, api_key: str, api_secret: str, environment: str
    ) -> None:
        events.append(f"adapter:{environment}")
        original_init(self, api_key, api_secret, environment)

    monkeypatch.setattr(system_control, "require_broker_environment", gate)
    monkeypatch.setattr(system_control, "decrypt_field", decrypt)
    monkeypatch.setattr(RecordingTrading212Adapter, "__init__", adapter_init)

    broker = await service._get_broker()

    assert isinstance(broker, RecordingTrading212Adapter)
    assert events == [
        "gate1:live:system control broker access",
        "db_execute",
        "gate2:live:system control broker access",
        "decrypt:encrypted-live-key",
        "decrypt:encrypted-live-secret",
        "adapter:live",
    ]
    assert RecordingTrading212Adapter.constructed == [
        ("decrypted-live-key", "decrypted-live-secret", "live")
    ]


@pytest.mark.asyncio
async def test_get_broker_scopes_active_connection_lookup_by_broker_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    conn = _connection(user_id=user_id)
    db = FakeSession(results=[conn])
    service = SystemControlService(db, broker_user_id=user_id)
    monkeypatch.setattr(system_control, "decrypt_field", _decrypt)

    await service._get_broker()

    assert _statement_has_user_id_filter(db.executed[0], user_id)
    assert RecordingTrading212Adapter.constructed == [
        ("decrypted-demo-key", "decrypted-demo-secret", "demo")
    ]


@pytest.mark.asyncio
async def test_get_snapshot_uses_read_methods_only_and_preserves_summary_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMarketRegimeService:
        async def evaluate(self) -> dict[str, str]:
            return {"regime": "ranging", "detail": "Stable tape."}

    broker = RecordingBroker()
    db = FakeSession(results=[FakeAppSettings(), [FakeOrder(id=uuid.uuid4())]])
    service = SystemControlService(db)
    monkeypatch.setattr(system_control, "MarketRegimeService", FakeMarketRegimeService)
    monkeypatch.setattr(service, "_get_broker", _broker_provider(broker))

    snapshot = await service.get_snapshot()

    assert broker.read_calls == ["get_account_summary", "get_positions"]
    assert broker.write_calls == []
    assert snapshot["broker_status"] == "connected"
    assert snapshot["pending_orders"] == 1
    assert snapshot["account"] == {
        "free_cash": 1000.5,
        "total_value": 1250.75,
        "invested": 250.25,
        "result": 12.34,
    }
    assert snapshot["positions"] == [{"ticker": "AAPL", "quantity": 2}]


@pytest.mark.asyncio
async def test_get_positions_summary_uses_positions_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = RecordingBroker(positions=[{"ticker": "MSFT", "quantity": 3}])
    service = SystemControlService(FakeSession(results=[]))
    monkeypatch.setattr(service, "_get_broker", _broker_provider(broker))

    positions = await service.get_positions_summary()

    assert positions == [{"ticker": "MSFT", "quantity": 3}]
    assert broker.read_calls == ["get_positions"]
    assert broker.write_calls == []


@pytest.mark.asyncio
async def test_cancel_all_pending_uses_shared_broker_boundary_and_cancels_selected_orders_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_orders = [FakeOrder(id=uuid.uuid4()), FakeOrder(id=uuid.uuid4(), ticker="MSFT")]
    broker = RecordingBroker()
    db = FakeSession(results=[selected_orders])
    service = SystemControlService(db)
    monkeypatch.setattr(service, "_get_broker", _broker_provider(broker))

    message = await service.cancel_all_pending(actor="operator")

    assert message == "Cancelled 2 pending orders."
    assert broker.read_calls == []
    assert broker.write_calls == []
    assert RecordingExecutionEngine.brokers == [broker]
    assert RecordingExecutionEngine.cancel_calls == selected_orders
    assert RecordingExecutionEngine.order_intents == []
    assert [entry.action for entry in db.added] == ["emergency_cancel_all"]
    assert db.added[0].payload == {"source": "system_control", "cancelled_count": 2}


@pytest.mark.asyncio
async def test_flatten_all_uses_shared_broker_boundary_and_submits_sell_intents_only_for_long_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = [
        {"ticker": "AAPL", "quantity": "2", "currentPrice": "170.25"},
        {"ticker": "MSFT", "quantity": "0", "currentPrice": "300"},
        {"ticker": "TSLA", "quantity": "-1", "currentPrice": "250"},
    ]
    broker = RecordingBroker(positions=positions, fail_on_write=False)
    db = FakeSession(results=[])
    service = SystemControlService(db)
    monkeypatch.setattr(service, "_get_broker", _broker_provider(broker))

    message = await service.flatten_all(actor="operator")

    assert message == "Flattened 1 positions."
    assert broker.read_calls == ["get_positions"]
    assert broker.write_calls == []
    assert RecordingExecutionEngine.brokers == [broker]
    assert RecordingExecutionEngine.order_intents == [
        {
            "ticker": "AAPL",
            "side": "sell",
            "order_type": "market",
            "quantity": Decimal("2"),
            "is_dry_run": False,
            "estimated_price": Decimal("170.25"),
        }
    ]
    assert RecordingExecutionEngine.submitted_orders == [{"order": "AAPL"}]
    assert [entry.action for entry in db.added] == ["emergency_flatten_all"]
    assert db.added[0].payload == {"source": "system_control", "flattened": 1}


def test_system_control_source_remains_direct_provider_unwired_and_mixed() -> None:
    tree = _parse_system_control()
    get_broker = _method_node("_get_broker")
    get_snapshot = _method_node("get_snapshot")
    get_positions_summary = _method_node("get_positions_summary")
    cancel_all_pending = _method_node("cancel_all_pending")
    flatten_all = _method_node("flatten_all")

    assert _adapter_counts(get_broker) == {"construct": 1, "import": 1}
    assert "create_trading212_provider_adapter" not in _call_names(tree)
    assert _adapter_counts(tree) == {"construct": 1, "import": 1}
    assert construction_inventory._trading212_adapter_references()[
        "app/services/system_control.py"
    ] == {"construct": 1, "import": 1}

    assert {"get_account_summary", "get_positions"} <= _call_names(get_snapshot)
    assert "get_positions" in _call_names(get_positions_summary)
    assert "cancel_order" in _call_names(cancel_all_pending)
    assert {"create_order_intent", "submit_order"} <= _call_names(flatten_all)
    assert "emergency_cancel_all" in ast.unparse(cancel_all_pending)
    assert "emergency_flatten_all" in ast.unparse(flatten_all)
    assert "ExecutionEngine" in ast.unparse(cancel_all_pending)
    assert "ExecutionEngine" in ast.unparse(flatten_all)


def test_system_control_file_has_no_provider_request_or_provider_helper_mentions() -> None:
    source = SYSTEM_CONTROL_PATH.read_text()

    assert "BrokerProviderRequest" not in source
    assert "BrokerProviderCredentials" not in source
    assert "create_trading212_provider_adapter" not in source
    assert "app.broker.provider" not in source
