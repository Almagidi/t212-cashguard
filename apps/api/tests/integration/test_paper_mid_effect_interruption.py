"""Cancellation after economic rows flush must not survive caller rollback."""

import asyncio
from decimal import Decimal

import pytest

from app.execution.paper_engine import PaperExecutionEngine
from tests.integration.test_paper_combined_rollback import _fill, _ledger
from tests.integration.test_paper_follow_up_halt import _partial
from tests.integration.test_paper_follow_up_halt import sessions as sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("boundary", ["paper_account_updated", "paper_position_updated"])
async def test_cancel_after_effect_flush_rolls_back_every_financial_row(
    sessions, monkeypatch, side, boundary
):
    async with sessions() as setup:
        user, order = await _partial(setup, side)
        user_id, order_id = user.id, order.id
        await setup.commit()
        before = await _ledger(setup)

    original = PaperExecutionEngine._audit
    reached = False

    async def interrupted(self, action, **kwargs):
        nonlocal reached
        await original(self, action, **kwargs)
        if action == boundary:
            reached = True
            assert await _ledger(self.db) != before
            raise asyncio.CancelledError("synthetic interruption after flushed effects")

    with monkeypatch.context() as patch:
        patch.setattr(PaperExecutionEngine, "_audit", interrupted)
        async with sessions() as caller:
            with pytest.raises(asyncio.CancelledError):
                await _fill(caller, user_id, order_id)
            await caller.rollback()
    assert reached
    async with sessions() as verify:
        assert await _ledger(verify) == before
    async with sessions() as retry:
        async with asyncio.timeout(5):
            filled = await _fill(retry, user_id, order_id)
            await retry.commit()
        assert filled.filled_quantity == Decimal("3")
        assert filled.remaining_quantity == Decimal("1")
