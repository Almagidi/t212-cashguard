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
from app.services import portfolio_execution_service
from app.services.portfolio_execution_service import MarketSnapshot, PortfolioExecutionService
from app.services.safety_policy import SafetyPolicyViolation
from app.strategies.indicators import Bar
from tests.unit import test_trading212_construction_inventory as construction_inventory

API_ROOT = Path(__file__).resolve().parents[2]
PORTFOLIO_EXECUTION_PATH = API_ROOT / "app" / "services" / "portfolio_execution_service.py"


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
    live_trading_unlocked: bool = False


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
            is_dry_run=bool(kwargs["is_dry_run"]),
        )

    async def submit_order(self, order: FakeSubmittedOrder) -> FakeSubmittedOrder:
        self.submitted_orders.append(order)
        return order


class AllowingRiskEngine:
    def __init__(self, _db: FakeSession) -> None:
        self.db = _db

    async def run_all_checks(self, **_kwargs: Any) -> None:
        return None

    async def check_sector_and_correlation(self, **_kwargs: Any) -> None:
        return None


@dataclass
class FakeAllocationDecision:
    status: str = "allocated"
    reason: str = "allocated"

    def to_payload(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


class AllowingSignalAllocator:
    def new_state(self) -> object:
        return object()

    def allocate_one(self, *_args: Any, **_kwargs: Any) -> FakeAllocationDecision:
        return FakeAllocationDecision()


@pytest.fixture(autouse=True)
def _reset_portfolio_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingTrading212Adapter.constructed.clear()
    RecordingTrading212Adapter.entered = 0
    RecordingTrading212Adapter.exited = 0
    RecordingExecutionEngine.brokers.clear()
    RecordingExecutionEngine.order_intents.clear()
    RecordingExecutionEngine.submitted_orders.clear()
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", RecordingTrading212Adapter)


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


def _parse_portfolio_execution() -> ast.Module:
    return ast.parse(PORTFOLIO_EXECUTION_PATH.read_text(), filename=str(PORTFOLIO_EXECUTION_PATH))


def _service_class() -> ast.ClassDef:
    return next(
        node
        for node in _parse_portfolio_execution().body
        if isinstance(node, ast.ClassDef) and node.name == "PortfolioExecutionService"
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


def _portfolio_strategy(*, is_live: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Portfolio Provider Equivalence",
        type="buy_hold_core",
        is_enabled=True,
        is_live=is_live,
        params={"capital_fraction": "1.0", "min_trade_value": "25", "min_weight_delta_pct": "0.5"},
        allowed_tickers=["SPY"],
        risk_profile=None,
        last_signal_at=None,
    )


def _market_snapshot() -> MarketSnapshot:
    dates = [
        datetime(2026, 1, 2, tzinfo=UTC),
        datetime(2026, 1, 5, tzinfo=UTC),
    ]
    bars = [
        Bar(
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000000"),
        ),
        Bar(
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000000"),
        ),
    ]
    return MarketSnapshot(
        histories={"SPY": (bars, dates)},
        latest_prices={"SPY": Decimal("100")},
        latest_quote_times={"SPY": dates[-1]},
        market_open=True,
        provider_name="fake_provider",
    )


async def _load_market_snapshot(*_args: Any) -> MarketSnapshot:
    return _market_snapshot()


async def _load_regime_payload() -> dict[str, str]:
    return {"regime": "fake"}


def _run_all_empty_summary() -> dict[str, Any]:
    return {
        "strategies_seen": 0,
        "strategies_due": 0,
        "strategies_rebalanced": 0,
        "signals_created": 0,
        "orders_submitted": 0,
        "dry_run_orders": 0,
        "risk_blocks": 0,
        "allocation_blocks": 0,
        "skipped": [],
        "errors": [],
    }


def test_portfolio_execution_source_remains_direct_provider_unwired_and_mixed() -> None:
    tree = _parse_portfolio_execution()
    service = _service_class()
    get_broker = _method_node("_get_broker")
    run_strategy_by_id = _method_node("run_strategy_by_id")
    run_all_enabled = _method_node("run_all_enabled")
    run_strategy_once = _method_node("run_strategy_once")
    source = PORTFOLIO_EXECUTION_PATH.read_text()

    assert _adapter_counts(get_broker) == {"construct": 1, "import": 1}
    assert _adapter_counts(tree) == {"construct": 1, "import": 1}
    assert construction_inventory._trading212_adapter_references()[
        "app/services/portfolio_execution_service.py"
    ] == {"construct": 1, "import": 1}
    assert "create_trading212_provider_adapter" not in source
    assert "app.broker.provider" not in source
    assert "BrokerProviderRequest" not in source
    assert "BrokerProviderCredentials" not in source

    assert {"get_account_summary", "get_positions"} <= (
        _call_names(run_strategy_by_id) | _call_names(run_all_enabled)
    )
    assert {"create_order_intent", "submit_order"} <= _call_names(run_strategy_once)
    assert "portfolio_rebalance_order" in ast.unparse(run_strategy_once)
    assert "ExecutionEngine" in ast.unparse(service)


@pytest.mark.asyncio
async def test_get_broker_mock_mode_returns_mock_adapter_without_trading212_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    db = FakeSession(results=[])
    service = PortfolioExecutionService(db)

    broker = await service._get_broker()

    assert type(broker).__name__ == "MockBrokerAdapter"
    assert db.executed == []
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_preserves_no_active_connection_as_no_broker_without_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    service = PortfolioExecutionService(FakeSession(results=[None]))

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
    service = PortfolioExecutionService(db)

    def fail_decrypt(_value: str) -> str:
        raise CredentialDecryptionError("cannot decrypt portfolio credentials")

    async def mark_reconnect(
        marked_db: FakeSession,
        marked_conn: FakeConnection,
        reason: str,
        *,
        actor: str,
    ) -> None:
        reconnect_calls.append((marked_db, marked_conn, reason, actor))

    monkeypatch.setattr(security_module, "decrypt_field", fail_decrypt)
    monkeypatch.setattr(
        portfolio_execution_service,
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
            "cannot decrypt portfolio credentials",
            "portfolio_execution_service",
        )
    ]
    assert RecordingTrading212Adapter.constructed == []


@pytest.mark.asyncio
async def test_get_broker_policy_rejection_happens_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection(environment="live")
    db = FakeSession(results=[conn])
    service = PortfolioExecutionService(db)
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(security_module, "decrypt_field", _decrypt)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _adapter_sentinel)
    gate_calls: list[tuple[str, str]] = []

    def rejecting_gate(environment: str, *, action: str) -> None:
        gate_calls.append((environment, action))
        raise SafetyPolicyViolation("blocked by portfolio test gate")

    monkeypatch.setattr(
        portfolio_execution_service,
        "require_broker_environment",
        rejecting_gate,
    )

    broker = await service._get_broker()

    assert broker is None
    assert gate_calls == [("live", "portfolio execution broker access")]
    assert RecordingTrading212Adapter.constructed == []
    assert "create_trading212_provider_adapter" not in PORTFOLIO_EXECUTION_PATH.read_text()


