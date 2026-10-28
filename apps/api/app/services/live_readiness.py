"""
Live-trading readiness evaluation and attestation workflow.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, BrokerConnection
from app.services.live_account import (
    LiveAccountBinding,
    LiveAccountError,
    bound_lock_wait,
    selected_live_account,
)
from app.services.live_evidence import (
    LiveEvidenceError,
    current_test_valid,
    validated_evidence,
    write_head,
)

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

# Manual attestations expire so stale evidence cannot keep readiness green.
# Missing, malformed, or future-skewed timestamps must always fail closed.
_ATTESTATION_MAX_AGE = timedelta(hours=24)


class LiveReadinessError(Exception):
    """Raised when a live-readiness action is invalid or unsafe."""


class LiveReadinessService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        owner_id: uuid.UUID | None = None,
        binding: LiveAccountBinding | None = None,
        lock: bool = False,
    ) -> None:
        self.db, self.owner_id, self.expected_binding, self.lock = db, owner_id, binding, lock
        self.current_binding: LiveAccountBinding | None = None
        self.current_head_id: uuid.UUID | None = None

    async def _get_settings(self) -> AppSettings:
        query = (
            select(AppSettings).where(AppSettings.id == 1).execution_options(populate_existing=True)
        )
        if self.lock:
            await bound_lock_wait(self.db)
            query = query.with_for_update()
        app_settings = (await self.db.execute(query)).scalar_one_or_none()
        if app_settings is None:
            raise LiveReadinessError("App settings are not initialized.")
        self.current_head_id = app_settings.live_readiness_audit_id
        return app_settings

    async def _get_live_broker_connection(self) -> BrokerConnection | None:
        self.current_binding = None
        owner = self.owner_id
        if self.expected_binding is not None:
            try:
                expected_owner = uuid.UUID(self.expected_binding.owner_id)
            except ValueError:
                return None
            if owner is not None and owner != expected_owner:
                return None
            owner = expected_owner
        try:
            binding, connection = await selected_live_account(self.db, owner, lock=self.lock)
        except LiveAccountError:
            return None
        if self.expected_binding is not None and binding != self.expected_binding:
            return None
        self.current_binding = binding
        return connection

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

    async def evaluate(self, *, require_market_data: bool = True) -> dict[str, Any]:
        from app.services.telegram_control import TelegramControlService

        # Match dispatch/action lock order: owner, connection, then global controls.
        live_connection = await self._get_live_broker_connection()
        app_settings = await self._get_settings()
        now = datetime.now(UTC)
        try:
            evidence, _ = await validated_evidence(
                self.db, app_settings, self.current_binding, live_connection
            )
        except LiveEvidenceError:
            evidence = {}
        telegram_status = TelegramControlService.status_payload()

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
            and self.current_binding is not None
            and await current_test_valid(self.db, live_connection, self.current_binding, now)
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

        market_source_ok = True
        if require_market_data and app_mode_live:
            from app.market_data.live_source import LiveMarketSourceError, resolve_live_source

            try:
                resolve_live_source()
            except LiveMarketSourceError:
                market_source_ok = False

        checks = [
            self._build_check(
                key="live_market_data_configuration",
                label="Real market-data configuration selected",
                passed=market_source_ok,
                detail="Live entries require an explicit resolvable real provider; configuration does not certify feed freshness.",
            ),
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
        # Global lock is always available, even with missing account/evidence.
        if action == "lock_live":
            self.lock = True
            app_settings = await self._get_settings()
            app_settings.live_trading_unlocked = False
            await write_head(
                self.db,
                app_settings,
                binding=None,
                connection=None,
                action=action,
                actor=actor,
                owner_id=None,
                notes=notes,
            )
            # Do not acquire owner/connection locks after the settings lock.
            self.lock = False
            self.expected_binding = None
            return await self.evaluate()

        self.lock = True
        if self.owner_id is None:
            raise LiveReadinessError(
                "An authenticated owner is required for a live readiness action."
            )
        connection = await self._get_live_broker_connection()
        if connection is None or self.current_binding is None:
            raise LiveReadinessError("An explicit authorized live Trading 212 account is required.")
        app_settings = await self._get_settings()
        evaluation = await self.evaluate()
        if action == "unlock_live" and not evaluation["eligible_for_unlock"]:
            raise LiveReadinessError(
                "Live trading cannot be unlocked until every bound readiness check passes."
            )
        if action == "record_broker_test":
            checked = next(
                item for item in evaluation["checks"] if item["key"] == "live_broker_test_recent"
            )
            if checked["status"] != "pass":
                raise LiveReadinessError(
                    "A successful current live broker test event is required first."
                )
        if action == "record_telegram_test":
            checked = next(item for item in evaluation["checks"] if item["key"] == "telegram_ready")
            if checked["status"] != "pass":
                raise LiveReadinessError(
                    "Telegram must be fully configured before its test can be recorded."
                )
        if action == "unlock_live":
            app_settings.live_trading_unlocked = True
        try:
            await write_head(
                self.db,
                app_settings,
                binding=self.current_binding,
                connection=connection,
                action=action,
                actor=actor,
                owner_id=connection.user_id,
                notes=notes,
            )
        except LiveEvidenceError as exc:
            raise LiveReadinessError(str(exc)) from exc
        return await self.evaluate()
