"""Dead-letter queue for failed Celery tasks.

On task_failure signal (final attempt, or any failure reported by the worker's
main process):
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
from types import TracebackType
from typing import Any

import redis
import structlog

from app.core.config import settings

log = structlog.get_logger()

_DLQ_KEY = "cashguard:dead_letter"
_DLQ_MAX = 1_000
_TRACEBACK_TAIL_LINES = 3
_MAX_EXCEPTION_CHARS = 2_000


def _sync_redis() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _safe_str(exception: BaseException) -> str:
    """The exception's message, bounded, and never raising for a broken ``__str__``."""
    try:
        return str(exception)[:_MAX_EXCEPTION_CHARS]
    except Exception:
        return f"<unprintable {type(exception).__name__}>"


def _traceback_tail(exception: BaseException, traceback: Any) -> list[str]:
    """Last lines of the failure's traceback, in whichever form Celery supplied it."""
    if isinstance(traceback, str):
        formatted = traceback
    else:
        if not isinstance(traceback, TracebackType):
            traceback = None
        try:
            formatted = "".join(tb.format_exception(type(exception), exception, traceback))
        except Exception:
            formatted = f"{type(exception).__name__}: {_safe_str(exception)}"
    return formatted.rstrip().splitlines()[-_TRACEBACK_TAIL_LINES:]


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
    """Celery task_failure signal — write to DLQ only when no retry can follow."""
    # A failure raised inside the task arrives with a traceback object. A pre-formatted
    # string means the worker's main process is reporting it (the pool job failed outside
    # the task body), which happens only once the task can no longer retry itself, and the
    # task object it passes carries no retry count. Such a failure is always final.
    reported_by_worker_main = isinstance(traceback, str)
    if not reported_by_worker_main:
        retries = getattr(getattr(sender, "request", None), "retries", 0) or 0
        max_retries = getattr(sender, "max_retries", 0)
        if max_retries is None:
            return  # infinite-retry task — never dead-letter
        if retries < max_retries:
            return  # retries remain — not dead yet

    task_name = getattr(sender, "name", str(sender))
    message = _safe_str(exception)
    payload = json.dumps(
        {
            "task_id": task_id,
            "task_name": task_name,
            "exception_type": type(exception).__name__,
            "exception": message,
            "traceback": _traceback_tail(exception, traceback),
            "failed_at": datetime.now(UTC).isoformat(),
        }
    )

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
        exception=message,
    )
