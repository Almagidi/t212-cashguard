from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.db.models import Order, Signal, Strategy
from app.services import portfolio_attribution_service as attribution_module
from app.services.portfolio_attribution_service import (
    PortfolioAttributionService,
    PositionLedger,
    RebalanceFill,
)


@dataclass
class ProviderBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class StaticAttributionProvider:
    def __init__(self, closes: dict[str, list[tuple[date, Decimal]]]) -> None:
        self._closes = closes

    async def get_bars(
        self,
        ticker: str,
        *,
        multiplier: int = 1,
        timespan: str = "day",
        limit: int = 50,
    ) -> list[ProviderBar]:
        del multiplier, timespan
        points = self._closes[ticker][-limit:]
        return [
            ProviderBar(
                timestamp=datetime.combine(current_date, time(16, 0), tzinfo=UTC),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("1000000"),
            )
            for current_date, close in points
        ]


@pytest.mark.asyncio
async def test_portfolio_attribution_replays_rebalance_orders(db, monkeypatch):
    today = datetime.now(UTC).date()
    timeline_dates = [
        today - timedelta(days=3),
        today - timedelta(days=2),
        today - timedelta(days=1),
        today,
    ]
    monkeypatch.setattr(
        attribution_module,
        "get_live_provider",
        lambda: StaticAttributionProvider(
            {
                "SPY": [
                    (timeline_dates[0], Decimal("100")),
                    (timeline_dates[1], Decimal("110")),
                    (timeline_dates[2], Decimal("115")),
                    (timeline_dates[3], Decimal("120")),
                ],
                "QQQ": [
                    (timeline_dates[0], Decimal("200")),
                    (timeline_dates[1], Decimal("200")),
                    (timeline_dates[2], Decimal("205")),
                    (timeline_dates[3], Decimal("210")),
                ],
            }
        ),
    )

    strategy = Strategy(
        id=uuid.uuid4(),
        name="Sleeve Attribution Test",
        type="buy_hold_core",
        is_enabled=True,
        is_live=False,
        params={},
        allowed_tickers=["SPY", "QQQ"],
        session_start="09:30",
        session_end="16:00",
        eod_flatten=False,
    )
    db.add(strategy)

    signal_spy_buy = Signal(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        ticker="SPY",
        side="buy",
        signal_type="portfolio_rebalance",
        status="approved",
        entry_price=Decimal("100"),
        suggested_quantity=Decimal("10"),
        generated_at=datetime.combine(timeline_dates[0], time(15, 0), tzinfo=UTC),
    )
    signal_qqq_buy = Signal(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        ticker="QQQ",
        side="buy",
        signal_type="portfolio_rebalance",
        status="approved",
        entry_price=Decimal("200"),
        suggested_quantity=Decimal("5"),
        generated_at=datetime.combine(timeline_dates[1], time(15, 0), tzinfo=UTC),
    )
    signal_spy_sell = Signal(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        ticker="SPY",
        side="sell",
        signal_type="portfolio_rebalance",
        status="approved",
        entry_price=Decimal("110"),
        suggested_quantity=Decimal("4"),
        generated_at=datetime.combine(timeline_dates[2], time(15, 0), tzinfo=UTC),
    )
    db.add_all([signal_spy_buy, signal_qqq_buy, signal_spy_sell])

    db.add_all(
        [
            Order(
                id=uuid.uuid4(),
                signal_id=signal_spy_buy.id,
                client_order_key="spy-buy",
                ticker="SPY",
                side="buy",
                order_type="market",
                quantity=Decimal("10"),
                filled_quantity=Decimal("10"),
                avg_fill_price=Decimal("100"),
                status="filled",
                is_dry_run=True,
                created_at=datetime.combine(timeline_dates[0], time(15, 30), tzinfo=UTC),
                updated_at=datetime.combine(timeline_dates[0], time(15, 31), tzinfo=UTC),
            ),
            Order(
                id=uuid.uuid4(),
                signal_id=signal_qqq_buy.id,
                client_order_key="qqq-buy",
                ticker="QQQ",
                side="buy",
                order_type="market",
                quantity=Decimal("5"),
                filled_quantity=Decimal("5"),
                avg_fill_price=Decimal("200"),
                status="filled",
                is_dry_run=True,
                created_at=datetime.combine(timeline_dates[1], time(15, 30), tzinfo=UTC),
                updated_at=datetime.combine(timeline_dates[1], time(15, 31), tzinfo=UTC),
            ),
            Order(
                id=uuid.uuid4(),
                signal_id=signal_spy_sell.id,
                client_order_key="spy-sell",
                ticker="SPY",
                side="sell",
                order_type="market",
                quantity=Decimal("4"),
                filled_quantity=Decimal("4"),
                avg_fill_price=Decimal("110"),
                status="filled",
                is_dry_run=True,
                created_at=datetime.combine(timeline_dates[2], time(15, 30), tzinfo=UTC),
                updated_at=datetime.combine(timeline_dates[2], time(15, 31), tzinfo=UTC),
            ),
        ]
    )
    await db.commit()

    service = PortfolioAttributionService(db)
    attribution = await service.build_strategy_attribution(strategy)

    assert attribution.rebalance_days == 3
    assert attribution.order_count == 3
    assert attribution.total_pnl == pytest.approx(210.0)
    assert attribution.realized_pnl == pytest.approx(40.0)
    assert attribution.unrealized_pnl == pytest.approx(170.0)
    assert attribution.total_return_pct == pytest.approx(21.0)
    assert attribution.benchmark_return_pct > 0
    assert attribution.alpha_vs_benchmark_pct != 0
    assert attribution.max_drawdown_pct >= 0
    assert attribution.current_market_value == pytest.approx(1770.0)
    assert attribution.cash_balance == pytest.approx(-1560.0)
    assert len(attribution.timeline) == 4
    assert attribution.timeline[-1].equity_pnl == pytest.approx(210.0)
    assert attribution.timeline[-1].benchmark_pnl > 0
    assert len(attribution.rebalance_events) == 3
    assert attribution.rebalance_events[0].weights[0].ticker == "SPY"
    assert attribution.ticker_attribution[0].ticker == "SPY"
    assert {row.ticker for row in attribution.ticker_attribution} == {"SPY", "QQQ"}

    caveats = " ".join(attribution.coverage_caveats).lower()
    assert "slippage" in caveats
    assert "fee" in caveats
    assert "rejected" in caveats or "cancelled" in caveats
    assert "reconcil" in caveats


