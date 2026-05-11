"""
Shared system-control operations used by both the web API and Telegram bot.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.config import settings
from app.core.security import CredentialDecryptionError, decrypt_field
from app.db.models import AppSettings, AuditLog, BrokerConnection
from app.db.repositories import OrderRepository
from app.execution.engine import ExecutionEngine
from app.risk.engine import activate_kill_switch
from app.services.broker_connection_recovery import mark_broker_connection_reconnect_required
from app.services.live_readiness import LiveReadinessService
from app.services.market_regime import MarketRegimeService
from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


class SystemControlError(Exception):
    """Raised when a system-control operation cannot be completed safely."""


class SystemControlService:
    def __init__(self, db: AsyncSession, broker_user_id: uuid.UUID | None = None) -> None:
        self.db = db
        self.broker_user_id = broker_user_id

    async def _get_settings(self) -> AppSettings:
        result = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        app_settings = result.scalar_one_or_none()
        if not app_settings:
            raise SystemControlError("App settings are not initialized.")
        return app_settings

    async def _get_broker_connection(self) -> BrokerConnection | None:
        query = (
            select(BrokerConnection)
            .where(BrokerConnection.is_active == True)  # noqa: E712
            .where(BrokerConnection.environment == settings.APP_MODE)
            .order_by(BrokerConnection.updated_at.desc(), BrokerConnection.created_at.desc())
            .limit(1)
        )
        if self.broker_user_id is not None:
            query = query.where(BrokerConnection.user_id == self.broker_user_id)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def _get_broker(self):
        if settings.APP_MODE == "mock":
            from app.broker.mock_adapter import MockBrokerAdapter

            return MockBrokerAdapter()

        try:
            require_broker_environment(settings.APP_MODE, action="system control broker access")
        except SafetyPolicyViolation as exc:
            raise SystemControlError(exc.reason) from exc

        conn = await self._get_broker_connection()
        if not conn:
            raise SystemControlError("No active broker connection is configured.")
        try:
            require_broker_environment(conn.environment, action="system control broker access")
        except SafetyPolicyViolation as exc:
            raise SystemControlError(exc.reason) from exc

        try:
            api_key = decrypt_field(conn.api_key_encrypted)
            api_secret = decrypt_field(conn.api_secret_encrypted)
        except CredentialDecryptionError as exc:
            await mark_broker_connection_reconnect_required(
                self.db,
                conn,
                str(exc),
                actor="system_control",
                commit=True,
            )
            raise SystemControlError(str(exc)) from exc

        from app.broker.trading212 import Trading212Adapter

        return Trading212Adapter(api_key, api_secret, conn.environment)

    async def get_snapshot(self) -> dict[str, Any]:
        app_settings = await self._get_settings()
        pending_orders = len(await OrderRepository(self.db).list_pending())
        snapshot: dict[str, Any] = {
            "mode": settings.APP_MODE,
            "auto_trading_enabled": app_settings.auto_trading_enabled,
            "kill_switch_active": app_settings.kill_switch_active,
            "pending_orders": pending_orders,
            "broker_status": "not_connected",
            "account": None,
            "positions": [],
            "regime": await MarketRegimeService().evaluate(),
        }

        try:
            broker = await self._get_broker()
        except SystemControlError:
            return snapshot

        async with broker as active_broker:
            summary = await active_broker.get_account_summary()
            positions = await active_broker.get_positions()

        snapshot["broker_status"] = "connected"
        snapshot["account"] = {
            "free_cash": float(summary.get("free", 0)),
            "total_value": float(summary.get("total", 0)),
            "invested": float(summary.get("invested", 0)),
            "result": float(summary.get("result", 0)),
        }
        snapshot["positions"] = positions
        return snapshot

    async def get_positions_summary(self) -> list[dict[str, Any]]:
        broker = await self._get_broker()
        async with broker as active_broker:
            return await active_broker.get_positions()

    async def pause_auto_trading(self, actor: str) -> str:
        app_settings = await self._get_settings()
        if not app_settings.auto_trading_enabled:
            return "Auto-trading is already disabled."

        app_settings.auto_trading_enabled = False
        self.db.add(
            AuditLog(
                action="auto_trading_disabled",
                actor=actor,
                payload={"source": "system_control"},
                occurred_at=datetime.now(UTC),
            )
        )
        return "Auto-trading disabled."

    async def resume_auto_trading(self, actor: str) -> str:
        app_settings = await self._get_settings()
        if app_settings.kill_switch_active:
            raise SystemControlError("Cannot enable auto-trading while the kill switch is active.")
        if settings.APP_MODE == "live":
            readiness = await LiveReadinessService(self.db).evaluate()
            if not readiness["ready_for_live"]:
                blocker = readiness["blockers"][0] if readiness["blockers"] else (
                    "Live readiness checks are incomplete."
                )
                raise SystemControlError(
                    f"Cannot enable live auto-trading yet. {blocker}"
                )
        if app_settings.auto_trading_enabled:
            return "Auto-trading is already enabled."

        app_settings.auto_trading_enabled = True
        self.db.add(
            AuditLog(
                action="auto_trading_enabled",
                actor=actor,
                payload={"source": "system_control"},
                occurred_at=datetime.now(UTC),
            )
        )
        return "Auto-trading enabled."

    async def activate_kill_switch(self, actor: str) -> str:
        await activate_kill_switch(self.db, actor=actor)
        app_settings = await self._get_settings()
        app_settings.auto_trading_enabled = False
        self.db.add(
            AuditLog(
                action="emergency_kill_switch",
                actor=actor,
                payload={"source": "system_control"},
                occurred_at=datetime.now(UTC),
            )
        )
        return "Kill switch activated. Auto-trading halted."

    async def cancel_all_pending(self, actor: str) -> str:
        repo = OrderRepository(self.db)
        orders = await repo.list_pending()
        if not orders:
            return "No pending orders to cancel."

        broker = await self._get_broker()
        async with broker as active_broker:
            engine = ExecutionEngine(self.db, active_broker)
            for order in orders:
                await engine.cancel_order(order)

        self.db.add(
            AuditLog(
                action="emergency_cancel_all",
                actor=actor,
                payload={"source": "system_control", "cancelled_count": len(orders)},
                occurred_at=datetime.now(UTC),
            )
        )
        return f"Cancelled {len(orders)} pending orders."

    async def flatten_all(self, actor: str) -> str:
        broker = await self._get_broker()
        flattened = 0
        async with broker as active_broker:
            positions = await active_broker.get_positions()
            engine = ExecutionEngine(self.db, active_broker)
            for position in positions:
                qty = Decimal(str(position.get("quantity", 0)))
                if qty <= 0:
                    continue
                order = await engine.create_order_intent(
                    ticker=position["ticker"],
                    side="sell",
                    order_type="market",
                    quantity=qty,
                    is_dry_run=(settings.APP_MODE == "mock"),
                    estimated_price=Decimal(str(position.get("currentPrice", 0) or position.get("averagePrice", 0) or 0)),
                )
                await engine.submit_order(order)
                flattened += 1

        self.db.add(
            AuditLog(
                action="emergency_flatten_all",
                actor=actor,
                payload={"source": "system_control", "flattened": flattened},
                occurred_at=datetime.now(UTC),
            )
        )
        return f"Flattened {flattened} positions."

    @staticmethod
    def confirmation_expiry() -> datetime:
        return datetime.now(UTC) + timedelta(
            seconds=settings.TELEGRAM_CONFIRM_WINDOW_SECONDS
        )
