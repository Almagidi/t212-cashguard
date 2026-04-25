"""
Unit tests for SystemControlService — kill switch, pause/resume, cancel_all_pending.
Uses the shared in-memory SQLite DB from conftest.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.models import AppSettings, RiskProfile


@pytest_asyncio.fixture
async def settings_db(db):
    """DB with seeded AppSettings (auto_trading_enabled=True, kill_switch=False)."""
    app_settings = AppSettings(
        id=1,
        theme="dark",
        timezone="UTC",
        auto_trading_enabled=True,
        kill_switch_active=False,
        live_trading_unlocked=False,
    )
    profile = RiskProfile(
        id=uuid.uuid4(),
        name="Test Profile",
        max_risk_per_trade_pct=Decimal("1.0"),
        max_daily_loss_pct=Decimal("3.0"),
        max_open_positions=5,
        max_position_size_pct=Decimal("10.0"),
        max_trades_per_day=20,
        stop_after_consecutive_losses=3,
        symbol_cooldown_seconds=0,
        force_flat_eod=False,
        is_default=True,
    )
    db.add(app_settings)
    db.add(profile)
    await db.flush()
    return db


# ── pause / resume ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pause_auto_trading_disables_auto_trading(settings_db):
    from app.services.system_control import SystemControlService

    svc = SystemControlService(settings_db)
    msg = await svc.pause_auto_trading(actor="test")
    assert msg == "Auto-trading disabled."

    s = (await settings_db.execute(
        select(AppSettings).where(AppSettings.id == 1)
    )).scalar_one()
    assert s.auto_trading_enabled is False


@pytest.mark.asyncio
async def test_pause_auto_trading_already_disabled(settings_db):
    from app.services.system_control import SystemControlService

    # Disable first
    svc = SystemControlService(settings_db)
    await svc.pause_auto_trading(actor="test")

    # Calling again should return the no-op message
    msg = await svc.pause_auto_trading(actor="test")
    assert "already" in msg.lower()


@pytest.mark.asyncio
async def test_resume_auto_trading_blocked_by_kill_switch(settings_db):
    from app.services.system_control import SystemControlError, SystemControlService

    # Enable kill switch in DB
    s = (await settings_db.execute(
        select(AppSettings).where(AppSettings.id == 1)
    )).scalar_one()
    s.kill_switch_active = True
    s.auto_trading_enabled = False
    await settings_db.flush()

    svc = SystemControlService(settings_db)
    with pytest.raises(SystemControlError, match="kill switch"):
        await svc.resume_auto_trading(actor="test")


@pytest.mark.asyncio
async def test_resume_auto_trading_in_mock_mode_succeeds(settings_db):
    """In mock mode, live readiness checks are skipped and resume should succeed."""
    from app.services.system_control import SystemControlService

    # Disable auto-trading first
    s = (await settings_db.execute(
        select(AppSettings).where(AppSettings.id == 1)
    )).scalar_one()
    s.auto_trading_enabled = False
    await settings_db.flush()

    svc = SystemControlService(settings_db)
    msg = await svc.resume_auto_trading(actor="test")
    assert msg == "Auto-trading enabled."


@pytest.mark.asyncio
async def test_resume_auto_trading_already_enabled(settings_db):
    from app.services.system_control import SystemControlService

    # auto_trading_enabled is already True from fixture
    svc = SystemControlService(settings_db)
    msg = await svc.resume_auto_trading(actor="test")
    assert "already" in msg.lower()


# ── kill switch ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_activate_kill_switch_sets_flag_and_returns_message(settings_db):
    from app.services.system_control import SystemControlService

    svc = SystemControlService(settings_db)
    msg = await svc.activate_kill_switch(actor="unit_test")
    assert "kill switch" in msg.lower()

    s = (await settings_db.execute(
        select(AppSettings).where(AppSettings.id == 1)
    )).scalar_one()
    assert s.kill_switch_active is True


@pytest.mark.asyncio
async def test_activate_kill_switch_disables_auto_trading(settings_db):
    from app.services.system_control import SystemControlService

    svc = SystemControlService(settings_db)
    await svc.activate_kill_switch(actor="unit_test")

    s = (await settings_db.execute(
        select(AppSettings).where(AppSettings.id == 1)
    )).scalar_one()
    assert s.auto_trading_enabled is False


# ── snapshot ─────────────────────────────────────────────────────────────────

class _SnapshotBroker:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get_account_summary(self):
        return {
            "free": Decimal("1000.50"),
            "total": Decimal("1250.75"),
            "invested": Decimal("250.25"),
            "result": Decimal("12.34"),
        }

    async def get_positions(self):
        return [{"ticker": "AAPL", "quantity": 2}]


@pytest.mark.asyncio
async def test_get_snapshot_returns_connected_broker_state(settings_db, monkeypatch):
    from app.services import system_control as system_control_module
    from app.services.system_control import SystemControlService

    class FakeMarketRegimeService:
        async def evaluate(self):
            return {"regime": "ranging", "detail": "Stable tape."}

    monkeypatch.setattr(
        system_control_module,
        "MarketRegimeService",
        FakeMarketRegimeService,
    )
    svc = SystemControlService(settings_db)
    monkeypatch.setattr(svc, "_get_broker", AsyncMock(return_value=_SnapshotBroker()))

    snapshot = await svc.get_snapshot()

    assert snapshot["broker_status"] == "connected"
    assert snapshot["account"] == {
        "free_cash": 1000.5,
        "total_value": 1250.75,
        "invested": 250.25,
        "result": 12.34,
    }
    assert snapshot["positions"] == [{"ticker": "AAPL", "quantity": 2}]
    assert snapshot["regime"]["regime"] == "ranging"


# ── cancel_all_pending ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_all_pending_with_no_orders_returns_message(settings_db):
    from app.services.system_control import SystemControlService

    svc = SystemControlService(settings_db)
    msg = await svc.cancel_all_pending(actor="test")
    assert "no pending" in msg.lower()


# ── confirmation_expiry ───────────────────────────────────────────────────────

def test_confirmation_expiry_is_in_the_future():
    from app.services.system_control import SystemControlService

    expiry = SystemControlService.confirmation_expiry()
    assert expiry > datetime.now(UTC)
