from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from app.core.config import settings
from app.db.models import Order, RiskProfile, Trade
from app.services import position_monitor
from app.services.position_monitor import PositionMonitor, _DailyLossOutcome


class FakeBroker:
    def __init__(
        self,
        positions: list[dict[str, Any]] | None = None,
        *,
        raise_on_positions: Exception | None = None,
    ) -> None:
        self.positions = positions or []
        self.raise_on_positions = raise_on_positions
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []

    async def __aenter__(self) -> FakeBroker:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def get_positions(self) -> list[dict[str, Any]]:
        self.read_calls.append("get_positions")
        if self.raise_on_positions:
            raise self.raise_on_positions
        return self.positions

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("place_", "cancel_", "modify_", "submit_")):
            self.write_calls.append(name)
            raise AssertionError(f"unexpected direct broker write method: {name}")
        raise AttributeError(name)


@dataclass
class FakeOrder:
    id: uuid.UUID
    ticker: str
    side: str
    is_dry_run: bool


class FakeExecutionEngine:
    order_intents: ClassVar[list[dict[str, Any]]] = []
    submitted_orders: ClassVar[list[FakeOrder]] = []

    def __init__(self, _db: FakeSession, _broker: FakeBroker) -> None:
        return None

    async def create_order_intent(self, **kwargs: Any) -> FakeOrder:
        self.order_intents.append(kwargs)
        return FakeOrder(
            id=uuid.uuid4(),
            ticker=str(kwargs["ticker"]),
            side=str(kwargs["side"]),
            is_dry_run=bool(kwargs.get("is_dry_run", False)),
        )

    async def submit_order(self, order: FakeOrder) -> FakeOrder:
        self.submitted_orders.append(order)
        return order


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.committed = 0

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed += 1


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.errors: list[tuple[str, dict[str, Any]]] = []
        self.criticals: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self.errors.append((event, kwargs))

    def critical(self, event: str, **kwargs: Any) -> None:
        self.criticals.append((event, kwargs))


async def _make_risk_profile(db: Any, max_daily_loss_pct: str = "3.0") -> RiskProfile:
    risk_profile = RiskProfile(
        id=uuid.uuid4(),
        name="Default",
        max_risk_per_trade_pct=Decimal("1.0"),
        max_daily_loss_pct=Decimal(max_daily_loss_pct),
        max_open_positions=5,
        max_position_size_pct=Decimal("10.0"),
        max_trades_per_day=20,
        stop_after_consecutive_losses=3,
        symbol_cooldown_seconds=300,
        force_flat_eod=True,
        is_default=True,
    )
    db.add(risk_profile)
    await db.commit()
    return risk_profile


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExecutionEngine.order_intents.clear()
    FakeExecutionEngine.submitted_orders.clear()
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(
        settings,
        "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
        "block_trading",
        raising=False,
    )


@pytest.mark.asyncio
async def test_daily_loss_uses_closed_trade_realized_pnl_not_order_cash_used(db: Any) -> None:
    await _make_risk_profile(db, max_daily_loss_pct="3.0")
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    close_order_id = uuid.uuid4()
    db.add(
        Order(
            id=close_order_id,
            client_order_key="sell-cash-flow-is-not-pnl",
            ticker="AAPL",
            side="sell",
            order_type="market",
            quantity=Decimal("5"),
            status="filled",
            cash_used=Decimal("1000"),
            is_dry_run=False,
            created_at=today,
        )
    )
    db.add(
        Trade(
            id=uuid.uuid4(),
            ticker="AAPL",
            side="sell",
            close_order_id=close_order_id,
            quantity=Decimal("5"),
            open_price=Decimal("300"),
            close_price=Decimal("220"),
            realized_pnl=Decimal("-400"),
            opened_at=today,
            closed_at=today,
            is_dry_run=False,
        )
    )
    await db.commit()

    outcome = await PositionMonitor(db)._check_daily_loss_with_unrealized(
        app_settings=object(),
        broker=FakeBroker(positions=[{"ticker": "AAPL", "ppl": "0"}]),
        account_value=Decimal("10000"),
    )

    assert outcome is _DailyLossOutcome.BREACH_KILL_SWITCH