@pytest.mark.asyncio
async def test_get_broker_constructs_direct_adapter_after_lookup_decrypt_and_environment_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    conn = _connection(environment="live")
    db = FakeSession(results=[conn], events=events)
    service = PortfolioExecutionService(db)
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
    monkeypatch.setattr(portfolio_execution_service, "require_broker_environment", gate)
    monkeypatch.setattr(RecordingTrading212Adapter, "__init__", adapter_init)

    broker = await service._get_broker()

    assert isinstance(broker, RecordingTrading212Adapter)
    assert events == [
        "db_execute",
        "decrypt:encrypted-live-key",
        "decrypt:encrypted-live-secret",
        "gate:live:portfolio execution broker access",
        "adapter:live",
    ]
    assert RecordingTrading212Adapter.constructed == [
        ("decrypted-live-key", "decrypted-live-secret", "live")
    ]


@pytest.mark.asyncio
async def test_run_all_enabled_reads_account_and_positions_before_strategy_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _portfolio_strategy()
    broker = RecordingBroker(
        account_summary={"total": Decimal("1200.25"), "free": Decimal("900.50")},
        positions=[{"ticker": "SPY", "quantity": "1"}],
    )
    db = FakeSession(results=[FakeAppSettings(), [strategy]])
    service = PortfolioExecutionService(db)
    observed: list[dict[str, Any]] = []

    async def get_broker() -> RecordingBroker:
        return broker

    async def run_strategy_once(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed.append(kwargs)
        return {
            "status": "rebalanced",
            "signals_created": 1,
            "orders_submitted": 0,
            "dry_run_orders": 1,
            "risk_blocks": 0,
            "allocation_blocks": 0,
            "available_cash": 800.5,
            "positions": [{"ticker": "SPY", "quantity": 2}],
        }

    monkeypatch.setattr(service, "_get_broker", get_broker)
    monkeypatch.setattr(service, "run_strategy_once", run_strategy_once)

    summary = await service.run_all_enabled(force=True, actor="portfolio-test")

    assert broker.read_calls == ["get_account_summary", "get_positions"]
    assert broker.write_calls == []
    assert observed[0]["broker"] is broker
    assert observed[0]["account_value"] == Decimal("1200.25")
    assert observed[0]["available_cash"] == Decimal("900.50")
    assert observed[0]["broker_positions"] == [{"ticker": "SPY", "quantity": "1"}]
    assert summary["strategies_seen"] == 1
    assert summary["strategies_rebalanced"] == 1
    assert summary["dry_run_orders"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_settings", "app_mode", "force", "expected_reason"),
    [
        (
            FakeAppSettings(kill_switch_active=True),
            "demo",
            False,
            "kill_switch",
        ),
        (
            FakeAppSettings(auto_trading_enabled=False),
            "demo",
            False,
            "auto_trading_off",
        ),
        (
            FakeAppSettings(live_trading_unlocked=False),
            "live",
            False,
            "live_not_unlocked",
        ),
    ],
)
async def test_run_all_enabled_safety_gates_skip_before_broker_and_strategy_execution(
    monkeypatch: pytest.MonkeyPatch,
    app_settings: FakeAppSettings,
    app_mode: str,
    force: bool,
    expected_reason: str,
) -> None:
    db = FakeSession(results=[app_settings])
    service = PortfolioExecutionService(db)
    broker = RecordingBroker()
    monkeypatch.setattr(settings, "APP_MODE", app_mode)

    async def get_broker() -> RecordingBroker:
        raise AssertionError("run_all_enabled safety gate must skip before broker lookup")

    async def run_strategy_once(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("run_all_enabled safety gate must skip before strategy execution")

    monkeypatch.setattr(service, "_get_broker", get_broker)
    monkeypatch.setattr(service, "run_strategy_once", run_strategy_once)
    monkeypatch.setattr(portfolio_execution_service, "ExecutionEngine", RecordingExecutionEngine)

    summary = await service.run_all_enabled(force=force, actor="portfolio-test")

    assert summary == {**_run_all_empty_summary(), "skipped_reason": expected_reason}
    assert db.results == []
    assert broker.read_calls == []
    assert broker.write_calls == []
    assert RecordingTrading212Adapter.constructed == []
    assert RecordingExecutionEngine.order_intents == []
    assert RecordingExecutionEngine.submitted_orders == []


@pytest.mark.asyncio
async def test_run_strategy_once_routes_dry_run_rebalance_orders_through_execution_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = RecordingBroker()
    db = FakeSession(results=[[]])
    service = PortfolioExecutionService(db)
    strategy = _portfolio_strategy(is_live=False)
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(portfolio_execution_service, "ExecutionEngine", RecordingExecutionEngine)
    monkeypatch.setattr(portfolio_execution_service, "RiskEngine", AllowingRiskEngine)
    monkeypatch.setattr(portfolio_execution_service, "SignalAllocator", AllowingSignalAllocator)
    monkeypatch.setattr(service, "_load_market_snapshot", _load_market_snapshot)
    monkeypatch.setattr(service, "_load_regime_payload", _load_regime_payload)

    summary = await service.run_strategy_once(
        strategy,
        broker=broker,
        account_value=Decimal("1000"),
        available_cash=Decimal("1000"),
        broker_positions=[],
        force=True,
        actor="portfolio-test",
    )

    assert broker.read_calls == []
    assert broker.write_calls == []
    assert RecordingExecutionEngine.brokers == [broker]
    assert len(RecordingExecutionEngine.order_intents) == 1
    assert RecordingExecutionEngine.order_intents[0] == {
        "ticker": "SPY",
        "side": "buy",
        "order_type": "market",
        "quantity": Decimal("10.00000000"),
        "signal_id": db.added[0].id,
        "is_dry_run": True,
        "available_cash": Decimal("1000"),
        "estimated_price": Decimal("100.0000"),
    }
    assert RecordingExecutionEngine.submitted_orders[0].is_dry_run is True
    assert [entry.action for entry in db.added if hasattr(entry, "action")] == [
        "portfolio_rebalance_order",
        "portfolio_rebalance_state",
    ]
    assert summary["status"] == "rebalanced"
    assert summary["orders_submitted"] == 0
    assert summary["dry_run_orders"] == 1
    assert summary["signals_created"] == 1
    assert strategy.params["portfolio_execution"]["last_mode"] == "dry_run"


@pytest.mark.asyncio
async def test_run_strategy_once_preserves_live_promotion_gate_before_order_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = RecordingBroker()
    db = FakeSession(results=[[]])
    service = PortfolioExecutionService(db)
    strategy = _portfolio_strategy(is_live=True)
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(portfolio_execution_service, "ExecutionEngine", RecordingExecutionEngine)
    monkeypatch.setattr(service, "_load_market_snapshot", _load_market_snapshot)

    class BlockingPromotionService:
        def __init__(self, _db: FakeSession) -> None:
            self.db = _db

        async def execution_gate(self, _strategy: Any) -> tuple[bool, str]:
            return False, "promotion_blocked_for_test"

    monkeypatch.setattr(
        portfolio_execution_service,
        "StrategyPromotionService",
        BlockingPromotionService,
    )

    summary = await service.run_strategy_once(
        strategy,
        broker=broker,
        account_value=Decimal("1000"),
        available_cash=Decimal("1000"),
        broker_positions=[],
        force=True,
        actor="portfolio-test",
    )

    assert summary == {"status": "skipped", "reason": "promotion_blocked_for_test"}
    assert broker.write_calls == []
    assert RecordingExecutionEngine.order_intents == []
    assert [entry.action for entry in db.added if hasattr(entry, "action")] == [
        "portfolio_rebalance_state"
    ]
