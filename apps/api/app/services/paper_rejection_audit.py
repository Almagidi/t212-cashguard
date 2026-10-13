"""Rollback-independent paper rejection evidence on PostgreSQL only.

Acknowledged commits survive caller rollback. Cancellation or connection failure
before acknowledgement has an unknown audit outcome; never retry that write.
The writer cannot hold a user FK because the caller may hold that user's row
lock or may not have committed the user yet. Preserve its UUID as payload data.
"""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.db.models import AuditLog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession as CallerSession


DURABLE_PAPER_REJECTION_ACTIONS = frozenset(
    {"paper_follow_up_fill_blocked", "paper_sell_fill_blocked"}
)
AUDIT_WRITE_TIMEOUT_SECONDS = 3.0


class PaperRejectionAuditUnavailable(Exception):
    """Evidence was not acknowledged; financial rejection must remain fail-closed."""


async def persist_paper_rejection(
    caller: CallerSession,
    *,
    action: str,
    actor: str,
    entity_type: str | None,
    entity_id: str | None,
    payload: dict[str, Any],
    user_id: uuid.UUID | None,
) -> None:
    if action not in DURABLE_PAPER_REJECTION_ACTIONS:
        raise PaperRejectionAuditUnavailable("Unsupported rejection action")
    bind = caller.bind
    if isinstance(bind, AsyncConnection):
        if bind.sync_connection is None:
            raise PaperRejectionAuditUnavailable("Caller connection is not initialized")
        engine = bind.engine.execution_options(**bind.sync_connection.get_execution_options())
    elif isinstance(bind, AsyncEngine):
        engine = bind
    else:
        raise PaperRejectionAuditUnavailable("Independent engine unavailable")
    if engine.dialect.name != "postgresql" or isinstance(engine.pool, StaticPool):
        raise PaperRejectionAuditUnavailable("Independent PostgreSQL connection required")

    evidence = AuditLog(
        id=uuid.uuid4(),
        user_id=None,
        action=action,
        actor=actor,
        entity_type=entity_type,
        entity_id=entity_id,
        payload={**deepcopy(payload), "audit_user_id": str(user_id) if user_id else None},
        occurred_at=datetime.now(UTC),
    )
    try:
        async with asyncio.timeout(AUDIT_WRITE_TIMEOUT_SECONDS):
            async with AsyncSession(engine, expire_on_commit=False) as writer:
                writer.add(evidence)
                await writer.commit()
    except Exception as exc:
        # No retry: a failed acknowledgement can follow a successful database commit.
        raise PaperRejectionAuditUnavailable("Rejection audit was not acknowledged") from exc
