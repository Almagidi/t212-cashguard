"""
Unit tests for the performance attribution service.

DB-touching methods are tested via AsyncMock sessions returning lightweight
fake ORM objects. The _hour_to_session helper is tested purely.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.performance_attribution import (
    PerformanceAttributor,
    SlippageRecord,
    SymbolAttribution,
    TimeAttribution,
    _hour_to_session,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _db_returning(rows):
    """Return a mock AsyncSession whose execute() yields ``rows`` via .all()."""
    result = MagicMock()
    result.all.return_value = rows
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _db_returning_scalars(items):
    scalars = MagicMock()
    scalars.all.return_value = items
    result = MagicMock()
    result.scalars.return_value = scalars
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _fake_order(**kwargs):
    o = MagicMock()
    o.id = "order-001"
    o.ticker = "AAPL"
    o.side = "buy"
    o.quantity = Decimal("10")
    o.avg_fill_price = Decimal("150.00")
    o.created_at = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
    o.is_dry_run = False
    for k, v in kwargs.items():
        setattr(o, k, v)
    return o


def _fake_signal(**kwargs):
    s = MagicMock()
    s.entry_price = Decimal("149.00")
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _fake_trade(**kwargs):
    t = MagicMock()
    t.ticker = "AAPL"
    t.realized_pnl = Decimal("100.00")
    t.opened_at = datetime(2024, 1, 15, 9, 30, tzinfo=timezone.utc)
    t.closed_at = datetime(2024, 1, 15, 15, 0, tzinfo=timezone.utc)
    t.is_dry_run = False
    for k, v in kwargs.items():
        setattr(t, k, v)
    return t


# ── _hour_to_session ──────────────────────────────────────────────────────────

class TestHourToSession:
    def test_opening_session_hour_9(self):
        assert _hour_to_session(9) == "Opening (09:00-10:00)"

    def test_morning_session_hour_10(self):
        assert _hour_to_session(10) == "Morning (10:00-12:00)"

    def test_morning_session_hour_11(self):
        assert _hour_to_session(11) == "Morning (10:00-12:00)"

    def test_lunch_session_hour_12(self):
        assert _hour_to_session(12) == "Lunch (12:00-14:00)"

    def test_lunch_session_hour_13(self):
        assert _hour_to_session(13) == "Lunch (12:00-14:00)"

    def test_afternoon_session_hour_14(self):
        assert _hour_to_session(14) == "Afternoon (14:00-16:00)"

    def test_afternoon_session_hour_15(self):
        assert _hour_to_session(15) == "Afternoon (14:00-16:00)"

    def test_extended_hours_before_market(self):
        assert _hour_to_session(7) == "Extended hours"

    def test_extended_hours_after_close(self):
        assert _hour_to_session(16) == "Extended hours"

    def test_extended_hours_midnight(self):
        assert _hour_to_session(0) == "Extended hours"


# ── slippage_report ───────────────────────────────────────────────────────────

class TestSlippageReport:
    @pytest.mark.asyncio
    async def test_empty_result_returns_empty_list(self):
        db = _db_returning([])
        svc = PerformanceAttributor(db)
        result = await svc.slippage_report()
        assert result == []

    @pytest.mark.asyncio
    async def test_buy_slippage_calculated(self):
        order = _fake_order(avg_fill_price=Decimal("151.00"))
        signal = _fake_signal(entry_price=Decimal("150.00"))
        db = _db_returning([(order, signal)])
        svc = PerformanceAttributor(db)
        records = await svc.slippage_report()
        assert len(records) == 1
        r = records[0]
        assert r.ticker == "AAPL"
        assert r.side == "buy"
        # slippage_pct = (151-150)/150 * 100
        assert float(r.slippage_pct) == pytest.approx(0.667, abs=0.01)

    @pytest.mark.asyncio
    async def test_sell_slippage_calculated(self):
        order = _fake_order(side="sell", avg_fill_price=Decimal("148.00"))
        signal = _fake_signal(entry_price=Decimal("150.00"))
        db = _db_returning([(order, signal)])
        svc = PerformanceAttributor(db)
        records = await svc.slippage_report()
        assert len(records) == 1
        # sell slippage: (expected - actual) / expected * 100 = (150-148)/150*100
        assert float(records[0].slippage_pct) == pytest.approx(1.333, abs=0.01)

    @pytest.mark.asyncio
    async def test_none_signal_skipped(self):
        order = _fake_order()
        db = _db_returning([(order, None)])
        svc = PerformanceAttributor(db)
        records = await svc.slippage_report()
        assert records == []

    @pytest.mark.asyncio
    async def test_signal_with_none_entry_price_skipped(self):
        order = _fake_order()
        signal = _fake_signal(entry_price=None)
        db = _db_returning([(order, signal)])
        svc = PerformanceAttributor(db)
        records = await svc.slippage_report()
        assert records == []

    @pytest.mark.asyncio
    async def test_order_with_none_fill_price_skipped(self):
        order = _fake_order(avg_fill_price=None)
        signal = _fake_signal(entry_price=Decimal("150.00"))
        db = _db_returning([(order, signal)])
        svc = PerformanceAttributor(db)
        records = await svc.slippage_report()
        assert records == []

    @pytest.mark.asyncio
    async def test_zero_expected_price_skipped(self):
        order = _fake_order(avg_fill_price=Decimal("150.00"))
        signal = _fake_signal(entry_price=Decimal("0"))
        db = _db_returning([(order, signal)])
        svc = PerformanceAttributor(db)
        records = await svc.slippage_report()
        assert records == []

    @pytest.mark.asyncio
    async def test_slippage_record_has_expected_fields(self):
        order = _fake_order()
        signal = _fake_signal()
        db = _db_returning([(order, signal)])
        svc = PerformanceAttributor(db)
        records = await svc.slippage_report()
        r = records[0]
        assert isinstance(r, SlippageRecord)
        assert r.order_id == "order-001"
        assert r.expected_price == signal.entry_price
        assert r.actual_price == order.avg_fill_price

    @pytest.mark.asyncio
    async def test_multiple_valid_records(self):
        rows = [
            (_fake_order(id=f"o{i}", ticker="AAPL"), _fake_signal())
            for i in range(3)
        ]
        db = _db_returning(rows)
        svc = PerformanceAttributor(db)
        records = await svc.slippage_report()
        assert len(records) == 3

    @pytest.mark.asyncio
    async def test_custom_days_parameter(self):
        db = _db_returning([])
        svc = PerformanceAttributor(db)
        result = await svc.slippage_report(days=7)
        assert result == []
        db.execute.assert_awaited_once()


# ── symbol_attribution ────────────────────────────────────────────────────────

class TestSymbolAttribution:
    @pytest.mark.asyncio
    async def test_empty_trades_returns_empty(self):
        db = _db_returning_scalars([])
        svc = PerformanceAttributor(db)
        result = await svc.symbol_attribution()
        assert result == []

    @pytest.mark.asyncio
    async def test_single_ticker_with_wins_and_losses(self):
        trades = [
            _fake_trade(realized_pnl=Decimal("50")),
            _fake_trade(realized_pnl=Decimal("30")),
            _fake_trade(realized_pnl=Decimal("-20")),
        ]
        db = _db_returning_scalars(trades)
        svc = PerformanceAttributor(db)
        attrs = await svc.symbol_attribution()
        assert len(attrs) == 1
        a = attrs[0]
        assert isinstance(a, SymbolAttribution)
        assert a.ticker == "AAPL"
        assert a.total_trades == 3
        assert a.winning_trades == 2
        assert a.losing_trades == 1
        assert a.win_rate == pytest.approx(2 / 3)
        assert a.total_pnl == 60.0

    @pytest.mark.asyncio
    async def test_multiple_tickers_sorted_by_pnl(self):
        trades = [
            _fake_trade(ticker="TSLA", realized_pnl=Decimal("-100")),
            _fake_trade(ticker="AAPL", realized_pnl=Decimal("200")),
        ]
        db = _db_returning_scalars(trades)
        svc = PerformanceAttributor(db)
        attrs = await svc.symbol_attribution()
        assert attrs[0].ticker == "AAPL"
        assert attrs[1].ticker == "TSLA"

    @pytest.mark.asyncio
    async def test_zero_total_pnl_contribution_pct_is_zero(self):
        trades = [
            _fake_trade(ticker="AAPL", realized_pnl=Decimal("100")),
            _fake_trade(ticker="TSLA", realized_pnl=Decimal("-100")),
        ]
        db = _db_returning_scalars(trades)
        svc = PerformanceAttributor(db)
        attrs = await svc.symbol_attribution()
        for a in attrs:
            assert a.contribution_pct == 0.0

    @pytest.mark.asyncio
    async def test_all_winning_trades(self):
        trades = [_fake_trade(realized_pnl=Decimal("50")) for _ in range(4)]
        db = _db_returning_scalars(trades)
        svc = PerformanceAttributor(db)
        attrs = await svc.symbol_attribution()
        a = attrs[0]
        assert a.win_rate == 1.0
        assert a.avg_loss == 0.0

    @pytest.mark.asyncio
    async def test_all_losing_trades(self):
        trades = [_fake_trade(realized_pnl=Decimal("-30")) for _ in range(2)]
        db = _db_returning_scalars(trades)
        svc = PerformanceAttributor(db)
        attrs = await svc.symbol_attribution()
        a = attrs[0]
        assert a.win_rate == 0.0
        assert a.avg_win == 0.0

    @pytest.mark.asyncio
    async def test_contribution_pct_sums_to_100_approx(self):
        trades = [
            _fake_trade(ticker="AAPL", realized_pnl=Decimal("75")),
            _fake_trade(ticker="TSLA", realized_pnl=Decimal("25")),
        ]
        db = _db_returning_scalars(trades)
        svc = PerformanceAttributor(db)
        attrs = await svc.symbol_attribution()
        total = sum(a.contribution_pct for a in attrs)
        assert total == pytest.approx(100.0, abs=1.0)


# ── time_of_day_attribution ───────────────────────────────────────────────────

class TestTimeOfDayAttribution:
    @pytest.mark.asyncio
    async def test_empty_trades_returns_empty(self):
        db = _db_returning_scalars([])
        svc = PerformanceAttributor(db)
        result = await svc.time_of_day_attribution()
        assert result == []

    @pytest.mark.asyncio
    async def test_groups_by_et_hour(self):
        # 14:30 UTC = 09:30 ET (during summer DST)
        t1 = _fake_trade(
            opened_at=datetime(2024, 6, 15, 14, 30, tzinfo=timezone.utc),
            realized_pnl=Decimal("100"),
        )
        t2 = _fake_trade(
            opened_at=datetime(2024, 6, 15, 15, 30, tzinfo=timezone.utc),
            realized_pnl=Decimal("50"),
        )
        db = _db_returning_scalars([t1, t2])
        svc = PerformanceAttributor(db)
        attrs = await svc.time_of_day_attribution()
        # Should have 2 distinct hour groups
        assert len(attrs) == 2
        hours = [a.hour_et for a in attrs]
        assert sorted(hours) == hours  # sorted ascending

    @pytest.mark.asyncio
    async def test_win_rate_calculated(self):
        winning = _fake_trade(
            opened_at=datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc),
            realized_pnl=Decimal("100"),
        )
        losing = _fake_trade(
            opened_at=datetime(2024, 6, 15, 14, 30, tzinfo=timezone.utc),
            realized_pnl=Decimal("-50"),
        )
        db = _db_returning_scalars([winning, losing])
        svc = PerformanceAttributor(db)
        attrs = await svc.time_of_day_attribution()
        assert len(attrs) == 1  # both in same ET hour (9 ET)
        a = attrs[0]
        assert a.win_rate == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_best_period_flagged(self):
        t1 = _fake_trade(
            opened_at=datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc),
            realized_pnl=Decimal("100"),
        )
        t2 = _fake_trade(
            opened_at=datetime(2024, 6, 15, 19, 0, tzinfo=timezone.utc),
            realized_pnl=Decimal("10"),
        )
        db = _db_returning_scalars([t1, t2])
        svc = PerformanceAttributor(db)
        attrs = await svc.time_of_day_attribution()
        best = [a for a in attrs if a.best_period]
        assert len(best) == 1

    @pytest.mark.asyncio
    async def test_returns_time_attribution_instances(self):
        t = _fake_trade(
            opened_at=datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc),
            realized_pnl=Decimal("50"),
        )
        db = _db_returning_scalars([t])
        svc = PerformanceAttributor(db)
        attrs = await svc.time_of_day_attribution()
        assert isinstance(attrs[0], TimeAttribution)


# ── mfe_mae_analysis ──────────────────────────────────────────────────────────

class TestMfeMaeAnalysis:
    @pytest.mark.asyncio
    async def test_returns_dict_with_message(self):
        db = _db_returning_scalars([])
        svc = PerformanceAttributor(db)
        result = await svc.mfe_mae_analysis()
        assert "message" in result
        assert "trades_analysed" in result
        assert "recommendation" in result

    @pytest.mark.asyncio
    async def test_trades_analysed_count(self):
        trades = [_fake_trade() for _ in range(5)]
        db = _db_returning_scalars(trades)
        svc = PerformanceAttributor(db)
        result = await svc.mfe_mae_analysis()
        assert result["trades_analysed"] == 5

    @pytest.mark.asyncio
    async def test_custom_days(self):
        db = _db_returning_scalars([])
        svc = PerformanceAttributor(db)
        result = await svc.mfe_mae_analysis(days=14)
        assert result["trades_analysed"] == 0


# ── full_report ───────────────────────────────────────────────────────────────

class TestFullReport:
    @pytest.mark.asyncio
    async def test_full_report_structure(self):
        """Smoke test: full_report with all-empty data returns well-shaped dict."""
        db = MagicMock()

        # slippage_report uses .all()
        empty_all = MagicMock()
        empty_all.all.return_value = []
        empty_scalars = MagicMock()
        empty_scalars.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))

        call_count = 0

        async def multi_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call: slippage (needs .all()), remainder: scalars
            if call_count == 1:
                return empty_all
            return empty_scalars

        db.execute = multi_execute

        svc = PerformanceAttributor(db)
        report = await svc.full_report(days=30)

        assert report["period_days"] == 30
        assert "generated_at" in report
        assert "execution_quality" in report
        assert "symbol_attribution" in report
        assert "time_of_day" in report
        assert "mfe_mae" in report

    @pytest.mark.asyncio
    async def test_full_report_with_slippage_data(self):
        """full_report aggregates slippage stats when orders exist."""
        order = _fake_order(avg_fill_price=Decimal("151.00"))
        signal = _fake_signal(entry_price=Decimal("150.00"))

        db = MagicMock()
        call_count = 0

        async def multi_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # slippage query — returns rows with .all()
                r = MagicMock()
                r.all.return_value = [(order, signal)]
                return r
            # rest: scalars
            scalars = MagicMock()
            scalars.all.return_value = []
            r = MagicMock()
            r.scalars.return_value = scalars
            return r

        db.execute = multi_execute
        svc = PerformanceAttributor(db)
        report = await svc.full_report()

        eq = report["execution_quality"]
        assert eq["total_orders_analysed"] == 1
        assert eq["total_slippage_cost"] >= 0
        assert "worst_fills" in eq