@pytest.mark.asyncio
async def test_unrealized_pnl_failure_assume_zero_remains_available(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _make_risk_profile(db, max_daily_loss_pct="3.0")
    logger = RecordingLogger()
    monkeypatch.setattr(position_monitor, "log", logger)
    monkeypatch.setattr(
        settings,
        "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
        "assume_zero",
        raising=False,
    )
    broker = FakeBroker(raise_on_positions=RuntimeError("broker snapshot unavailable"))

    outcome = await PositionMonitor(db)._check_daily_loss_with_unrealized(
        app_settings=object(),
        broker=broker,
        account_value=Decimal("10000"),
    )

    assert outcome is _DailyLossOutcome.NO_BREACH
    assert broker.read_calls == ["get_positions"]
    assert broker.write_calls == []
    assert logger.warnings == []
    assert logger.errors == [
        (
            "position_monitor.unrealized_pnl_error",
            {
                "error": "broker snapshot unavailable",
                "exc_info": True,
                "unrealized_assumed": 0.0,
                "failure_policy": "assume_zero",
            },
        )
    ]


@pytest.mark.asyncio
async def test_unrealized_pnl_failure_block_trading_fails_closed(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _make_risk_profile(db, max_daily_loss_pct="3.0")
    logger = RecordingLogger()
    monkeypatch.setattr(position_monitor, "log", logger)
    monkeypatch.setattr(
        settings,
        "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
        "block_trading",
        raising=False,
    )
    broker = FakeBroker(raise_on_positions=RuntimeError("broker snapshot unavailable"))

    outcome = await PositionMonitor(db)._check_daily_loss_with_unrealized(
        app_settings=object(),
        broker=broker,
        account_value=Decimal("10000"),
    )

    assert outcome is _DailyLossOutcome.POLICY_BLOCK_TRADING
    assert broker.read_calls == ["get_positions"]
    assert broker.write_calls == []
    assert logger.errors == [
        (
            "position_monitor.unrealized_pnl_error",
            {
                "error": "broker snapshot unavailable",
                "exc_info": True,
                "failure_policy": "block_trading",
                "fail_closed": True,
            },
        )
    ]
    assert logger.warnings == [
        (
            "position_monitor.realized_pnl_skipped",
            {
                "reason": "unrealized_pnl_failure_policy_halt",
                "failure_policy": "block_trading",
            },
        )
    ]


@pytest.mark.asyncio
async def test_unrealized_pnl_failure_activate_kill_switch_fails_closed(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _make_risk_profile(db, max_daily_loss_pct="3.0")
    logger = RecordingLogger()
    activated_by: list[str] = []
    alerted_by: list[str] = []

    async def fake_activate_kill_switch(_db: Any, *, actor: str = "system") -> None:
        activated_by.append(actor)

    async def fake_alert_kill_switch_activated(_db: Any, *, actor: str = "system") -> None:
        alerted_by.append(actor)

    monkeypatch.setattr(position_monitor, "log", logger)
    monkeypatch.setattr(
        settings,
        "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
        "activate_kill_switch",
        raising=False,
    )
    monkeypatch.setattr(position_monitor, "activate_kill_switch", fake_activate_kill_switch)
    monkeypatch.setattr(
        position_monitor,
        "alert_kill_switch_activated",
        fake_alert_kill_switch_activated,
    )
    broker = FakeBroker(raise_on_positions=RuntimeError("broker snapshot unavailable"))

    outcome = await PositionMonitor(db)._check_daily_loss_with_unrealized(
        app_settings=object(),
        broker=broker,
        account_value=Decimal("10000"),
    )

    assert outcome is _DailyLossOutcome.POLICY_KILL_SWITCH
    assert activated_by == ["position_monitor:unrealized_pnl_failure"]
    assert alerted_by == ["position_monitor:unrealized_pnl_failure"]
    assert broker.write_calls == []
    assert logger.errors == [
        (
            "position_monitor.unrealized_pnl_error",
            {
                "error": "broker snapshot unavailable",
                "exc_info": True,
                "failure_policy": "activate_kill_switch",
                "fail_closed": True,
                "kill_switch_activated": True,
            },
        )
    ]
    assert logger.warnings == [
        (
            "position_monitor.realized_pnl_skipped",
            {
                "reason": "unrealized_pnl_failure_policy_halt",
                "failure_policy": "activate_kill_switch",
            },
        )
    ]


@pytest.mark.asyncio
async def test_unrealized_pnl_failure_invalid_policy_fails_closed(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _make_risk_profile(db, max_daily_loss_pct="3.0")
    logger = RecordingLogger()
    monkeypatch.setattr(position_monitor, "log", logger)
    monkeypatch.setattr(
        settings,
        "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
        "unexpected_value",
        raising=False,
    )
    broker = FakeBroker(raise_on_positions=RuntimeError("broker snapshot unavailable"))

    outcome = await PositionMonitor(db)._check_daily_loss_with_unrealized(
        app_settings=object(),
        broker=broker,
        account_value=Decimal("10000"),
    )

    assert outcome is _DailyLossOutcome.POLICY_BLOCK_TRADING
    assert broker.write_calls == []
    assert logger.errors == [
        (
            "position_monitor.unrealized_pnl_failure_policy_invalid",
            {
                "configured_policy": "unexpected_value",
                "fallback_policy": "block_trading",
                "fail_closed": True,
                "error": "broker snapshot unavailable",
                "exc_info": True,
            },
        )
    ]
    assert logger.warnings == [
        (
            "position_monitor.realized_pnl_skipped",
            {
                "reason": "unrealized_pnl_failure_policy_halt",
                "failure_policy": "block_trading",
            },
        )
    ]


@pytest.mark.asyncio
async def test_malformed_unrealized_position_payload_block_trading_fails_closed(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _make_risk_profile(db, max_daily_loss_pct="3.0")
    logger = RecordingLogger()
    monkeypatch.setattr(position_monitor, "log", logger)
    monkeypatch.setattr(
        settings,
        "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
        "block_trading",
        raising=False,
    )
    broker = FakeBroker(positions=[{"ticker": "AAPL", "ppl": "not-a-number"}])

    outcome = await PositionMonitor(db)._check_daily_loss_with_unrealized(
        app_settings=object(),
        broker=broker,
        account_value=Decimal("10000"),
    )

    assert outcome is _DailyLossOutcome.POLICY_BLOCK_TRADING
    assert broker.read_calls == ["get_positions"]
    assert broker.write_calls == []
    assert logger.errors[0][0] == "position_monitor.unrealized_pnl_error"
    payload = logger.errors[0][1]
    assert payload["failure_policy"] == "block_trading"
    assert payload["fail_closed"] is True
    assert "not-a-number" in payload["error"]


@pytest.mark.asyncio
async def test_daily_loss_handles_null_realized_pnl_as_zero(db: Any) -> None:
    await _make_risk_profile(db, max_daily_loss_pct="3.0")
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    close_order_id = uuid.uuid4()
    db.add(
        Order(
            id=close_order_id,
            client_order_key="closed-trade-null-realized-pnl",
            ticker="MSFT",
            side="sell",
            order_type="market",
            quantity=Decimal("1"),
            status="filled",
            cash_used=Decimal("0"),
            is_dry_run=False,
            created_at=today,
        )
    )
    db.add(
        Trade(
            id=uuid.uuid4(),
            ticker="MSFT",
            side="sell",
            close_order_id=close_order_id,
            quantity=Decimal("1"),
            open_price=Decimal("100"),
            close_price=Decimal("100"),
            realized_pnl=None,
            opened_at=today,
            closed_at=today,
            is_dry_run=False,
        )
    )
    await db.commit()

    outcome = await PositionMonitor(db)._check_daily_loss_with_unrealized(
        app_settings=object(),
        broker=FakeBroker(positions=[]),
        account_value=Decimal("10000"),
    )

    assert outcome is _DailyLossOutcome.NO_BREACH


@pytest.mark.asyncio
async def test_eod_flatten_commits_audit_and_routes_long_positions_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = [
        {"ticker": "AAPL", "quantity": "2", "currentPrice": "170.25"},
        {"ticker": "MSFT", "quantity": "0", "currentPrice": "300"},
        {"ticker": "TSLA", "quantity": "-1", "currentPrice": "250"},
    ]
    broker = FakeBroker(positions=positions)
    db = FakeSession()
    service = PositionMonitor(db)

    async def get_broker() -> FakeBroker:
        return broker

    monkeypatch.setattr(service, "_get_broker", get_broker)
    monkeypatch.setattr(position_monitor, "ExecutionEngine", FakeExecutionEngine)

    summary = await service.eod_flatten()

    assert summary == {"flattened": 1}
    assert broker.read_calls == ["get_positions"]
    assert broker.write_calls == []
    assert FakeExecutionEngine.order_intents == [
        {
            "ticker": "AAPL",
            "side": "sell",
            "order_type": "market",
            "quantity": Decimal("2"),
            "is_dry_run": False,
            "estimated_price": Decimal("170.25"),
        }
    ]
    assert FakeExecutionEngine.submitted_orders[0].side == "sell"
    audit_logs = [entry for entry in db.added if getattr(entry, "action", None)]
    assert [entry.action for entry in audit_logs] == ["eod_flatten_executed"]
    assert audit_logs[0].payload == {"flattened": 1}
    assert db.committed == 1
