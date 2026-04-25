"""Dead-letter queue for failed Celery tasks.

On task_failure signal (final attempt only):
  - Pushes failed-task metadata to Redis list cashguard:dead_letter (FIFO, capped)
  - Increments Prometheus counter cashguard_task_failures_total
  - Emits a structured error log for Alertmanager to pick up

Fail-open: Redis unavailability is logged but never raises — a missing DLQ
entry is far better than crashing the worker signal handler.
"""
from __future__ import annotations

import json
import traceback as tb
from datetime import UTC, datetime
from typing import Any

import redis
import structlog

from app.core.config import settings

log = structlog.get_logger()

_DLQ_KEY = "cashguard:dead_letter"
_DLQ_MAX = 1_000


def _sync_redis() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def handle_task_failure(
    sender: Any,
    task_id: str,
    exception: Exception,
    args: tuple,
    kwargs: dict,
    traceback: Any,
    einfo: Any,
    **kw: Any,
) -> None:
    """Celery task_failure signal — write to DLQ only on the final attempt."""
    retries = getattr(getattr(sender, "request", None), "retries", 0) or 0
    max_retries = getattr(sender, "max_retries", 0)
    if max_retries is None:
        return  # infinite-retry task — never dead-letter
    if retries < max_retries:
        return  # retries remain — not dead yet

    task_name = getattr(sender, "name", str(sender))
    payload = json.dumps({
        "task_id": task_id,
        "task_name": task_name,
        "exception_type": type(exception).__name__,
        "exception": str(exception),
        "traceback": tb.format_exception(type(exception), exception, traceback)[-3:],
        "failed_at": datetime.now(UTC).isoformat(),
    })

    try:
        r = _sync_redis()
        r.lpush(_DLQ_KEY, payload)
        r.ltrim(_DLQ_KEY, 0, _DLQ_MAX - 1)
    except Exception as exc:
        log.warning("dead_letter.redis_unavailable", task=task_name, error=str(exc))

    try:
        from app.api.metrics import record_task_failure as _prom
        _prom(task_name=task_name)
    except Exception:
        pass

    log.error(
        "tasks.dead_lettered",
        task_id=task_id,
        task_name=task_name,
        exception_type=type(exception).__name__,
        exception=str(exception),
    )