# ─── Shared helpers for the focused live-service coverage below ────────────────
#
# These tests target ``PortfolioAttributionService`` — the LIVE module wired into
# the portfolio-attribution API routes (see
# docs/architecture/portfolio-attribution-duplication-investigation.md). They are
# intentionally value-exact: each asserts specific replayed PnL / weight / ledger
# numbers so that a silent simplification of the live attribution math would fail
# here rather than pass as "returns something".


class _ClosesProvider:
    """Daily-close bar provider for the live service's price-history step.

    Unlike ``StaticAttributionProvider`` above, it returns an empty series for
    any unknown ticker instead of raising ``KeyError``, so missing-history
    fallbacks can be exercised deterministically.
    """

    def __init__(self, closes: dict[str, list[tuple[date, Decimal]]]) -> None:
        self._closes = closes

    async def get_bars(
        self,
        ticker: str,
        *,
        multiplier: int = 1,
        timespan: str = "day",
        limit: int = 50,
    ) -> list[ProviderBar]:
        del multiplier, timespan
        points = self._closes.get(ticker, [])[-limit:]
        return [
            ProviderBar(
                timestamp=datetime.combine(current_date, time(16, 0), tzinfo=UTC),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("1000000"),
            )
            for current_date, close in points
        ]


def _install_provider(monkeypatch, closes: dict[str, list[tuple[date, Decimal]]]) -> None:
    monkeypatch.setattr(
        attribution_module,
        "get_live_provider",
        lambda: _ClosesProvider(closes),
    )


