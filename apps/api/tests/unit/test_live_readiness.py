"""Tests for live-readiness recency and fail-closed attestation expiry."""

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
JUST_EXPIRED = timedelta(hours=24, minutes=1)
STILL_FRESH = timedelta(hours=23, minutes=59)

ATTESTATION_CHECK_KEYS = (
    "demo_validated",
    "broker_test_attested",
    "telegram_test_attested",
    "kill_switch_tested",
)


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
    overrides: dict[str, Any] | None = None,
) -> None:
    app_settings = await _app_settings(db)
    evidence: dict[str, Any] = {
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
        **(overrides or {}),
    }
    app_settings.extra = {
        **(app_settings.extra or {}),
        "live_readiness": evidence,
    }
    app_settings.live_trading_unlocked = live_trading_unlocked
    app_settings.kill_switch_active = kill_switch_active
    await db.commit()


async def _evaluate_with_evidence_override(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    field: str,
    value: Any,
    live_trading_unlocked: bool = True,
) -> dict[str, Any]:
    """Evaluate readiness with fresh evidence except one overridden field."""
    _set_live_runtime(monkeypatch)
    await _add_live_broker_connection(
        db,
        last_test_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    await _set_manual_evidence(
        db,
        recorded_at=datetime.now(UTC) - timedelta(hours=1),
        live_trading_unlocked=live_trading_unlocked,
        overrides={field: value},
    )
    return await LiveReadinessService(db).evaluate()


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
async def test_old_manual_attestations_expire_and_block(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replaces the pre-expiry characterization test: stale evidence now blocks."""
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

    for key in ATTESTATION_CHECK_KEYS:
        assert _check(status, key)["status"] == "fail"
    assert _check(status, "live_unlock_acknowledged")["status"] == "fail"
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_fresh_attestations_within_24_hours_pass(
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
        recorded_at=datetime.now(UTC) - STILL_FRESH,
        live_trading_unlocked=True,
    )

    status = await LiveReadinessService(db).evaluate()

    for key in ATTESTATION_CHECK_KEYS:
        assert _check(status, key)["status"] == "pass"
    assert _check(status, "live_unlock_acknowledged")["status"] == "pass"
    assert status["eligible_for_unlock"] is True
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
        recorded_at=datetime.now(UTC) - timedelta(minutes=5),
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
        recorded_at=datetime.now(UTC) - timedelta(minutes=5),
        live_trading_unlocked=True,
        kill_switch_active=True,
    )

    status = await LiveReadinessService(db).evaluate()

    assert _check(status, "kill_switch_clear")["status"] == "fail"
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_broker_test_attestation_expires_after_24h(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _evaluate_with_evidence_override(
        db,
        monkeypatch,
        field="broker_test_verified_at",
        value=(datetime.now(UTC) - JUST_EXPIRED).isoformat(),
    )

    check = _check(status, "broker_test_attested")
    assert check["status"] == "fail"
    assert "older than 24 hours" in check["detail"]
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_telegram_attestation_expires_after_24h(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _evaluate_with_evidence_override(
        db,
        monkeypatch,
        field="telegram_test_verified_at",
        value=(datetime.now(UTC) - JUST_EXPIRED).isoformat(),
    )

    check = _check(status, "telegram_test_attested")
    assert check["status"] == "fail"
    assert "older than 24 hours" in check["detail"]
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_kill_switch_drill_expires_after_24h(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _evaluate_with_evidence_override(
        db,
        monkeypatch,
        field="kill_switch_tested_at",
        value=(datetime.now(UTC) - JUST_EXPIRED).isoformat(),
    )

    check = _check(status, "kill_switch_tested")
    assert check["status"] == "fail"
    assert "older than 24 hours" in check["detail"]
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_demo_validation_expires_after_24h(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _evaluate_with_evidence_override(
        db,
        monkeypatch,
        field="demo_validated_at",
        value=(datetime.now(UTC) - JUST_EXPIRED).isoformat(),
    )

    check = _check(status, "demo_validated")
    assert check["status"] == "fail"
    assert "older than 24 hours" in check["detail"]
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_live_unlock_acknowledgement_expires_after_24h(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _evaluate_with_evidence_override(
        db,
        monkeypatch,
        field="live_unlock_acknowledged_at",
        value=(datetime.now(UTC) - JUST_EXPIRED).isoformat(),
    )

    assert _check(status, "live_unlock_acknowledged")["status"] == "fail"
    # Every other check is fresh, so only the expired acknowledgement blocks.
    assert status["eligible_for_unlock"] is True
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_missing_attestation_timestamp_fails_closed(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _evaluate_with_evidence_override(
        db,
        monkeypatch,
        field="kill_switch_tested_at",
        value=None,
    )

    assert _check(status, "kill_switch_tested")["status"] == "fail"
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_malformed_attestation_timestamp_fails_closed(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _evaluate_with_evidence_override(
        db,
        monkeypatch,
        field="broker_test_verified_at",
        value="not-a-timestamp",
    )

    assert _check(status, "broker_test_attested")["status"] == "fail"
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_future_skewed_attestation_timestamp_fails_closed(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _evaluate_with_evidence_override(
        db,
        monkeypatch,
        field="demo_validated_at",
        value=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    )

    assert _check(status, "demo_validated")["status"] == "fail"
    assert status["eligible_for_unlock"] is False
    assert status["ready_for_live"] is False


@pytest.mark.asyncio
async def test_unlocked_flag_without_acknowledgement_timestamp_fails_closed(
    db: AsyncSession,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _evaluate_with_evidence_override(
        db,
        monkeypatch,
        field="live_unlock_acknowledged_at",
        value=None,
    )

    assert _check(status, "live_unlock_acknowledged")["status"] == "fail"
    assert status["ready_for_live"] is False


DEFERRED_RECONCILIATION_REASON = (
    "Deferred: demo validation now expires after 24 hours; tying it to fresh "
    "reconciliation-backed evidence needs a reliable reconciliation-cycle link first."
)
DEFERRED_SESSION_REASON = (
    "Deferred: live unlock acknowledgement now expires after 24 hours; true "
    "same-session scoping needs a server-side session identity that does not exist yet."
)
DEFERRED_REASON_CODES_REASON = (
    "Deferred: expiry is enforced fail-closed, but the readiness API schema is "
    "unchanged; machine-readable freshness/reason-code fields are a later additive PR."
)


@pytest.mark.skip(reason=DEFERRED_RECONCILIATION_REASON)
def test_future_demo_validation_requires_fresh_reconciliation_evidence() -> None:
    """Activate when demo validation is tied to current reconciliation evidence."""


@pytest.mark.skip(reason=DEFERRED_SESSION_REASON)
def test_future_live_unlock_acknowledgement_is_session_scoped() -> None:
    """Activate when final live unlock acknowledgement becomes session-scoped."""


@pytest.mark.skip(reason=DEFERRED_REASON_CODES_REASON)
def test_future_expired_attestations_surface_expired_reason_codes() -> None:
    """Activate when readiness responses expose expired/stale reason codes."""
