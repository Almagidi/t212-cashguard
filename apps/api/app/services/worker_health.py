"""
Worker heartbeat tracking for health and operations visibility.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import AppSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_HEARTBEAT_KEY = "worker_heartbeats"
_EXPECTED_INTERVALS = {
    "run_strategy_signals": 300,
    "run_position_monitor": 60,
    "reconcile_pending_orders": 30,
    "sync_account_snapshot": 60,
    "check_eod_flatten": 120,
}


async def record_worker_heartbeat(
    db: AsyncSession,
    *,
    task_name: str,
    payload: dict[str, Any] | None = None,
) -> None:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    app_settings = result.scalar_one_or_none()
    if not app_settings:
        return

    extra = dict(app_settings.extra or {})
    heartbeats = dict(extra.get(_HEARTBEAT_KEY) or {})
    heartbeats[task_name] = {
        "last_seen_at": datetime.now(UTC).isoformat(),
        "payload": payload or {},
    }
    app_settings.extra = {
        **extra,
        _HEARTBEAT_KEY: heartbeats,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def build_worker_health(db: AsyncSession) -> dict[str, Any]:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    app_settings = result.scalar_one_or_none()
    if not app_settings:
        return {"status": "unknown", "tasks": []}

    heartbeats = ((app_settings.extra or {}).get(_HEARTBEAT_KEY) or {})
    now = datetime.now(UTC)
    tasks: list[dict[str, Any]] = []
    statuses: list[str] = []

    for task_name, interval_seconds in _EXPECTED_INTERVALS.items():
        heartbeat = heartbeats.get(task_name, {}) if isinstance(heartbeats, dict) else {}
        last_seen_at = _parse_timestamp(heartbeat.get("last_seen_at"))
        max_age = timedelta(seconds=max(interval_seconds * 3, 600))

        if last_seen_at is None:
            status = "unknown"
            detail = "Task heartbeat has not been recorded yet."
            age_seconds = None
        else:
            age_seconds = max(0, int((now - last_seen_at).total_seconds()))
            if now - last_seen_at <= max_age:
                status = "ok"
                detail = "Task heartbeat is within the expected freshness window."
            else:
                status = "stale"
                detail = "Task heartbeat is older than the expected freshness window."

        statuses.append(status)
        tasks.append({
            "task_name": task_name,
            "status": status,
            "detail": detail,
            "last_seen_at": last_seen_at,
            "age_seconds": age_seconds,
        })

    if any(status == "stale" for status in statuses):
        summary = "stale"
    elif all(status == "ok" for status in statuses):
        summary = "ok"
    else:
        summary = "unknown"

    return {"status": summary, "tasks": tasks}
