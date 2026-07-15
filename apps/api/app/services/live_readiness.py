"""
Live-trading readiness evaluation and attestation workflow.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, AuditLog, BrokerConnection

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ReadinessAction = Literal[
    "record_demo_validation",
    "record_broker_test",
    "record_telegram_test",
    "record_kill_switch_test",
    "unlock_live",
    "lock_live",
]

_EVIDENCE_KEY = "live_readiness"
_BROKER_TEST_MAX_AGE = timedelta(hours=24)
# Manual attestations expire so stale evidence cannot keep readiness green.
# Missing, malformed, or future-skewed timestamps must always fail closed.
_ATTESTATION_MAX_AGE = timedelta(hours=24)


class LiveReadinessError(Exception):
    """Raised when a live-readiness action is invalid or unsafe."""


class LiveReadinessService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_settings(self) -> AppSettings:
        result = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        app_settings = result.scalar_one_or_none()
        if not app_settings:
            raise LiveReadinessError("App settings are not initialized.")
        return app_settings

    async def _get_live_broker_connection(self) -> BrokerConnection | None:
        result = await self.db.execute(
            select(BrokerConnection)
            .where(
                BrokerConnection.environment == "live",
                BrokerConnection.is_active == True,  # noqa: E712
            )
            .order_by(BrokerConnection.updated_at.desc(), BrokerConnection.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _evidence(app_settings: AppSettings) -> dict[str, Any]:
        extra = app_settings.extra or {}
        evidence = extra.get(_EVIDENCE_KEY)
        return evidence if isinstance(evidence, dict) else {}

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not value or not isinstance(value, str):
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            return LiveReadinessService._normalize_timestamp(datetime.fromisoformat(normalized))
        except ValueError:
            return None

    @staticmethod
    def _normalize_timestamp(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _is_fresh(
        verified_at: datetime | None,
        now: datetime,
        max_age: timedelta = _ATTESTATION_MAX_AGE,
    ) -> bool:
        if verified_at is None:
            return False
        age = now - verified_at
        return timedelta(0) <= age <= max_age

    @staticmethod
    def _attestation_detail(
        *,
        fresh: bool,
        verified_at: datetime | None,
        fresh_detail: str,
        expired_detail: str,
        missing_detail: str,
    ) -> str:
        if fresh:
            return fresh_detail
        if verified_at is not None:
            return expired_detail
        return missing_detail

    @staticmethod
    def _status(passed: bool) -> Literal["pass", "fail"]:
        return "pass" if passed else "fail"

    @staticmethod
    def _build_check(
        *,
        key: str,
        label: str,
        passed: bool,
        detail: str,
        verified_at: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "status": LiveReadinessService._status(passed),
            "detail": detail,
            "verified_at": verified_at,
        }

    async def evaluate(self) -> dict[str, Any]:
        from app.services.telegram_control import TelegramControlService

        now = datetime.now(UTC)
        app_settings = await self._get_settings()
        evidence = self._evidence(app_settings)
        telegram_status = TelegramControlService.status_payload()
        live_connection = await self._get_live_broker_connection()

        demo_validated_at = self._parse_timestamp(evidence.get("demo_validated_at"))
        broker_verified_at = self._parse_timestamp(evidence.get("broker_test_verified_at"))
        telegram_verified_at = self._parse_timestamp(evidence.get("telegram_test_verified_at"))
        kill_switch_tested_at = self._parse_timestamp(evidence.get("kill_switch_tested_at"))
        unlock_acknowledged_at = self._parse_timestamp(evidence.get("live_unlock_acknowledged_at"))

        app_mode_live = settings.APP_MODE == "live"
        live_execution_enabled = bool(settings.LIVE_TRADING_ENABLED)
        live_broker_connected = live_connection is not None
        last_test_at = (
            self._normalize_timestamp(live_connection.last_test_at) if live_connection else None
        )
        broker_test_recent = bool(
            live_connection
            and live_connection.last_test_ok
            and self._is_fresh(last_test_at, now, _BROKER_TEST_MAX_AGE)
        )
        demo_validated_fresh = self._is_fresh(demo_validated_at, now)
        broker_attested_fresh = self._is_fresh(broker_verified_at, now)
        telegram_attested_fresh = self._is_fresh(telegram_verified_at, now)
        kill_switch_tested_fresh = self._is_fresh(kill_switch_tested_at, now)
        unlock_acknowledged_fresh = self._is_fresh(unlock_acknowledged_at, now)
        telegram_ready = bool(
            telegram_status["bot_configured"]
            and telegram_status["alert_chat_configured"]
            and telegram_status["webhook_secret_configured"]
            and telegram_status["control_enabled"]
        )
        kill_switch_clear = not app_settings.kill_switch_active
        live_unlock_acknowledged = bool(
            app_settings.live_trading_unlocked and unlock_acknowledged_fresh
        )

        checks = [
            self._build_check(
                key="app_mode_live",
                label="Server in live mode",
                passed=app_mode_live,
                detail="`APP_MODE` must be `live` before the app can place real orders.",
            ),
            self._build_check(
                key="live_execution_enabled",
                label="Live execution env enabled",
                passed=live_execution_enabled,
                detail="`LIVE_TRADING_ENABLED=true` must be set in the server environment.",
            ),
            self._build_check(
                key="live_broker_connected",
                label="Live broker connection present",
                passed=live_broker_connected,
                detail=(
                    "A live Trading 212 connection is active."
                    if live_broker_connected
                    else "Connect and save a live Trading 212 account before enabling auto-trading."
                ),
                verified_at=last_test_at,
            ),
            self._build_check(
                key="live_broker_test_recent",
                label="Live broker test passed recently",
                passed=broker_test_recent,
                detail=(
                    "The active live broker connection passed a test in the last 24 hours."
                    if broker_test_recent
                    else "Run a successful live broker connection test within the last 24 hours."
                ),
                verified_at=last_test_at,
            ),
            self._build_check(
                key="telegram_ready",
                label="Telegram supervision configured",
                passed=telegram_ready,
                detail=(
                    "Bot token, alert chat, webhook secret, and allowlists are configured."
                    if telegram_ready
                    else "Configure Telegram bot token, alert chat, webhook secret, and allowlists."
                ),
            ),
            self._build_check(
                key="demo_validated",
                label="Demo soak reviewed",
                passed=demo_validated_fresh,
                detail=self._attestation_detail(
                    fresh=demo_validated_fresh,
                    verified_at=demo_validated_at,
                    fresh_detail=(
                        f"Demo validation recorded by {evidence.get('demo_validated_by', 'an admin')}."
                    ),
                    expired_detail=(
                        "Demo validation is older than 24 hours; record a fresh demo/paper review."
                    ),
                    missing_detail="Record that demo/paper trading has been reviewed and accepted.",
                ),
                verified_at=demo_validated_at,
            ),
            self._build_check(
                key="broker_test_attested",
                label="Broker test manually reviewed",
                passed=broker_attested_fresh,
                detail=self._attestation_detail(
                    fresh=broker_attested_fresh,
                    verified_at=broker_verified_at,
                    fresh_detail=(
                        f"Live broker test review recorded by {evidence.get('broker_test_verified_by', 'an admin')}."
                    ),
                    expired_detail=(
                        "Live broker test review is older than 24 hours; "
                        "run a fresh live broker test and record a new review."
                    ),
                    missing_detail="Record that the latest live broker connection test was reviewed.",
                ),
                verified_at=broker_verified_at,
            ),
            self._build_check(
                key="telegram_test_attested",
                label="Telegram alert manually reviewed",
                passed=telegram_attested_fresh,
                detail=self._attestation_detail(
                    fresh=telegram_attested_fresh,
                    verified_at=telegram_verified_at,
                    fresh_detail=(
                        f"Telegram alert review recorded by {evidence.get('telegram_test_verified_by', 'an admin')}."
                    ),
                    expired_detail=(
                        "Telegram alert review is older than 24 hours; "
                        "send a new test alert and record a fresh review."
                    ),
                    missing_detail="Send a Telegram test alert and record that it arrived correctly.",
                ),
                verified_at=telegram_verified_at,
            ),
            self._build_check(
                key="kill_switch_tested",
                label="Kill switch drill completed",
                passed=kill_switch_tested_fresh,
                detail=self._attestation_detail(
                    fresh=kill_switch_tested_fresh,
                    verified_at=kill_switch_tested_at,
                    fresh_detail=(
                        f"Kill-switch drill recorded by {evidence.get('kill_switch_tested_by', 'an admin')}."
                    ),
                    expired_detail=(
                        "Kill-switch drill is older than 24 hours; perform and record a fresh drill."
                    ),
                    missing_detail=(
                        "Perform and record a kill-switch drill before allowing live automation."
                    ),
                ),
                verified_at=kill_switch_tested_at,
            ),
            self._build_check(
                key="kill_switch_clear",
                label="Kill switch currently clear",
                passed=kill_switch_clear,
                detail=(
                    "Kill switch is not active."
                    if kill_switch_clear
                    else "Disable the kill switch before resuming live auto-trading."
                ),
            ),
            self._build_check(
                key="live_unlock_acknowledged",
                label="Final live unlock acknowledged",
                passed=live_unlock_acknowledged,
                detail=(
                    f"Live trading unlocked by {evidence.get('live_unlock_acknowledged_by', 'an admin')}."
                    if live_unlock_acknowledged
                    else (
                        "Live unlock acknowledgement is missing or older than 24 hours; "
                        "lock and unlock live trading again to refresh it."
                        if app_settings.live_trading_unlocked
                        else "An admin must explicitly unlock live trading after the checklist passes."
                    )
                ),
                verified_at=unlock_acknowledged_at,
            ),
        ]

        eligible_for_unlock = all(
            check["status"] == "pass"
            for check in checks
            if check["key"] != "live_unlock_acknowledged"
        )
        ready_for_live = eligible_for_unlock and live_unlock_acknowledged
        blockers = [
            check["detail"]
            for check in checks
            if check["status"] != "pass"
            and (check["key"] != "live_unlock_acknowledged" or eligible_for_unlock)
        ]

        return {
            "mode": settings.APP_MODE,
            "live_execution_enabled": live_execution_enabled,
            "live_trading_unlocked": app_settings.live_trading_unlocked,
            "eligible_for_unlock": eligible_for_unlock,
            "ready_for_live": ready_for_live,
            "blockers": blockers,
            "checks": checks,
        }

    async def apply_action(
        self,
        *,
        action: ReadinessAction,
        actor: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        app_settings = await self._get_settings()
        evidence = dict(self._evidence(app_settings))
        now = datetime.now(UTC)

        if action == "unlock_live":
            evaluation = await self.evaluate()
            if not evaluation["eligible_for_unlock"]:
                raise LiveReadinessError(
                    "Live trading cannot be unlocked until every readiness check passes."
                )
            app_settings.live_trading_unlocked = True
            evidence["live_unlock_acknowledged_at"] = now.isoformat()
            evidence["live_unlock_acknowledged_by"] = actor
            if notes:
                evidence["live_unlock_notes"] = notes
            audit_action = "live_trading_unlocked"
        elif action == "lock_live":
            app_settings.live_trading_unlocked = False
            evidence["live_unlock_acknowledged_at"] = None
            evidence["live_unlock_acknowledged_by"] = None
            if notes:
                evidence["live_lock_notes"] = notes
            audit_action = "live_trading_locked"
        else:
            evaluation = await self.evaluate()
            field_map = {
                "record_demo_validation": ("demo_validated_at", "demo_validated_by", "demo_notes"),
                "record_broker_test": (
                    "broker_test_verified_at",
                    "broker_test_verified_by",
                    "broker_test_notes",
                ),
                "record_telegram_test": (
                    "telegram_test_verified_at",
                    "telegram_test_verified_by",
                    "telegram_test_notes",
                ),
                "record_kill_switch_test": (
                    "kill_switch_tested_at",
                    "kill_switch_tested_by",
                    "kill_switch_test_notes",
                ),
            }
            if action == "record_broker_test":
                broker_check = next(
                    check
                    for check in evaluation["checks"]
                    if check["key"] == "live_broker_test_recent"
                )
                if broker_check["status"] != "pass":
                    raise LiveReadinessError(
                        "A successful live broker test from the last 24 hours is required first."
                    )
            if action == "record_telegram_test":
                telegram_check = next(
                    check for check in evaluation["checks"] if check["key"] == "telegram_ready"
                )
                if telegram_check["status"] != "pass":
                    raise LiveReadinessError(
                        "Telegram must be fully configured before its test can be recorded."
                    )
            timestamp_field, actor_field, notes_field = field_map[action]
            evidence[timestamp_field] = now.isoformat()
            evidence[actor_field] = actor
            if notes:
                evidence[notes_field] = notes
            audit_action = "live_readiness_recorded"

        app_settings.extra = {
            **(app_settings.extra or {}),
            _EVIDENCE_KEY: evidence,
        }
        self.db.add(
            AuditLog(
                action=audit_action,
                actor=actor,
                payload={"action": action, "notes": notes},
                occurred_at=now,
            )
        )
        await self.db.flush()
        await self.db.refresh(app_settings)
        return await self.evaluate()
