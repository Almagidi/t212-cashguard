from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.db.models import AppSettings, RiskProfile
from app.risk.engine import RiskEngine, RiskViolation
from app.services.feed_health import record_feed_health, reset_feed_health
from app.services.market_intelligence_monitor import MarketIntelligenceMonitor


@pytest.fixture(autouse=True)
def reset_feed_health_state():
    reset_feed_health()
    yield
    reset_feed_health()


@pytest.mark.asyncio
async def test_market_intelligence_monitor_alerts_once_per_state(db, monkeypatch):
    from app.services import market_intelligence_monitor as monitor_module

    db.add(AppSettings(id=1))
    db.add(
        RiskProfile(
            id=uuid.uuid4(),
            name="Default",
            max_risk_per_trade_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("3.0"),
            max_open_positions=5,
            max_position_size_pct=Decimal("10.0"),
            max_trades_per_day=10,
            stop_after_consecutive_losses=3,
            symbol_cooldown_seconds=300,
            force_flat_eod=True,
            is_default=True,
        )
    )
    await db.commit()

    alerts: list[tuple[str, dict]] = []

    async def fake_regime_alert(db, **kwargs):
        del db
        alerts.append(("regime", kwargs))

    async def fake_feed_issue(db, **kwargs):
        del db
        alerts.append(("feed_issue", kwargs))

    async def fake_feed_recovered(db, **kwargs):
        del db
        alerts.append(("feed_recovered", kwargs))

    class FakeRegimeService:
        async def evaluate(self):
            return {
                "regime": "risk_off",
                "detail": "Breadth deteriorated sharply.",
                "suppressed_strategies": ["orb", "closing_momentum"],
            }

    monkeypatch.setattr(monitor_module, "MarketRegimeService", FakeRegimeService)
    monkeypatch.setattr(monitor_module, "alert_regime_shift", fake_regime_alert)
    monkeypatch.setattr(monitor_module, "alert_feed_health_issue", fake_feed_issue)
    monkeypatch.setattr(monitor_module, "alert_feed_health_recovered", fake_feed_recovered)

    reset_feed_health()
    record_feed_health(
        provider="alpaca_primary_polygon_validator",
        ticker="AAPL",
        status="degraded",
        detail="Quote divergence exceeded tolerance.",
        used_source="alpaca",
        validator_source="polygon",
        divergence_pct=2.4,
    )

    monitor = MarketIntelligenceMonitor(db)
    await monitor.evaluate_and_alert()
    await monitor.evaluate_and_alert()

    assert [kind for kind, _ in alerts].count("regime") == 1
    assert [kind for kind, _ in alerts].count("feed_issue") == 1
    assert "AAPL" in alerts[1][1]["affected_symbols"]


@pytest.mark.asyncio
async def test_risk_engine_blocks_unsafe_market_conditions(db):
    db.add(AppSettings(id=1))
    await db.commit()

    engine = RiskEngine(db)

    with pytest.raises(RiskViolation, match="blocked in unsafe regime"):
        await engine.check_market_conditions(
            ticker="AAPL",
            strategy_type="orb",
            market_regime={
                "regime": "unsafe",
                "suppressed_strategies": ["orb", "opening_fade"],
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market_regime", "expected"),
    [
        (None, "unknown market regime"),
        ({}, "unknown market regime"),
        ({"regime": "unknown", "suppressed_strategies": []}, "unknown market regime"),
        ({"regime": "high_volatility", "suppressed_strategies": []}, "high_volatility regime"),
        ({"regime": "mystery", "suppressed_strategies": []}, "invalid market regime"),
    ],
)
async def test_risk_engine_blocks_untrusted_market_regime_states(db, market_regime, expected):
    db.add(AppSettings(id=1))
    await db.commit()

    engine = RiskEngine(db)

    with pytest.raises(RiskViolation, match=expected):
        await engine.check_market_conditions(
            ticker="AAPL",
            strategy_type="orb",
            market_regime=market_regime,
        )


@pytest.mark.asyncio
async def test_risk_engine_blocks_mean_reversion_on_fresh_catalyst(db):
    db.add(AppSettings(id=1))
    await db.commit()

    engine = RiskEngine(db)

    with pytest.raises(RiskViolation, match="mean-reversion unreliable"):
        await engine.check_market_conditions(
            ticker="TSLA",
            strategy_type="opening_fade",
            market_regime={"regime": "trending_up", "suppressed_strategies": []},
            watchlist_context={
                "catalyst_score": 0.88,
                "catalyst_event_type": "earnings",
            },
        )
