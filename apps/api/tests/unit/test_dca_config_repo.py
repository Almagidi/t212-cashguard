"""Tests for DcaConfigRepository and dca_config_from_row."""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import DcaConfig
from app.db.repositories.dca_config_repo import DcaConfigRepository, dca_config_from_row


def _db_with_result(result: MagicMock) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalars_result(values):
    scalars = MagicMock()
    scalars.all.return_value = values
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _config_row(**overrides) -> DcaConfig:
    params = {
        "ticker": "BTC/USD",
        "venue": "kraken",
        "cadence_days": 7,
        "fixed_cash_amount": Decimal("100.00000000"),
        "dip_buy_enabled": True,
        "dip_threshold_pct": Decimal("5.0000"),
        "dip_buy_multiplier": Decimal("2.0000"),
        "dip_ema_period": 20,
        "min_cash_reserve": Decimal("500.00000000"),
        "max_position_percent": Decimal("25.0000"),
        "paper_only": True,
        "enabled": False,
    }
    params.update(overrides)
    return DcaConfig(**params)


class TestDcaConfigRepositoryMock:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_scalar(self):
        row = MagicMock(spec=DcaConfig)
        repo = DcaConfigRepository(_db_with_result(_scalar_result(row)))
        assert await repo.get_by_id(uuid.uuid4()) is row

    @pytest.mark.asyncio
    async def test_get_by_ticker_venue_returns_scalar(self):
        row = MagicMock(spec=DcaConfig)
        repo = DcaConfigRepository(_db_with_result(_scalar_result(row)))
        assert await repo.get_by_ticker_venue("BTC/USD", "kraken") is row

    @pytest.mark.asyncio
    async def test_get_by_ticker_venue_returns_none_when_absent(self):
        repo = DcaConfigRepository(_db_with_result(_scalar_result(None)))
        assert await repo.get_by_ticker_venue("BTC/USD", "kraken") is None

    @pytest.mark.asyncio
    async def test_list_enabled_returns_scalars(self):
        rows = [MagicMock(spec=DcaConfig), MagicMock(spec=DcaConfig)]
        repo = DcaConfigRepository(_db_with_result(_scalars_result(rows)))
        assert await repo.list_enabled() == rows

    @pytest.mark.asyncio
    async def test_list_all_returns_scalars(self):
        rows = [MagicMock(spec=DcaConfig), MagicMock(spec=DcaConfig)]
        repo = DcaConfigRepository(_db_with_result(_scalars_result(rows)))
        assert await repo.list_all() == rows

    @pytest.mark.asyncio
    async def test_create_adds_and_flushes(self):
        db = _db_with_result(MagicMock())
        repo = DcaConfigRepository(db)
        config = MagicMock(spec=DcaConfig)

        result = await repo.create(config)

        assert result is config
        db.add.assert_called_once_with(config)
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_sets_fields_and_flushes(self):
        db = _db_with_result(MagicMock())
        repo = DcaConfigRepository(db)
        config = _config_row()

        result = await repo.update(config, {"enabled": True, "cadence_days": 14})

        assert result is config
        assert config.enabled is True
        assert config.cadence_days == 14
        db.flush.assert_awaited_once()


