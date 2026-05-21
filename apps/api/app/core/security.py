"""
Security utilities: JWT tokens, password hashing, field-level encryption.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import bcrypt as _bcrypt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class CredentialDecryptionError(Exception):
    """Raised when stored broker credentials can no longer be decrypted."""


class TokenDecodeError(Exception):
    """Raised when an access token is invalid or expired."""


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

_JWT_ALGORITHM = "HS256"
_JWT_TYPE = "JWT"
_JWT_ACCESS_TYPE = "access"


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except Exception as exc:
        raise TokenDecodeError("Invalid token encoding") from exc


def _require_hs256_settings() -> None:
    if settings.ALGORITHM != _JWT_ALGORITHM:
        raise ValueError(f"Unsupported JWT algorithm configured: {settings.ALGORITHM!r}")


def _json_default(value: object) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp())
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, default=_json_default, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _numeric_date(value: Any, claim: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TokenDecodeError(f"Token {claim} claim must be numeric")
    return cast(int | float, value)


def create_access_token(subject: str | int, extra: dict[str, Any] | None = None) -> str:
    _require_hs256_settings()
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "type": _JWT_ACCESS_TYPE,
    }
    if extra:
        # Callers cannot override canonical access-token claims.
        _RESERVED_CLAIMS = {"sub", "iat", "exp", "type"}
        payload.update({k: v for k, v in extra.items() if k not in _RESERVED_CLAIMS})

    header = {"alg": _JWT_ALGORITHM, "typ": _JWT_TYPE}
    signing_input = ".".join(
        [_base64url_encode(_json_bytes(header)), _base64url_encode(_json_bytes(payload))]
    )
    # TODO(security): consider enforcing a minimum SECRET_KEY length once all
    # deployed environments are confirmed to use sufficiently long secrets.
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_base64url_encode(signature)}"


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode a strict internal HS256 access token."""
    _require_hs256_settings()
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
    except ValueError as exc:
        raise TokenDecodeError("Token must have exactly 3 parts") from exc

    signing_input = f"{header_segment}.{payload_segment}"
    header_bytes = _base64url_decode(header_segment)
    payload_bytes = _base64url_decode(payload_segment)
    signature = _base64url_decode(signature_segment)

    try:
        header = json.loads(header_bytes)
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise TokenDecodeError("Token contains invalid JSON") from exc

    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise TokenDecodeError("Token header and payload must be JSON objects")
    if header.get("alg") != _JWT_ALGORITHM:
        raise TokenDecodeError("Token algorithm is not supported")
    if header.get("typ") != _JWT_TYPE:
        raise TokenDecodeError("Token type header is invalid")

    expected_signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(signature, expected_signature):
        raise TokenDecodeError("Token signature is invalid")

    exp = _numeric_date(payload.get("exp"), "exp")
    if datetime.now(UTC).timestamp() >= exp:
        raise TokenDecodeError("Token has expired")
    if payload.get("type") != _JWT_ACCESS_TYPE:
        raise TokenDecodeError("Token is not an access token")

    return cast(dict[str, Any], payload)


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
    return cast(str, _fernet.encrypt(value.encode()).decode())


def decrypt_field(encrypted: str) -> str:
    """Decrypt a stored encrypted string."""
    try:
        return cast(str, _fernet.decrypt(encrypted.encode()).decode())
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "Stored broker credentials could not be decrypted with the current "
            "MASTER_KEY. Restore the original MASTER_KEY or reconnect the broker "
            "with fresh credentials."
        ) from exc