def _make_strategy(
    *,
    tickers: list[str],
    name: str = "Live Attribution",
    strategy_type: str = "buy_hold_core",
) -> Strategy:
    return Strategy(
        id=uuid.uuid4(),
        name=name,
        type=strategy_type,
        is_enabled=True,
        is_live=False,
        params={},
        allowed_tickers=list(tickers),
        session_start="09:30",
        session_end="16:00",
        eod_flatten=False,
    )


async def _add_fill(
    db,
    strategy: Strategy,
    *,
    ticker: str,
    side: str,
    quantity: str,
    on: date,
    key: str,
    avg_fill_price: str | None = None,
    cash_used: str | None = None,
    entry_price: str | None = None,
    target_weight: str | None = None,
    signal_type: str = "portfolio_rebalance",
    status: str = "filled",
) -> None:
    signal = Signal(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        ticker=ticker,
        side=side,
        signal_type=signal_type,
        status="approved",
        entry_price=Decimal(entry_price) if entry_price is not None else None,
        suggested_quantity=Decimal(quantity),
        params_snapshot=({"target_weight": target_weight} if target_weight is not None else None),
        generated_at=datetime.combine(on, time(15, 0), tzinfo=UTC),
    )
    db.add(signal)
    order = Order(
        id=uuid.uuid4(),
        signal_id=signal.id,
        client_order_key=key,
        ticker=ticker,
        side=side,
        order_type="market",
        quantity=Decimal(quantity),
        filled_quantity=Decimal(quantity),
        avg_fill_price=Decimal(avg_fill_price) if avg_fill_price is not None else None,
        cash_used=Decimal(cash_used) if cash_used is not None else None,
        status=status,
        is_dry_run=True,
        created_at=datetime.combine(on, time(15, 30), tzinfo=UTC),
        updated_at=datetime.combine(on, time(15, 31), tzinfo=UTC),
    )
    db.add(order)


# ─── End-to-end replay behaviour (build_strategy_attribution) ─────────────────


@pytest.mark.asyncio
async def test_empty_strategy_returns_zeroed_attribution(db, monkeypatch):
    """No filled rebalance orders → a fully-zeroed, well-formed result."""
    _install_provider(monkeypatch, {})
    strategy = _make_strategy(tickers=["SPY"])
    db.add(strategy)
    await db.commit()

    result = await PortfolioAttributionService(db).build_strategy_attribution(strategy)

    assert result.strategy_id == strategy.id
    assert result.strategy_name == "Live Attribution"
    assert result.order_count == 0
    assert result.rebalance_days == 0
    assert result.total_pnl == 0.0
    assert result.realized_pnl == 0.0
    assert result.unrealized_pnl == 0.0
    assert result.cash_balance == 0.0
    assert result.current_market_value == 0.0
    assert result.turnover_notional == 0.0
    assert result.buys_notional == 0.0
    assert result.sells_notional == 0.0
    assert result.total_return_pct == 0.0
    assert result.benchmark_return_pct == 0.0
    assert result.alpha_vs_benchmark_pct == 0.0
    assert result.benchmark_name == "Equal-weight SPY"
    assert result.timeline == []
    assert result.recent_timeline == []
    assert result.ticker_attribution == []
    assert result.rebalance_events == []
    assert len(result.coverage_caveats) > 0


