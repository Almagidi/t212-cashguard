"""Unit 7: a manual order is checked against real figures before it reaches the broker.

N-02: a real market buy has no price to check against cash and risk limits, so it is refused
(a limit order caps the cost). N-03: open positions are the broker's positions, not every order
that ever filled. The daily-loss limit sees today's realised losses. N-28: the risk checks see
the ticker as it is stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.broker.trading212 import T212APIError
from app.core.config import settings
from app.db.models import AppSettings, Order, RiskProfile, Trade

TICKER = "AAPL_US_EQ"


def _order(side: str, order_type: str = "limit", ticker: str = TICKER, **extra: Any) -> dict:
    body: dict[str, Any] = {
        "ticker": ticker,
        "side": side,
        "order_type": order_type,
        "quantity": "2",
        "time_validity": "DAY",
    }
    if order_type in ("limit", "stop_limit"):
        body["limit_price"] = "150"
    body.update(extra)
    return body


class AccountBroker:
    environment = "demo"
    base_url = "https://demo.trading212.com"

    def __init__(self) -> None:
        self.positions: Any = []
        self.summary_reads = 0
        self.position_reads = 0
        self.posts: list[tuple[str, str, Decimal]] = []

    async def __aenter__(self) -> AccountBroker:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get_account_summary(self) -> dict[str, Any]:
        self.summary_reads += 1
        return {
            "cash": {"availableToTrade": 5000.0, "blockedForPendingOrders": 0.0},
            "invested": 0.0,
            "result": 0.0,
            "total": 5000.0,
            "currencyCode": "USD",
        }

    async def get_positions(self) -> Any:
        self.position_reads += 1
        if isinstance(self.positions, Exception):
            raise self.positions
        return self.positions

    async def _post(self, kind: str, ticker: str, quantity: Any) -> dict[str, Any]:
        self.posts.append((kind, ticker, Decimal(str(quantity))))
        return {"id": f"B-{len(self.posts)}", "status": "WORKING", "filledQuantity": 0}

    async def place_market_order(self, ticker: str, quantity: Any, **_kw: Any) -> Any:
        return await self._post("market", ticker, quantity)

    async def place_limit_order(self, ticker: str, quantity: Any, _limit: Any, **_kw: Any) -> Any:
        return await self._post("limit", ticker, quantity)

    async def place_stop_order(self, ticker: str, quantity: Any, _stop: Any, **_kw: Any) -> Any:
        return await self._post("stop", ticker, quantity)

    async def place_stop_limit_order(
        self, ticker: str, quantity: Any, _stop: Any, _limit: Any, **_kw: Any
    ) -> Any:
        return await self._post("stop_limit", ticker, quantity)


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
    profile = (
        await db.execute(select(RiskProfile).where(RiskProfile.is_default.is_(True)))
    ).scalar_one_or_none()
    if profile is None:
        profile = RiskProfile(name="preflight limits", is_default=True)
        db.add(profile)
    profile.max_open_positions = 2
    profile.max_daily_loss_pct = Decimal("5")
    profile.max_position_size_pct = Decimal("100")
    profile.max_trades_per_day = 1000
    profile.stop_after_consecutive_losses = 0
    profile.symbol_cooldown_seconds = 0
    await db.commit()
    account = AccountBroker()

    async def fake_get_broker(*_args: Any, **_kwargs: Any) -> AccountBroker:
        return account

    monkeypatch.setattr(orders_route, "get_broker", fake_get_broker)
    app.dependency_overrides[get_broker] = lambda: account
    yield account
    app.dependency_overrides.pop(get_broker, None)


async def _order_count(db: Any) -> int:
    return int((await db.execute(select(func.count(Order.id)))).scalar_one())


# ── N-02: no placeholder price ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_real_market_buy_is_refused_before_the_broker_is_asked(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy", "market"))

    assert response.status_code == 422
    assert "limit order" in response.json()["detail"]
    assert broker.summary_reads == 0
    assert broker.posts == []
    assert await _order_count(db) == 0


@pytest.mark.asyncio
async def test_a_real_market_sell_still_goes_out(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    broker.positions = [{"ticker": TICKER, "quantity": 10, "maxSell": 10}]

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("sell", "market"))

    assert response.status_code == 201, response.text
    assert broker.posts == [("market", TICKER, Decimal("-2"))]


@pytest.mark.asyncio
async def test_a_limit_buy_is_costed_at_its_limit(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    # A guard (this held before the change too): 2 x 150 = 300 of 5,000 cash.
    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy"))

    assert response.status_code == 201, response.text
    order = (await db.execute(select(Order))).scalar_one()
    assert order.cash_used == Decimal("300")


@pytest.mark.asyncio
async def test_a_real_stop_buy_is_refused_its_stop_does_not_cap_the_cost(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    response = await client.post(
        "/v1/orders", headers=auth_headers, json=_order("buy", "stop", stop_price="500")
    )

    assert response.status_code == 422
    assert "stop-limit" in response.json()["detail"]
    assert broker.summary_reads == 0
    assert broker.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "unused"),
    [
        pytest.param(
            {"order_type": "stop", "stop_price": "90", "limit_price": "0.01"},
            "limit_price is not used by a stop order",
            id="limit-price-on-a-stop-order",
        ),
        pytest.param(
            {"order_type": "market", "limit_price": "0.01"},
            "limit_price is not used by a market order",
            id="limit-on-market",
        ),
        pytest.param(
            {"order_type": "market", "stop_price": "0.01"},
            "stop_price is not used by a market order",
            id="stop-on-market",
        ),
        pytest.param(
            {"order_type": "limit", "limit_price": "150", "stop_price": "1"},
            "stop_price is not used by a limit order",
            id="stop-price-on-a-limit-order",
        ),
    ],
)
async def test_a_price_the_order_type_does_not_use_is_refused(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker, body: dict, unused: str
) -> None:
    # A sell of a held position: nothing but the unused price can refuse it.
    broker.positions = [{"ticker": TICKER, "quantity": 10, "maxSell": 10}]
    order = {k: v for k, v in _order("sell").items() if k != "limit_price"}

    response = await client.post("/v1/orders", headers=auth_headers, json={**order, **body})

    assert response.status_code == 422
    assert unused in response.text
    assert broker.posts == []
    assert await _order_count(db) == 0


@pytest.mark.asyncio
async def test_a_stop_limit_buy_is_costed_at_its_limit(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    body = _order("buy", "stop_limit", stop_price="140")

    response = await client.post("/v1/orders", headers=auth_headers, json=body)

    assert response.status_code == 201, response.text
    order = (await db.execute(select(Order))).scalar_one()
    assert order.cash_used == Decimal("300")
    assert broker.posts == [("stop_limit", TICKER, Decimal("2"))]


@pytest.mark.asyncio
async def test_a_stop_sell_of_a_held_position_still_goes_out(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    broker.positions = [{"ticker": TICKER, "quantity": 10, "maxSell": 10}]

    response = await client.post(
        "/v1/orders", headers=auth_headers, json=_order("sell", "stop", stop_price="90")
    )

    assert response.status_code == 201, response.text
    assert [kind for kind, _, _ in broker.posts] == ["stop"]


@pytest.mark.asyncio
async def test_a_market_sell_is_estimated_at_the_brokers_price(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    broker.positions = [{"ticker": TICKER, "quantity": 10, "maxSell": 10, "currentPrice": 187.5}]

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("sell", "market"))

    assert response.status_code == 201, response.text
    order = (await db.execute(select(Order))).scalar_one()
    assert order.expected_fill_price == Decimal("187.5")
    assert order.cash_used is None


@pytest.mark.asyncio
async def test_a_mock_market_buy_keeps_its_placeholder_cost(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "mock")

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy", "market"))

    assert response.status_code in (201, 202), response.text
    order = (await db.execute(select(Order))).scalar_one()
    assert order.is_dry_run is True
    assert order.cash_used == Decimal("200")


# ── N-03: open positions are the broker's ────────────────────────────────────


async def _lifetime_fills(db: Any, count: int) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=30)
    for index in range(count):
        db.add(
            Order(
                id=uuid.uuid4(),
                client_order_key=f"old-fill-{index}-{uuid.uuid4()}",
                ticker=f"OLD{index}_US_EQ",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                status="filled",
                filled_quantity=Decimal("1"),
                avg_fill_price=Decimal("10"),
                execution_environment="demo",
                is_dry_run=False,
                created_at=long_ago,
                submitted_at=long_ago,
                filled_at=long_ago,
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_orders_that_filled_long_ago_are_not_open_positions(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    await _lifetime_fills(db, 3)  # the profile allows 2 open positions
    broker.positions = []

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy"))

    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_the_brokers_positions_count_towards_the_limit(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    broker.positions = [
        {"ticker": "MSFT_US_EQ", "quantity": 1},
        {"ticker": "NVDA_US_EQ", "quantity": 1},
    ]

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy"))

    assert response.status_code == 422
    assert "Max open positions" in response.json()["detail"]
    assert broker.posts == []


@pytest.mark.asyncio
async def test_a_buy_is_refused_when_the_positions_cannot_be_read(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    broker.positions = T212APIError(503, {"code": "Unavailable"})

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy"))

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "broker_unavailable"
    assert broker.posts == []
    assert await _order_count(db) == 0


@pytest.mark.asyncio
async def test_a_buy_is_refused_when_the_positions_answer_is_not_a_list(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    broker.positions = {"ticker": "MSFT_US_EQ", "quantity": 1}

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy"))

    assert response.status_code == 502
    assert "positions could not be read" in response.json()["detail"]
    assert broker.posts == []


@pytest.mark.asyncio
async def test_an_order_is_refused_when_todays_result_cannot_be_read(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1.routes import orders as orders_route

    async def unreadable(_db: Any) -> Decimal:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(orders_route, "realized_pnl_today", unreadable)

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy"))

    assert response.status_code == 503
    assert "database" not in response.text
    assert broker.posts == []


# ── today's realised losses ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_todays_realised_losses_reach_the_daily_loss_limit(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    now = datetime.now(UTC)
    db.add(
        Trade(
            ticker="MSFT_US_EQ",
            side="buy",
            quantity=Decimal("10"),
            open_price=Decimal("100"),
            close_price=Decimal("70"),
            realized_pnl=Decimal("-300"),  # 6% of 5,000; the limit is 5%
            opened_at=now - timedelta(hours=2),
            closed_at=now - timedelta(minutes=5),
            is_dry_run=False,
        )
    )
    await db.commit()

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("buy"))

    assert response.status_code == 422
    assert "Daily loss" in response.json()["detail"]
    assert broker.posts == []


# ── an exit is not held back by entry limits ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_sell_goes_out_after_the_daily_loss_limit_is_reached(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    now = datetime.now(UTC)
    db.add(
        Trade(
            ticker="MSFT_US_EQ",
            side="buy",
            quantity=Decimal("10"),
            open_price=Decimal("100"),
            close_price=Decimal("70"),
            realized_pnl=Decimal("-300"),
            opened_at=now - timedelta(hours=2),
            closed_at=now - timedelta(minutes=5),
            is_dry_run=False,
        )
    )
    await db.commit()
    broker.positions = [{"ticker": TICKER, "quantity": 10, "maxSell": 10}]

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("sell", "market"))

    assert response.status_code == 201, response.text
    assert [kind for kind, _, _ in broker.posts] == ["market"]


@pytest.mark.asyncio
async def test_a_sell_goes_out_with_the_position_limit_reached(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    broker.positions = [
        {"ticker": TICKER, "quantity": 10, "maxSell": 10},
        {"ticker": "NVDA_US_EQ", "quantity": 1},
        {"ticker": "MSFT_US_EQ", "quantity": 1},
    ]

    response = await client.post("/v1/orders", headers=auth_headers, json=_order("sell", "market"))

    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_a_blank_ticker_is_refused(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    response = await client.post(
        "/v1/orders", headers=auth_headers, json=_order("buy", ticker="   ")
    )

    assert response.status_code == 422
    assert broker.posts == []


# ── N-28: the ticker as it is stored ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_ticker_typed_in_lower_case_meets_the_duplicate_check(
    client: Any, auth_headers: Any, db: Any, broker: AccountBroker
) -> None:
    first = await client.post("/v1/orders", headers=auth_headers, json=_order("buy"))
    assert first.status_code == 201, first.text

    again = await client.post(
        "/v1/orders", headers=auth_headers, json=_order("buy", ticker=" aapl_us_eq ")
    )

    assert again.status_code == 422, again.text
    assert "Duplicate order blocked" in again.json()["detail"]
    assert len(broker.posts) == 1
