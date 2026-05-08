"""
Mock broker adapter.
Returns realistic fake data so the app runs fully without Trading 212 credentials.
All mock data is deterministic and based on realistic market values.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


class MockBrokerAdapter:
    """
    Drop-in replacement for Trading212Adapter during mock mode.
    Uses in-memory state so orders and positions persist for the session.
    """

    def __init__(self) -> None:
        self._orders: dict[str, dict[str, Any]] = {}
        self._positions: dict[str, dict[str, Any]] = {}
        self._order_counter = 1000
        self._account_currency = "USD"
        self._available_cash = Decimal("10000.00")
        self._total_value = Decimal("12500.00")

        # Seed some fake positions
        self._positions = {
            "AAPL": {
                "ticker": "AAPL",
                "quantity": 10.0,
                "averagePrice": 172.50,
                "currentPrice": 178.25,
                "ppl": 57.50,
                "fxPpl": 0.0,
                "maxBuy": 50.0,
                "maxSell": 10.0,
                "initialFillDate": "2025-01-10T09:35:00.000Z",
                "frontend": "WC4",
                "pieQuantity": 0.0,
            },
            "MSFT": {
                "ticker": "MSFT",
                "quantity": 5.0,
                "averagePrice": 388.00,
                "currentPrice": 395.50,
                "ppl": 37.50,
                "fxPpl": 0.0,
                "maxBuy": 20.0,
                "maxSell": 5.0,
                "initialFillDate": "2025-01-08T10:02:00.000Z",
                "frontend": "WC4",
                "pieQuantity": 0.0,
            },
        }

    async def __aenter__(self) -> MockBrokerAdapter:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def get_account_summary(self) -> dict[str, Any]:
        invested = sum(
            pos["quantity"] * pos["averagePrice"]
            for pos in self._positions.values()
        )
        ppl = sum(pos["ppl"] for pos in self._positions.values())
        return {
            "cash": float(self._available_cash),
            "free": float(self._available_cash),
            "invested": invested,
            "result": ppl,
            "total": float(self._available_cash) + invested + ppl,
            "pieCash": 0.0,
        }

    async def get_account_metadata(self) -> dict[str, Any]:
        return {
            "id": "MOCK-ACCOUNT-001",
            "currencyCode": self._account_currency,
            "type": "LIVE",
        }

    async def get_instruments(self) -> list[dict[str, Any]]:
        return [
            {"ticker": "AAPL", "name": "Apple Inc.", "type": "STOCK", "currencyCode": "USD", "extendedHours": True, "workingScheduleId": 1},
            {"ticker": "MSFT", "name": "Microsoft Corporation", "type": "STOCK", "currencyCode": "USD", "extendedHours": True, "workingScheduleId": 1},
            {"ticker": "TSLA", "name": "Tesla Inc.", "type": "STOCK", "currencyCode": "USD", "extendedHours": True, "workingScheduleId": 1},
            {"ticker": "GOOGL", "name": "Alphabet Inc.", "type": "STOCK", "currencyCode": "USD", "extendedHours": True, "workingScheduleId": 1},
            {"ticker": "AMZN", "name": "Amazon.com Inc.", "type": "STOCK", "currencyCode": "USD", "extendedHours": True, "workingScheduleId": 1},
            {"ticker": "NVDA", "name": "NVIDIA Corporation", "type": "STOCK", "currencyCode": "USD", "extendedHours": True, "workingScheduleId": 1},
            {"ticker": "META", "name": "Meta Platforms Inc.", "type": "STOCK", "currencyCode": "USD", "extendedHours": True, "workingScheduleId": 1},
            {"ticker": "SPY", "name": "SPDR S&P 500 ETF", "type": "ETF", "currencyCode": "USD", "extendedHours": False, "workingScheduleId": 1},
            {"ticker": "QQQ", "name": "Invesco QQQ Trust", "type": "ETF", "currencyCode": "USD", "extendedHours": False, "workingScheduleId": 1},
            {"ticker": "IWM", "name": "iShares Russell 2000 ETF", "type": "ETF", "currencyCode": "USD", "extendedHours": False, "workingScheduleId": 1},
        ]

    async def get_exchanges(self) -> list[dict[str, Any]]:
        return [
            {"id": 1, "name": "NASDAQ", "workingSchedules": [{"id": 1, "timeFrom": "09:30", "timeTo": "16:00"}]},
            {"id": 2, "name": "NYSE", "workingSchedules": [{"id": 1, "timeFrom": "09:30", "timeTo": "16:00"}]},
        ]

    async def get_positions(self) -> list[dict[str, Any]]:
        # Simulate slight price movements
        result = []
        for pos in self._positions.values():
            pos_copy = dict(pos)
            pos_copy["currentPrice"] = pos["averagePrice"] * (1 + random.uniform(-0.02, 0.03))
            pos_copy["ppl"] = (pos_copy["currentPrice"] - pos["averagePrice"]) * pos["quantity"]
            result.append(pos_copy)
        return result

    async def get_position(self, ticker: str) -> dict[str, Any] | None:
        return self._positions.get(ticker)

    async def get_pending_orders(self) -> list[dict[str, Any]]:
        return [
            o for o in self._orders.values()
            if o["status"] in ("PENDING", "WORKING")
        ]

    async def get_order_by_id(self, order_id: str) -> dict[str, Any]:
        if order_id not in self._orders:
            raise KeyError(f"Order not found: {order_id}")
        return self._orders[order_id]

    async def place_market_order(
        self,
        ticker: str,
        quantity: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        self._order_counter += 1
        order_id = str(self._order_counter)

        is_sell = quantity < 0
        abs_qty = abs(float(quantity))

        # Simulate fill price
        base_prices = {
            "AAPL": 178.0, "MSFT": 395.0, "TSLA": 248.0, "GOOGL": 168.0,
            "AMZN": 198.0, "NVDA": 875.0, "META": 540.0, "SPY": 560.0,
            "QQQ": 480.0, "IWM": 220.0,
        }
        fill_price = base_prices.get(ticker, 100.0) * (1 + random.uniform(-0.001, 0.001))
        cost = abs_qty * fill_price

        if not is_sell:
            # Update mock cash
            if Decimal(str(cost)) > self._available_cash:
                raise ValueError(f"Insufficient cash: need {cost:.2f}, have {float(self._available_cash):.2f}")
            self._available_cash -= Decimal(str(cost))
            # Update or create position
            if ticker in self._positions:
                old = self._positions[ticker]
                new_qty = old["quantity"] + abs_qty
                new_avg = (old["averagePrice"] * old["quantity"] + fill_price * abs_qty) / new_qty
                self._positions[ticker]["quantity"] = new_qty
                self._positions[ticker]["averagePrice"] = new_avg
            else:
                self._positions[ticker] = {
                    "ticker": ticker,
                    "quantity": abs_qty,
                    "averagePrice": fill_price,
                    "currentPrice": fill_price,
                    "ppl": 0.0,
                    "fxPpl": 0.0,
                    "maxBuy": 100.0,
                    "maxSell": abs_qty,
                    "initialFillDate": datetime.now(UTC).isoformat(),
                    "frontend": "WC4",
                    "pieQuantity": 0.0,
                }
        else:
            # Sell: reduce position
            if ticker in self._positions:
                old_qty = self._positions[ticker]["quantity"]
                new_qty = old_qty - abs_qty
                if new_qty <= 0:
                    del self._positions[ticker]
                else:
                    self._positions[ticker]["quantity"] = new_qty
            self._available_cash += Decimal(str(cost))

        order = {
            "id": order_id,
            "ticker": ticker,
            "quantity": float(quantity),
            "timeValidity": time_validity,
            "type": "MARKET",
            "status": "FILLED",
            "filledQuantity": abs_qty,
            "filledPrice": fill_price,
            "dateCreated": datetime.now(UTC).isoformat(),
            "dateModified": datetime.now(UTC).isoformat(),
        }
        self._orders[order_id] = order
        return order

    async def place_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        limit_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        self._order_counter += 1
        order_id = str(self._order_counter)
        order = {
            "id": order_id,
            "ticker": ticker,
            "quantity": float(quantity),
            "limitPrice": float(limit_price),
            "timeValidity": time_validity,
            "type": "LIMIT",
            "status": "WORKING",
            "filledQuantity": 0.0,
            "dateCreated": datetime.now(UTC).isoformat(),
            "dateModified": datetime.now(UTC).isoformat(),
        }
        self._orders[order_id] = order
        return order

    async def place_stop_order(
        self,
        ticker: str,
        quantity: Decimal,
        stop_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        self._order_counter += 1
        order_id = str(self._order_counter)
        order = {
            "id": order_id,
            "ticker": ticker,
            "quantity": float(quantity),
            "stopPrice": float(stop_price),
            "timeValidity": time_validity,
            "type": "STOP",
            "status": "WORKING",
            "filledQuantity": 0.0,
            "dateCreated": datetime.now(UTC).isoformat(),
            "dateModified": datetime.now(UTC).isoformat(),
        }
        self._orders[order_id] = order
        return order

    async def place_stop_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        stop_price: Decimal,
        limit_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        self._order_counter += 1
        order_id = str(self._order_counter)
        order = {
            "id": order_id,
            "ticker": ticker,
            "quantity": float(quantity),
            "stopPrice": float(stop_price),
            "limitPrice": float(limit_price),
            "timeValidity": time_validity,
            "type": "STOP_LIMIT",
            "status": "WORKING",
            "filledQuantity": 0.0,
            "dateCreated": datetime.now(UTC).isoformat(),
            "dateModified": datetime.now(UTC).isoformat(),
        }
        self._orders[order_id] = order
        return order

    async def cancel_order(self, order_id: str) -> None:
        if order_id in self._orders:
            self._orders[order_id]["status"] = "CANCELLED"

    async def get_historical_orders(
        self,
        cursor: int | None = None,
        ticker: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        items = list(self._orders.values())
        if ticker:
            items = [o for o in items if o["ticker"] == ticker]
        return {"items": items[:limit], "nextPagePath": None}

    async def get_historical_transactions(
        self,
        cursor: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {"items": [], "nextPagePath": None}

    async def test_connection(self) -> dict[str, Any]:
        return {
            "is_ok": True,
            "account_id": "MOCK-ACCOUNT-001",
            "currency": "USD",
            "error": None,
        }
