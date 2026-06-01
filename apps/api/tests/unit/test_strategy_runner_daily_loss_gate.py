from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.db.models import AppSettings, RiskProfile, Trade
from app.risk.engine import RiskEngine
from app.services import strategy_runner
from app.services.strategy_runner import StrategyRunner
from tests.unit.test_strategy_runner_provider_equivalence import (
    AllowingRiskEngine,
    AllowingSignalAllocator,
    FakeEntryEngine,
    FakeSession,
    RecordingBroker,
    RecordingExecutionEngine,
    _market_context,
    _strategy,
)


@pytest.fixture(autouse=True)
def _reset_recording_execution_engine() -> None:
    RecordingExecutionEngine.brokers.clear()
    RecordingExecutionEngine.order_intents.clear()
    RecordingExecutionEngine.submitted_orders.clear()


class DailyLossOnlyRisk:
    def __init__(self, db: Any) -> None:
        self.engine = RiskEngine(db)
        self.run_all_calls: list[dict[str, Any]] = []

    async def check_market_conditions(self, **_kwargs: Any) -> None:
        return None

    async def run_all_checks(self, **kwargs: Any) -> None:
        self.run_all_calls.append(kwargs)
        await self.engine.check_daily_loss_limit(
            kwargs["realized_pnl_today"],
            kwargs["account_value"],
        )

    async def check_sector_and_correlation(self, **_kwargs: Any) -> None:
        return None


async def _make_risk_profile(db: Any, max_daily_loss_pct: str = "3.0") -> RiskProfile:
    risk_profile = RiskProfile(
        id=uuid.uuid4(),
        name="Strategy Runner Daily Loss Gate",
        max_risk_per_trade_pct=Decimal("1.0"),
        max_daily_loss_pct=Decimal(max_daily_loss_pct),
        max_open_positions=5,
        max_position_size_pct=Decimal("50.0"),
        max_trades_per_day=20,
        stop_after_consecutive_losses=3,
        symbol_cooldown_seconds=0,
        force_flat_eod=True,
        is_default=True,
    )
    db.add(risk_profile)
    await db.commit()
    return risk_profile


def _trade(
    *,
    realized_pnl: Decimal | None,
    closed_at: datetime,
    ticker: str = "AAPL",
    is_dry_run: bool = False,
) -> Trade:
    return Trade(
        id=uuid.uuid4(),
        ticker=ticker,
        side="sell",
        quantity=Decimal("1"),
        open_price=Decimal("100"),
        close_price=Decimal("90"),
        realized_pnl=realized_pnl,
        opened_at=closed_at - timedelta(hours=1),
        closed_at=closed_at,
        is_dry_run=is_dry_run,
    )


@pytest.mark.asyncio
async def test_get_realized_pnl_today_uses_today_closed_trades_and_ignores_nulls(
    db: Any,
) -> None:
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    db.add_all(
        [
            _trade(realized_pnl=Decimal("-25.50"), closed_at=today),
            _trade(realized_pnl=Decimal("5.25"), closed_at=today + timedelta(minutes=5)),
            _trade(realized_pnl=None, closed_at=today + timedelta(minutes=10)),
            _trade(realized_pnl=Decimal("-1000"), closed_at=yesterday),
            _trade(realized_pnl=Decimal("-2000"), closed_at=tomorrow),
        ]
    )
    await db.commit()

    realized = await StrategyRunner(db)._get_realized_pnl_today()

    assert realized == Decimal("-20.25000000")


@pytest.mark.asyncio
async def test_get_realized_pnl_today_excludes_dry_run_trades(db: Any) -> None:
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    db.add_all(
        [
            _trade(realized_pnl=Decimal("-15"), closed_at=today),
            _trade(realized_pnl=Decimal("-500"), closed_at=today, is_dry_run=True),
        ]
    )
    await db.commit()

    realized = await StrategyRunner(db)._get_realized_pnl_today()

    assert realized == Decimal("-15.00000000")


@pytest.mark.asyncio
async def test_get_realized_pnl_today_defaults_to_zero_when_no_closed_trades(
    db: Any,
) -> None:
    realized = await StrategyRunner(db)._get_realized_pnl_today()

    assert realized == Decimal("0")


@pytest.mark.asyncio
async def test_live_entry_daily_loss_breach_blocks_before_order_submission(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _make_risk_profile(db, max_daily_loss_pct="3.0")
    db.add(
        AppSettings(
            id=1,
            theme="dark",
            timezone="UTC",
            auto_trading_enabled=True,
            kill_switch_active=False,
            live_trading_unlocked=False,
        )
    )
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    db.add(_trade(realized_pnl=Decimal("-400"), closed_at=today))
    await db.commit()

    service = StrategyRunner(db)
    broker = RecordingBroker()
    monkeypatch.setattr(service, "_fetch_market_context", _market_context)
    monkeypatch.setattr(strategy_runner, "ExecutionEngine", RecordingExecutionEngine)

    risk = DailyLossOnlyRisk(db)

    result = await service._process_ticker(
        ticker="AAPL",
        strategy=_strategy(is_live=True),
        engine=FakeEntryEngine(),
        risk=risk,
        broker=broker,
        cash=Decimal("10000"),
        total=Decimal("10000"),
        n_open=0,
        pos_map={},
        all_positions=[],
        intelligence={"regime": {"regime": "test"}},
        allocator=AllowingSignalAllocator(),
        allocation_state=object(),
    )

    assert result == (1, 0, 1)
    assert len(risk.run_all_calls) == 1
    assert risk.run_all_calls[0]["realized_pnl_today"] == Decimal("-400.00000000")
    assert broker.read_calls == []
    assert broker.write_calls == []
    assert RecordingExecutionEngine.order_intents == []
    assert RecordingExecutionEngine.submitted_orders == []


@pytest.mark.asyncio
async def test_realized_pnl_query_failure_fails_closed_before_risk_or_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty FakeSession results make execute() raise, simulating a DB/query failure.
    db = FakeSession(results=[])
    service = StrategyRunner(db)
    broker = RecordingBroker()
    risk = AllowingRiskEngine()
    monkeypatch.setattr(service, "_fetch_market_context", _market_context)
    monkeypatch.setattr(strategy_runner, "ExecutionEngine", RecordingExecutionEngine)

    result = await service._process_ticker(
        ticker="AAPL",
        strategy=_strategy(is_live=True),
        engine=FakeEntryEngine(),
        risk=risk,
        broker=broker,
        cash=Decimal("1000"),
        total=Decimal("1500"),
        n_open=0,
        pos_map={},
        all_positions=[],
        intelligence={"regime": {"regime": "test"}},
        allocator=AllowingSignalAllocator(),
        allocation_state=object(),
    )

    assert result == (1, 0, 1)
    assert risk.run_all_calls == []
    assert broker.read_calls == []
    assert broker.write_calls == []
    assert RecordingExecutionEngine.order_intents == []
    assert RecordingExecutionEngine.submitted_orders == []