@pytest.mark.asyncio
async def test_single_buy_reports_unrealized_only(db, monkeypatch):
    """A single open buy marks-to-market: unrealized PnL, no realized PnL."""
    today = datetime.now(UTC).date()
    d0, d1 = today - timedelta(days=2), today - timedelta(days=1)
    _install_provider(
        monkeypatch,
        {"AAA": [(d0, Decimal("100")), (d1, Decimal("110"))]},
    )
    strategy = _make_strategy(tickers=["AAA"])
    db.add(strategy)
    await _add_fill(
        db,
        strategy,
        ticker="AAA",
        side="buy",
        quantity="10",
        on=d0,
        key="aaa-buy",
        avg_fill_price="100",
    )
    await db.commit()

    result = await PortfolioAttributionService(db).build_strategy_attribution(strategy)

    assert result.order_count == 1
    assert result.rebalance_days == 1
    assert result.realized_pnl == 0.0
    assert result.unrealized_pnl == pytest.approx(100.0)
    assert result.total_pnl == pytest.approx(100.0)
    assert result.cash_balance == pytest.approx(-1000.0)
    assert result.current_market_value == pytest.approx(1100.0)
    assert result.buys_notional == pytest.approx(1000.0)
    assert result.sells_notional == 0.0
    assert result.turnover_notional == pytest.approx(1000.0)
    # Benchmark for a single-ticker sleeve equals the sleeve, so alpha is zero.
    assert result.total_return_pct == pytest.approx(10.0)
    assert result.benchmark_return_pct == pytest.approx(10.0)
    assert result.alpha_vs_benchmark_pct == pytest.approx(0.0)

    assert len(result.timeline) == 2
    assert result.timeline[-1].equity_pnl == pytest.approx(100.0)
    assert result.timeline[-1].unrealized_pnl == pytest.approx(100.0)

    assert len(result.ticker_attribution) == 1
    row = result.ticker_attribution[0]
    assert row.ticker == "AAA"
    assert row.quantity == pytest.approx(10.0)
    assert row.avg_cost == pytest.approx(100.0)
    assert row.market_price == pytest.approx(110.0)
    assert row.market_value == pytest.approx(1100.0)
    assert row.realized_pnl == 0.0
    assert row.unrealized_pnl == pytest.approx(100.0)
    assert row.total_pnl == pytest.approx(100.0)
    assert row.weight_pct == pytest.approx(100.0)

    assert len(result.rebalance_events) == 1
    event = result.rebalance_events[0]
    assert event.order_count == 1
    assert event.turnover_notional == pytest.approx(1000.0)
    assert event.total_pnl_after == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_buy_then_full_sell_realizes_pnl_and_closes_position(db, monkeypatch):
    """Buy then a full sell realizes PnL and the closed ledger is still shown."""
    today = datetime.now(UTC).date()
    d0, d1 = today - timedelta(days=2), today - timedelta(days=1)
    _install_provider(
        monkeypatch,
        {"BBB": [(d0, Decimal("100")), (d1, Decimal("120"))]},
    )
    strategy = _make_strategy(tickers=["BBB"])
    db.add(strategy)
    await _add_fill(
        db,
        strategy,
        ticker="BBB",
        side="buy",
        quantity="10",
        on=d0,
        key="bbb-buy",
        avg_fill_price="100",
    )
    await _add_fill(
        db,
        strategy,
        ticker="BBB",
        side="sell",
        quantity="10",
        on=d1,
        key="bbb-sell",
        avg_fill_price="120",
    )
    await db.commit()

    result = await PortfolioAttributionService(db).build_strategy_attribution(strategy)

    assert result.order_count == 2
    assert result.rebalance_days == 2
    assert result.realized_pnl == pytest.approx(200.0)
    assert result.unrealized_pnl == pytest.approx(0.0)
    assert result.total_pnl == pytest.approx(200.0)
    assert result.cash_balance == pytest.approx(200.0)
    assert result.current_market_value == pytest.approx(0.0)
    assert result.buys_notional == pytest.approx(1000.0)
    assert result.sells_notional == pytest.approx(1200.0)
    assert result.turnover_notional == pytest.approx(2200.0)
    assert result.total_return_pct == pytest.approx(20.0)

    # A flat-but-realized position must remain in the attribution table.
    assert len(result.ticker_attribution) == 1
    row = result.ticker_attribution[0]
    assert row.ticker == "BBB"
    assert row.quantity == pytest.approx(0.0)
    assert row.realized_pnl == pytest.approx(200.0)
    assert row.unrealized_pnl == pytest.approx(0.0)
    assert row.total_pnl == pytest.approx(200.0)
    assert row.market_value == pytest.approx(0.0)
    assert row.weight_pct == pytest.approx(0.0)
    assert len(result.rebalance_events) == 2


