"""
Unit tests for the portfolio sleeve attribution service.

The pure methods _replay() and _build_ticker_attribution() are called
directly without any mocking. DB-touching methods are patched via AsyncMock.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.portfolio_attribution import (
    PositionLedger,
    PortfolioAttributionService,
    SleeveAttribution,
    SleeveOrderFill,
    TickerAttribution,
    TimelinePoint,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_fill(
    ticker="AAPL",
    side="buy",
    quantity="10",
    fill_price="100",
    occurred_at=None,
    **kwargs,
):
    if occurred_at is None:
        occurred_at = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
    return SleeveOrderFill(
        order_id="order-001",
        signal_id="signal-001",
        occurred_at=occurred_at,
        ticker=ticker,
        side=side,
        quantity=Decimal(quantity),
        fill_price=Decimal(fill_price),
        is_dry_run=False,
        target_weight=None,
        **kwargs,
    )


def _make_histories(ticker="AAPL", price=Decimal("110"), base_date=None):
    if base_date is None:
        base_date = date(2024, 1, 10)
    return {ticker: {base_date: price}}


# ── _replay ───────────────────────────────────────────────────────────────────

class TestReplay:
    def _svc(self):
        return PortfolioAttributionService(MagicMock())

    def test_single_buy_creates_timeline_point(self):
        svc = self._svc()
        d = date(2024, 1, 10)
        fills = [_make_fill()]
        histories = _make_histories()
        timeline, ledger, prices = svc._replay(fills, histories)
        assert len(timeline) >= 1
        point = timeline[0]
        assert isinstance(point, TimelinePoint)
        assert point.order_count >= 1

    def test_buy_increases_quantity_in_ledger(self):
        svc = self._svc()
        fills = [_make_fill(quantity="5", fill_price="100")]
        histories = _make_histories(price=Decimal("100"))
        _, ledger, _ = svc._replay(fills, histories)
        assert ledger["AAPL"].quantity == Decimal("5")

    def test_buy_then_sell_reduces_quantity(self):
        svc = self._svc()
        d1 = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        d2 = datetime(2024, 1, 11, 10, 0, tzinfo=UTC)
        fills = [
            _make_fill(side="buy", quantity="10", fill_price="100", occurred_at=d1),
            _make_fill(side="sell", quantity="5", fill_price="110", occurred_at=d2),
        ]
        histories = {
            "AAPL": {date(2024, 1, 10): Decimal("100"), date(2024, 1, 11): Decimal("110")},
        }
        _, ledger, _ = svc._replay(fills, histories)
        assert ledger["AAPL"].quantity == Decimal("5")

    def test_sell_generates_realized_pnl(self):
        svc = self._svc()
        d1 = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        d2 = datetime(2024, 1, 11, 10, 0, tzinfo=UTC)
        fills = [
            _make_fill(side="buy", quantity="10", fill_price="100", occurred_at=d1),
            _make_fill(side="sell", quantity="10", fill_price="120", occurred_at=d2),
        ]
        histories = {
            "AAPL": {date(2024, 1, 10): Decimal("100"), date(2024, 1, 11): Decimal("120")},
        }
        _, ledger, _ = svc._replay(fills, histories)
        # realized = (120 - 100) * 10 = 200
        assert ledger["AAPL"].realized_pnl == Decimal("200")

    def test_avg_cost_updated_on_buy(self):
        svc = self._svc()
        d1 = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        d2 = datetime(2024, 1, 11, 10, 0, tzinfo=UTC)
        fills = [
            _make_fill(side="buy", quantity="10", fill_price="100", occurred_at=d1),
            _make_fill(side="buy", quantity="10", fill_price="120", occurred_at=d2),
        ]
        histories = {
            "AAPL": {date(2024, 1, 10): Decimal("100"), date(2024, 1, 11): Decimal("120")},
        }
        _, ledger, _ = svc._replay(fills, histories)
        # avg_cost = (100*10 + 120*10) / 20 = 110
        assert ledger["AAPL"].avg_cost == Decimal("110")
        assert ledger["AAPL"].quantity == Decimal("20")

    def test_sell_clears_avg_cost_when_fully_exited(self):
        svc = self._svc()
        d1 = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        d2 = datetime(2024, 1, 11, 10, 0, tzinfo=UTC)
        fills = [
            _make_fill(side="buy", quantity="5", fill_price="100", occurred_at=d1),
            _make_fill(side="sell", quantity="5", fill_price="110", occurred_at=d2),
        ]
        histories = {
            "AAPL": {date(2024, 1, 10): Decimal("100"), date(2024, 1, 11): Decimal("110")},
        }
        _, ledger, _ = svc._replay(fills, histories)
        assert ledger["AAPL"].quantity == Decimal("0")
        assert ledger["AAPL"].avg_cost == Decimal("0")

    def test_cash_decreases_on_buy(self):
        svc = self._svc()
        fills = [_make_fill(quantity="10", fill_price="100")]
        histories = _make_histories(price=Decimal("100"))
        timeline, _, _ = svc._replay(fills, histories)
        # cash_balance should be negative after a buy (we spent cash)
        assert timeline[-1].cash_balance == Decimal("-1000")

    def test_cash_increases_on_sell(self):
        svc = self._svc()
        d1 = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        d2 = datetime(2024, 1, 11, 10, 0, tzinfo=UTC)
        fills = [
            _make_fill(side="buy", quantity="10", fill_price="100", occurred_at=d1),
            _make_fill(side="sell", quantity="10", fill_price="110", occurred_at=d2),
        ]
        histories = {
            "AAPL": {date(2024, 1, 10): Decimal("100"), date(2024, 1, 11): Decimal("110")},
        }
        timeline, _, _ = svc._replay(fills, histories)
        # after buying -1000 and selling +1100, cash = +100
        assert timeline[-1].cash_balance == Decimal("100")

    def test_turnover_counted(self):
        svc = self._svc()
        fills = [_make_fill(quantity="10", fill_price="100")]
        histories = _make_histories(price=Decimal("100"))
        timeline, _, _ = svc._replay(fills, histories)
        buy_day = timeline[0]
        assert buy_day.turnover_notional == Decimal("1000")

    def test_multiple_tickers(self):
        svc = self._svc()
        d = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        fills = [
            _make_fill(ticker="AAPL", quantity="5", fill_price="100", occurred_at=d),
            _make_fill(ticker="TSLA", quantity="2", fill_price="200", occurred_at=d),
        ]
        histories = {
            "AAPL": {date(2024, 1, 10): Decimal("100")},
            "TSLA": {date(2024, 1, 10): Decimal("200")},
        }
        _, ledger, _ = svc._replay(fills, histories)
        assert "AAPL" in ledger
        assert "TSLA" in ledger

    def test_latest_prices_returned(self):
        svc = self._svc()
        fills = [_make_fill(quantity="1", fill_price="100")]
        histories = _make_histories(price=Decimal("115"))
        _, _, prices = svc._replay(fills, histories)
        assert prices["AAPL"] == Decimal("115")

    def test_empty_fills_with_no_history_gives_empty_timeline(self):
        svc = self._svc()
        timeline, ledger, prices = svc._replay([], {})
        assert timeline == []
        assert ledger == {}
        assert prices == {}

    def test_market_value_uses_latest_price(self):
        svc = self._svc()
        fills = [_make_fill(quantity="10", fill_price="100")]
        histories = _make_histories(price=Decimal("130"))
        timeline, _, _ = svc._replay(fills, histories)
        last = timeline[-1]
        # market_value = 10 * 130 = 1300
        assert last.gross_exposure == Decimal("1300")

    def test_unrealized_pnl_computed(self):
        svc = self._svc()
        fills = [_make_fill(quantity="10", fill_price="100")]
        histories = _make_histories(price=Decimal("110"))
        timeline, _, _ = svc._replay(fills, histories)
        last = timeline[-1]
        # unrealized = (110 - 100) * 10 = 100
        assert last.unrealized_pnl == Decimal("100")

    def test_sell_cannot_exceed_held_quantity(self):
        svc = self._svc()
        d1 = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        d2 = datetime(2024, 1, 11, 10, 0, tzinfo=UTC)
        fills = [
            _make_fill(side="buy", quantity="3", fill_price="100", occurred_at=d1),
            _make_fill(side="sell", quantity="10", fill_price="110", occurred_at=d2),
        ]
        histories = {
            "AAPL": {date(2024, 1, 10): Decimal("100"), date(2024, 1, 11): Decimal("110")},
        }
        _, ledger, _ = svc._replay(fills, histories)
        # Should not go negative
        assert ledger["AAPL"].quantity >= Decimal("0")


# ── _build_ticker_attribution ─────────────────────────────────────────────────

class TestBuildTickerAttribution:
    def _svc(self):
        return PortfolioAttributionService(MagicMock())

    def _ledger(self, qty, avg_cost, realized=Decimal("0")):
        pl = PositionLedger()
        pl.quantity = Decimal(str(qty))
        pl.avg_cost = Decimal(str(avg_cost))
        pl.realized_pnl = Decimal(str(realized))
        return pl

    def test_single_position_attribution(self):
        svc = self._svc()
        ledger = {"AAPL": self._ledger(10, 100)}
        prices = {"AAPL": Decimal("110")}
        attrs = svc._build_ticker_attribution(ledger, prices)
        assert len(attrs) == 1
        a = attrs[0]
        assert isinstance(a, TickerAttribution)
        assert a.ticker == "AAPL"
        assert a.quantity == Decimal("10")
        assert a.avg_cost == Decimal("100")
        assert a.market_price == Decimal("110")
        assert a.market_value == Decimal("1100")
        assert a.unrealized_pnl == Decimal("100")  # (110-100)*10

    def test_realized_pnl_included(self):
        svc = self._svc()
        ledger = {"AAPL": self._ledger(10, 100, realized=Decimal("50"))}
        prices = {"AAPL": Decimal("100")}
        attrs = svc._build_ticker_attribution(ledger, prices)
        a = attrs[0]
        assert a.realized_pnl == Decimal("50")
        assert a.total_pnl == Decimal("50")  # unrealized=0, realized=50

    def test_weight_pct_sums_to_100(self):
        svc = self._svc()
        ledger = {
            "AAPL": self._ledger(10, 100),
            "TSLA": self._ledger(5, 200),
        }
        prices = {"AAPL": Decimal("100"), "TSLA": Decimal("200")}
        attrs = svc._build_ticker_attribution(ledger, prices)
        total_weight = sum(a.weight_pct for a in attrs)
        assert total_weight == pytest.approx(Decimal("100"), abs=Decimal("0.01"))

    def test_zero_quantity_position_has_zero_weight(self):
        svc = self._svc()
        ledger = {
            "AAPL": self._ledger(0, 100),
            "TSLA": self._ledger(5, 200),
        }
        prices = {"AAPL": Decimal("100"), "TSLA": Decimal("200")}
        attrs = svc._build_ticker_attribution(ledger, prices)
        aapl = next(a for a in attrs if a.ticker == "AAPL")
        assert aapl.weight_pct == Decimal("0")

    def test_fallback_to_avg_cost_when_no_market_price(self):
        svc = self._svc()
        ledger = {"AAPL": self._ledger(10, 150)}
        prices = {}  # no market price available
        attrs = svc._build_ticker_attribution(ledger, prices)
        # Falls back to avg_cost for mark_price
        assert attrs[0].market_price == Decimal("150")

    def test_sorted_by_total_pnl_descending(self):
        svc = self._svc()
        ledger = {
            "LOSER": self._ledger(10, 200, realized=Decimal("-500")),
            "WINNER": self._ledger(10, 100, realized=Decimal("300")),
        }
        prices = {"LOSER": Decimal("150"), "WINNER": Decimal("110")}
        attrs = svc._build_ticker_attribution(ledger, prices)
        assert attrs[0].ticker == "WINNER"

    def test_empty_ledger_returns_empty_list(self):
        svc = self._svc()
        attrs = svc._build_ticker_attribution({}, {})
        assert attrs == []


# ── I/O boundary helpers with mocked dependencies ─────────────────────────────

class TestLoadAndFetchHelpers:
    def _svc(self):
        return PortfolioAttributionService(MagicMock())

    def _db_result(self, rows):
        result = MagicMock()
        result.all.return_value = rows
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        return db

    def _order(self, **kwargs):
        order = MagicMock()
        order.id = "order-001"
        order.signal_id = "signal-001"
        order.created_at = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        order.ticker = "aapl"
        order.side = "buy"
        order.filled_quantity = Decimal("4")
        order.quantity = Decimal("5")
        order.avg_fill_price = Decimal("101.25")
        order.is_dry_run = True
        for key, value in kwargs.items():
            setattr(order, key, value)
        return order

    def _signal(self, **kwargs):
        signal = MagicMock()
        signal.params_snapshot = {"target_weight": "0.25"}
        for key, value in kwargs.items():
            setattr(signal, key, value)
        return signal

    @pytest.mark.asyncio
    async def test_load_order_fills_transforms_rows(self):
        db = self._db_result([(self._order(), self._signal())])
        svc = PortfolioAttributionService(db)

        fills = await svc._load_order_fills("strategy-id")

        assert len(fills) == 1
        fill = fills[0]
        assert fill.order_id == "order-001"
        assert fill.signal_id == "signal-001"
        assert fill.ticker == "AAPL"
        assert fill.quantity == Decimal("4")
        assert fill.fill_price == Decimal("101.25")
        assert fill.is_dry_run is True
        assert fill.target_weight == Decimal("0.25")

    @pytest.mark.asyncio
    async def test_load_order_fills_skips_missing_quantity_or_fill_price(self):
        db = self._db_result([
            (self._order(filled_quantity=None, quantity=None), self._signal()),
            (self._order(avg_fill_price=None), self._signal()),
        ])
        svc = PortfolioAttributionService(db)

        assert await svc._load_order_fills("strategy-id") == []

    @pytest.mark.asyncio
    async def test_load_order_fills_handles_missing_signal_id_and_target_weight(self):
        db = self._db_result([
            (self._order(signal_id=None, filled_quantity=None, quantity=Decimal("7")), self._signal(params_snapshot=None)),
        ])
        svc = PortfolioAttributionService(db)

        fills = await svc._load_order_fills("strategy-id")

        assert fills[0].signal_id is None
        assert fills[0].quantity == Decimal("7")
        assert fills[0].target_weight is None

    @pytest.mark.asyncio
    async def test_load_histories_uses_plain_provider(self):
        svc = self._svc()
        class PlainProvider:
            pass

        provider = PlainProvider()
        fill = _make_fill(ticker="MSFT")

        with patch("app.services.portfolio_attribution.get_live_provider", return_value=provider), \
             patch.object(svc, "_fetch_histories", new=AsyncMock(return_value={"MSFT": {}})) as fetch:
            result = await svc._load_histories([fill])

        assert result == {"MSFT": {}}
        fetch.assert_awaited_once()
        assert fetch.await_args.args[0] is provider
        assert fetch.await_args.args[1] == ["MSFT"]
        assert fetch.await_args.args[2] >= 45

    @pytest.mark.asyncio
    async def test_load_histories_uses_async_context_provider(self):
        svc = self._svc()
        active_provider = MagicMock()
        provider = MagicMock()
        provider.__aenter__ = AsyncMock(return_value=active_provider)
        provider.__aexit__ = AsyncMock(return_value=False)
        fills = [_make_fill(ticker="TSLA"), _make_fill(ticker="AAPL")]

        with patch("app.services.portfolio_attribution.get_live_provider", return_value=provider), \
             patch.object(svc, "_fetch_histories", new=AsyncMock(return_value={"AAPL": {}, "TSLA": {}})) as fetch:
            result = await svc._load_histories(fills)

        assert result == {"AAPL": {}, "TSLA": {}}
        fetch.assert_awaited_once()
        assert fetch.await_args.args[0] is active_provider
        assert fetch.await_args.args[1] == ["AAPL", "TSLA"]

    @pytest.mark.asyncio
    async def test_fetch_histories_maps_bars_to_dates(self):
        svc = self._svc()
        bars = [
            MagicMock(close=Decimal("101")),
            MagicMock(close=Decimal("102")),
        ]
        times = [
            datetime(2024, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2024, 1, 11, 0, 0, tzinfo=UTC),
        ]

        with patch.object(svc, "_fetch_daily_bars", new=AsyncMock(return_value=(bars, times))) as fetch:
            histories = await svc._fetch_histories(MagicMock(), ["AAPL", "MSFT"], 50)

        assert histories["AAPL"][date(2024, 1, 10)] == Decimal("101")
        assert histories["MSFT"][date(2024, 1, 11)] == Decimal("102")
        assert fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_fetch_daily_bars_uses_get_bars_provider(self):
        svc = self._svc()
        raw_bar = MagicMock(
            open=100,
            high=105,
            low=99,
            close=104,
            volume=12345,
            timestamp=datetime(2024, 1, 12, 0, 0, tzinfo=UTC),
        )
        provider = MagicMock()
        provider.get_bars = AsyncMock(return_value=[raw_bar])

        bars, times = await svc._fetch_daily_bars(provider, "AAPL", 30)

        provider.get_bars.assert_awaited_once_with(
            "AAPL",
            multiplier=1,
            timespan="day",
            limit=30,
        )
        assert bars[0].open == Decimal("100")
        assert bars[0].close == Decimal("104")
        assert times == [raw_bar.timestamp]

    @pytest.mark.asyncio
    async def test_fetch_daily_bars_uses_ohlcv_provider(self):
        svc = self._svc()
        provider = MagicMock(spec=["get_ohlcv"])
        provider.get_ohlcv.return_value = [
            {
                "open": "10",
                "high": "11",
                "low": "9.5",
                "close": "10.75",
                "volume": "9000",
                "timestamp": "2024-01-12T00:00:00+00:00",
            }
        ]

        bars, times = await svc._fetch_daily_bars(provider, "AAPL", 20)

        provider.get_ohlcv.assert_called_once_with("AAPL", interval_minutes=1440, bars=20)
        assert bars[0].low == Decimal("9.5")
        assert bars[0].volume == Decimal("9000")
        assert times[0] == datetime(2024, 1, 12, 0, 0, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_maybe_await_returns_plain_values(self):
        svc = self._svc()
        assert await svc._maybe_await("ready") == "ready"


# ── build_for_strategy (integration of pure methods via mocked I/O) ───────────

class TestBuildForStrategy:
    def _fake_strategy(self):
        s = MagicMock()
        s.id = "strategy-uuid"
        s.name = "ORB Breakout"
        s.type = "orb"
        return s

    @pytest.mark.asyncio
    async def test_empty_fills_returns_empty_attribution(self):
        db = MagicMock()
        svc = PortfolioAttributionService(db)
        strategy = self._fake_strategy()
        with patch.object(svc, "_load_order_fills", new=AsyncMock(return_value=[])):
            result = await svc.build_for_strategy(strategy)
        assert isinstance(result, SleeveAttribution)
        assert result.timeline == []
        assert result.ticker_attribution == []
        assert result.total_pnl == Decimal("0")

    @pytest.mark.asyncio
    async def test_with_fills_builds_attribution(self):
        db = MagicMock()
        svc = PortfolioAttributionService(db)
        strategy = self._fake_strategy()

        fill = _make_fill(quantity="10", fill_price="100")
        histories = _make_histories(price=Decimal("110"))

        with patch.object(svc, "_load_order_fills", new=AsyncMock(return_value=[fill])), \
             patch.object(svc, "_load_histories", new=AsyncMock(return_value=histories)):
            result = await svc.build_for_strategy(strategy)

        assert isinstance(result, SleeveAttribution)
        assert result.order_count == 1
        assert len(result.timeline) >= 1
        assert result.buys_notional == Decimal("1000")
        assert result.sells_notional == Decimal("0")

    @pytest.mark.asyncio
    async def test_rebalance_days_counted(self):
        db = MagicMock()
        svc = PortfolioAttributionService(db)
        strategy = self._fake_strategy()

        d1 = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        d2 = datetime(2024, 1, 11, 10, 0, tzinfo=UTC)
        fills = [
            _make_fill(quantity="5", fill_price="100", occurred_at=d1),
            _make_fill(quantity="5", fill_price="105", occurred_at=d2),
        ]
        histories = {
            "AAPL": {
                date(2024, 1, 10): Decimal("100"),
                date(2024, 1, 11): Decimal("105"),
            }
        }
        with patch.object(svc, "_load_order_fills", new=AsyncMock(return_value=fills)), \
             patch.object(svc, "_load_histories", new=AsyncMock(return_value=histories)):
            result = await svc.build_for_strategy(strategy)

        assert result.rebalance_days == 2

    @pytest.mark.asyncio
    async def test_sells_notional_counted(self):
        db = MagicMock()
        svc = PortfolioAttributionService(db)
        strategy = self._fake_strategy()

        d1 = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
        d2 = datetime(2024, 1, 11, 10, 0, tzinfo=UTC)
        fills = [
            _make_fill(side="buy", quantity="10", fill_price="100", occurred_at=d1),
            _make_fill(side="sell", quantity="5", fill_price="110", occurred_at=d2),
        ]
        histories = {
            "AAPL": {
                date(2024, 1, 10): Decimal("100"),
                date(2024, 1, 11): Decimal("110"),
            }
        }
        with patch.object(svc, "_load_order_fills", new=AsyncMock(return_value=fills)), \
             patch.object(svc, "_load_histories", new=AsyncMock(return_value=histories)):
            result = await svc.build_for_strategy(strategy)

        assert result.sells_notional == Decimal("550")  # 5 * 110

    @pytest.mark.asyncio
    async def test_attribution_strategy_fields_preserved(self):
        db = MagicMock()
        svc = PortfolioAttributionService(db)
        strategy = self._fake_strategy()

        with patch.object(svc, "_load_order_fills", new=AsyncMock(return_value=[])):
            result = await svc.build_for_strategy(strategy)

        assert result.strategy_name == "ORB Breakout"
        assert result.strategy_type == "orb"
