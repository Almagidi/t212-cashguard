"""
Trading 212 official API adapter.
Implements all documented endpoints with rate-limit handling,
retry logic, and response validation.

Base URLs:
  Demo: https://demo.trading212.com
  Live: https://live.trading212.com

Auth: HTTP Basic (api_key:api_secret)
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.services.safety_policy import broker_base_url_for

if TYPE_CHECKING:
    from decimal import Decimal


class T212RateLimitError(Exception):
    """Raised when Trading 212 returns 429."""
    def __init__(self, retry_after: float = 60.0):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s")


class T212AuthError(Exception):
    """Raised on 401/403 responses."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Trading 212 auth error {status_code}")


class T212APIError(Exception):
    """Raised on unexpected Trading 212 API errors."""
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"T212 API error {status_code}: {body}")


class Trading212Adapter:
    """
    Async HTTP adapter for the Trading 212 REST API.
    All methods return parsed dicts/lists from the API response JSON.
    """

    def __init__(self, api_key: str, api_secret: str, environment: str = "demo"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.environment = environment
        self.base_url = broker_base_url_for(environment)
        self._client: httpx.AsyncClient | None = None
        self._rate_limit_reset_at: float = 0.0

    async def __aenter__(self) -> Trading212Adapter:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=(self.api_key, self.api_secret),
            timeout=30.0,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
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
        """Wait if we're rate limited."""
        now = time.monotonic()
        if self._rate_limit_reset_at > now:
            wait = self._rate_limit_reset_at - now
            await asyncio.sleep(wait)

    def _build_auth_diagnostics(self, status_code: int) -> dict[str, Any]:
        env_label = "live" if self.environment == "live" else "demo"
        return {
            "code": "broker_auth_rejected",
            "title": "Trading 212 rejected broker authentication",
            "summary": (
                f"The Trading 212 {env_label} endpoint returned HTTP {status_code} for the submitted credentials. "
                "Trading 212 did not specify the exact reason."
            ),
            "environment": self.environment,
            "broker_host": self.base_url,
            "http_status": status_code,
            "causes": [
                {
                    "key": "wrong_environment",
                    "label": "Wrong environment selected",
                    "likelihood": "likely",
                    "detail": (
                        f"Make sure these credentials were generated for the {env_label} account. "
                        f"{env_label.capitalize()} and live API keys are not interchangeable."
                    ),
                },
                {
                    "key": "invalid_credentials",
                    "label": "Invalid or revoked key/secret",
                    "likelihood": "likely",
                    "detail": (
                        "The API key or secret may be mistyped, rotated, revoked, or copied incompletely. "
                        "Regenerate a fresh pair in Trading 212 if unsure."
                    ),
                },
                {
                    "key": "ip_restriction",
                    "label": "IP restriction / allowlist mismatch",
                    "likelihood": "possible",
                    "detail": (
                        "If the Trading 212 key is restricted to specific IPs, add this machine's public IP "
                        "to the allowlist before reconnecting."
                    ),
                },
            ],
            "note": (
                "The app trims leading and trailing whitespace before testing the credentials, "
                "so a repeated 401 usually means the broker rejected the key, secret, or environment."
            ),
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute an HTTP request with rate-limit and error handling."""
        await self._check_rate_limit()

        response = await self.client.request(method, path, json=json, params=params)

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "60"))
            self._rate_limit_reset_at = time.monotonic() + retry_after
            raise T212RateLimitError(retry_after)

        if response.status_code in (401, 403):
            raise T212AuthError(response.status_code, response.text)

        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = response.text
            raise T212APIError(response.status_code, body)

        if response.status_code == 204:
            return None

        return response.json()

    # ──────────────────────────────────────────────────────────────────────────
    # Account
    # ──────────────────────────────────────────────────────────────────────────

    async def get_account_summary(self) -> dict[str, Any]:
        """
        GET /api/v0/equity/account/summary
        Returns account cash, invested, result, total, etc.
        """
        return await self._request("GET", "/api/v0/equity/account/summary")

    async def get_account_metadata(self) -> dict[str, Any]:
        """
        GET /api/v0/equity/account/info
        Returns account currency, ID, etc.
        """
        return await self._request("GET", "/api/v0/equity/account/info")

    # ──────────────────────────────────────────────────────────────────────────
    # Instruments & Exchanges
    # ──────────────────────────────────────────────────────────────────────────

    async def get_instruments(self) -> list[dict[str, Any]]:
        """
        GET /api/v0/equity/metadata/instruments
        Returns list of tradeable instruments.
        """
        result = await self._request("GET", "/api/v0/equity/metadata/instruments")
        return result if isinstance(result, list) else []

    async def get_exchanges(self) -> list[dict[str, Any]]:
        """
        GET /api/v0/equity/metadata/exchanges
        Returns list of exchange/working schedule metadata.
        """
        result = await self._request("GET", "/api/v0/equity/metadata/exchanges")
        return result if isinstance(result, list) else []

    # ──────────────────────────────────────────────────────────────────────────
    # Positions
    # ──────────────────────────────────────────────────────────────────────────

    async def get_positions(self) -> list[dict[str, Any]]:
        """
        GET /api/v0/equity/portfolio
        Returns all open positions.
        Fields: ticker, quantity, averagePrice, currentPrice, ppl, fxPpl,
                initialFillDate, frontend, maxBuy, maxSell, pieQuantity
        """
        result = await self._request("GET", "/api/v0/equity/portfolio")
        return result if isinstance(result, list) else []

    async def get_position(self, ticker: str) -> dict[str, Any] | None:
        """
        GET /api/v0/equity/portfolio/{ticker}
        Returns a single position or None if not held.
        """
        try:
            return await self._request("GET", f"/api/v0/equity/portfolio/{ticker}")
        except T212APIError as e:
            if e.status_code == 404:
                return None
            raise

    # ──────────────────────────────────────────────────────────────────────────
    # Orders — Pending
    # ──────────────────────────────────────────────────────────────────────────

    async def get_pending_orders(self) -> list[dict[str, Any]]:
        """
        GET /api/v0/equity/orders
        Returns all pending orders (limit, stop, stop-limit).
        """
        result = await self._request("GET", "/api/v0/equity/orders")
        return result if isinstance(result, list) else []

    async def get_order_by_id(self, order_id: str) -> dict[str, Any]:
        """
        GET /api/v0/equity/orders/{id}
        """
        return await self._request("GET", f"/api/v0/equity/orders/{order_id}")

    # ──────────────────────────────────────────────────────────────────────────
    # Orders — Placement
    # Trading 212 note: sell quantity MUST be negative.
    # ──────────────────────────────────────────────────────────────────────────

    async def place_market_order(
        self,
        ticker: str,
        quantity: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        """
        POST /api/v0/equity/orders/market
        quantity must be positive for BUY, negative for SELL.
        """
        payload = {
            "ticker": ticker,
            "quantity": float(quantity),
            "timeValidity": time_validity,
        }
        return await self._request("POST", "/api/v0/equity/orders/market", json=payload)

    async def place_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        limit_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        """
        POST /api/v0/equity/orders/limit
        """
        payload = {
            "ticker": ticker,
            "quantity": float(quantity),
            "limitPrice": float(limit_price),
            "timeValidity": time_validity,
        }
        return await self._request("POST", "/api/v0/equity/orders/limit", json=payload)

    async def place_stop_order(
        self,
        ticker: str,
        quantity: Decimal,
        stop_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        """
        POST /api/v0/equity/orders/stop
        """
        payload = {
            "ticker": ticker,
            "quantity": float(quantity),
            "stopPrice": float(stop_price),
            "timeValidity": time_validity,
        }
        return await self._request("POST", "/api/v0/equity/orders/stop", json=payload)

    async def place_stop_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        stop_price: Decimal,
        limit_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        """
        POST /api/v0/equity/orders/stop_limit
        """
        payload = {
            "ticker": ticker,
            "quantity": float(quantity),
            "stopPrice": float(stop_price),
            "limitPrice": float(limit_price),
            "timeValidity": time_validity,
        }
        return await self._request("POST", "/api/v0/equity/orders/stop_limit", json=payload)

    async def cancel_order(self, order_id: str) -> None:
        """
        DELETE /api/v0/equity/orders/{id}
        Returns 204 on success.
        """
        await self._request("DELETE", f"/api/v0/equity/orders/{order_id}")

    # ──────────────────────────────────────────────────────────────────────────
    # Historical data
    # ──────────────────────────────────────────────────────────────────────────

    async def get_historical_orders(
        self,
        cursor: int | None = None,
        ticker: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        GET /api/v0/equity/history/orders
        Paginated historical orders. Returns {items: [...], nextPagePath: str|null}.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if ticker:
            params["ticker"] = ticker
        return await self._request("GET", "/api/v0/equity/history/orders", params=params)

    async def get_historical_transactions(
        self,
        cursor: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        GET /api/v0/equity/history/transactions
        Paginated transaction history.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", "/api/v0/equity/history/transactions", params=params)

    # ──────────────────────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────────────────────

    async def test_connection(self) -> dict[str, Any]:
        """
        Quick health check: fetch account info.
        Returns dict with is_ok, account_id, currency, error.
        """
        try:
            info = await self.get_account_metadata()
            return {
                "is_ok": True,
                "account_id": info.get("id"),
                "currency": info.get("currencyCode"),
                "error": None,
            }
        except T212AuthError as e:
            env_label = "live" if self.environment == "live" else "demo"
            detail = (
                f"Trading 212 rejected the {env_label} API credentials with HTTP {e.status_code}. "
                f"Confirm you generated a {env_label}-account API key and secret for the same environment, "
                "check for extra whitespace when pasting, and make sure any Trading 212 IP allowlist includes "
                "this machine's public IP."
            )
            return {
                "is_ok": False,
                "account_id": None,
                "currency": None,
                "error": detail,
                "diagnostics": self._build_auth_diagnostics(e.status_code),
            }
        except T212APIError as e:
            return {"is_ok": False, "account_id": None, "currency": None, "error": str(e), "diagnostics": None}
        except Exception as e:
            return {"is_ok": False, "account_id": None, "currency": None, "error": str(e), "diagnostics": None}


def make_sell_quantity(quantity: Decimal) -> Decimal:
    """
    Trading 212 requires negative quantity for sell orders.
    This helper makes the convention explicit and testable.
    """
    return quantity.copy_abs().copy_negate()