class TestDcaConfigRepositoryDB:
    @pytest.mark.asyncio
    async def test_create_and_fetch_round_trip(self, db):
        repo = DcaConfigRepository(db)
        created = await repo.create(_config_row(enabled=True))
        await db.commit()

        fetched = await repo.get_by_ticker_venue("BTC/USD", "kraken")
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.ticker == "BTC/USD"
        assert fetched.venue == "kraken"
        assert fetched.paper_only is True
        assert fetched.enabled is True
        fetched_by_id = await repo.get_by_id(created.id)
        assert fetched_by_id is not None
        assert fetched_by_id.id == created.id

    @pytest.mark.asyncio
    async def test_list_enabled_returns_enabled_paper_only_configs(self, db):
        repo = DcaConfigRepository(db)
        await repo.create(_config_row(ticker="BTC/USD", enabled=True, paper_only=True))
        await repo.create(_config_row(ticker="ETH/USD", enabled=False, paper_only=True))
        await repo.create(_config_row(ticker="SOL/USD", enabled=True, paper_only=False))
        await db.commit()

        rows = await repo.list_enabled()

        assert [row.ticker for row in rows] == ["BTC/USD"]

    @pytest.mark.asyncio
    async def test_list_all_returns_all_configs_ordered(self, db):
        repo = DcaConfigRepository(db)
        await repo.create(_config_row(ticker="ETH/USD", enabled=False))
        await repo.create(_config_row(ticker="BTC/USD", enabled=True))
        await db.commit()

        rows = await repo.list_all()

        assert [(row.ticker, row.venue) for row in rows] == [
            ("BTC/USD", "kraken"),
            ("ETH/USD", "kraken"),
        ]

    @pytest.mark.asyncio
    async def test_decimal_fields_round_trip_safely(self, db):
        repo = DcaConfigRepository(db)
        await repo.create(
            _config_row(
                fixed_cash_amount=Decimal("123.45678901"),
                min_cash_reserve=Decimal("987.65432109"),
                dip_buy_multiplier=Decimal("1.7500"),
                max_position_percent=Decimal("12.3456"),
            )
        )
        await db.commit()

        fetched = await repo.get_by_ticker_venue("BTC/USD", "kraken")
        assert fetched is not None
        assert fetched.fixed_cash_amount == Decimal("123.45678901")
        assert fetched.min_cash_reserve == Decimal("987.65432109")
        assert fetched.dip_buy_multiplier == Decimal("1.7500")
        assert fetched.max_position_percent == Decimal("12.3456")

    @pytest.mark.asyncio
    async def test_uniqueness_constraint_ticker_venue(self, db):
        repo = DcaConfigRepository(db)
        await repo.create(_config_row(ticker="BTC/USD", venue="kraken"))
        await db.commit()

        with pytest.raises((IntegrityError, Exception)):
            await repo.create(_config_row(ticker="BTC/USD", venue="kraken"))
            await db.commit()

    @pytest.mark.asyncio
    async def test_same_ticker_different_venues_are_independent_rows(self, db):
        repo = DcaConfigRepository(db)
        await repo.create(_config_row(ticker="BTC/USD", venue="kraken", cadence_days=7))
        await repo.create(_config_row(ticker="BTC/USD", venue="paper-test", cadence_days=14))
        await db.commit()

        kraken = await repo.get_by_ticker_venue("BTC/USD", "kraken")
        paper_test = await repo.get_by_ticker_venue("BTC/USD", "paper-test")
        assert kraken is not None
        assert paper_test is not None
        assert kraken.cadence_days == 7
        assert paper_test.cadence_days == 14


class TestDcaConfigFromRow:
    def test_converts_orm_row_to_planner_config(self):
        from app.strategies.kraken_dca_planner import DCAConfig

        config = dca_config_from_row(
            _config_row(
                enabled=True,
                cadence_days=14,
                fixed_cash_amount=Decimal("150.00000000"),
                dip_buy_enabled=False,
                dip_threshold_pct=Decimal("4.2500"),
                dip_buy_multiplier=Decimal("1.5000"),
                dip_ema_period=30,
                min_cash_reserve=Decimal("750.00000000"),
                max_position_percent=Decimal("15.0000"),
            )
        )

        assert isinstance(config, DCAConfig)
        assert config.ticker == "BTC/USD"
        assert config.venue == "kraken"
        assert config.cadence_days == 14
        assert config.base_allocation_usd == Decimal("150.00000000")
        assert config.enable_dip_enhancement is False
        assert config.dip_threshold_pct == 4.25
        assert config.dip_multiplier == 1.5
        assert config.dip_ema_period == 30
        assert config.min_cash_reserve_usd == Decimal("750.00000000")
        assert config.max_position_pct == 15.0
        assert config.paper_only is True
        assert config.enabled is True
