from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, Order, RiskProfile, Signal, Strategy
from app.services import portfolio_execution_service as portfolio_service_module
from app.services.portfolio_execution_service import PortfolioExecutionService


@dataclass
class ProviderBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass
class ProviderQuote:
    last: Decimal
    timestamp: datetime


class StaticPortfolioProvider:
    def __init__(self, *, tickers: list[str], days: int = 240) -> None:
        self._tickers = tickers
        self._days = days
        self._now = datetime(2026, 4, 10, tzinfo=UTC)

    async def get_bars(
        self,
        ticker: str,
        *,
        multiplier: int = 1,
        timespan: str = "day",
        limit: int = 50,
    ) -> list[ProviderBar]:
        del multiplier, timespan
        base_prices = {
            "SPY": Decimal("510"),
            "QQQ": Decimal("440"),
            "IWM": Decimal("210"),
            "AAPL": Decimal("180"),
            "MSFT": Decimal("405"),
        }
        slope = {
            "SPY": Decimal("0.65"),
            "QQQ": Decimal("0.90"),
            "IWM": Decimal("0.30"),
            "AAPL": Decimal("0.80"),
            "MSFT": Decimal("0.75"),
        }
        start = self._now - timedelta(days=self._days)
        price = base_prices.get(ticker, Decimal("100"))
        all_bars: list[ProviderBar] = []
        for offset in range(self._days):
            day = start + timedelta(days=offset)
            close = price + (slope.get(ticker, Decimal("0.2")) * Decimal(str(offset)))
            all_bars.append(
                ProviderBar(
                    timestamp=day,
                    open=close - Decimal("1.5"),
                    high=close + Decimal("2.0"),
                    low=close - Decimal("2.0"),
                    close=close,
                    volume=Decimal("1000000"),
                )
            )
        return all_bars[-limit:]

    async def get_quote(self, ticker: str) -> ProviderQuote:
        latest = (await self.get_bars(ticker, limit=self._days))[-1]
        return ProviderQuote(last=latest.close, timestamp=latest.timestamp)

    async def is_market_open(self) -> bool:
        return True


async def seed_portfolio_strategy(
    db,
    *,
    strategy_type: str,
    params: dict | None = None,
    is_live: bool = False,
) -> Strategy:
    app_settings = AppSettings(
        id=1,
        theme="dark",
        timezone="UTC",
        auto_trading_enabled=True,
        kill_switch_active=False,
        live_trading_unlocked=False,
    )
    risk_profile = RiskProfile(
        id=uuid.uuid4(),
        name="Default Test Risk",
        max_risk_per_trade_pct=Decimal("10.0"),
        max_daily_loss_pct=Decimal("10.0"),
        max_open_positions=20,
        max_position_size_pct=Decimal("40.0"),
        max_trades_per_day=50,
        stop_after_consecutive_losses=0,
        symbol_cooldown_seconds=0,
        force_flat_eod=False,
        is_default=True,
    )
    strategy = Strategy(
        id=uuid.uuid4(),
        name="Portfolio Test Strategy",
        type=strategy_type,
        is_enabled=True,
        is_live=is_live,
        params=params or {},
        allowed_tickers=["SPY", "QQQ", "IWM"],
        session_start="09:30",
        session_end="16:00",
        eod_flatten=False,
        risk_profile_id=risk_profile.id,
    )
    db.add_all([app_settings, risk_profile, strategy])
    await db.commit()
    return strategy


@pytest.mark.asyncio
async def test_portfolio_service_creates_dry_run_orders(db, monkeypatch):
    monkeypatch.setattr(portfolio_service_module, "get_live_provider", lambda: StaticPortfolioProvider(tickers=["SPY", "QQQ", "IWM"]))
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    strategy = await seed_portfolio_strategy(
        db,
        strategy_type="buy_hold_core",
        params={"capital_fraction": 0.4, "min_trade_value": 25, "min_weight_delta_pct": 0.5},
        is_live=False,
    )

    service = PortfolioExecutionService(db)
    summary = await service.run_all_enabled(force=True)

    assert summary["strategies_rebalanced"] == 1
    assert summary["dry_run_orders"] > 0

    orders = (await db.execute(select(Order))).scalars().all()
    signals = (await db.execute(select(Signal))).scalars().all()

    assert orders
    assert signals
    assert all(order.is_dry_run for order in orders)
    assert all((signal.params_snapshot or {}).get("allocation") for signal in signals)
    assert strategy.params["portfolio_execution"]["last_allocation_decisions"]
    assert strategy.params["portfolio_execution"]["last_allocation_blocks"] == 0
    assert strategy.params["portfolio_execution"]["last_status"] == "rebalanced"


@pytest.mark.asyncio
async def test_portfolio_service_skips_when_not_due(db, monkeypatch):
    monkeypatch.setattr(portfolio_service_module, "get_live_provider", lambda: StaticPortfolioProvider(tickers=["SPY", "QQQ", "IWM"]))
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    await seed_portfolio_strategy(
        db,
        strategy_type="equal_weight_rebalance",
        params={
            "capital_fraction": 0.5,
            "portfolio_execution": {"last_rebalance_signal_at": "2026-04-01T00:00:00+00:00"},
        },
        is_live=False,
    )

    service = PortfolioExecutionService(db)
    summary = await service.run_all_enabled(force=False)

    assert summary["strategies_rebalanced"] == 0
    assert summary["signals_created"] == 0
    assert summary["skipped"]
    assert summary["skipped"][0]["reason"] == "not_due"
