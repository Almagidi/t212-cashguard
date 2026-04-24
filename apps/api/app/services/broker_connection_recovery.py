"""
Helpers for handling broker connections that can no longer be decrypted.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, BrokerConnection


BROKER_RECOVERY_HINT = (
    "Saved broker credentials can no longer be decrypted with the current MASTER_KEY. "
    "Reconnect Trading 212 with fresh API credentials to restore live broker features."
)


async def mark_broker_connection_reconnect_required(
    db: AsyncSession,
    conn: BrokerConnection,
    reason: str,
    *,
    actor: str = "system",
    commit: bool = False,
) -> None:
    """
    Deactivate an unreadable broker connection once and surface a reconnect-required state.
    """
    now = datetime.now(UTC)
    should_audit = conn.is_active or conn.last_test_ok is not False

    conn.is_active = False
    conn.last_test_ok = False
    conn.last_test_at = now

    if should_audit:
        db.add(
            AuditLog(
                action="broker_reconnect_required",
                entity_type="broker_connection",
                entity_id=str(conn.id),
                actor=actor,
                payload={
                    "environment": conn.environment,
                    "reason": reason,
                },
                occurred_at=now,
            )
        )

    if commit:
        await db.commit()
    else:
        await db.flush()