@pytest.mark.asyncio
async def test_partial_sell_splits_realized_and_unrealized(db, monkeypatch):
    """A partial sell books realized PnL while the remainder stays unrealized."""
    today = datetime.now(UTC).date()
    d0, d1 = today - timedelta(days=2), today - timedelta(days=1)
    _install_provider(
        monkeypatch,
        {"CCC": [(d0, Decimal("100")), (d1, Decimal("150"))]},
    )
    strategy = _make_strategy(tickers=["CCC"])
    db.add(strategy)
    await _add_fill(
        db,
        strategy,
        ticker="CCC",
        side="buy",
        quantity="10",
        on=d0,
        key="ccc-buy",
        avg_fill_price="100",
    )
    await _add_fill(
        db,
        strategy,
        ticker="CCC",
        side="sell",
        quantity="4",
        on=d1,
        key="ccc-sell",
        avg_fill_price="150",
    )
    await db.commit()

    result = await PortfolioAttributionService(db).build_strategy_attribution(strategy)

    assert result.realized_pnl == pytest.approx(200.0)  # (150-100) * 4 sold
    assert result.unrealized_pnl == pytest.approx(300.0)  # (150-100) * 6 held
    assert result.total_pnl == pytest.approx(500.0)
    assert result.cash_balance == pytest.approx(-400.0)  # -1000 buy + 600 sell

    assert len(result.ticker_attribution) == 1
    row = result.ticker_attribution[0]
    assert row.ticker == "CCC"
    assert row.quantity == pytest.approx(6.0)
    assert row.avg_cost == pytest.approx(100.0)
    assert row.market_price == pytest.approx(150.0)
    assert row.market_value == pytest.approx(900.0)
    assert row.realized_pnl == pytest.approx(200.0)
    assert row.unrealized_pnl == pytest.approx(300.0)
    assert row.total_pnl == pytest.approx(500.0)
    assert row.weight_pct == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_multi_ticker_attribution_weights_and_ordering(db, monkeypatch):
    """Multi-ticker replay: per-ticker rows, weight normalisation, PnL ordering."""
    today = datetime.now(UTC).date()
    d0, d1 = today - timedelta(days=2), today - timedelta(days=1)
    _install_provider(
        monkeypatch,
        {
            "DDD": [(d0, Decimal("100")), (d1, Decimal("120"))],
            "EEE": [(d0, Decimal("100")), (d1, Decimal("100"))],
        },
    )
    strategy = _make_strategy(tickers=["DDD", "EEE"])
    db.add(strategy)
    await _add_fill(
        db,
        strategy,
        ticker="DDD",
        side="buy",
        quantity="10",
        on=d0,
        key="ddd-buy",
        avg_fill_price="100",
    )
    await _add_fill(
        db,
        strategy,
        ticker="EEE",
        side="buy",
        quantity="5",
        on=d0,
        key="eee-buy",
        avg_fill_price="100",
    )
    await db.commit()

    result = await PortfolioAttributionService(db).build_strategy_attribution(strategy)

    assert result.realized_pnl == pytest.approx(0.0)
    assert result.unrealized_pnl == pytest.approx(200.0)  # only DDD moved
    assert result.total_pnl == pytest.approx(200.0)
    # capital_base = 1500 (10*100 + 5*100); total return = 200 / 1500.
    assert result.total_return_pct == pytest.approx(13.33, abs=0.01)
    assert result.benchmark_return_pct == pytest.approx(10.0)
    assert result.alpha_vs_benchmark_pct == pytest.approx(3.33, abs=0.01)

    rows = result.ticker_attribution
    assert [row.ticker for row in rows] == ["DDD", "EEE"]  # sorted by total_pnl desc
    by_ticker = {row.ticker: row for row in rows}
    assert by_ticker["DDD"].market_value == pytest.approx(1200.0)
    assert by_ticker["EEE"].market_value == pytest.approx(500.0)
    assert by_ticker["DDD"].weight_pct == pytest.approx(70.59, abs=0.01)
    assert by_ticker["EEE"].weight_pct == pytest.approx(29.41, abs=0.01)
    assert by_ticker["DDD"].weight_pct + by_ticker["EEE"].weight_pct == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_recent_timeline_caps_at_30_points(db, monkeypatch):
    """Full timeline is unbounded; recent_timeline is the deterministic last 30."""
    today = datetime.now(UTC).date()
    days = [today - timedelta(days=34 - i) for i in range(35)]
    closes = [(day, Decimal("100") + Decimal(i)) for i, day in enumerate(days)]
    _install_provider(monkeypatch, {"CAP": closes})
    strategy = _make_strategy(tickers=["CAP"])
    db.add(strategy)
    await _add_fill(
        db,
        strategy,
        ticker="CAP",
        side="buy",
        quantity="1",
        on=days[0],
        key="cap-buy",
        avg_fill_price="100",
    )
    await db.commit()

    result = await PortfolioAttributionService(db).build_strategy_attribution(strategy)

    assert len(result.timeline) == 35
    assert len(result.recent_timeline) == 30
    assert result.recent_timeline == result.timeline[-30:]
    # Timeline is chronologically ordered for deterministic API consumers.
    assert result.timeline[0].date == days[0].isoformat()
    assert result.timeline[-1].date == days[-1].isoformat()


