"""Tests for disabled, paper-only Kraken DCA seed configs."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.models import AuditLog, DcaConfig, DcaPlanState, Order
from app.db.seed import seed_dca_configs
from app.strategies.kraken_dca_planner import DEFAULT_PARAMS


async def _count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


class TestDcaSeedConfigs:
    @pytest.mark.asyncio
    async def test_seed_creates_disabled_paper_only_kraken_dca_configs(self, db):
        await seed_dca_configs(db)
        await db.commit()

        rows = (
            await db.execute(
                select(DcaConfig)
                .where(DcaConfig.venue == "kraken")
                .order_by(DcaConfig.ticker)
            )
        ).scalars().all()

        assert [row.ticker for row in rows] == ["BTC/USD", "ETH/USD"]
        for row in rows:
            assert row.venue == "kraken"
            assert row.paper_only is True
            assert row.enabled is False
            assert row.cadence_days == DEFAULT_PARAMS["interval_days"]
            assert row.fixed_cash_amount == Decimal(str(DEFAULT_PARAMS["base_allocation_usd"]))
            assert row.dip_buy_enabled == DEFAULT_PARAMS["enable_dip_enhancement"]
            assert row.dip_threshold_pct == Decimal(str(DEFAULT_PARAMS["dip_threshold_pct"]))
            assert row.dip_buy_multiplier == Decimal(str(DEFAULT_PARAMS["dip_multiplier"]))
            assert row.dip_ema_period == DEFAULT_PARAMS["dip_ema_period"]
            assert row.min_cash_reserve == Decimal(str(DEFAULT_PARAMS["min_cash_reserve_usd"]))
            assert row.max_position_percent == Decimal(str(DEFAULT_PARAMS["max_position_pct"]))

    @pytest.mark.asyncio
    async def test_seed_is_idempotent_and_does_not_create_runtime_rows(self, db):
        await seed_dca_configs(db)
        await seed_dca_configs(db)
        await db.commit()

        assert await _count(db, DcaConfig) == 2
        assert await _count(db, Order) == 0
        assert await _count(db, DcaPlanState) == 0
        assert await _count(db, AuditLog) == 0

    @pytest.mark.asyncio
    async def test_seed_does_not_overwrite_existing_user_modified_config(self, db):
        existing = DcaConfig(
            ticker="BTC/USD",
            venue="kraken",
            cadence_days=14,
            fixed_cash_amount=Decimal("25.00000000"),
            paper_only=True,
            enabled=True,
        )
        db.add(existing)
        await db.commit()

        await seed_dca_configs(db)
        await db.commit()

        btc = (
            await db.execute(
                select(DcaConfig).where(
                    DcaConfig.ticker == "BTC/USD",
                    DcaConfig.venue == "kraken",
                )
            )
        ).scalar_one()
        tickers = (
            await db.execute(select(DcaConfig.ticker).order_by(DcaConfig.ticker))
        ).scalars().all()

        assert tickers == ["BTC/USD", "ETH/USD"]
        assert btc.enabled is True
        assert btc.cadence_days == 14
        assert btc.fixed_cash_amount == Decimal("25.00000000")
