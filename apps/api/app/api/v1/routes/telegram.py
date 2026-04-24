"""Telegram monitoring and control routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.api.schemas import TelegramStatusOut, TelegramTestResult, TelegramWebhookResult
from app.core.config import settings
from app.db.models import User
from app.db.session import get_db
from app.services.alert_service import AlertService
from app.services.telegram_control import TelegramControlService

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.get("/status", response_model=TelegramStatusOut)
async def telegram_status(_: User = Depends(get_current_admin)) -> TelegramStatusOut:
    return TelegramStatusOut(**TelegramControlService.status_payload())


@router.post("/test-alert", response_model=TelegramTestResult)
async def send_test_telegram_alert(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> TelegramTestResult:
    status = TelegramControlService.status_payload()
    if not status["bot_configured"] or not status["alert_chat_configured"]:
        raise HTTPException(
            status_code=400,
            detail="Telegram bot token and alert chat ID must both be configured.",
        )

    svc = AlertService(db)
    await svc.send(
        alert_type="telegram_test",
        title="Telegram Test Alert",
        message=f"Telegram connectivity check requested by {current_user.email}.",
        severity="info",
        payload={"source": "settings_page"},
    )
    return TelegramTestResult(sent=True, message="Telegram test alert queued.")


@router.post("/webhook", response_model=TelegramWebhookResult)
async def telegram_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> TelegramWebhookResult:
    payload = await request.json()
    status = TelegramControlService.status_payload()
    if not status["bot_configured"]:
        raise HTTPException(status_code=400, detail="Telegram bot token is not configured.")
    if status["webhook_secret_configured"] and secret_token != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret.")

    service = TelegramControlService(db)
    return TelegramWebhookResult(**(await service.handle_update(payload)))