@pytest.mark.asyncio
async def test_rebalance_event_weight_change_uses_target_weight(db, monkeypatch):
    """Rebalance events expose target/before/after weights and the gaps."""
    today = datetime.now(UTC).date()
    d0 = today - timedelta(days=1)
    _install_provider(monkeypatch, {"HHH": [(d0, Decimal("100"))]})
    strategy = _make_strategy(tickers=["HHH"])
    db.add(strategy)
    await _add_fill(
        db,
        strategy,
        ticker="HHH",
        side="buy",
        quantity="10",
        on=d0,
        key="hhh-buy",
        avg_fill_price="100",
        target_weight="0.5",
    )
    await db.commit()

    result = await PortfolioAttributionService(db).build_strategy_attribution(strategy)

    assert len(result.rebalance_events) == 1
    weights = result.rebalance_events[0].weights
    assert len(weights) == 1
    change = weights[0]
    assert change.ticker == "HHH"
    assert change.target_weight == pytest.approx(0.5)
    assert change.before_weight is None  # no position before the first buy
    assert change.after_weight == pytest.approx(1.0)
    assert change.before_gap == pytest.approx(0.5)  # 0.5 - 0.0
    assert change.after_gap == pytest.approx(-0.5)  # 0.5 - 1.0


@pytest.mark.asyncio
async def test_priceless_filled_order_is_skipped(db, monkeypatch):
    """A filled order with no derivable price is not counted as a fill."""
    today = datetime.now(UTC).date()
    d0 = today - timedelta(days=1)
    _install_provider(monkeypatch, {"WWW": [(d0, Decimal("100"))]})
    strategy = _make_strategy(tickers=["WWW"])
    db.add(strategy)
    # No avg_fill_price, no cash_used, no signal entry_price → price resolves to 0.
    await _add_fill(db, strategy, ticker="WWW", side="buy", quantity="10", on=d0, key="www-buy")
    await db.commit()

    result = await PortfolioAttributionService(db).build_strategy_attribution(strategy)

    assert result.order_count == 0
    assert result.rebalance_days == 0
    assert result.total_pnl == 0.0
    assert result.ticker_attribution == []


@pytest.mark.asyncio
async def test_only_filled_portfolio_rebalance_orders_are_counted(db, monkeypatch):
    """The fill query ignores non-filled and non-rebalance orders."""
    today = datetime.now(UTC).date()
    d0 = today - timedelta(days=1)
    _install_provider(monkeypatch, {"ZZZ": [(d0, Decimal("100"))]})
    strategy = _make_strategy(tickers=["ZZZ"])
    db.add(strategy)
    # Counted: filled portfolio_rebalance.
    await _add_fill(
        db,
        strategy,
        ticker="ZZZ",
        side="buy",
        quantity="10",
        on=d0,
        key="zzz-buy",
        avg_fill_price="100",
    )
    # Ignored: wrong signal_type.
    await _add_fill(
        db,
        strategy,
        ticker="YYY",
        side="buy",
        quantity="5",
        on=d0,
        key="yyy-buy",
        avg_fill_price="100",
        signal_type="entry",
    )
    # Ignored: not filled.
    await _add_fill(
        db,
        strategy,
        ticker="XXX",
        side="buy",
        quantity="5",
        on=d0,
        key="xxx-buy",
        avg_fill_price="100",
        status="submitted",
    )
    await db.commit()

    result = await PortfolioAttributionService(db).build_strategy_attribution(strategy)

    assert result.order_count == 1
    assert result.buys_notional == pytest.approx(1000.0)
    assert {row.ticker for row in result.ticker_attribution} == {"ZZZ"}


