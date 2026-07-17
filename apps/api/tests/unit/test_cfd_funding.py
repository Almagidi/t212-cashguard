"""
Unit tests for the CFD overnight funding cost service.

All DB interactions are mocked via AsyncMock — no live database required.
The ORM model (CFDFundingCost) is instantiated in-memory; no real DB needed.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.cfd_funding import (
    DEFAULT_ANNUAL_RATE_PCT,
    get_funding_costs_summary,
    track_cfd_funding,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_db():
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _pos(ticker="AAPL", quantity=10, price=150.0, **kwargs):
    base = {"ticker": ticker, "quantity": quantity, "currentPrice": price}
    base.update(kwargs)
    return base


# ── track_cfd_funding ─────────────────────────────────────────────────────────


class TestTrackCFDFunding:
    @pytest.mark.asyncio
    async def test_empty_positions_returns_empty_list(self):
        db = _make_db()
        result = await track_cfd_funding(db, [])
        assert result == []
        db.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_basic_notional_and_charge_calculation(self):
        db = _make_db()
        # notional = 10 * 100 = 1000
        # daily_charge = 1000 * (5.5/100) / 360
        result = await track_cfd_funding(db, [_pos(quantity=10, price=100)])
        assert len(result) == 1
        rec = result[0]
        assert rec.notional == Decimal("1000")
        expected = Decimal("1000") * (DEFAULT_ANNUAL_RATE_PCT / 100) / 360
        assert rec.daily_charge == expected

    @pytest.mark.asyncio
    async def test_default_rate_applied_when_no_broker_rate(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos()])
        assert result[0].annual_rate_pct == DEFAULT_ANNUAL_RATE_PCT

    @pytest.mark.asyncio
    async def test_broker_overnight_fee_overrides_default(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos(overnightFee=7.5)])
        assert result[0].annual_rate_pct == Decimal("7.5")

    @pytest.mark.asyncio
    async def test_alternate_overnight_fee_field(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos(overnight_fee=4.0)])
        assert result[0].annual_rate_pct == Decimal("4.0")

    @pytest.mark.asyncio
    async def test_invalid_broker_rate_falls_back_to_default(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos(overnightFee="bad")])
        assert result[0].annual_rate_pct == DEFAULT_ANNUAL_RATE_PCT

    @pytest.mark.asyncio
    async def test_custom_default_rate_used(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos()], default_rate_pct=Decimal("3.0"))
        assert result[0].annual_rate_pct == Decimal("3.0")

    @pytest.mark.asyncio
    async def test_missing_ticker_skipped(self):
        db = _make_db()
        result = await track_cfd_funding(db, [{"quantity": 10, "currentPrice": 100}])
        assert result == []

    @pytest.mark.asyncio
    async def test_symbol_field_accepted_as_ticker(self):
        db = _make_db()
        result = await track_cfd_funding(
            db, [{"symbol": "NVDA", "quantity": 3, "currentPrice": 500}]
        )
        assert len(result) == 1
        assert result[0].ticker == "NVDA"

    @pytest.mark.asyncio
    async def test_zero_quantity_skipped(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos(quantity=0)])
        assert result == []

    @pytest.mark.asyncio
    async def test_negative_quantity_skipped(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos(quantity=-5)])
        assert result == []

    @pytest.mark.asyncio
    async def test_zero_price_skipped(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos(price=0)])
        assert result == []

    @pytest.mark.asyncio
    async def test_invalid_quantity_raises_and_skips(self):
        db = _make_db()
        pos = {"ticker": "TSLA", "quantity": "not_a_number", "currentPrice": 100}
        result = await track_cfd_funding(db, [pos])
        assert result == []

    @pytest.mark.asyncio
    async def test_alternate_qty_field(self):
        db = _make_db()
        pos = {"ticker": "TSLA", "qty": 5, "currentPrice": 200}
        result = await track_cfd_funding(db, [pos])
        assert len(result) == 1
        assert result[0].quantity == Decimal("5")

    @pytest.mark.asyncio
    async def test_current_price_snake_case_accepted(self):
        db = _make_db()
        pos = {"ticker": "TSLA", "quantity": 5, "current_price": 200}
        result = await track_cfd_funding(db, [pos])
        assert len(result) == 1
        assert result[0].price_at_close == Decimal("200")

    @pytest.mark.asyncio
    async def test_average_price_fallback(self):
        db = _make_db()
        pos = {"ticker": "MSFT", "quantity": 2, "averagePrice": 300}
        result = await track_cfd_funding(db, [pos])
        assert len(result) == 1
        assert result[0].price_at_close == Decimal("300")

    @pytest.mark.asyncio
    async def test_avg_price_snake_case_fallback(self):
        db = _make_db()
        pos = {"ticker": "MSFT", "quantity": 2, "avg_price": 310}
        result = await track_cfd_funding(db, [pos])
        assert len(result) == 1
        assert result[0].price_at_close == Decimal("310")

    @pytest.mark.asyncio
    async def test_strategy_map_applied(self):
        db = _make_db()
        sid = str(uuid.uuid4())
        result = await track_cfd_funding(db, [_pos(ticker="AAPL")], strategy_map={"AAPL": sid})
        assert result[0].strategy_id == uuid.UUID(sid)

    @pytest.mark.asyncio
    async def test_strategy_map_none_gives_no_strategy_id(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos()], strategy_map=None)
        assert result[0].strategy_id is None

    @pytest.mark.asyncio
    async def test_ticker_absent_from_strategy_map_gives_none(self):
        db = _make_db()
        result = await track_cfd_funding(
            db, [_pos(ticker="AAPL")], strategy_map={"TSLA": str(uuid.uuid4())}
        )
        assert result[0].strategy_id is None

    @pytest.mark.asyncio
    async def test_currency_field_stored(self):
        db = _make_db()
        pos = _pos()
        pos["currency"] = "GBP"
        result = await track_cfd_funding(db, [pos])
        assert result[0].currency == "GBP"

    @pytest.mark.asyncio
    async def test_default_currency_is_usd(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos()])
        assert result[0].currency == "USD"

    @pytest.mark.asyncio
    async def test_multiple_positions_all_returned(self):
        db = _make_db()
        positions = [_pos("AAPL"), _pos("TSLA"), _pos("MSFT")]
        result = await track_cfd_funding(db, positions)
        assert len(result) == 3
        assert {r.ticker for r in result} == {"AAPL", "TSLA", "MSFT"}

    @pytest.mark.asyncio
    async def test_db_add_called_per_record(self):
        db = _make_db()
        await track_cfd_funding(db, [_pos("AAPL"), _pos("TSLA")])
        assert db.add.call_count == 2

    @pytest.mark.asyncio
    async def test_db_flush_called_when_records_written(self):
        db = _make_db()
        await track_cfd_funding(db, [_pos()])
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_flush_not_called_when_all_positions_skipped(self):
        db = _make_db()
        await track_cfd_funding(db, [{"quantity": 10, "currentPrice": 100}])  # no ticker
        db.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_record_has_uuid_id(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos()])
        assert isinstance(result[0].id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_record_has_utc_recorded_at(self):
        db = _make_db()
        result = await track_cfd_funding(db, [_pos()])
        assert result[0].recorded_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_positions(self):
        db = _make_db()
        positions = [
            _pos("AAPL"),
            {"quantity": 5, "currentPrice": 100},  # no ticker → skip
            _pos("TSLA", quantity=0),  # qty=0 → skip
            _pos("MSFT"),
        ]
        result = await track_cfd_funding(db, positions)
        assert len(result) == 2
        assert {r.ticker for r in result} == {"AAPL", "MSFT"}


# ── get_funding_costs_summary ─────────────────────────────────────────────────


class TestGetFundingCostsSummary:
    @pytest.mark.asyncio
    async def test_empty_result_returns_zero_totals(self):
        db = MagicMock()
        result_mock = MagicMock()
        result_mock.all.return_value = []
        db.execute = AsyncMock(return_value=result_mock)

        out = await get_funding_costs_summary(db, days=30)
        assert out["total_funding_cost"] == 0.0
        assert out["by_ticker"] == []
        assert out["period_days"] == 30

    @pytest.mark.asyncio
    async def test_with_rows_sums_correctly(self):
        db = MagicMock()
        row1 = MagicMock()
        row1.ticker = "AAPL"
        row1.total_charge = Decimal("10.50")
        row1.days = 5
        row2 = MagicMock()
        row2.ticker = "TSLA"
        row2.total_charge = Decimal("5.25")
        row2.days = 3

        result_mock = MagicMock()
        result_mock.all.return_value = [row1, row2]
        db.execute = AsyncMock(return_value=result_mock)

        out = await get_funding_costs_summary(db, days=7)
        assert out["total_funding_cost"] == round(10.50 + 5.25, 4)
        assert out["period_days"] == 7

    @pytest.mark.asyncio
    async def test_by_ticker_structure(self):
        db = MagicMock()
        row = MagicMock()
        row.ticker = "AAPL"
        row.total_charge = Decimal("20.0")
        row.days = 4
        result_mock = MagicMock()
        result_mock.all.return_value = [row]
        db.execute = AsyncMock(return_value=result_mock)

        out = await get_funding_costs_summary(db)
        aapl = out["by_ticker"][0]
        assert aapl["ticker"] == "AAPL"
        assert aapl["total_charge"] == 20.0
        assert aapl["days_charged"] == 4
        assert aapl["avg_daily"] == round(20.0 / 4, 4)

    @pytest.mark.asyncio
    async def test_days_one_avoids_division_by_zero(self):
        db = MagicMock()
        row = MagicMock()
        row.ticker = "X"
        row.total_charge = Decimal("1.0")
        row.days = 0  # edge: 0 days should clamp to 1 via max(days, 1)
        result_mock = MagicMock()
        result_mock.all.return_value = [row]
        db.execute = AsyncMock(return_value=result_mock)

        out = await get_funding_costs_summary(db)
        assert out["by_ticker"][0]["avg_daily"] == 1.0
