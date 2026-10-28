"""The mock market stream must not fabricate execution-control state."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1.routes import ws
from app.db.models import AppSettings, Order, RiskProfile


@pytest.fixture(autouse=True)
def local_regime(monkeypatch):
    monkeypatch.setattr(
        ws.MarketRegimeService, "evaluate", AsyncMock(return_value={"regime": "test"})
    )


@pytest.mark.parametrize("mode", ["mock", "paper", "demo", "live"])
@pytest.mark.parametrize("enabled,kill_switch", [(False, False), (True, False), (True, True)])
async def test_snapshot_control_state_matches_saved_settings(
    db, monkeypatch, mode, enabled, kill_switch
):
    monkeypatch.setattr(ws.settings, "APP_MODE", mode)
    db.add(AppSettings(id=1, auto_trading_enabled=enabled, kill_switch_active=kill_switch))
    db.add(
        RiskProfile(
            name="Synthetic risk profile", is_default=True, max_daily_loss_pct=Decimal("3.25")
        )
    )
    await db.commit()

    payload = await ws._build_payload(db)

    assert payload["system"] == {
        "auto_trading_enabled": enabled,
        "kill_switch_active": kill_switch,
        "max_daily_loss_pct": 3.25,
    }
    saved = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalar_one()
    assert saved.auto_trading_enabled is enabled
    assert saved.kill_switch_active is kill_switch
    assert saved.live_trading_unlocked is False
    assert (await db.execute(select(Order))).scalars().all() == []
    assert not db.dirty


@pytest.mark.parametrize("mode", ["mock", "demo"])
async def test_uninitialised_snapshot_never_claims_automation_is_enabled(db, monkeypatch, mode):
    monkeypatch.setattr(ws.settings, "APP_MODE", mode)

    payload = await ws._build_payload(db)

    assert payload["system"]["auto_trading_enabled"] is False


async def test_each_mock_snapshot_reads_the_current_saved_state(db, monkeypatch):
    monkeypatch.setattr(ws.settings, "APP_MODE", "mock")
    saved = AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False)
    db.add(saved)
    await db.commit()
    assert (await ws._build_payload(db))["system"]["auto_trading_enabled"] is True

    saved.auto_trading_enabled = False
    saved.kill_switch_active = True
    await db.commit()

    second = await ws._build_payload(db)
    assert second["system"]["auto_trading_enabled"] is False
    assert second["system"]["kill_switch_active"] is True


async def test_mock_settings_read_error_cannot_produce_a_success_snapshot(monkeypatch):
    monkeypatch.setattr(ws.settings, "APP_MODE", "mock")
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("synthetic settings database unavailable")

    with pytest.raises(RuntimeError, match="synthetic settings database unavailable"):
        await ws._build_payload(db)


@pytest.mark.parametrize("mode,has_synthetic_positions", [("mock", True), ("demo", False)])
async def test_mock_market_fixtures_stay_separate_from_demo_data(
    db, monkeypatch, mode, has_synthetic_positions
):
    monkeypatch.setattr(ws.settings, "APP_MODE", mode)
    db.add(AppSettings(id=1, auto_trading_enabled=False))
    await db.commit()

    payload = await ws._build_payload(db)

    assert bool(payload["positions"]) is has_synthetic_positions
    assert bool(payload["orders"]) is has_synthetic_positions
