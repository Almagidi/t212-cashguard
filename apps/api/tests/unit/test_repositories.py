from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.repositories.order_repo import OrderRepository
from app.db.repositories.strategy_repo import StrategyRepository


def _db_with_result(result: MagicMock) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _result_for_scalar(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _result_for_scalars(values):
    scalars = MagicMock()
    scalars.all.return_value = values
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


class TestOrderRepository:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_scalar(self):
        order = MagicMock()
        repo = OrderRepository(_db_with_result(_result_for_scalar(order)))

        assert await repo.get_by_id(uuid.uuid4()) is order

    @pytest.mark.asyncio
    async def test_get_by_client_key_returns_scalar(self):
        order = MagicMock()
        repo = OrderRepository(_db_with_result(_result_for_scalar(order)))

        assert await repo.get_by_client_key("client-key") is order

    @pytest.mark.asyncio
    async def test_get_by_broker_id_returns_scalar(self):
        order = MagicMock()
        repo = OrderRepository(_db_with_result(_result_for_scalar(order)))

        assert await repo.get_by_broker_id("broker-123") is order

    @pytest.mark.asyncio
    async def test_list_returns_scalars_and_accepts_filters(self):
        orders = [MagicMock(), MagicMock()]
        repo = OrderRepository(_db_with_result(_result_for_scalars(orders)))

        result = await repo.list(status="filled", ticker="aapl", is_dry_run=False, limit=25, offset=5)

        assert result == orders

    @pytest.mark.asyncio
    async def test_list_pending_returns_scalars(self):
        orders = [MagicMock()]
        repo = OrderRepository(_db_with_result(_result_for_scalars(orders)))

        assert await repo.list_pending() == orders

    @pytest.mark.asyncio
    async def test_count_today_returns_scalar_one(self):
        result = MagicMock()
        result.scalar_one.return_value = 3
        repo = OrderRepository(_db_with_result(result))

        assert await repo.count_today(dry_run=True) == 3

    @pytest.mark.asyncio
    async def test_has_active_for_ticker_side_detects_presence(self):
        result = MagicMock()
        result.scalar_one_or_none.return_value = uuid.uuid4()
        repo = OrderRepository(_db_with_result(result))

        assert await repo.has_active_for_ticker_side("AAPL", "buy") is True

    @pytest.mark.asyncio
    async def test_has_active_for_ticker_side_detects_absence(self):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        repo = OrderRepository(_db_with_result(result))

        assert await repo.has_active_for_ticker_side("AAPL", "buy") is False

    @pytest.mark.asyncio
    async def test_create_adds_and_flushes(self):
        db = _db_with_result(MagicMock())
        repo = OrderRepository(db)
        order = MagicMock()

        assert await repo.create(order) is order
        db.add.assert_called_once_with(order)
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_status_returns_rowcount(self):
        result = MagicMock(rowcount=1)
        repo = OrderRepository(_db_with_result(result))

        assert await repo.update_status(uuid.uuid4(), "filled") == 1

    @pytest.mark.asyncio
    async def test_add_event_adds_and_flushes(self):
        db = _db_with_result(MagicMock())
        repo = OrderRepository(db)
        event = MagicMock()

        await repo.add_event(event)

        db.add.assert_called_once_with(event)
        db.flush.assert_awaited_once()


class TestStrategyRepository:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_scalar(self):
        strategy = MagicMock()
        repo = StrategyRepository(_db_with_result(_result_for_scalar(strategy)))

        assert await repo.get_by_id(uuid.uuid4()) is strategy

    @pytest.mark.asyncio
    async def test_list_enabled_returns_scalars(self):
        strategies = [MagicMock()]
        repo = StrategyRepository(_db_with_result(_result_for_scalars(strategies)))

        assert await repo.list_enabled() == strategies

    @pytest.mark.asyncio
    async def test_list_all_returns_scalars(self):
        strategies = [MagicMock(), MagicMock()]
        repo = StrategyRepository(_db_with_result(_result_for_scalars(strategies)))

        assert await repo.list_all() == strategies

    @pytest.mark.asyncio
    async def test_create_adds_and_flushes(self):
        db = _db_with_result(MagicMock())
        repo = StrategyRepository(db)
        strategy = MagicMock()

        assert await repo.create(strategy) is strategy
        db.add.assert_called_once_with(strategy)
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_signals_returns_scalars(self):
        signals = [MagicMock()]
        repo = StrategyRepository(_db_with_result(_result_for_scalars(signals)))

        assert await repo.get_signals(uuid.uuid4(), limit=5) == signals

    @pytest.mark.asyncio
    async def test_get_default_risk_profile_returns_scalar(self):
        profile = MagicMock()
        repo = StrategyRepository(_db_with_result(_result_for_scalar(profile)))

        assert await repo.get_default_risk_profile() is profile
