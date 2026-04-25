from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.services.strategy_promotion import (
    StrategyPromotionError,
    StrategyPromotionService,
)


def _strategy(**overrides):
    base = {
        "id": uuid.uuid4(),
        "name": "Opening range promotion candidate",
        "params": {},
        "risk_profile_id": uuid.uuid4(),
        "allowed_tickers": ["AAPL", "MSFT"],
        "is_live": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _signal(strategy_id, generated_at, *, risk_rejected=False):
    return SimpleNamespace(
        strategy_id=strategy_id,
        generated_at=generated_at,
        risk_rejected=risk_rejected,
    )


def _order(generated_at, *, is_dry_run, status):
    return SimpleNamespace(
        created_at=generated_at,
        is_dry_run=is_dry_run,
        status=status,
    )


@pytest.mark.asyncio
async def test_evaluate_strategy_reports_live_ready_promotion_state(monkeypatch):
    async def ready_live_readiness(_self):
        return {"eligible_for_unlock": True, "blockers": []}

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(
        "app.services.strategy_promotion.LiveReadinessService.evaluate",
        ready_live_readiness,
    )

    now = datetime(2024, 1, 10, 14, 30, tzinfo=UTC)
    demo_promoted_at = now - timedelta(days=3)
    demo_reviewed_at = now - timedelta(hours=1)
    strategy = _strategy(
        is_live=True,
        params={
            "promotion": {
                "dry_run_reviewed_at": (now - timedelta(days=4)).isoformat(),
                "dry_run_reviewed_by": "ops",
                "demo_promoted_at": demo_promoted_at.isoformat(),
                "demo_promoted_by": "ops",
                "demo_reviewed_at": demo_reviewed_at.isoformat(),
                "demo_reviewed_by": "ops",
            }
        },
    )
    signals = [
        _signal(strategy.id, now - timedelta(days=5)),
        _signal(strategy.id, now - timedelta(days=4)),
        _signal(strategy.id, now - timedelta(days=3, minutes=1)),
        _signal(strategy.id, now - timedelta(days=2)),
        _signal(strategy.id, now - timedelta(days=1), risk_rejected=True),
        _signal(strategy.id, now),
    ]
    orders = [
        _order(now - timedelta(days=5), is_dry_run=True, status="filled"),
        _order(now - timedelta(days=2), is_dry_run=False, status="filled"),
        _order(now - timedelta(days=1), is_dry_run=False, status="filled"),
        _order(now, is_dry_run=False, status="cancelled"),
    ]
    service = StrategyPromotionService(MagicMock())
    service._load_strategy_activity = AsyncMock(return_value=(signals, orders))

    status = await service.evaluate_strategy(strategy)

    assert status["current_stage"] == "demo"
    assert status["eligible_for_demo"] is True
    assert status["eligible_for_live"] is True
    assert status["recommended_next_action"] == "promote_to_live"
    assert status["metrics"]["dry_run_signal_count"] == 3
    assert status["metrics"]["demo_signal_count"] == 3
    assert status["metrics"]["demo_order_count"] == 3
    assert status["metrics"]["demo_filled_count"] == 2
    assert status["metrics"]["demo_cancelled_count"] == 1
    assert status["metrics"]["demo_fill_rate"] == pytest.approx(2 / 3)
    assert status["metrics"]["demo_risk_block_rate"] == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_evaluate_strategy_surfaces_dry_run_blockers(monkeypatch):
    async def blocked_live_readiness(_self):
        return {
            "eligible_for_unlock": False,
            "blockers": ["Broker connection needs attention."],
        }

    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(
        "app.services.strategy_promotion.LiveReadinessService.evaluate",
        blocked_live_readiness,
    )

    now = datetime(2024, 1, 10, 14, 30, tzinfo=UTC)
    strategy = _strategy(
        is_live=True,
        risk_profile_id=None,
        allowed_tickers=[],
    )
    service = StrategyPromotionService(MagicMock())
    service._load_strategy_activity = AsyncMock(
        return_value=([_signal(strategy.id, now)], [])
    )

    status = await service.evaluate_strategy(strategy)

    assert status["current_stage"] == "dry_run"
    assert status["eligible_for_demo"] is False
    assert status["eligible_for_live"] is False
    assert status["recommended_next_action"] == "record_dry_run_review"
    assert any("Broker execution is enabled" in blocker for blocker in status["blockers"])
    assert any("Attach a risk profile" in blocker for blocker in status["blockers"])
    assert any("Switch the app out of live mode" in blocker for blocker in status["blockers"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "params", "expected"),
    [
        ("mock", {}, (True, None)),
        ("demo", {}, (False, "strategy_not_promoted_to_demo")),
        (
            "demo",
            {"promotion": {"demo_promoted_at": "2024-01-01T09:30:00Z"}},
            (True, None),
        ),
        (
            "live",
            {"promotion": {"demo_promoted_at": "2024-01-01T09:30:00Z"}},
            (False, "strategy_not_approved_for_live"),
        ),
        (
            "live",
            {"promotion": {"live_approved_at": "2024-01-02T09:30:00Z"}},
            (True, None),
        ),
    ],
)
async def test_execution_gate_requires_stage_approval(monkeypatch, mode, params, expected):
    monkeypatch.setattr(settings, "APP_MODE", mode)

    service = StrategyPromotionService(MagicMock())
    assert await service.execution_gate(_strategy(is_live=True, params=params)) == expected


@pytest.mark.asyncio
async def test_execution_gate_allows_dry_run_strategy_in_live_mode(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "live")

    service = StrategyPromotionService(MagicMock())

    assert await service.execution_gate(_strategy(is_live=False)) == (True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "status", "expected_key"),
    [
        (
            "record_dry_run_review",
            {
                "metrics": {"dry_run_signal_count": 1, "dry_run_order_count": 0},
                "eligible_for_demo": False,
                "eligible_for_live": False,
            },
            "dry_run_review_notes",
        ),
        (
            "promote_to_demo",
            {"metrics": {}, "eligible_for_demo": True, "eligible_for_live": False},
            "demo_promotion_notes",
        ),
        (
            "record_demo_review",
            {
                "metrics": {"demo_order_count": 1},
                "eligible_for_demo": True,
                "eligible_for_live": False,
            },
            "demo_review_notes",
        ),
        (
            "promote_to_live",
            {"metrics": {}, "eligible_for_demo": True, "eligible_for_live": True},
            "live_approval_notes",
        ),
        (
            "demote_to_dry_run",
            {"metrics": {}, "eligible_for_demo": False, "eligible_for_live": False},
            "demoted_to_dry_run_notes",
        ),
        (
            "revoke_live_promotion",
            {"metrics": {}, "eligible_for_demo": False, "eligible_for_live": False},
            "live_approval_revoked_notes",
        ),
    ],
)
async def test_apply_action_updates_promotion_state_and_audits(action, status, expected_key):
    strategy = _strategy(
        params={"promotion": {"live_approved_at": "2024-01-02T09:30:00Z"}}
    )
    db = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    service = StrategyPromotionService(db)
    service._get_strategy = AsyncMock(return_value=strategy)
    service.evaluate_strategy = AsyncMock(side_effect=[status, {"after": action}])

    result = await service.apply_action(
        strategy_id=strategy.id,
        action=action,
        actor="ops",
        notes="reviewed",
    )

    assert result == {"after": action}
    assert strategy.params["promotion"][expected_key] == "reviewed"
    db.add.assert_called_once()
    db.flush.assert_awaited_once()
    db.refresh.assert_awaited_once_with(strategy)


@pytest.mark.asyncio
async def test_apply_action_rejects_unsafe_transitions():
    strategy = _strategy()
    service = StrategyPromotionService(MagicMock())
    service._get_strategy = AsyncMock(return_value=strategy)
    service.evaluate_strategy = AsyncMock(
        return_value={
            "metrics": {"dry_run_signal_count": 0, "dry_run_order_count": 0},
            "eligible_for_demo": False,
            "eligible_for_live": False,
        }
    )

    with pytest.raises(StrategyPromotionError, match="dry-run first"):
        await service.apply_action(
            strategy_id=strategy.id,
            action="record_dry_run_review",
            actor="ops",
        )


@pytest.mark.asyncio
async def test_apply_action_rejects_unknown_action():
    strategy = _strategy()
    service = StrategyPromotionService(MagicMock())
    service._get_strategy = AsyncMock(return_value=strategy)
    service.evaluate_strategy = AsyncMock(
        return_value={"metrics": {}, "eligible_for_demo": False, "eligible_for_live": False}
    )

    with pytest.raises(StrategyPromotionError, match="Unsupported promotion action"):
        await service.apply_action(
            strategy_id=strategy.id,
            action="not_a_real_action",  # type: ignore[arg-type]
            actor="ops",
        )
