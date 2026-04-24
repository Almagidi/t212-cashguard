"""
Telegram monitoring and control workflow with confirmation gates.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.config import settings
from app.db.models import TelegramControlRequest
from app.services.feed_health import get_feed_health_snapshot
from app.services.market_regime import MarketRegimeService
from app.services.news_intelligence import NewsIntelligenceService
from app.services.system_control import SystemControlError, SystemControlService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SUPPORTED_TELEGRAM_COMMANDS = [
    "/help",
    "/status",
    "/positions",
    "/watchlist",
    "/risk",
    "/health",
    "/pause",
    "/resume",
    "/kill",
    "/cancelall",
    "/flatten",
    "/confirm <code>",
    "/cancel",
]


class TelegramControlService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.system_control = SystemControlService(db)

    @staticmethod
    def control_enabled() -> bool:
        return bool(
            settings.TELEGRAM_BOT_TOKEN
            and (
                settings.telegram_allowed_chat_ids
                or settings.telegram_allowed_user_ids
            )
        )

    @staticmethod
    def status_payload() -> dict[str, Any]:
        return {
            "bot_configured": bool(settings.TELEGRAM_BOT_TOKEN),
            "alert_chat_configured": bool(settings.TELEGRAM_CHAT_ID),
            "webhook_secret_configured": bool(settings.TELEGRAM_WEBHOOK_SECRET),
            "control_enabled": TelegramControlService.control_enabled(),
            "allowed_chat_count": len(settings.telegram_allowed_chat_ids),
            "allowed_user_count": len(settings.telegram_allowed_user_ids),
            "confirmation_window_seconds": settings.TELEGRAM_CONFIRM_WINDOW_SECONDS,
            "supported_commands": SUPPORTED_TELEGRAM_COMMANDS,
        }

    async def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return {"ok": True, "handled": False, "authorized": False, "reply_text": None}

        text = str(message.get("text") or "").strip()
        chat_id = str(message.get("chat", {}).get("id") or "")
        user_id = str(message.get("from", {}).get("id") or "")
        if not text or not chat_id or not user_id:
            return {"ok": True, "handled": False, "authorized": False, "reply_text": None}

        if not self._is_authorized(chat_id, user_id):
            return {
                "ok": True,
                "handled": False,
                "authorized": False,
                "reply_text": "Telegram control is not authorized for this chat.",
            }

        result = await self._dispatch(text=text, chat_id=chat_id, user_id=user_id)
        if result.get("reply_text"):
            await self._send_message(chat_id, result["reply_text"])
        return result

    def _is_authorized(self, chat_id: str, user_id: str) -> bool:
        allowed_chats = settings.telegram_allowed_chat_ids
        allowed_users = settings.telegram_allowed_user_ids
        if not allowed_chats and not allowed_users:
            return False
        if allowed_chats and chat_id not in allowed_chats:
            return False
        return not (allowed_users and user_id not in allowed_users)

    async def _dispatch(self, *, text: str, chat_id: str, user_id: str) -> dict[str, Any]:
        normalized = text.lower()
        if normalized.startswith("/help") or normalized.startswith("/start"):
            return {
                "ok": True,
                "handled": True,
                "authorized": True,
                "reply_text": self._help_text(),
            }
        if normalized.startswith("/status"):
            snapshot = await self.system_control.get_snapshot()
            return {
                "ok": True,
                "handled": True,
                "authorized": True,
                "action": "status",
                "reply_text": self._format_status(snapshot),
            }
        if normalized.startswith("/positions"):
            return {
                "ok": True,
                "handled": True,
                "authorized": True,
                "action": "positions",
                "reply_text": await self._positions_text(),
            }
        if normalized.startswith("/watchlist"):
            return {
                "ok": True,
                "handled": True,
                "authorized": True,
                "action": "watchlist",
                "reply_text": await self._watchlist_text(),
            }
        if normalized.startswith("/risk"):
            return {
                "ok": True,
                "handled": True,
                "authorized": True,
                "action": "risk",
                "reply_text": await self._risk_text(),
            }
        if normalized.startswith("/health"):
            return {
                "ok": True,
                "handled": True,
                "authorized": True,
                "action": "health",
                "reply_text": await self._health_text(),
            }
        if normalized.startswith("/pause"):
            return await self._queue_confirmation(
                chat_id=chat_id,
                user_id=user_id,
                command="/pause",
                action="pause",
            )
        if normalized.startswith("/resume"):
            return await self._queue_confirmation(
                chat_id=chat_id,
                user_id=user_id,
                command="/resume",
                action="resume",
            )
        if normalized.startswith("/kill"):
            return await self._queue_confirmation(
                chat_id=chat_id,
                user_id=user_id,
                command="/kill",
                action="kill",
            )
        if normalized.startswith("/cancelall"):
            return await self._queue_confirmation(
                chat_id=chat_id,
                user_id=user_id,
                command="/cancelall",
                action="cancelall",
            )
        if normalized.startswith("/flatten"):
            return await self._queue_confirmation(
                chat_id=chat_id,
                user_id=user_id,
                command="/flatten",
                action="flatten",
            )
        if normalized.startswith("/confirm"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return {
                    "ok": True,
                    "handled": True,
                    "authorized": True,
                    "action": "confirm",
                    "reply_text": "Usage: /confirm <code>",
                }
            return await self._confirm(chat_id=chat_id, user_id=user_id, code=parts[1].strip())
        if normalized.startswith("/cancel"):
            return await self._cancel_pending(chat_id=chat_id, user_id=user_id)
        return {
            "ok": True,
            "handled": True,
            "authorized": True,
            "reply_text": self._help_text(),
        }

    async def _queue_confirmation(
        self,
        *,
        chat_id: str,
        user_id: str,
        command: str,
        action: str,
    ) -> dict[str, Any]:
        await self._expire_pending()
        await self._cancel_existing(chat_id=chat_id, user_id=user_id)

        confirmation_code = f"{secrets.randbelow(900000) + 100000}"
        pending = TelegramControlRequest(
            chat_id=chat_id,
            user_id=user_id,
            command=command,
            action=action,
            confirmation_code=confirmation_code,
            status="pending",
            expires_at=self.system_control.confirmation_expiry(),
        )
        self.db.add(pending)
        await self.db.flush()

        return {
            "ok": True,
            "handled": True,
            "authorized": True,
            "action": action,
            "requires_confirmation": True,
            "reply_text": (
                f"Confirmation required for {action}. "
                f"Reply with /confirm {confirmation_code} within "
                f"{settings.TELEGRAM_CONFIRM_WINDOW_SECONDS} seconds."
            ),
        }

    async def _confirm(self, *, chat_id: str, user_id: str, code: str) -> dict[str, Any]:
        await self._expire_pending()
        result = await self.db.execute(
            select(TelegramControlRequest).where(
                TelegramControlRequest.chat_id == chat_id,
                TelegramControlRequest.user_id == user_id,
                TelegramControlRequest.confirmation_code == code,
                TelegramControlRequest.status == "pending",
            )
        )
        pending = result.scalar_one_or_none()
        if not pending:
            return {
                "ok": True,
                "handled": True,
                "authorized": True,
                "action": "confirm",
                "reply_text": "No pending Telegram action matched that confirmation code.",
            }

        actor = f"telegram:{user_id}"
        try:
            if pending.action == "pause":
                reply_text = await self.system_control.pause_auto_trading(actor)
            elif pending.action == "resume":
                reply_text = await self.system_control.resume_auto_trading(actor)
            elif pending.action == "kill":
                reply_text = await self.system_control.activate_kill_switch(actor)
            elif pending.action == "cancelall":
                reply_text = await self.system_control.cancel_all_pending(actor)
            elif pending.action == "flatten":
                reply_text = await self.system_control.flatten_all(actor)
            else:
                reply_text = f"Unsupported Telegram action: {pending.action}"
        except SystemControlError as exc:
            pending.status = "cancelled"
            pending.resolved_at = datetime.now(UTC)
            await self.db.flush()
            return {
                "ok": True,
                "handled": True,
                "authorized": True,
                "action": pending.action,
                "reply_text": str(exc),
            }

        pending.status = "executed"
        pending.executed_at = datetime.now(UTC)
        pending.resolved_at = pending.executed_at
        await self.db.flush()

        return {
            "ok": True,
            "handled": True,
            "authorized": True,
            "action": pending.action,
            "executed": True,
            "reply_text": reply_text,
        }

    async def _cancel_pending(self, *, chat_id: str, user_id: str) -> dict[str, Any]:
        cancelled = await self._cancel_existing(chat_id=chat_id, user_id=user_id)
        return {
            "ok": True,
            "handled": True,
            "authorized": True,
            "action": "cancel",
            "reply_text": (
                "Cancelled pending Telegram confirmations."
                if cancelled
                else "There were no pending Telegram confirmations."
            ),
        }

    async def _cancel_existing(self, *, chat_id: str, user_id: str) -> int:
        await self._expire_pending()
        result = await self.db.execute(
            select(TelegramControlRequest).where(
                TelegramControlRequest.chat_id == chat_id,
                TelegramControlRequest.user_id == user_id,
                TelegramControlRequest.status == "pending",
            )
        )
        pending_requests = result.scalars().all()
        if not pending_requests:
            return 0

        now = datetime.now(UTC)
        for pending in pending_requests:
            pending.status = "cancelled"
            pending.resolved_at = now
        await self.db.flush()
        return len(pending_requests)

    async def _expire_pending(self) -> None:
        result = await self.db.execute(
            select(TelegramControlRequest).where(TelegramControlRequest.status == "pending")
        )
        now = datetime.now(UTC)
        expired = False
        for pending in result.scalars().all():
            expires_at = pending.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                pending.status = "expired"
                pending.resolved_at = now
                expired = True
        if expired:
            await self.db.flush()

    async def _positions_text(self) -> str:
        try:
            positions = await self.system_control.get_positions_summary()
        except SystemControlError as exc:
            return str(exc)

        if not positions:
            return "No open positions."

        lines = ["Open positions:"]
        for position in positions[:8]:
            lines.append(
                f"- {position['ticker']}: qty {position.get('quantity', 0)}"
            )
        return "\n".join(lines)

    async def _watchlist_text(self) -> str:
        from app.db.models import Strategy

        strategies = (
            await self.db.execute(select(Strategy).where(Strategy.is_enabled == True))  # noqa: E712
        ).scalars().all()
        watchlist: list[str] = []
        for strategy in strategies:
            params = strategy.params or {}
            todays = params.get("todays_watchlist")
            if isinstance(todays, list) and todays:
                watchlist.extend(str(item).upper() for item in todays)
            else:
                watchlist.extend(str(item).upper() for item in strategy.allowed_tickers)

        deduped = list(dict.fromkeys(watchlist))[:8]
        if not deduped:
            return "No active watchlist is available yet."

        news = await NewsIntelligenceService().get_watchlist_intelligence(deduped, limit=4)
        lines = ["Watchlist:", ", ".join(deduped)]
        if news:
            lines.append("")
            lines.append("Top catalysts:")
            for item in news[:3]:
                tickers = ",".join(item.get("tickers", [])[:2]) or "market"
                lines.append(
                    f"- {tickers}: {item['event_type']} ({item['catalyst_score']:.2f})"
                )
        return "\n".join(lines)

    async def _risk_text(self) -> str:
        regime = await MarketRegimeService().evaluate()
        snapshot = await self.system_control.get_snapshot()
        lines = [
            f"Regime: {regime['label']}",
            f"Confidence: {int(regime['confidence'] * 100)}%",
            f"Auto-trading: {'ON' if snapshot['auto_trading_enabled'] else 'OFF'}",
            f"Kill switch: {'ACTIVE' if snapshot['kill_switch_active'] else 'CLEAR'}",
            f"Pending orders: {snapshot['pending_orders']}",
        ]
        if regime.get("suppressed_strategies"):
            lines.append(f"Suppressed: {', '.join(regime['suppressed_strategies'])}")
        return "\n".join(lines)

    async def _health_text(self) -> str:
        feed = get_feed_health_snapshot()
        regime = await MarketRegimeService().evaluate()
        lines = [
            f"Feed: {feed['status']} ({feed['provider']})",
            f"Regime: {regime['label']}",
            f"Detail: {feed['detail']}",
        ]
        for symbol in feed["symbols"][:3]:
            lines.append(
                f"- {symbol['ticker']}: {symbol['status']} via {symbol['used_source']}"
            )
        return "\n".join(lines)

    async def _send_message(self, chat_id: str, text: str) -> None:
        if not settings.TELEGRAM_BOT_TOKEN:
            return

        import httpx

        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()

    @staticmethod
    def _help_text() -> str:
        return (
            "CashGuard Telegram commands:\n"
            "/status\n"
            "/positions\n"
            "/watchlist\n"
            "/risk\n"
            "/health\n"
            "/pause\n"
            "/resume\n"
            "/kill\n"
            "/cancelall\n"
            "/flatten\n"
            "/confirm <code>\n"
            "/cancel"
        )

    @staticmethod
    def _format_status(snapshot: dict[str, Any]) -> str:
        lines = [
            f"Mode: {str(snapshot['mode']).upper()}",
            f"Broker: {snapshot['broker_status']}",
            f"Auto-trading: {'ON' if snapshot['auto_trading_enabled'] else 'OFF'}",
            f"Kill switch: {'ACTIVE' if snapshot['kill_switch_active'] else 'CLEAR'}",
            f"Pending orders: {snapshot['pending_orders']}",
            f"Open positions: {len(snapshot['positions'])}",
        ]
        regime = snapshot.get("regime")
        if regime:
            lines.append(f"Regime: {regime['label']}")

        account = snapshot.get("account")
        if account:
            lines.extend(
                [
                    f"Free cash: {account['free_cash']:.2f}",
                    f"Equity: {account['total_value']:.2f}",
                    f"Open PnL: {account['result']:.2f}",
                ]
            )
        return "\n".join(lines)
