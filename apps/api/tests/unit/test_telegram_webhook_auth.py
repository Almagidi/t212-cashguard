"""Telegram webhook authentication: mandatory secret outside mock mode."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.api.v1.routes import telegram as telegram_route
from app.core.config import settings
from app.services.telegram_control import TelegramControlService

UPDATE = {"message": {"text": "/help", "chat": {"id": 12345}, "from": {"id": 777}}}


class _FakeRequest:
    def __init__(self) -> None:
        self.json_calls = 0

    async def json(self) -> dict[str, Any]:
        self.json_calls += 1
        return UPDATE


@pytest.fixture(autouse=True)
def _telegram_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_IDS", "12345")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", "777")

    async def _fake_handle(self: Any, update: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "handled": True, "authorized": True, "reply_text": "ok"}

    monkeypatch.setattr(TelegramControlService, "handle_update", _fake_handle)


async def _call(token: str | None) -> tuple[Any, _FakeRequest]:
    request = _FakeRequest()
    result = await telegram_route.telegram_webhook(
        request=request,  # type: ignore[arg-type]
        db=object(),  # type: ignore[arg-type]
        secret_token=token,
    )
    return result, request


@pytest.mark.parametrize("app_mode", ["paper", "demo", "live"])
async def test_unset_secret_outside_mock_is_rejected_before_parsing(
    monkeypatch: pytest.MonkeyPatch, app_mode: str
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", app_mode)
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
    request = _FakeRequest()
    for token in (None, "", "anything"):
        with pytest.raises(HTTPException) as exc:
            await telegram_route.telegram_webhook(
                request=request,  # type: ignore[arg-type]
                db=object(),  # type: ignore[arg-type]
                secret_token=token,
            )
        assert exc.value.status_code == 401
    assert request.json_calls == 0


async def test_wrong_or_missing_secret_is_rejected_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "paper")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "right-secret")
    for token in ("wrong-secret", None, ""):
        request = _FakeRequest()
        with pytest.raises(HTTPException) as exc:
            await telegram_route.telegram_webhook(
                request=request,  # type: ignore[arg-type]
                db=object(),  # type: ignore[arg-type]
                secret_token=token,
            )
        assert exc.value.status_code == 401
        assert request.json_calls == 0


async def test_correct_secret_is_processed_outside_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "right-secret")
    result, request = await _call("right-secret")
    assert result.handled is True
    assert request.json_calls == 1


async def test_mock_mode_behaviour_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    # Secret unset in mock mode: still accepted (unchanged).
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
    result, _ = await _call(None)
    assert result.handled is True
    # Secret set in mock mode: still enforced.
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "right-secret")
    with pytest.raises(HTTPException) as exc:
        await _call("nope")
    assert exc.value.status_code == 401
    result, _ = await _call("right-secret")
    assert result.handled is True
