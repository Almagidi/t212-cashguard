"""
Shared system-control operations used by both the web API and Telegram bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import select

from app.broker.provider import (
    BrokerProviderCredentials,
    BrokerProviderPurpose,
    BrokerProviderRequest,
    BrokerProviderValidationError,
    BrokerRuntimeEnvironment,
    create_trading212_provider_adapter,
)
from app.core.config import settings
from app.core.security import CredentialDecryptionError, decrypt_field
from app.db.models import AppSettings, AuditLog, BrokerConnection, Order, OrderEvent
from app.db.repositories import OrderRepository
from app.execution.engine import ExecutionEngine, OrderCancellationFailed
from app.execution.state_machine import InvalidOrderTransition, require_already_finished
from app.risk.engine import activate_kill_switch
from app.services.broker_connection_recovery import mark_broker_connection_reconnect_required
from app.services.live_readiness import LiveReadinessService
from app.services.market_regime import MarketRegimeService
from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


class SystemControlError(Exception):
    """Raised when a system-control operation cannot be completed safely."""


@dataclass(frozen=True)
class CancellationSummary:
    cancelled: int
    failed: int
    # Orders that were listed as pending but had ended (filled, rejected) by the time
    # their turn came. There was nothing left to cancel.
    already_finished: int = 0
    # Which orders those were, and which still need attention, so that the operator does
    # not have to go looking: "TICKER" for a failed cancellation, "TICKER status" for an
    # order that had ended. A fill among them is exposure the cancel did not prevent.
    failed_orders: tuple[str, ...] = ()
    finished_orders: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        if self.cancelled == 0 and self.failed == 0 and self.already_finished == 0:
            return "No pending orders to cancel."
        finished = ""
        if self.already_finished:
            finished = f" {self.already_finished} had already finished" + (
                f" ({', '.join(self.finished_orders)})." if self.finished_orders else "."
            )
        if self.failed:
            still_open = f" ({', '.join(self.failed_orders)})" if self.failed_orders else ""
            return (
                f"Cancelled {self.cancelled} pending orders; {self.failed} cancellation "
                f"attempts require reconciliation{still_open}.{finished}"
            )
        return f"Cancelled {self.cancelled} pending orders.{finished}"


@dataclass(frozen=True)
class FlattenSummary:
    message: str
    # True when the safety policy (the kill switch, live readiness) did not allow an exit
    # order to be sent. Nothing after that point was attempted.
    refused: bool
    # Exit orders the broker accepted.
    flattened: int = 0
    # True when no exit request left this process at all: none accepted, none failed at
    # the broker, none with an unknown outcome.
    nothing_went_out: bool = True
    # False when anything is left for the operator: a refusal, a failed exit, or an exit
    # whose outcome is unknown.
    complete: bool = True


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

    async def _get_broker(self, purpose: BrokerProviderPurpose) -> Any:
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

        try:
            return create_trading212_provider_adapter(
                BrokerProviderRequest(
                    broker_id="trading212",
                    environment=cast("BrokerRuntimeEnvironment", conn.environment),
                    purpose=purpose,
                    user_id=conn.user_id,
                    account_id=getattr(conn, "account_id", None),
                ),
                BrokerProviderCredentials(api_key=api_key, api_secret=api_secret),
                app_mode=settings.APP_MODE,
                live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
            )
        except BrokerProviderValidationError as exc:
            raise SystemControlError(str(exc)) from exc

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
            broker = await self._get_broker("operator_system_control_read")
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
        broker = await self._get_broker("operator_system_control_read")
        async with broker as active_broker:
            return cast("list[dict[str, Any]]", await active_broker.get_positions())

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
                blocker = (
                    readiness["blockers"][0]
                    if readiness["blockers"]
                    else ("Live readiness checks are incomplete.")
                )
                raise SystemControlError(f"Cannot enable live auto-trading yet. {blocker}")
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
        return (await self.cancel_all_pending_summary(actor)).message

    async def cancel_all_pending_summary(self, actor: str) -> CancellationSummary:
        repo = OrderRepository(self.db)
        orders = await repo.list_pending()
        if not orders:
            return CancellationSummary(cancelled=0, failed=0)

        cancelled = 0
        failed = 0
        # An order can end (a fill recorded by reconciliation, say) between being listed
        # and being reached. That is not a reason to stop: the orders after it still have
        # to be cancelled, and the cancellations already made still have to be saved.
        already_finished = 0
        failed_orders: list[Order] = []
        finished_orders: list[Order] = []
        paper_orders = [order for order in orders if order.is_dry_run]
        external_orders = [order for order in orders if not order.is_dry_run]
        if paper_orders:
            paper_engine = ExecutionEngine(self.db, None)
            for order in paper_orders:
                try:
                    await paper_engine.cancel_order(order)
                    cancelled += 1
                except InvalidOrderTransition:
                    require_already_finished(order)
                    already_finished += 1
                    finished_orders.append(order)

        if external_orders:
            broker = await self._get_broker("operator_system_control_emergency")
            async with broker as active_broker:
                engine = ExecutionEngine(self.db, active_broker)
                for order in external_orders:
                    try:
                        await engine.cancel_order(order)
                        cancelled += 1
                    except OrderCancellationFailed:
                        failed += 1
                        failed_orders.append(order)
                    except InvalidOrderTransition:
                        require_already_finished(order)
                        already_finished += 1
                        finished_orders.append(order)

        audit_payload: dict[str, Any] = {
            "source": "system_control",
            "cancelled_count": cancelled,
        }
        if failed:
            audit_payload["failed_count"] = failed
            audit_payload["failed_orders"] = [
                {"order_id": str(order.id), "ticker": order.ticker} for order in failed_orders
            ]
        if already_finished:
            audit_payload["already_finished_count"] = already_finished
            audit_payload["already_finished_orders"] = [
                {"order_id": str(order.id), "ticker": order.ticker, "status": order.status}
                for order in finished_orders
            ]
        if failed or already_finished:
            log.warning(
                "system_control.cancel_all_needs_attention",
                failed=[str(order.id) for order in failed_orders],
                already_finished={str(order.id): order.status for order in finished_orders},
            )
        self.db.add(
            AuditLog(
                action="emergency_cancel_all",
                actor=actor,
                payload=audit_payload,
                occurred_at=datetime.now(UTC),
            )
        )
        return CancellationSummary(
            cancelled=cancelled,
            failed=failed,
            already_finished=already_finished,
            failed_orders=tuple(order.ticker for order in failed_orders),
            finished_orders=tuple(f"{order.ticker} {order.status}" for order in finished_orders),
        )

    async def flatten_all(self, actor: str) -> str:
        return (await self.flatten_all_summary(actor)).message

    async def flatten_all_summary(self, actor: str) -> FlattenSummary:
        broker = await self._get_broker("operator_system_control_emergency")
        flattened = 0
        unconfirmed = 0
        failed = 0
        already_open = 0
        refused_reason: str | None = None
        still_open: list[str] = []
        unconfirmed_tickers: list[str] = []
        failed_tickers: list[str] = []
        async with broker as active_broker:
            positions = await active_broker.get_positions()
            engine = ExecutionEngine(self.db, active_broker)
            for position in positions:
                qty = Decimal(str(position.get("quantity", 0)))
                if qty <= 0:
                    continue
                if refused_reason is not None:
                    # Nothing more is attempted after a refusal; the position is named.
                    still_open.append(str(position["ticker"]))
                    continue
                asked_at = datetime.now(UTC)
                order = await engine.create_order_intent(
                    ticker=position["ticker"],
                    side="sell",
                    order_type="market",
                    quantity=qty,
                    is_dry_run=(settings.APP_MODE == "mock"),
                    estimated_price=Decimal(
                        str(position.get("currentPrice", 0) or position.get("averagePrice", 0) or 0)
                    ),
                )
                if order.status == "pending_intent":
                    try:
                        order = await engine.submit_order(order)
                    except SafetyPolicyViolation as exc:
                        # Trading is halted, or not ready: the exit may not be sent. Say
                        # so, close the intent that was not sent, and stop. Raising from
                        # here would turn the refusal into an error page and discard the
                        # record of it. Only an intent this call created is closed: one
                        # that was already there belongs to whoever made it.
                        if await self._created_since(order, asked_at):
                            await engine.cancel_order(order)
                        refused_reason = exc.reason
                        still_open.append(str(position["ticker"]))
                        continue
                elif order.status != "submission_unknown":
                    # An identical exit order is already working; it is not sent again.
                    already_open += 1
                    continue
                if order.status == "submission_unknown":
                    unconfirmed += 1
                    unconfirmed_tickers.append(str(position["ticker"]))
                elif order.status in {"error", "rejected"}:
                    failed += 1
                    failed_tickers.append(str(position["ticker"]))
                else:
                    flattened += 1

        audit_payload: dict[str, Any] = {"source": "system_control", "flattened": flattened}
        message = f"Flattened {flattened} positions."
        if refused_reason is not None:
            audit_payload["refused"] = refused_reason
            audit_payload["not_attempted"] = still_open
            nothing_went_out = flattened + unconfirmed + failed == 0
            message = (
                f"Flatten refused: {refused_reason} "
                + (
                    "No exit order was sent."
                    if nothing_went_out
                    else f"{flattened} exit order(s) had been accepted by the broker before that."
                )
                + f" {len(still_open)} position(s) remain OPEN and were not attempted"
                + f" ({', '.join(still_open)})."
            )
        if unconfirmed:
            audit_payload["unconfirmed"] = unconfirmed
            audit_payload["unconfirmed_tickers"] = unconfirmed_tickers
            message += (
                f" {unconfirmed} exit order(s) have an unknown outcome and need reconciliation"
                f" ({', '.join(unconfirmed_tickers)})."
            )
        if failed:
            audit_payload["failed"] = failed
            audit_payload["failed_tickers"] = failed_tickers
            message += f" {failed} exit order(s) failed ({', '.join(failed_tickers)})."
        if already_open:
            audit_payload["already_open"] = already_open
            message += f" {already_open} exit order(s) were already open."
        self.db.add(
            AuditLog(
                action="emergency_flatten_all",
                actor=actor,
                payload=audit_payload,
                occurred_at=datetime.now(UTC),
            )
        )
        refused = refused_reason is not None
        return FlattenSummary(
            message=message,
            refused=refused,
            flattened=flattened,
            nothing_went_out=flattened + unconfirmed + failed == 0,
            complete=not refused and failed == 0 and unconfirmed == 0,
        )

    async def _created_since(self, order: Order, since: datetime) -> bool:
        """True when the order intent was created at or after ``since``.

        Asking for an exit can hand back an identical intent that already exists, made by
        another part of the application (the end-of-day flatten keeps one and sends it
        later). Only an intent this flatten created may be closed by it.
        """
        # The event of a new intent may still be waiting to be written.
        await self.db.flush()
        result = await self.db.execute(
            select(OrderEvent.id)
            .where(
                OrderEvent.order_id == order.id,
                OrderEvent.event_type == "intent_created",
                OrderEvent.occurred_at >= since,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    def confirmation_expiry() -> datetime:
        return datetime.now(UTC) + timedelta(seconds=settings.TELEGRAM_CONFIRM_WINDOW_SECONDS)
