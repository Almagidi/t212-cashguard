"""Credential-source visibility for GET /v1/broker/trading212/status.

The broker dependency (app/api/deps.py::get_broker) can use credentials
from two different places in demo mode: a stored, encrypted
BrokerConnection row, or the T212_DEMO_API_KEY/T212_DEMO_API_SECRET
environment fallback. Before credential_source existed, the status
endpoint reported credential_state="not_connected" even when the
environment fallback would silently serve broker-backed demo
operations, so the operator could not tell which credential path was
live.

These tests pin the credential_source values for each path without ever
exposing credential material.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


class TestBrokerCredentialSource:
    async def test_mock_mode_reports_mock_source(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_MODE", "mock")

        resp = await client.get("/v1/broker/trading212/status", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["credential_state"] == "mock"
        assert data["credential_source"] == "mock"

    async def test_demo_without_connection_or_env_reports_none_source(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_MODE", "demo")
        monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
        monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")

        resp = await client.get("/v1/broker/trading212/status", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["credential_state"] == "not_connected"
        assert data["credential_source"] == "none"
        assert "demo credentials" in data["recovery_hint"].lower()

    async def test_demo_env_fallback_is_visible_without_leaking_secrets(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_MODE", "demo")
        monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "env-demo-key")
        monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "env-demo-secret")

        resp = await client.get("/v1/broker/trading212/status", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["credential_state"] == "not_connected"
        assert data["credential_source"] == "environment_fallback"
        assert "environment credentials" in data["recovery_hint"].lower()
        # The credential values themselves must never appear in the response.
        assert "env-demo-key" not in str(data)
        assert "env-demo-secret" not in str(data)

    async def test_demo_stored_connection_reports_stored_source(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db: AsyncSession,
        monkeypatch,
    ):
        from app.core.config import settings
        from app.core.security import encrypt_field
        from app.db.models import BrokerConnection, User

        monkeypatch.setattr(settings, "APP_MODE", "demo")

        user = (await db.execute(select(User).where(User.email == "admin@test.com"))).scalar_one()
        db.add(
            BrokerConnection(
                id=uuid.uuid4(),
                user_id=user.id,
                broker="trading212",
                environment="demo",
                api_key_encrypted=encrypt_field("stored-demo-key"),
                api_secret_encrypted=encrypt_field("stored-demo-secret"),
                is_active=True,
            )
        )
        await db.commit()

        resp = await client.get("/v1/broker/trading212/status", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["credential_state"] == "configured"
        assert data["credential_source"] == "stored_connection"
        assert "stored-demo-key" not in str(data)
        assert "stored-demo-secret" not in str(data)
