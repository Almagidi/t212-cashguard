"""The scheduled runner opens no short position, whatever a strategy row or engine asks for."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.api.schemas import PaperOrderCreate
from app.core.config import settings
from app.db.models import Order, Signal, User
from app.execution.paper_engine import PaperExecutionEngine
from app.services import strategy_runner
from app.services.strategy_runner import StrategyRunner
from tests.integration import test_scheduled_signal_paper_fill_gap as gap_cases

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SHORT_REQUESTS = [True, 1, "true", "false"]


@pytest.fixture(autouse=True)
def _isolate_unrelated_subsystems(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        strategy_runner, "MarketIntelligenceMonitor", gap_cases._NoOpMarketIntelligenceMonitor
    )
    monkeypatch.setattr(strategy_runner, "RiskEngine", lambda _db: gap_cases._AllowingRiskEngine())
    monkeypatch.setattr(strategy_runner, "SignalAllocator", gap_cases._AllowingSignalAllocator)
    monkeypatch.setattr(strategy_runner, "alert_daily_summary", lambda *_a, **_kw: None)
    for target in ("app.broker.trading212.Trading212Adapter", "app.broker.kraken.KrakenAdapter"):
        monkeypatch.setattr(target, gap_cases._live_adapter_sentinel)
    monkeypatch.setattr(
        strategy_runner, "create_trading212_provider_adapter", gap_cases._live_adapter_sentinel
    )


async def _run(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    service = StrategyRunner(db)
    monkeypatch.setattr(service, "_fetch_market_context", gap_cases._fake_market_context)
    summary = await service.run_all_enabled()
    await db.commit()
    return summary


async def _rows(db: AsyncSession, model: Any) -> list[Any]:
    return list((await db.execute(select(model))).scalars().all())


@pytest.mark.asyncio
async def test_same_setup_without_a_short_request_does_trade(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the fixture really produces an order, so the tests below are not vacuous."""
    assert settings.APP_MODE == "mock"
    await gap_cases._seed_open_gates(db, is_live=True)

    summary = await _run(db, monkeypatch)

    assert summary["errors"] == []
    assert summary["orders_submitted"] == 1
    assert [order.side for order in await _rows(db, Order)] == ["buy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", SHORT_REQUESTS)
async def test_strategy_row_asking_for_short_selling_opens_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    """The row is written directly, as it would be if it bypassed the API's validation."""
    strategy = await gap_cases._seed_open_gates(db, is_live=True)
    strategy.params = {**dict(strategy.params or {}), "allow_short": value}
    await db.flush()
    asked: list[int] = []
    monkeypatch.setattr(
        strategy_runner.OpeningRangeBreakoutStrategy,
        "generate_signal",
        lambda *_args, **_kwargs: asked.append(1),
    )

    summary = await _run(db, monkeypatch)

    assert summary["errors"] == []
    assert summary["orders_submitted"] == 0
    assert asked == []
    assert await _rows(db, Signal) == []
    assert await _rows(db, Order) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["sell", "short", "SELL", ""])
async def test_engine_that_returns_a_sell_entry_opens_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    await gap_cases._seed_open_gates(db, is_live=True)

    def sell_entry(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(side=side, signal_type="entry")

    monkeypatch.setattr(strategy_runner.OpeningRangeBreakoutStrategy, "generate_signal", sell_entry)

    summary = await _run(db, monkeypatch)

    assert summary["errors"] == []
    assert summary["orders_submitted"] == 0
    assert await _rows(db, Signal) == []
    assert await _rows(db, Order) == []


@pytest.mark.asyncio
async def test_open_position_of_a_strategy_asking_for_short_selling_still_gets_its_exit_check(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking entries must not leave a held position without stop or target management."""
    strategy = await gap_cases._seed_open_gates(db, is_live=True)
    entry_signal = Signal(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        ticker="NVDA",
        side="buy",
        signal_type="entry",
        status="executed",
        stop_price=Decimal("95"),
        take_profit_price=Decimal("110"),
        generated_at=datetime(2026, 1, 2, 14, 30, tzinfo=UTC),
    )
    db.add(entry_signal)
    await db.commit()
    paper_user = (
        await db.execute(select(User).where(User.email == settings.ADMIN_EMAIL))
    ).scalar_one()
    await PaperExecutionEngine(db).execute(
        PaperOrderCreate(
            ticker="NVDA",
            side="buy",
            quantity=Decimal("2"),
            estimated_price=Decimal("100"),
            source="cash_only_exit_test",
            strategy=strategy.name,
        ),
        user=paper_user,
        signal_id=entry_signal.id,
    )
    strategy.params = {**dict(strategy.params or {}), "allow_short": True}
    await db.commit()
    exit_checks: list[str] = []
    entries_asked: list[int] = []

    async def recording_exit(self: Any, **kwargs: Any) -> None:
        exit_checks.append(kwargs["ticker"])

    monkeypatch.setattr(StrategyRunner, "_check_exit", recording_exit)
    monkeypatch.setattr(
        strategy_runner.OpeningRangeBreakoutStrategy,
        "generate_signal",
        lambda *_args, **_kwargs: entries_asked.append(1),
    )

    summary = await _run(db, monkeypatch)

    assert summary["errors"] == []
    assert exit_checks == ["NVDA"]
    assert entries_asked == []


def test_runner_no_longer_selects_cfd_treatment() -> None:
    source = Path(strategy_runner.__file__).read_text()

    assert "is_cfd" not in source
