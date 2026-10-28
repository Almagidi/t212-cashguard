"""Task-local state must not authorize another transport, payload or send."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.broker.live_guard import LiveSubmissionPermit, live_submission_guard


async def test_permit_matches_exact_adapter_path_payload_and_is_one_use():
    adapter = object()
    check = AsyncMock()
    payload = {"ticker": "FIXTURE", "quantity": 1.0}
    permit = LiveSubmissionPermit(adapter, "/market", payload, check)
    for wrong_adapter, path, body in [
        (object(), "/market", payload),
        (adapter, "/limit", payload),
        (adapter, "/market", {**payload, "quantity": 2.0}),
    ]:
        with pytest.raises(RuntimeError):
            await permit.consume(wrong_adapter, path, body)
    check.assert_not_awaited()
    await permit.consume(adapter, "/market", dict(payload))
    check.assert_awaited_once()
    with pytest.raises(RuntimeError):
        await permit.consume(adapter, "/market", payload)


async def test_child_task_cannot_borrow_inherited_permit():
    adapter = object()
    check = AsyncMock()
    permit = LiveSubmissionPermit(adapter, "/market", {}, check)
    token = live_submission_guard.set(permit)
    try:

        async def child():
            assert live_submission_guard.get() is permit  # asyncio copies context.
            with pytest.raises(RuntimeError):
                await permit.consume(adapter, "/market", {})

        await asyncio.create_task(child())
        check.assert_not_awaited()
        await permit.consume(adapter, "/market", {})
        check.assert_awaited_once()
    finally:
        live_submission_guard.reset(token)


async def test_shared_adapter_concurrent_permits_are_separate():
    adapter = object()
    checks = [AsyncMock(), AsyncMock()]

    async def dispatch(index):
        payload = {"ticker": f"FIXTURE_{index}", "quantity": 1.0}
        permit = LiveSubmissionPermit(adapter, "/market", payload, checks[index])
        token = live_submission_guard.set(permit)
        try:
            await asyncio.sleep(0)
            assert live_submission_guard.get() is permit
            await permit.consume(adapter, "/market", payload)
        finally:
            live_submission_guard.reset(token)

    await asyncio.gather(dispatch(0), dispatch(1))
    for check in checks:
        check.assert_awaited_once()


async def test_refusal_consumes_permit_before_await():
    adapter = object()
    check = AsyncMock(side_effect=RuntimeError("synthetic-revocation"))
    permit = LiveSubmissionPermit(adapter, "/market", {}, check)
    with pytest.raises(RuntimeError):
        await permit.consume(adapter, "/market", {})
    with pytest.raises(RuntimeError):
        await permit.consume(adapter, "/market", {})
    check.assert_awaited_once()
