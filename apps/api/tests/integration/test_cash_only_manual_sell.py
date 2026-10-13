"""A manual sell is sent to the broker only when a held position covers it."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.broker.trading212 import T212APIError
from app.core.config import settings
from app.db.models import AppSettings, Order

TICKER = "AAPL_US_EQ"


def _order(side: str, quantity: str) -> dict[str, str]:
    return {
        "ticker": TICKER,
        "side": side,
        "order_type": "market",
        "quantity": quantity,
        "time_validity": "DAY",
    }


class HoldingBroker:
    environment = "demo"
    base_url = "https://demo.trading212.com"

    def __init__(self) -> None:
        self.positions: Any = []
        self.position_reads = 0
        self.posts: list[tuple[str, Decimal]] = []

    async def __aenter__(self) -> HoldingBroker:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get_account_summary(self) -> dict[str, Any]:
        return {
            "cash": {"availableToTrade": 5000.0, "blockedForPendingOrders": 0.0},
            "invested": 0.0,
            "result": 0.0,
            "total": 5000.0,
            "currencyCode": "GBP",
        }

    async def get_positions(self) -> Any:
        self.position_reads += 1
        if isinstance(self.positions, Exception):
            raise self.positions
        return self.positions

    async def place_market_order(
        self, ticker: str, quantity: Any, time_validity: str = "DAY"
    ) -> Any:
        self.posts.append((ticker, Decimal(str(quantity))))
        raise httpx.ReadTimeout("response lost after the broker accepted the order")


@pytest_asyncio.fixture
async def broker(db: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.api.deps import get_broker
    from app.api.v1.routes import orders as orders_route
    from app.main import app

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_ORDER_ENABLED", True)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    app_settings = await db.get(AppSettings, 1)
    if app_settings is None:
        db.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False))
    else:
        app_settings.auto_trading_enabled = True
        app_settings.kill_switch_active = False
    await db.commit()
    holding_broker = HoldingBroker()

    async def fake_get_broker(*_args: Any, **_kwargs: Any) -> HoldingBroker:
        return holding_broker

    monkeypatch.setattr(orders_route, "get_broker", fake_get_broker)
    app.dependency_overrides[get_broker] = lambda: holding_broker
    yield holding_broker
    app.dependency_overrides.pop(get_broker, None)


async def _orders(db: Any) -> list[Order]:
    return list((await db.execute(select(Order))).scalars().all())


UNCOVERED = {
    "nothing held": [],
    "another instrument held": [{"ticker": "MSFT_US_EQ", "quantity": 50, "maxSell": 50}],
    "fewer held than sold": [{"ticker": TICKER, "quantity": 4.99, "maxSell": 4.99}],
    "held but not free to sell": [{"ticker": TICKER, "quantity": 10, "maxSell": 2}],
    "free amount above the holding": [{"ticker": TICKER, "quantity": 2, "maxSell": 10}],
    "a short position reported": [{"ticker": TICKER, "quantity": -10}],
    "unreadable quantity": [{"ticker": TICKER, "quantity": "many"}],
    "no quantity field": [{"ticker": TICKER}],
    "positions not a list": {"ticker": TICKER, "quantity": 10},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(UNCOVERED))
async def test_sell_that_no_held_position_covers_is_refused_before_the_broker(
    client: Any, auth_headers: Any, db: Any, broker: HoldingBroker, case: str
) -> None:
    broker.positions = UNCOVERED[case]

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("sell", "5"))

    assert response.status_code == 422
    assert "cash-only" in response.json()["detail"]
    assert broker.position_reads == 1
    assert broker.posts == []
    assert await _orders(db) == []


@pytest.mark.asyncio
async def test_sell_is_refused_when_positions_cannot_be_read(
    client: Any, auth_headers: Any, db: Any, broker: HoldingBroker
) -> None:
    broker.positions = T212APIError(503, "positions unavailable")

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("sell", "5"))

    assert response.status_code >= 400
    assert broker.posts == []
    assert await _orders(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held", "quantity"),
    [
        ([{"ticker": TICKER, "quantity": 5, "maxSell": 5}], "5"),
        ([{"ticker": TICKER, "quantity": 5}], "0.01"),
        ([{"ticker": "aapl_us_eq", "quantity": "10", "maxSell": "7.5"}], "7.5"),
    ],
)
async def test_sell_covered_by_a_held_position_is_sent_once(
    client: Any, auth_headers: Any, broker: HoldingBroker, held: Any, quantity: str
) -> None:
    broker.positions = held

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("sell", quantity))

    assert response.status_code in {201, 202}
    assert len(broker.posts) == 1
    assert broker.posts[0][0] == TICKER
    assert abs(broker.posts[0][1]) == Decimal(quantity)


@pytest.mark.asyncio
async def test_buy_is_not_affected_and_reads_no_positions(
    client: Any, auth_headers: Any, broker: HoldingBroker
) -> None:
    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy", "0.01"))

    assert response.status_code in {201, 202}
    assert broker.position_reads == 0
    assert len(broker.posts) == 1
