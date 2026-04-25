"""
Security utilities: JWT tokens, password hashing, field-level encryption.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt as _bcrypt
from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt

from app.core.config import settings


class CredentialDecryptionError(Exception):
    """Raised when stored broker credentials can no longer be decrypted."""


# ──────────────────────────────────────────────────────────────────────────────
# Password hashing
#
# bcrypt hard-truncates at 72 bytes.  passlib 1.7.4 is incompatible with
# bcrypt ≥4.0 (its internal wrap-bug probe uses a >72-byte secret, causing
# bcrypt to raise ValueError).  We call bcrypt directly and explicitly cap
# at 72 bytes so callers never hit a surprising silent truncation.
# ──────────────────────────────────────────────────────────────────────────────

_BCRYPT_ROUNDS = 12
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    pw_bytes = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return _bcrypt.hashpw(pw_bytes, _bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    pw_bytes = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return _bcrypt.checkpw(pw_bytes, hashed.encode("utf-8"))


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
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)  # type: ignore[no-any-return]


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises JWTError on invalid/expired tokens."""
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])  # type: ignore[no-any-return]


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
