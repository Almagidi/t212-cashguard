"""
FastAPI dependency injection.
- Auth via httpOnly cookies (secure) with Bearer token fallback for API clients
- Broker adapter factory
- Common DB session
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select

from app.core.config import settings
from app.core.security import CredentialDecryptionError, decode_access_token
from app.db.models import BrokerConnection, User
from app.db.session import get_db
from app.services.broker_connection_recovery import mark_broker_connection_reconnect_required
from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

bearer = HTTPBearer(auto_error=False)


async def _resolve_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    cg_token: str | None = Cookie(default=None),
) -> str | None:
    if cg_token:
        return cg_token
    if credentials:
        return credentials.credentials
    return None


async def get_current_user(
    token: str | None = Depends(_resolve_token),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(token)
        user_id_raw = payload.get("sub", "")
        if not user_id_raw:
            raise HTTPException(status_code=401, detail="Invalid token")
        try:
            user_id = uuid.UUID(str(user_id_raw))
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Invalid token subject") from exc
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def get_broker(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if settings.APP_MODE == "mock":
        from app.broker.mock_adapter import MockBrokerAdapter
        return MockBrokerAdapter()

    try:
        require_broker_environment(settings.APP_MODE, action="broker dependency")
    except SafetyPolicyViolation as exc:
        raise HTTPException(status_code=403, detail=exc.reason) from exc

    result = await db.execute(
        select(BrokerConnection)
        .where(BrokerConnection.is_active.is_(True))
        .where(BrokerConnection.user_id == current_user.id)
        .where(BrokerConnection.environment == settings.APP_MODE)
        .limit(1)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        if settings.APP_MODE == "demo":
            if settings.T212_DEMO_API_KEY and settings.T212_DEMO_API_SECRET:
                from app.broker.trading212 import Trading212Adapter

                return Trading212Adapter(
                    settings.T212_DEMO_API_KEY,
                    settings.T212_DEMO_API_SECRET,
                    "demo",
                )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Demo credentials are not configured. Configure Trading 212 demo "
                    "credentials or connect a demo broker account before broker-backed "
                    "demo execution."
                ),
            )
        raise HTTPException(
            status_code=400,
            detail="No active broker connection. Connect your Trading 212 account first.",
        )

    from app.broker.trading212 import Trading212Adapter
    from app.core.security import decrypt_field
    try:
        api_key = decrypt_field(conn.api_key_encrypted)
        api_secret = decrypt_field(conn.api_secret_encrypted)
    except CredentialDecryptionError as exc:
        await mark_broker_connection_reconnect_required(
            db,
            conn,
            str(exc),
            actor=str(current_user.id),
            commit=True,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Trading212Adapter(api_key, api_secret, conn.environment)
