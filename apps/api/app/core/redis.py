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


class TaskLockUnavailable(Exception):
    """Redis could not be asked for a lock, so nobody can tell whether it is held."""


@contextlib.asynccontextmanager
async def exclusive_task_lock(name: str, ttl_seconds: int) -> AsyncGenerator[bool, None]:
    """The same lock as ``task_lock``, for callers that must tell two refusals apart.

    Yields True  — lock acquired.
    Yields False — another holder has it.
    Raises ``TaskLockUnavailable`` — Redis could not be asked; nothing is known.

    Uses the same key and release rule as ``task_lock``, so the two exclude each other.
    """
    client: aioredis.Redis = aioredis.Redis(connection_pool=_get_pool())
    key = f"celery:task_lock:{name}"
    token = secrets.token_urlsafe(32)
    try:
        result = await client.set(key, token, nx=True, ex=ttl_seconds)
    except Exception as exc:
        log.warning("redis.task_lock_unavailable", key=key, error=str(exc))
        raise TaskLockUnavailable(
            f"The {name} lock could not be requested: Redis is unavailable."
        ) from exc
    acquired = result is not None
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await cast(
                    "Awaitable[int]",
                    client.eval(_RELEASE_LOCK_SCRIPT, 1, key, token),
                )


@contextlib.asynccontextmanager
async def task_lock(name: str, ttl_seconds: int) -> AsyncGenerator[bool, None]:
    """Distributed Celery task lock via Redis SET NX.

    Yields True  — lock acquired, task should run.
    Yields False — another instance is already running, task should skip.

    The lock is released explicitly on normal exit.  TTL is the safety net
    when the worker process is killed before the finally block can run.

    Fail-closed: if Redis is unavailable the task must not run without
    distributed exclusion.
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
        acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await cast(
                    "Awaitable[int]",
                    client.eval(_RELEASE_LOCK_SCRIPT, 1, key, token),
                )
