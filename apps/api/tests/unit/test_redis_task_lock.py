from __future__ import annotations

import pytest

from app.core import redis as redis_module


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.acquisition_tokens: list[str] = []

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool,
        ex: int,
    ) -> bool | None:
        assert nx is True
        assert ex > 0
        if key in self.values:
            return None
        self.values[key] = value
        self.acquisition_tokens.append(value)
        return True

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def eval(
        self,
        script: str,
        numkeys: int,
        key: str,
        token: str,
    ) -> int:
        assert script == redis_module._RELEASE_LOCK_SCRIPT
        assert numkeys == 1
        if self.values.get(key) != token:
            return 0
        del self.values[key]
        return 1


class UnavailableRedis:
    def __init__(self) -> None:
        self.release_attempted = False

    async def set(self, *_args: object, **_kwargs: object) -> None:
        raise ConnectionError("deterministic Redis outage")

    async def eval(self, *_args: object, **_kwargs: object) -> int:
        self.release_attempted = True
        return 0


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(redis_module, "_get_pool", lambda: object())
    monkeypatch.setattr(redis_module.aioredis, "Redis", lambda **_kwargs: client)
    return client


@pytest.mark.asyncio
async def test_expired_owner_cannot_delete_replacement_lock(fake_redis: FakeRedis) -> None:
    key = "celery:task_lock:ownership"

    async with redis_module.task_lock("ownership", ttl_seconds=30) as acquired:
        assert acquired is True
        old_token = fake_redis.values[key]
        fake_redis.values[key] = "replacement-owner-token"

    assert old_token != "replacement-owner-token"
    assert fake_redis.values[key] == "replacement-owner-token"


@pytest.mark.asyncio
async def test_owner_releases_its_own_lock(fake_redis: FakeRedis) -> None:
    key = "celery:task_lock:normal-release"

    async with redis_module.task_lock("normal-release", ttl_seconds=30) as acquired:
        assert acquired is True
        assert key in fake_redis.values

    assert key not in fake_redis.values


@pytest.mark.asyncio
async def test_each_acquisition_uses_a_unique_token(fake_redis: FakeRedis) -> None:
    for _ in range(2):
        async with redis_module.task_lock("unique-token", ttl_seconds=30) as acquired:
            assert acquired is True

    first, second = fake_redis.acquisition_tokens
    assert first != second


@pytest.mark.asyncio
async def test_redis_acquisition_failure_does_not_authorize_or_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UnavailableRedis()
    monkeypatch.setattr(redis_module, "_get_pool", lambda: object())
    monkeypatch.setattr(redis_module.aioredis, "Redis", lambda **_kwargs: client)

    async with redis_module.task_lock("redis-unavailable", ttl_seconds=30) as acquired:
        assert acquired is False

    assert client.release_attempted is False


# ── exclusive_task_lock: the same lock, for callers that must tell refusals apart ──


@pytest.mark.asyncio
async def test_exclusive_lock_and_task_lock_exclude_each_other(fake_redis: FakeRedis) -> None:
    async with (
        redis_module.task_lock("shared", ttl_seconds=30) as scheduled,
        redis_module.exclusive_task_lock("shared", ttl_seconds=30) as other,
    ):
        assert (scheduled, other) == (True, False)

    async with (
        redis_module.exclusive_task_lock("shared", ttl_seconds=30) as owner,
        redis_module.task_lock("shared", ttl_seconds=30) as scheduled,
    ):
        assert (owner, scheduled) == (True, False)

    assert fake_redis.values == {}


@pytest.mark.asyncio
async def test_exclusive_lock_is_released_when_its_holder_fails(fake_redis: FakeRedis) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with redis_module.exclusive_task_lock("failing", ttl_seconds=30) as acquired:
            assert acquired is True
            raise RuntimeError("boom")

    assert fake_redis.values == {}


@pytest.mark.asyncio
async def test_expired_exclusive_owner_cannot_delete_its_replacement(
    fake_redis: FakeRedis,
) -> None:
    key = "celery:task_lock:expiring"
    async with redis_module.exclusive_task_lock("expiring", ttl_seconds=1) as acquired:
        assert acquired is True
        fake_redis.values[key] = "replacement-owner-token"

    assert fake_redis.values[key] == "replacement-owner-token"


@pytest.mark.asyncio
async def test_exclusive_lock_reports_an_outage_instead_of_a_held_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UnavailableRedis()
    monkeypatch.setattr(redis_module, "_get_pool", lambda: object())
    monkeypatch.setattr(redis_module.aioredis, "Redis", lambda **_kwargs: client)
    entered = False

    with pytest.raises(redis_module.TaskLockUnavailable, match="Redis is unavailable"):
        async with redis_module.exclusive_task_lock("redis-unavailable", ttl_seconds=30):
            entered = True

    assert entered is False
    assert client.release_attempted is False
