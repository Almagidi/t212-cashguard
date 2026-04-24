"""
Security utilities: JWT tokens, password hashing, field-level encryption.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class CredentialDecryptionError(Exception):
    """Raised when stored broker credentials can no longer be decrypted."""


# ──────────────────────────────────────────────────────────────────────────────
# Password hashing
# ──────────────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ──────────────────────────────────────────────────────────────────────────────
# JWT
# ──────────────────────────────────────────────────────────────────────────────

def create_access_token(subject: str | int, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises JWTError on invalid/expired tokens."""
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])


# ──────────────────────────────────────────────────────────────────────────────
# Field-level encryption (for broker credentials stored in DB)
# ──────────────────────────────────────────────────────────────────────────────

def _derive_fernet_key(master_key: str) -> bytes:
    """Derive a 32-byte Fernet key from an arbitrary master key string."""
    digest = hashlib.sha256(master_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_derive_fernet_key(settings.MASTER_KEY))


def encrypt_field(value: str) -> str:
    """Encrypt a string for storage in DB."""
    return _fernet.encrypt(value.encode()).decode()


def decrypt_field(encrypted: str) -> str:
    """Decrypt a stored encrypted string."""
    try:
        return _fernet.decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "Stored broker credentials could not be decrypted with the current "
            "MASTER_KEY. Restore the original MASTER_KEY or reconnect the broker "
            "with fresh credentials."
        ) from exc
