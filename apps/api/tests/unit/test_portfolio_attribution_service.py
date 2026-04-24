from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.db.models import Order, Signal, Strategy
from app.services import portfolio_attribution_service as attribution_module
from app.services.portfolio_attribution_service import PortfolioAttributionService


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
    timeline_dates = [today - timedelta(days=3), today - timedelta(days=2), today - timedelta(days=1), today]
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
