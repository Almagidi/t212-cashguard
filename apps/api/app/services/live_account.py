"""Frozen live account identity; no implicit account or credential selection."""

from __future__ import annotations

import hashlib
import inspect
import json
import secrets
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text

from app.broker.provider import BrokerProviderRequest, trading212_account_scope
from app.core.security import CredentialDecryptionError, decrypt_field
from app.db.models import BrokerConnection, User

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


class LiveAccountError(RuntimeError):
    """The selected live account cannot be established safely."""


@dataclass(frozen=True)
class LiveAccountBinding:
    owner_id: str
    connection_id: str
    broker: str
    environment: str
    account_scope: str
    revision_id: str

    def payload(self) -> dict[str, str]:
        return asdict(self)


def binding_for(connection: BrokerConnection) -> LiveAccountBinding:
    if connection.broker != "trading212" or connection.environment != "live":
        raise LiveAccountError("Selected account is not a live Trading 212 connection.")
    if not connection.is_active or not connection.account_id or not connection.account_id.strip():
        raise LiveAccountError(
            "Selected live account is inactive or has no verified account identity."
        )
    scope = trading212_account_scope(
        BrokerProviderRequest(
            broker_id="trading212",
            environment="live",
            purpose="dependency",
            user_id=connection.user_id,
            account_id=connection.account_id,
        )
    )
    if scope is None:
        raise LiveAccountError("Selected live account has no attributable scope.")
    # Randomized encrypted token revisions are opaque metadata, never raw credentials.
    revision = hashlib.sha256(
        json.dumps(
            [
                str(connection.id),
                str(connection.user_id),
                connection.broker,
                connection.environment,
                connection.account_id,
                connection.api_key_encrypted,
                connection.api_secret_encrypted,
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return LiveAccountBinding(
        str(connection.user_id), str(connection.id), "trading212", "live", scope, revision
    )


async def bound_lock_wait(db: AsyncSession) -> None:
    """Bound only this transaction's PostgreSQL lock wait; no global settings."""
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("SELECT set_config('lock_timeout', '2000ms', true)"))


async def selected_live_account(
    db: AsyncSession,
    owner_id: uuid.UUID | None,
    *,
    lock: bool = False,
) -> tuple[LiveAccountBinding, BrokerConnection]:
    if owner_id is None:
        raise LiveAccountError("An explicit authenticated live account owner is required.")
    if lock:
        await bound_lock_wait(db)
    user_query = select(User).where(User.id == owner_id).execution_options(populate_existing=True)
    if lock:
        user_query = user_query.with_for_update()
    user = (await db.execute(user_query)).scalar_one_or_none()
    if user is None or not user.is_active or not user.is_admin:
        raise LiveAccountError("The live account owner is missing, inactive or unauthorized.")
    query = (
        select(BrokerConnection)
        .where(
            BrokerConnection.user_id == owner_id,
            BrokerConnection.broker == "trading212",
            BrokerConnection.environment == "live",
            BrokerConnection.is_active.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    connections = list((await db.execute(query.limit(2))).scalars())
    if len(connections) != 1:
        raise LiveAccountError("One explicit active live Trading 212 account must be selected.")
    return binding_for(connections[0]), connections[0]


async def binding_for_adapter(
    db: AsyncSession,
    broker: Any,
    *,
    lock: bool = False,
) -> tuple[LiveAccountBinding, BrokerConnection]:
    request = inspect.getattr_static(broker, "provider_request", None)
    if not isinstance(request, BrokerProviderRequest):
        raise LiveAccountError("Live transport lacks a trusted selected-account context.")
    if request.broker_id != "trading212" or request.environment != "live":
        raise LiveAccountError("Live transport venue or environment does not match.")
    if inspect.getattr_static(broker, "environment", None) != "live":
        raise LiveAccountError("Live transport environment changed after selection.")
    transport_environment = inspect.getattr_static(broker, "_transport_environment", None)
    if transport_environment is not None and transport_environment != "live":
        raise LiveAccountError("Live client environment does not match its account context.")
    binding, connection = await selected_live_account(db, request.user_id, lock=lock)
    if request.account_id != connection.account_id:
        raise LiveAccountError("Live transport account identity changed.")
    if inspect.getattr_static(broker, "account_scope", None) != binding.account_scope:
        raise LiveAccountError("Live transport account scope does not match.")
    try:
        actual = (
            decrypt_field(connection.api_key_encrypted),
            decrypt_field(connection.api_secret_encrypted),
        )
    except CredentialDecryptionError as exc:
        raise LiveAccountError("Selected live credentials are unavailable.") from exc
    supplied = inspect.getattr_static(broker, "_transport_credentials", None)
    if supplied is None:
        supplied = (
            inspect.getattr_static(broker, "api_key", None),
            inspect.getattr_static(broker, "api_secret", None),
        )
    if not isinstance(supplied, tuple) or len(supplied) != 2:
        raise LiveAccountError("Live transport credential identity is unavailable.")
    if any(not isinstance(value, str) for value in supplied):
        raise LiveAccountError("Live transport credentials are unbound.")
    if not all(
        secrets.compare_digest(current, used)
        for current, used in zip(actual, supplied, strict=True)
    ):
        raise LiveAccountError("Live transport credentials changed after selection.")
    return binding, connection
