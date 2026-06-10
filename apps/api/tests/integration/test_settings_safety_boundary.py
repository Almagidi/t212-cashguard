"""PATCH /v1/settings must never reach the safety gates on AppSettings.

The settings row also stores ``auto_trading_enabled``, ``kill_switch_active``
and ``live_trading_unlocked`` (the final live-trading unlock consumed by the
live-readiness checklist). Requests that try to write them through the
generic settings route must be rejected loudly — not silently ignored — and
must leave the stored gates untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.db.models import AppSettings

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _stored_settings(db: AsyncSession) -> AppSettings:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    return result.scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"live_trading_unlocked": True},
        {"kill_switch_active": False},
        {"auto_trading_enabled": True},
        {"theme": "light", "live_trading_unlocked": True},
        {"extra": {"live_trading_unlocked": True}},
    ],
    ids=[
        "live_unlock",
        "kill_switch",
        "auto_trading",
        "mixed_with_allowlisted",
        "via_extra_blob",
    ],
)
async def test_patch_settings_rejects_safety_gate_fields(
    client: AsyncClient,
    db: AsyncSession,
    auth_headers: dict[str, str],
    payload: dict[str, object],
) -> None:
    resp = await client.patch("/v1/settings", json=payload, headers=auth_headers)

    assert resp.status_code == 422, (
        f"Safety-gate payload {payload} must be rejected, got {resp.status_code}: " f"{resp.text}"
    )

    stored = await _stored_settings(db)
    assert stored.live_trading_unlocked is False
    assert stored.auto_trading_enabled is False
    assert stored.kill_switch_active is False


@pytest.mark.asyncio
async def test_patch_settings_allowlisted_fields_still_work(
    client: AsyncClient,
    db: AsyncSession,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.patch(
        "/v1/settings",
        json={"theme": "light", "timezone": "Europe/London"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["theme"] == "light"
    assert body["timezone"] == "Europe/London"

    stored = await _stored_settings(db)
    assert stored.theme == "light"
    assert stored.live_trading_unlocked is False
