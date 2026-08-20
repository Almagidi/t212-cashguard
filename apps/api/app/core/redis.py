"""Async Redis client — distributed task locks and app-level caching."""

from __future__ import annotations

import contextlib
import secrets
from typing import TYPE_CHECKING, cast

import redis.asyncio as aioredis
import structlog

from app.core.config import settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable

log = structlog.get_logger()

_pool: aioredis.ConnectionPool | None = None

_RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


def _get_pool() -> aioredis.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=10,
            decode_responses=True,
        )
    return _pool


@contextlib.asynccontextmanager
async def task_lock(name: str, ttl_seconds: int) -> AsyncGenerator[bool, None]:
    """Distributed Celery task lock via Redis SET NX.

    Yields True  — lock acquired, task should run.
    Yields False — another instance is already running, task should skip.

    The lock is released explicitly on normal exit.  TTL is the safety net
    when the worker process is killed before the finally block can run.

    Fail-open: if Redis is unavailable the task runs unguarded rather than
    being silently suppressed — broken locking is better than no trading.
    """
    client: aioredis.Redis = aioredis.Redis(connection_pool=_get_pool())
    key = f"celery:task_lock:{name}"
    token = secrets.token_urlsafe(32)
    acquired = False
    try:
        result = await client.set(key, token, nx=True, ex=ttl_seconds)
        acquired = result is not None
    except Exception as exc:
        log.warning("redis.task_lock_unavailable", key=key, error=str(exc))
        acquired = True  # fail open so trading tasks still execute
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await cast(
                    "Awaitable[int]",
                    client.eval(_RELEASE_LOCK_SCRIPT, 1, key, token),
                )