@pytest.mark.asyncio
async def test_build_summary_matches_detail_scalars(db, monkeypatch):
    """build_summary mirrors the detail scalars and the recent_timeline slice."""
    today = datetime.now(UTC).date()
    d0, d1 = today - timedelta(days=2), today - timedelta(days=1)
    _install_provider(
        monkeypatch,
        {"AAA": [(d0, Decimal("100")), (d1, Decimal("110"))]},
    )
    strategy = _make_strategy(tickers=["AAA"])
    db.add(strategy)
    await _add_fill(
        db,
        strategy,
        ticker="AAA",
        side="buy",
        quantity="10",
        on=d0,
        key="aaa-buy",
        avg_fill_price="100",
    )
    await db.commit()

    service = PortfolioAttributionService(db)
    detail = await service.build_strategy_attribution(strategy)
    summary = await service.build_summary(strategy)

    assert summary.strategy_id == detail.strategy_id
    assert summary.strategy_name == detail.strategy_name
    assert summary.benchmark_name == detail.benchmark_name
    assert summary.total_pnl == detail.total_pnl
    assert summary.realized_pnl == detail.realized_pnl
    assert summary.unrealized_pnl == detail.unrealized_pnl
    assert summary.total_return_pct == detail.total_return_pct
    assert summary.benchmark_return_pct == detail.benchmark_return_pct
    assert summary.order_count == detail.order_count
    assert summary.rebalance_days == detail.rebalance_days
    assert summary.recent_timeline == detail.recent_timeline
    assert summary.coverage_caveats == detail.coverage_caveats


# ─── Pure helper math (static methods) ────────────────────────────────────────


def test_effective_price_fallback_chain():
    """Price resolution prefers avg fill, then cash/qty, then signal entry."""
    price_from_fill = PortfolioAttributionService._effective_price(
        Order(avg_fill_price=Decimal("123"), quantity=Decimal("2"), cash_used=Decimal("999")),
        Signal(entry_price=Decimal("50")),
    )
    assert price_from_fill == Decimal("123")

    price_from_cash = PortfolioAttributionService._effective_price(
        Order(avg_fill_price=None, quantity=Decimal("4"), cash_used=Decimal("200")),
        Signal(entry_price=Decimal("50")),
    )
    assert price_from_cash == Decimal("50")  # 200 / 4

    price_from_signal = PortfolioAttributionService._effective_price(
        Order(avg_fill_price=None, quantity=Decimal("4"), cash_used=None),
        Signal(entry_price=Decimal("77")),
    )
    assert price_from_signal == Decimal("77")

    price_none = PortfolioAttributionService._effective_price(
        Order(avg_fill_price=None, quantity=Decimal("4"), cash_used=None),
        Signal(entry_price=None),
    )
    assert price_none == Decimal("0")


def test_positive_decimal_normalizes_inputs():
    assert PortfolioAttributionService._positive_decimal(Decimal("-5")) == Decimal("5")
    assert PortfolioAttributionService._positive_decimal(None) == Decimal("0")
    assert PortfolioAttributionService._positive_decimal("3.5") == Decimal("3.5")


