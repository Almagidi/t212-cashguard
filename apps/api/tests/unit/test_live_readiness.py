"""Characterization tests for live-readiness recency and attestation expiry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, BrokerConnection, User
from app.services.live_readiness import LiveReadinessService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

OLD_EVIDENCE_AT = datetime(2026, 1, 10, 9, 0, tzinfo=UTC)


def _check(status: dict[str, Any], key: str) -> dict[str, Any]:
    return next(check for check in status["checks"] if check["key"] == key)


def _set_live_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")


async def _admin_user(db: AsyncSession) -> User:
    return (await db.execute(select(User).where(User.email == "admin@test.com"))).scalar_one()


async def _app_settings(db: AsyncSession) -> AppSettings:
    return (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalar_one()


async def _add_live_broker_connection(
    db: AsyncSession,
    *,
    last_test_at: datetime,
    last_test_ok: bool = True,
) -> None:
    user = await _admin_user(db)
    db.add(
        BrokerConnection(
            id=uuid.uuid4(),
            user_id=user.id,
            broker="trading212",
            environment="live",
            api_key_encrypted="enc-key",
            api_secret_encrypted="enc-secret",
            is_active=True,
            last_test_at=last_test_at,
            last_test_ok=last_test_ok,
            account_id="LIVE-123",
            account_currency="USD",
        )
    )
    await db.commit()


async def _set_manual_evidence(
    db: AsyncSession,
    *,
    recorded_at: datetime = OLD_EVIDENCE_AT,
    live_trading_unlocked: bool = False,
    kill_switch_active: bool = False,
) -> None:
    app_settings = await _app_settings(db)
    app_settings.extra = {
        **(app_settings.extra or {}),
        "live_readiness": {
            "demo_validated_at": recorded_at.isoformat(),
            "demo_validated_by": "admin@test.com",
            "broker_test_verified_at": recorded_at.isoformat(),
            "broker_test_verified_by": "admin@test.com",
            "telegram_test_verified_at": recorded_at.isoformat(),
            "telegram_test_verified_by": "admin@test.com",
            "kill_switch_tested_at": recorded_at.isoformat(),
            "kill_switch_tested_by": "admin@test.com",
            "live_unlock_acknowledged_at": recorded_at.isoformat(),
            "live_unlock_acknowledged_by": "admin@test.com",
        },
    }
    app_settings.live_trading_unlocked = live_trading_unlocked
    app_settings.kill_switch_active = kill_switch_active
    await db.commit()


@pytest.mark.asyncio
async def test_live_broker_test_older_than_24_hours_fails_recency(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_live_runtime(monkeypatch)
    await _add_live_broker_connection(
        db,
        last_test_at=datetime.now(UTC) - timedelta(hours=24, minutes=1),
    )

    status = await LiveReadinessService(db).evaluate()

    assert _check(status, "live_broker_connected")["status"] == "pass"
    assert _check(status, "live_broker_test_recent")["status"] == "fail"
    assert status["eligible_for_unlock"] is False


@pytest.mark.asyncio
async def test_live_broker_test_within_24_hours_passes_recency(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_live_runtime(monkeypatch)
    await _add_live_broker_connection(
        db,
        last_test_at=datetime.now(UTC) - timedelta(hours=23, minutes=59),
    )

    status = await LiveReadinessService(db).evaluate()

    assert _check(status, "live_broker_test_recent")["status"] == "pass"


@pytest.mark.asyncio
async def test_old_manual_attestations_remain_accepted_currently(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_live_runtime(monkeypatch)
    await _add_live_broker_connection(
        db,
        last_test_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    await _set_manual_evidence(
        db,
        recorded_at=OLD_EVIDENCE_AT,
        live_trading_unlocked=True,
    )

    status = await LiveReadinessService(db).evaluate()

    assert _check(status, "demo_validated")["status"] == "pass"
    assert _check(status, "broker_test_attested")["status"] == "pass"
    assert _check(status, "telegram_test_attested")["status"] == "pass"
    assert _check(status, "kill_switch_tested")["status"] == "pass"
    assert _check(status, "live_unlock_acknowledged")["status"] == "pass"
    assert status["ready_for_live"] is True


@pytest.mark.asyncio
async def test_stale_broker_test_is_not_offset_by_broker_attestation(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_live_runtime(monkeypatch)
    await _add_live_broker_connection(
        db,
        last_test_at=datetime.now(UTC) - timedelta(days=2),
    )
    await _set_manual_evidence(
        db,
        recorded_at=OLD_EVIDENCE_AT,
        live_trading_unlocked=True,
    )

    status = await LiveReadinessService(db).evaluate()

    assert _check(status, "live_broker_test_recent")["status"] == "fail"
    assert _check(status, "broker_test_attested")["status"] == "pass"
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_global_kill_switch_blocks_readiness(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_live_runtime(monkeypatch)
    await _add_live_broker_connection(
        db,
        last_test_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    await _set_manual_evidence(
        db,
        recorded_at=OLD_EVIDENCE_AT,
        live_trading_unlocked=True,
        kill_switch_active=True,
    )

    status = await LiveReadinessService(db).evaluate()

    assert _check(status, "kill_switch_clear")["status"] == "fail"
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


FUTURE_SKIP_REASON = "Skipped until Level C expiry enforcement PR is approved and implemented."


@pytest.mark.skip(reason=FUTURE_SKIP_REASON)
def test_future_broker_test_attestation_expires_after_24h() -> None:
    """Activate when broker-test manual review receives a 24h TTL."""


@pytest.mark.skip(reason=FUTURE_SKIP_REASON)
def test_future_telegram_attestation_expires_after_24h() -> None:
    """Activate when delivered Telegram alert review receives a 24h TTL."""


@pytest.mark.skip(reason=FUTURE_SKIP_REASON)
def test_future_kill_switch_drill_expires_before_live_smoke() -> None:
    """Activate when the kill-switch drill must be fresh for live smoke tests."""


@pytest.mark.skip(reason=FUTURE_SKIP_REASON)
def test_future_demo_validation_requires_fresh_reconciliation_evidence() -> None:
    """Activate when demo validation is tied to current reconciliation evidence."""


@pytest.mark.skip(reason=FUTURE_SKIP_REASON)
def test_future_live_unlock_acknowledgement_is_session_scoped() -> None:
    """Activate when final live unlock acknowledgement becomes session-scoped."""


@pytest.mark.skip(reason=FUTURE_SKIP_REASON)
def test_future_expired_attestations_surface_expired_reason_codes() -> None:
    """Activate when readiness responses expose expired/stale reason codes."""
