"""
Kraken crypto exchange API adapter.
Implements endpoints for account, positions, and orders.

Base URL: https://api.kraken.com
Auth: API-Key header + API-Sign (HMAC-SHA512 of (path + SHA256(nonce + POST body)) with base64-decoded secret)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal
from typing import Any

import httpx


class KrakenAuthError(Exception):
    """Raised on Kraken auth failures."""
    def __init__(self, message: str, code: int = 0):
        self.message = message
        self.code = code
        super().__init__(message)


class KrakenAPIError(Exception):
    """Raised on Kraken API errors."""
    def __init__(self, message: str, code: int = 0):
        self.message = message
        self.code = code
        super().__init__(message)


class KrakenRateLimitError(Exception):
    """Raised when rate limited."""
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s")


class KrakenAdapter:
    """
    Async HTTP adapter for the Kraken REST API.
    """

    def __init__(self, api_key: str, api_secret: str, environment: str = "demo"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.environment = environment
        self.base_url = "https://api.kraken.com"
        self._client: httpx.AsyncClient | None = None
        self._rate_limit_reset_at: float = 0.0

    async def __aenter__(self) -> KrakenAdapter:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("Adapter must be used as async context manager")
        return self._client

    async def _check_rate_limit(self) -> None:
        now = time.monotonic()
        if self._rate_limit_reset_at > now:
            wait = self._rate_limit_reset_at - now
            await asyncio.sleep(wait)

    def _sign(self, path: str, post_data: str, nonce: str) -> str:
        """Generate Kraken API-Sign header.

        Kraken spec: HMAC-SHA512(base64_decoded_secret, path + SHA256(nonce + POST_body))
        post_data must be the full urlencoded body including the nonce field.
        """
        secret = base64.b64decode(self.api_secret)
        sha256 = hashlib.sha256((nonce + post_data).encode()).digest()
        message = path.encode() + sha256  # path first, then SHA256 digest
        return base64.b64encode(hmac.new(secret, message, hashlib.sha512).digest()).decode()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._check_rate_limit()

        is_private = "/private/" in path

        if is_private:
            # Kraken private endpoints require form-encoded bodies, not JSON.
            # Nonce must be in the body that is signed and sent.
            nonce = str(int(time.time() * 1000))
            params = {"nonce": nonce, **(json or {})}
            post_body = urllib.parse.urlencode(params)
            api_sign = self._sign(path, post_body, nonce)
            headers: dict[str, str] = {
                "API-Key": self.api_key,
                "API-Sign": api_sign,
                "Content-Type": "application/x-www-form-urlencoded",
            }
            try:
                response = await self.client.request(
                    method, path, content=post_body.encode(), headers=headers
                )
            except httpx.RequestError as e:
                raise KrakenAPIError(str(e)) from e
        else:
            try:
                response = await self.client.request(method, path)
            except httpx.RequestError as e:
                raise KrakenAPIError(str(e)) from e

        if response.status_code in (401, 403):
            raise KrakenAuthError(f"Auth failed: {response.text}", response.status_code)

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "60"))
            self._rate_limit_reset_at = time.monotonic() + retry_after
            raise KrakenRateLimitError(retry_after)

        if response.status_code >= 400:
            raise KrakenAPIError(f"HTTP {response.status_code}: {response.text}", response.status_code)

        data = response.json()

        if not data.get("error"):
            return data.get("result", {})

        errors = data.get("error", [])
        if errors and errors[0]:
            raise KrakenAPIError(str(errors[0]))

        return data.get("result", {})

    # ──────────────────────────────────────────────────────────────────────────
    # Account
    # ──────────────────────────────────────────────────────────────────────────

    async def get_account_summary(self) -> dict[str, Any]:
        """
        Get extended account balance.
        """
        result = await self._request("POST", "/0/private/Balance")
        balances = {}
        for asset, balance in result.items():
            if Decimal(balance) > 0:
                balances[asset] = balance

        usd_balance = Decimal(balances.get("ZUSD", "0"))
        return {
            "cash": float(usd_balance),
            "free": float(usd_balance),
            "invested": 0.0,
            "result": 0.0,
            "total": float(usd_balance),
            "balances": balances,
        }

    async def get_account_metadata(self) -> dict[str, Any]:
        """
        Get account info.
        """
        result = await self._request("POST", "/0/private/AccountBalance")
        return {
            "currency": "USD",
            "balances": result,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Instruments & Exchanges
    # ──────────────────────────────────────────────────────────────────────────

    async def get_instruments(self) -> list[dict[str, Any]]:
        """
        Get tradable asset pairs.
        """
        result = await self._request("POST", "/0/public/Assets")
        instruments = []
        for asset, data in result.items():
            if data.get("status") == "online":
                instruments.append({
                    "asset": asset,
                    "name": data.get("altname", asset),
                    "decimals": data.get("decimals"),
                    "display_decimals": data.get("displayDecimal"),
                })
        return instruments

    async def get_exchanges(self) -> list[dict[str, Any]]:
        return [{"name": "Kraken", "code": "kraken"}]

    # ──────────────────────────────────────────────────────────────────────────
    # Positions
    # ─��────────────────────────────────────────────────────────────────────────

    async def get_positions(self) -> list[dict[str, Any]]:
        """
        Get open positions.
        """
        result = await self._request("POST", "/0/private/OpenPositions", json={"docalcs": "true"})
        positions = []
        for txid, pos in result.items():
            if Decimal(pos.get("vol", "0")) != 0:
                positions.append({
                    "txid": txid,
                    "pair": pos.get("pair"),
                    "type": pos.get("type"),
                    "volume": pos.get("vol"),
                    "cost": pos.get("cost"),
                    "fee": pos.get("fee"),
                    "price": pos.get("price"),
                    "result": pos.get("result"),
                    "margin": pos.get("margin"),
                })
        return positions

    async def get_position(self, ticker: str) -> dict[str, Any] | None:
        """
        Get a single position.
        """
        positions = await self.get_positions()
        for pos in positions:
            if pos.get("pair") == ticker:
                return pos
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Orders — Pending
    # ──────────────────────────────────────────────────────────────────────────

    async def get_pending_orders(self) -> list[dict[str, Any]]:
        result = await self._request("POST", "/0/private/OpenOrders")
        return list(result.get("open", {}).values())

    async def get_order_by_id(self, order_id: str) -> dict[str, Any]:
        result = await self._request("POST", "/0/private/QueryOrders", json={"txid": order_id})
        return result.get(order_id, {})

    # ──────────────────────────────────────────────────────────────────────────
    # Orders — Placement
    # ──────────────────────────────────────────────────────────────────────────

    async def place_market_order(
        self,
        ticker: str,
        quantity: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        """
        Place a market order on Kraken.
        """
        pair = self._ticker_to_pair(ticker)
        order_type = "buy" if quantity > 0 else "sell"
        return await self._request("POST", "/0/private/AddOrder", json={
            "pair": pair,
            "type": order_type,
            "ordertype": "market",
            "volume": str(abs(quantity)),
        })

    async def place_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        limit_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        """
        Place a limit order on Kraken.
        """
        pair = self._ticker_to_pair(ticker)
        order_type = "buy" if quantity > 0 else "sell"
        validity = "day" if time_validity == "DAY" else "gtc"
        return await self._request("POST", "/0/private/AddOrder", json={
            "pair": pair,
            "type": order_type,
            "ordertype": "limit",
            "price": str(limit_price),
            "volume": str(abs(quantity)),
            "validity": validity,
        })

    async def place_stop_order(
        self,
        ticker: str,
        quantity: Decimal,
        stop_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        pair = self._ticker_to_pair(ticker)
        order_type = "buy" if quantity > 0 else "sell"
        return await self._request("POST", "/0/private/AddOrder", json={
            "pair": pair,
            "type": order_type,
            "ordertype": "stop-loss",
            "price": str(stop_price),
            "volume": str(abs(quantity)),
        })

    async def place_stop_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        stop_price: Decimal,
        limit_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        pair = self._ticker_to_pair(ticker)
        order_type = "buy" if quantity > 0 else "sell"
        return await self._request("POST", "/0/private/AddOrder", json={
            "pair": pair,
            "type": order_type,
            "ordertype": "stop-loss-limit",
            "price": str(limit_price),
            "price2": str(stop_price),
            "volume": str(abs(quantity)),
        })

    async def cancel_order(self, order_id: str) -> None:
        await self._request("POST", "/0/private/CancelOrder", json={"txid": order_id})

    # ──────────────────────────────────────────────────────────────────────────
    # Historical data
    # ──────────────────────────────────────────────────────────────────────────

    async def get_historical_orders(
        self,
        cursor: int | None = None,
        ticker: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        result = await self._request("POST", "/0/private/ClosedOrders", json={"count": limit})
        return result

    async def get_historical_transactions(
        self,
        cursor: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        result = await self._request("POST", "/0/private/Ledgers", json={"limit": limit})
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────────────────────

    async def test_connection(self) -> dict[str, Any]:
        """Quick health check: fetch account info."""
        try:
            info = await self.get_account_metadata()
            return {
                "is_ok": True,
                "account_id": self.api_key[:8] + "...",
                "currency": info.get("currency", "USD"),
                "error": None,
            }
        except (KrakenAuthError, KrakenAPIError) as e:
            return {
                "is_ok": False,
                "account_id": None,
                "currency": None,
                "error": str(e),
                "diagnostics": {
                    "code": "broker_auth_rejected",
                    "title": "Kraken rejected broker authentication",
                    "summary": e.message if e.message else "Invalid API key or secret",
                    "causes": [
                        {
                            "key": "invalid_credentials",
                            "label": "Invalid API key or secret",
                            "likelihood": "likely",
                            "detail": "Check that the API key has the required permissions and the secret is correct.",
                        },
                    ],
                },
            }
        except Exception as e:
            return {"is_ok": False, "account_id": None, "currency": None, "error": str(e), "diagnostics": None}

    def _ticker_to_pair(self, ticker: str) -> str:
        """Convert ticker symbol to Kraken pair format (e.g., BTCUSD -> XXBTZUSD)."""
        # Explicit base-currency normalisation: Kraken calls Bitcoin 'XBT', not 'BTC'.
        # This must run before the quote-suffix branches so that 'BTCUSD' reaches
        # the USD branch as 'XBTUSD' and produces 'XXBTZUSD', not 'XBTCZUSD'.
        if ticker.startswith("BTC"):
            ticker = "XBT" + ticker[3:]
        if ticker.endswith("USD"):
            return f"X{ticker.replace('USD', 'ZUSD')}"
        if ticker.endswith("EUR"):
            return f"X{ticker.replace('EUR', 'ZEUR')}"
        if ticker.endswith("GBP"):
            return f"X{ticker.replace('GBP', 'ZGBP')}"
        if ticker.endswith("BTC"):
            return f"X{ticker.replace('BTC', 'XXBT')}"
        return ticker.upper()


def make_sell_quantity(quantity: Decimal) -> Decimal:
    """Kraken uses positive quantity for both buy and sell, type determines direction."""
    return quantity.copy_abs()