def test_infer_capital_base_branches():
    today = datetime.now(UTC).date()

    def _fill(side: str, qty: str, price: str, day: date) -> RebalanceFill:
        return RebalanceFill(
            ticker="X",
            side=side,
            quantity=Decimal(qty),
            price=Decimal(price),
            occurred_at=datetime.combine(day, time(15, 0), tzinfo=UTC),
        )

    assert PortfolioAttributionService._infer_capital_base([]) == Decimal("0")
    # Single first-day buy.
    assert PortfolioAttributionService._infer_capital_base(
        [_fill("buy", "10", "100", today)]
    ) == Decimal("1000")
    # First-day buys net of first-day sells.
    assert PortfolioAttributionService._infer_capital_base(
        [_fill("buy", "10", "100", today), _fill("sell", "5", "100", today)]
    ) == Decimal("500")
    # Net non-positive falls back to gross initial buys.
    assert PortfolioAttributionService._infer_capital_base(
        [_fill("buy", "10", "100", today), _fill("sell", "20", "100", today)]
    ) == Decimal("1000")
    # First-day sells only → no inferable capital base.
    assert PortfolioAttributionService._infer_capital_base(
        [_fill("sell", "5", "100", today)]
    ) == Decimal("0")


def test_build_benchmark_positions_equal_weight_and_missing_prices():
    today = datetime.now(UTC).date()
    history = {
        "X": {today: Decimal("100")},
        "Y": {today: Decimal("50")},
    }
    positions, cash = PortfolioAttributionService._build_benchmark_positions(
        tickers=["X", "Y"],
        price_history=history,
        start_date=today,
        capital_base=Decimal("1000"),
    )
    assert positions == {"X": Decimal("5"), "Y": Decimal("10")}  # equal 500/leg
    assert cash == Decimal("0")

    # A ticker without a start-date price is excluded from the benchmark basket.
    positions_missing, cash_missing = PortfolioAttributionService._build_benchmark_positions(
        tickers=["X", "Y"],
        price_history={"X": {today: Decimal("100")}},
        start_date=today,
        capital_base=Decimal("1000"),
    )
    assert positions_missing == {"X": Decimal("10")}
    assert cash_missing == Decimal("0")

    # No eligible prices → all capital stays as cash.
    none_positions, none_cash = PortfolioAttributionService._build_benchmark_positions(
        tickers=["X"],
        price_history={},
        start_date=today,
        capital_base=Decimal("1000"),
    )
    assert none_positions == {}
    assert none_cash == Decimal("1000")

    # Non-positive capital base yields an empty benchmark.
    empty_positions, empty_cash = PortfolioAttributionService._build_benchmark_positions(
        tickers=["X"],
        price_history=history,
        start_date=today,
        capital_base=Decimal("0"),
    )
    assert empty_positions == {}
    assert empty_cash == Decimal("0")


def test_snapshot_weights_normalizes_and_excludes():
    positions = {
        "X": PositionLedger(quantity=Decimal("10"), avg_cost=Decimal("100")),
        "Y": PositionLedger(quantity=Decimal("5"), avg_cost=Decimal("100")),
        "Z": PositionLedger(quantity=Decimal("0"), avg_cost=Decimal("0")),
    }
    prices = {"X": Decimal("100"), "Y": Decimal("200")}  # Z has no price and no qty
    weights = PortfolioAttributionService._snapshot_weights(positions, prices)
    assert weights == {"X": Decimal("0.5"), "Y": Decimal("0.5")}  # 1000 / 1000 each

    # No valued positions → empty mapping (no division by zero).
    assert (
        PortfolioAttributionService._snapshot_weights(
            {"X": PositionLedger(quantity=Decimal("0"))}, {"X": Decimal("100")}
        )
        == {}
    )


def test_benchmark_name_formats_universe():
    assert (
        PortfolioAttributionService._benchmark_name(_make_strategy(tickers=["spy", "qqq"]))
        == "Equal-weight QQQ/SPY"
    )
    assert (
        PortfolioAttributionService._benchmark_name(_make_strategy(tickers=["a", "b", "c", "d"]))
        == "Equal-weight A/B/C…"
    )
    assert (
        PortfolioAttributionService._benchmark_name(_make_strategy(tickers=[]))
        == "Equal-weight sleeve universe"
    )
