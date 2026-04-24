"""Auth routes: login, logout, refresh."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import LoginRequest, LoginResponse, UserOut
from app.core.config import settings
from app.core.security import create_access_token, verify_password
from app.db.models import AuditLog, User
from app.db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


async def _audit(db, action: str, user_id=None, actor: str = "system", ip: str | None = None):
    log = AuditLog(
        action=action,
        entity_type="user",
        entity_id=str(user_id) if user_id else None,
        actor=actor,
        ip_address=ip,
        occurred_at=datetime.now(timezone.utc),
    )
    db.add(log)
    await db.flush()


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.hashed_password):
        await _audit(db, "login_failed", actor=body.email, ip=request.client.host if request.client else None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    token = create_access_token(str(user.id))
    await _audit(db, "login_success", user_id=user.id, actor=user.email, ip=request.client.host if request.client else None)

    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user_id=str(user.id),
        email=user.email,
        is_admin=user.is_admin,
    )


@router.post("/logout")
async def logout(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _audit(db, "logout", user_id=current_user.id, actor=current_user.email)
    return {"message": "Logged out successfully"}


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return current_user
