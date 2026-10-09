"""Real-provider scheduled ORB signal-to-paper-fill regression coverage."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import desc, func, select

from app.api.schemas import PaperOrderCreate
from app.core.config import settings
from app.db.models import (
    AppSettings,
    BrokerAccountSnapshot,
    Order,
    PositionSnapshot,
    Signal,
    Strategy,
    Trade,
    User,
    VenueConfig,
)
from app.execution.paper_engine import PaperExecutionEngine
from app.market_data import mock_provider
from app.risk.engine import RiskEngine
from app.services import strategy_runner
from app.services.signal_allocator import SignalAllocator
from app.services.strategy_runner import StrategyRunner
from app.strategies.orb_production import DEFAULT_PARAMS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


FROZEN_MID_SESSION = datetime(2026, 7, 6, 18, 0, tzinfo=UTC)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if tz is None:
            return FROZEN_MID_SESSION.replace(tzinfo=None)
        return FROZEN_MID_SESSION.astimezone(tz)


class _TrendingMarketIntelligenceMonitor:
    def __init__(self, _db: AsyncSession) -> None:
        pass

    async def evaluate_and_alert(self) -> dict[str, Any]:
        return {
            "regime": {
                "regime": "trending_up",
                "active_strategies": ["orb"],
                "suppressed_strategies": [],
            }
        }


async def _no_op_daily_summary(*_args: Any, **_kwargs: Any) -> None:
    return None


def _live_adapter_sentinel(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a live broker adapter must not be constructed in APP_MODE=mock")


def _outbound_network_sentinel(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the scheduled mock path must not make an outbound HTTP request")


async def _seed_real_profile_run(db: AsyncSession) -> Strategy:
    db.add(
        AppSettings(
            id=1,
            auto_trading_enabled=True,
            kill_switch_active=False,
            live_trading_unlocked=False,
        )
    )
    db.add(
        VenueConfig(
            venue="t212",
            kill_switch_active=False,
            auto_trading_enabled=True,
            degraded_mode_active=False,
        )
    )
    db.add(
        User(
            id=uuid.uuid4(),
            email=settings.ADMIN_EMAIL,
            hashed_password="test-only-not-a-real-credential",
            is_active=True,
            is_admin=True,
        )
    )
    strategy = Strategy(
        id=uuid.uuid4(),
        name="Real Provider ORB Paper Fill",
        type="orb",
        is_enabled=True,
        is_live=True,
        params=dict(DEFAULT_PARAMS),
        allowed_tickers=["NVDA"],
        venue="t212",
    )
    db.add(strategy)
    await db.flush()
    return strategy


@pytest.mark.asyncio
async def test_real_orb_profile_reaches_paper_fill_and_default_profile_does_not(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive real provider output through the real runner, risk, and allocator."""
    assert settings.APP_MODE == "mock"
    assert strategy_runner.RiskEngine is RiskEngine
    assert strategy_runner.SignalAllocator is SignalAllocator

    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")
    monkeypatch.setattr(settings, "MOCK_MARKET_SEED", 212)
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "unit1-dummy-demo-key")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "unit1-dummy-demo-secret")
    monkeypatch.setattr(strategy_runner, "datetime", _FrozenDateTime)
    monkeypatch.setattr(mock_provider, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        strategy_runner,
        "MarketIntelligenceMonitor",
        _TrendingMarketIntelligenceMonitor,
    )
    monkeypatch.setattr(strategy_runner, "alert_daily_summary", _no_op_daily_summary)
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _live_adapter_sentinel)
    monkeypatch.setattr("app.broker.kraken.KrakenAdapter", _live_adapter_sentinel)
    monkeypatch.setattr(
        strategy_runner,
        "create_trading212_provider_adapter",
        _live_adapter_sentinel,
    )
    monkeypatch.setattr("httpx.AsyncClient.request", _outbound_network_sentinel)
    monkeypatch.setattr("httpx.AsyncClient.send", _outbound_network_sentinel)
    monkeypatch.setattr("httpx.Client.request", _outbound_network_sentinel)
    monkeypatch.setattr("httpx.Client.send", _outbound_network_sentinel)

    # Make the default-profile negative control reproducible without changing
    # the provider's module-global random implementation.
    default_rng = random.Random(212)
    monkeypatch.setattr(mock_provider.random, "uniform", default_rng.uniform)
    monkeypatch.setattr(mock_provider.random, "randint", default_rng.randint)

    strategy = await _seed_real_profile_run(db)
    service = StrategyRunner(db)

    monkeypatch.setattr(settings, "MOCK_MARKET_PROFILE", "default")
    negative = await service.run_all_enabled()
    await db.commit()
    assert negative == {
        "strategies_run": 1,
        "signals_generated": 0,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
    }
    assert (await db.execute(select(Signal))).scalar_one_or_none() is None

    monkeypatch.setattr(settings, "MOCK_MARKET_PROFILE", "orb_breakout")
    first = await service.run_all_enabled()
    await db.commit()
    assert first == {
        "strategies_run": 1,
        "signals_generated": 1,
        "orders_submitted": 1,
        "risk_blocks": 0,
        "errors": [],
    }

    signal = (
        await db.execute(select(Signal).where(Signal.strategy_id == strategy.id))
    ).scalar_one()
    order = (await db.execute(select(Order).where(Order.signal_id == signal.id))).scalar_one()
    opening_account = (await db.execute(select(BrokerAccountSnapshot))).scalar_one()
    filled_position = (
        await db.execute(
            select(PositionSnapshot).where(
                PositionSnapshot.ticker == "NVDA",
                PositionSnapshot.quantity > 0,
            )
        )
    ).scalar_one()

    assert signal.status == "executed"
    assert signal.decision_key is not None
    assert signal.params_snapshot["allocation"]["status"] == "allocated"
    assert order.status == "filled"
    assert order.is_dry_run is True
    assert order.execution_environment == "paper_mock"
    assert order.broker_response["no_broker_order_sent"] is True
    assert order.filled_at is not None
    assert order.filled_quantity == order.quantity
    assert order.filled_quantity is not None and order.filled_quantity > 0
    assert order.avg_fill_price is not None and order.avg_fill_price > 0
    assert order.cash_used is not None and order.cash_used > 0
    assert filled_position.quantity == order.filled_quantity
    assert opening_account.cash == Decimal("100000") - order.cash_used
    assert await db.scalar(select(func.count()).select_from(Signal)) == 1
    assert await db.scalar(select(func.count()).select_from(Order)) == 1

    # Close through the real local execution engine so the evidence chain
    # includes the trade, flat position, and restored cash ledger state.
    user = (await db.execute(select(User).where(User.email == settings.ADMIN_EMAIL))).scalar_one()
    close_order = await PaperExecutionEngine(db).execute(
        PaperOrderCreate(
            ticker="NVDA",
            side="sell",
            quantity=filled_position.quantity,
            estimated_price=filled_position.current_price,
            source="unit1_roundtrip",
            venue="paper",
            paper_only=True,
        ),
        user=user,
    )
    await db.commit()

    trade = (
        await db.execute(select(Trade).where(Trade.close_order_id == close_order.id))
    ).scalar_one()
    flat_position = (
        await db.execute(
            select(PositionSnapshot)
            .where(PositionSnapshot.connection_id == filled_position.connection_id)
            .order_by(desc(PositionSnapshot.snapshotted_at), desc(PositionSnapshot.id))
            .limit(1)
        )
    ).scalar_one()
    closing_account = (
        await db.execute(
            select(BrokerAccountSnapshot)
            .where(BrokerAccountSnapshot.connection_id == filled_position.connection_id)
            .order_by(
                desc(BrokerAccountSnapshot.snapshotted_at),
                desc(BrokerAccountSnapshot.id),
            )
            .limit(1)
        )
    ).scalar_one()

    assert close_order.status == "filled"
    assert close_order.is_dry_run is True
    assert close_order.execution_environment == "paper_mock"
    assert close_order.broker_response["no_broker_order_sent"] is True
    assert trade.is_dry_run is True
    assert trade.open_order_id == order.id
    assert trade.close_order_id == close_order.id
    assert trade.quantity == order.filled_quantity
    assert flat_position.quantity == Decimal("0")
    assert close_order.filled_quantity is not None
    assert close_order.avg_fill_price is not None
    assert close_order.fee_amount is not None
    assert closing_account.cash == (
        opening_account.cash
        + close_order.avg_fill_price * close_order.filled_quantity
        - close_order.fee_amount
    ).quantize(Decimal("0.00000001"))
    assert closing_account.cash < Decimal("100000")

    # Replay the exact same completed bar from a genuinely flat portfolio. The
    # persisted decision_key must suppress a duplicate signal and order.
    signal_count = await db.scalar(select(func.count()).select_from(Signal))
    order_count = await db.scalar(select(func.count()).select_from(Order))
    trade_count = await db.scalar(select(func.count()).select_from(Trade))

    repeated = await service.run_all_enabled()
    await db.commit()
    assert repeated == {
        "strategies_run": 1,
        "signals_generated": 0,
        "orders_submitted": 0,
        "risk_blocks": 0,
        "errors": [],
    }
    assert await db.scalar(select(func.count()).select_from(Signal)) == signal_count == 1
    assert await db.scalar(select(func.count()).select_from(Order)) == order_count == 2
    assert await db.scalar(select(func.count()).select_from(Trade)) == trade_count == 1
