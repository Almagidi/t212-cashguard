"""
Integration tests: full API flows via HTTP client.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient


class FakeTrading212Adapter:
    def __init__(self, api_key: str, api_secret: str, environment: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.environment = environment

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def test_connection(self):
        return {
            "is_ok": True,
            "account_id": f"{self.environment.upper()}-ACCOUNT",
            "currency": "USD",
            "error": None,
        }

    async def get_account_summary(self):
        return {
            "free": 100000,
            "total": 100000,
            "invested": 0,
            "result": 0,
            "cash": 100000,
        }

    async def get_positions(self):
        return []

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        filled_quantity = abs(float(quantity))
        return {
            "id": f"{self.environment.upper()}-{ticker}-ORDER",
            "status": "FILLED",
            "filledQuantity": filled_quantity,
            "filledPrice": 101.25,
            "timeValidity": time_validity,
        }

    async def place_limit_order(self, ticker, quantity, limit_price, time_validity="DAY"):
        return await self.place_market_order(ticker, quantity, time_validity)

    async def place_stop_order(self, ticker, quantity, stop_price, time_validity="DAY"):
        return await self.place_market_order(ticker, quantity, time_validity)

    async def place_stop_limit_order(
        self,
        ticker,
        quantity,
        stop_price,
        limit_price,
        time_validity="DAY",
    ):
        return await self.place_market_order(ticker, quantity, time_validity)

    async def cancel_order(self, order_id):
        return {"id": order_id, "status": "CANCELLED"}

    async def get_order_by_id(self, order_id):
        return {
            "id": order_id,
            "status": "FILLED",
            "filledQuantity": 1,
            "filledPrice": 101.25,
        }


class FakeRejectingTrading212Adapter(FakeTrading212Adapter):
    async def test_connection(self):
        return {
            "is_ok": False,
            "account_id": None,
            "currency": None,
            "error": (
                "Trading 212 rejected the demo API credentials with HTTP 401. "
                "Confirm you generated a demo-account API key and secret for the same environment."
            ),
            "diagnostics": {
                "code": "broker_auth_rejected",
                "title": "Trading 212 rejected broker authentication",
                "summary": "The Trading 212 demo endpoint returned HTTP 401 for the submitted credentials.",
                "environment": "demo",
                "broker_host": "https://demo.trading212.com",
                "http_status": 401,
                "causes": [
                    {
                        "key": "wrong_environment",
                        "label": "Wrong environment selected",
                        "likelihood": "likely",
                        "detail": "Use demo credentials for the demo endpoint.",
                    },
                    {
                        "key": "invalid_credentials",
                        "label": "Invalid or revoked key/secret",
                        "likelihood": "likely",
                        "detail": "Regenerate the key if needed.",
                    },
                    {
                        "key": "ip_restriction",
                        "label": "IP restriction / allowlist mismatch",
                        "likelihood": "possible",
                        "detail": "Check the allowlist.",
                    },
                ],
                "note": "Trading 212 did not specify the exact reason.",
            },
        }


class FakeNestedCashBroker:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get_account_summary(self):
        return {
            "cash": {
                "availableToTrade": 5125.25,
                "blockedForPendingOrders": 74.75,
                "inPies": 100.0,
            },
            "invested": 1200.0,
            "result": 50.0,
            "currencyCode": "GBP",
        }


class FakeRateLimitedBroker:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get_positions(self):
        from app.broker.trading212 import T212RateLimitError

        raise T212RateLimitError(0.0)


class FakePortfolioBar:
    def __init__(self, timestamp: datetime, close: Decimal) -> None:
        self.timestamp = timestamp
        self.open = close - Decimal("1.0")
        self.high = close + Decimal("1.0")
        self.low = close - Decimal("1.5")
        self.close = close
        self.volume = Decimal("1000000")


class FakePortfolioQuote:
    def __init__(self, price: Decimal, timestamp: datetime) -> None:
        self.last = price
        self.timestamp = timestamp


class FakePortfolioProvider:
    async def get_bars(
        self, ticker: str, *, multiplier: int = 1, timespan: str = "day", limit: int = 50
    ):
        del multiplier, timespan
        base_map = {
            "SPY": Decimal("500"),
            "QQQ": Decimal("430"),
            "IWM": Decimal("205"),
        }
        slope_map = {
            "SPY": Decimal("0.5"),
            "QQQ": Decimal("0.8"),
            "IWM": Decimal("0.25"),
        }
        now = datetime(2026, 4, 10, tzinfo=UTC)
        start = now - timedelta(days=240)
        all_bars = []
        for idx in range(240):
            close = base_map.get(ticker, Decimal("100")) + slope_map.get(
                ticker, Decimal("0.2")
            ) * Decimal(str(idx))
            all_bars.append(FakePortfolioBar(start + timedelta(days=idx), close))
        return all_bars[-limit:]

    async def get_quote(self, ticker: str):
        bars = await self.get_bars(ticker, limit=240)
        latest = bars[-1]
        return FakePortfolioQuote(latest.close, latest.timestamp)

    async def is_market_open(self):
        return True


class FakeRegimeProvider(FakePortfolioProvider):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get_bars(
        self, ticker: str, *, multiplier: int = 1, timespan: str = "day", limit: int = 50
    ):
        del ticker
        now = datetime(2026, 4, 10, tzinfo=UTC)
        bars = []
        if timespan == "day":
            for idx in range(limit):
                close = Decimal("500") + Decimal(str(idx))
                bars.append(FakePortfolioBar(now - timedelta(days=limit - idx), close))
            return bars
        for idx in range(limit):
            close = Decimal("560") + Decimal(str(idx)) * Decimal("0.2")
            bars.append(FakePortfolioBar(now - timedelta(minutes=5 * (limit - idx)), close))
        return bars


@pytest.mark.asyncio
class TestAuthFlow:
    async def test_login_success(self, client: AsyncClient, admin_token: str):
        assert admin_token is not None
        assert len(admin_token) > 20

    async def test_login_wrong_password(self, client: AsyncClient, admin_token: str):
        resp = await client.post(
            "/v1/auth/login",
            json={
                "email": "admin@test.com",
                "password": "wrongpassword",
            },
        )
        assert resp.status_code == 401

    async def test_login_accepts_localhost_admin_identifier(self, client: AsyncClient, db):
        from app.core.security import hash_password
        from app.db.models import User

        db.add(
            User(
                id=uuid.uuid4(),
                email="admin@localhost",
                hashed_password=hash_password("change-me"),
                is_active=True,
                is_admin=True,
            )
        )
        await db.commit()

        resp = await client.post(
            "/v1/auth/login",
            json={
                "email": "admin@localhost",
                "password": "change-me",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "admin@localhost"

    async def test_login_rate_limit_sets_retry_after_header(self, client: AsyncClient, db):
        from app.core.security import hash_password
        from app.db.models import User

        db.add(
            User(
                id=uuid.uuid4(),
                email="admin@localhost",
                hashed_password=hash_password("change-me"),
                is_active=True,
                is_admin=True,
            )
        )
        await db.commit()

        for _ in range(5):
            resp = await client.post(
                "/v1/auth/login",
                json={
                    "email": "admin@localhost",
                    "password": "wrong-password",
                },
            )
            assert resp.status_code == 401

        resp = await client.post(
            "/v1/auth/login",
            json={
                "email": "admin@localhost",
                "password": "wrong-password",
            },
        )
        assert resp.status_code == 429
        assert resp.headers["Retry-After"].isdigit()

    async def test_me_endpoint(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "admin@test.com"
        assert data["is_admin"] is True

    async def test_protected_route_requires_auth(self, client: AsyncClient):
        resp = await client.get("/v1/account/summary")
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestAccountFlow:
    async def test_account_summary_mock_mode(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/account/summary", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_value" in data
        assert "cash" in data
        assert "free_funds" in data
        assert data["mode"] == "mock"

    async def test_account_summary_accepts_trading212_nested_cash(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        from app.api.deps import get_broker
        from app.main import app

        app.dependency_overrides[get_broker] = lambda: FakeNestedCashBroker()
        try:
            resp = await client.get("/v1/account/summary", headers=auth_headers)
        finally:
            app.dependency_overrides.pop(get_broker, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["cash"] == 5300.0
        assert data["free_funds"] == 5125.25
        assert data["currency"] == "GBP"

    async def test_cash_guard_status(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/account/cash-guard", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["cash_only_mode"] is True  # Always true
        assert "available_to_trade" in data

    async def test_cash_guard_cash_only_mode_is_always_true(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Critical safety test: cash_only_mode must always be True."""
        resp = await client.get("/v1/account/cash-guard", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["cash_only_mode"] is True


@pytest.mark.asyncio
class TestHealthFlow:
    async def test_health_live(self, client: AsyncClient):
        resp = await client.get("/v1/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_health_ready(self, client: AsyncClient):
        resp = await client.get("/v1/health/ready")
        assert resp.status_code == 200

    async def test_health_startup(self, client: AsyncClient):
        resp = await client.get("/v1/health/startup")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pass"
        assert any(check["key"] == "market_data_provider" for check in data["checks"])

    async def test_health_workers_default_unknown(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/health/workers", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "unknown"
        assert any(task["task_name"] == "run_strategy_signals" for task in data["tasks"])

    async def test_health_deps_includes_startup_and_workers(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/v1/health/deps", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["startup"] == "pass"
        assert data["workers"] in {"ok", "stale", "unknown"}

    async def test_health_market_data_endpoint(self, client: AsyncClient):
        from app.services.feed_health import record_feed_health, reset_feed_health

        reset_feed_health()
        record_feed_health(
            provider="alpaca_primary_polygon_validator",
            ticker="AAPL",
            status="ok",
            detail="Alpaca quote validated.",
            used_source="alpaca",
            validator_source="polygon",
        )

        resp = await client.get("/v1/health/market-data")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "alpaca_primary_polygon_validator"
        assert data["symbols"][0]["ticker"] == "AAPL"
        assert data["symbols"][0]["used_source"] == "alpaca"

    async def test_health_workers_reports_recent_heartbeat(
        self, client: AsyncClient, auth_headers: dict, db
    ):
        from app.db.models import AppSettings

        settings_row = await db.get(AppSettings, 1)
        settings_row.extra = {
            "worker_heartbeats": {
                "run_strategy_signals": {"last_seen_at": datetime.now(UTC).isoformat()},
                "run_position_monitor": {"last_seen_at": datetime.now(UTC).isoformat()},
                "reconcile_pending_orders": {"last_seen_at": datetime.now(UTC).isoformat()},
                "sync_account_snapshot": {"last_seen_at": datetime.now(UTC).isoformat()},
                "check_eod_flatten": {"last_seen_at": datetime.now(UTC).isoformat()},
            }
        }
        await db.commit()

        resp = await client.get("/v1/health/workers", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert all(task["status"] == "ok" for task in data["tasks"])

    async def test_root(self, client: AsyncClient):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "T212 CashGuard" in resp.json()["name"]

    async def test_health_deps_reports_reconnect_required_for_unreadable_broker_credentials(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
        monkeypatch,
    ):
        from sqlalchemy import select

        from app.core.config import settings
        from app.db.models import BrokerConnection, User

        monkeypatch.setattr(settings, "APP_MODE", "demo")
        user = (await db.execute(select(User).where(User.email == "admin@test.com"))).scalar_one()
        conn = BrokerConnection(
            id=uuid.uuid4(),
            user_id=user.id,
            broker="trading212",
            environment="demo",
            api_key_encrypted="invalid-token",
            api_secret_encrypted="invalid-token",
            is_active=True,
            last_test_ok=True,
        )
        db.add(conn)
        await db.commit()

        resp = await client.get("/v1/health/deps", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["broker"] == "reconnect_required"

        await db.refresh(conn)
        assert conn.is_active is False
        assert conn.last_test_ok is False


@pytest.mark.asyncio
class TestIntelligenceFlow:
    async def test_regime_endpoint_returns_live_classification(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        from app.services import market_regime as regime_module

        monkeypatch.setattr(regime_module, "get_live_provider", lambda: FakeRegimeProvider())

        resp = await client.get("/v1/regime", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["regime"] == "trending_up"
        assert "orb" in data["active_strategies"]

    async def test_watchlist_intelligence_endpoint_returns_news(
        self, client: AsyncClient, auth_headers: dict, db, monkeypatch
    ):
        from app.db.models import Strategy
        from app.services import news_intelligence as news_module

        db.add(
            Strategy(
                id=uuid.uuid4(),
                name="Watchlist ORB",
                type="orb",
                is_enabled=True,
                is_live=False,
                params={"todays_watchlist": ["AAPL", "MSFT"]},
                allowed_tickers=["AAPL", "MSFT"],
                session_start="09:30",
                session_end="16:00",
            )
        )
        await db.commit()

        async def fake_watchlist_news(self, tickers, *, limit=8):
            return [
                {
                    "id": "n1",
                    "source": "benzinga",
                    "title": "Apple beats earnings",
                    "summary": "Positive catalyst.",
                    "url": None,
                    "published_at": datetime.now(UTC).isoformat(),
                    "tickers": ["AAPL"],
                    "event_type": "earnings",
                    "sentiment_score": 0.6,
                    "urgency_score": 0.9,
                    "credibility_score": 0.85,
                    "impact_horizon": "multi_day",
                    "catalyst_score": 0.78,
                }
            ][:limit]

        monkeypatch.setattr(
            news_module.NewsIntelligenceService, "get_watchlist_intelligence", fake_watchlist_news
        )

        resp = await client.get("/v1/intelligence/watchlist", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["watchlist"][:2] == ["AAPL", "MSFT"]
        assert data["news"][0]["event_type"] == "earnings"

    async def test_strategy_intelligence_endpoint_includes_block_reasons(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
        monkeypatch,
    ):
        from app.db.models import Strategy
        from app.services import market_regime as regime_module
        from app.services.feed_health import record_feed_health, reset_feed_health

        strategy = Strategy(
            id=uuid.uuid4(),
            name="Fade Intel",
            type="opening_fade",
            is_enabled=True,
            is_live=False,
            params={
                "todays_watchlist": ["TSLA"],
                "watchlist_candidates": {
                    "TSLA": {
                        "score": 81.5,
                        "reason": "RVOL and gap setup",
                        "pre_market_rvol": 2.4,
                        "gap_pct": 3.2,
                        "catalyst_score": 0.88,
                        "catalyst_event_type": "earnings",
                        "catalyst_summary": "Tesla beats earnings.",
                        "catalyst_source": "benzinga",
                    }
                },
            },
            allowed_tickers=["TSLA"],
            session_start="09:30",
            session_end="16:00",
        )
        db.add(strategy)
        await db.commit()

        reset_feed_health()
        record_feed_health(
            provider="alpaca_primary_polygon_validator",
            ticker="TSLA",
            status="degraded",
            detail="Quote divergence exceeded tolerance.",
            used_source="alpaca",
            validator_source="polygon",
            divergence_pct=2.3,
        )
        monkeypatch.setattr(regime_module, "get_live_provider", lambda: FakeRegimeProvider())

        resp = await client.get(f"/v1/strategies/{strategy.id}/intelligence", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["strategy_id"] == str(strategy.id)
        assert data["regime"]["regime"] == "trending_up"
        assert data["feed_health"]["status"] == "degraded"
        assert data["watchlist"][0]["ticker"] == "TSLA"
        assert data["watchlist"][0]["feed_status"] == "degraded"
        assert data["watchlist"][0]["blocked_reason"] is not None


@pytest.mark.asyncio
class TestInstrumentsFlow:
    async def test_list_instruments_empty_initially(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/instruments", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data

    async def test_sync_instruments(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/v1/instruments/sync", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] > 0

    async def test_list_instruments_after_sync(self, client: AsyncClient, auth_headers: dict):
        await client.post("/v1/instruments/sync", headers=auth_headers)
        resp = await client.get("/v1/instruments", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] > 0

    async def test_get_instrument_by_ticker(self, client: AsyncClient, auth_headers: dict):
        await client.post("/v1/instruments/sync", headers=auth_headers)
        resp = await client.get("/v1/instruments/AAPL", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "AAPL"

    async def test_get_instrument_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/instruments/NOTEXIST", headers=auth_headers)
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestStrategiesFlow:
    async def test_list_strategies(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/strategies", headers=auth_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_list_strategy_presets(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/strategies/presets", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 5
        assert {item["key"] for item in data} == {
            "orb",
            "opening_fade",
            "vwap_reclaim",
            "closing_momentum",
            "intraday_periodicity",
        }
        assert all(item["risk_template_name"] for item in data)

    async def test_create_strategy(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Test ORB",
                "type": "orb",
                "description": "Test strategy",
                "params": {"orb_minutes": 15},
                "allowed_tickers": ["AAPL", "MSFT"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test ORB"
        assert data["is_enabled"] is False  # Disabled by default
        assert data["is_live"] is False  # Not live by default
        return data["id"]

    async def test_create_strategy_from_preset(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/v1/strategies/presets/closing_momentum", headers=auth_headers, json={}
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["type"] == "closing_momentum"
        assert data["risk_profile_id"] is not None
        assert data["risk_profile"] is not None
        assert data["risk_profile"]["name"].startswith("Demo ")
        assert data["is_enabled"] is False
        assert data["is_live"] is False
        assert data["params"]["risk_per_trade_pct"] == 0.35
        assert data["params"]["preset_metadata"]["preset_key"] == "closing_momentum"
        assert (
            data["params"]["preset_metadata"]["risk_template_name"] == data["risk_profile"]["name"]
        )
        assert data["params"]["execution_metadata"]["created_from_preset_by"] == "admin@test.com"
        assert data["params"]["execution_metadata"]["created_from_preset_at"]

    async def test_get_strategy_detail_returns_attached_risk_profile(
        self, client: AsyncClient, auth_headers: dict
    ):
        create_resp = await client.post("/v1/strategies/presets/orb", headers=auth_headers, json={})
        assert create_resp.status_code == 201
        strategy_id = create_resp.json()["id"]

        resp = await client.get(f"/v1/strategies/{strategy_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["risk_profile_id"] is not None
        assert data["risk_profile"] is not None
        assert data["risk_profile"]["id"] == data["risk_profile_id"]
        assert data["params"]["preset_metadata"]["preset_label"] == "Opening Range Breakout"

    async def test_create_strategy_is_disabled_by_default(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Another ORB",
                "type": "orb",
                "params": {},
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["is_enabled"] is False
        assert data["is_live"] is False

    async def test_create_portfolio_strategy(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Portfolio Core",
                "type": "buy_hold_core",
                "description": "Long-horizon rebalance sleeve",
                "params": {"capital_fraction": 0.4},
                "allowed_tickers": ["SPY", "QQQ", "IWM"],
                "eod_flatten": False,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["type"] == "buy_hold_core"
        assert data["is_live"] is False

    async def test_create_intraday_strategy_variant(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Late Momentum",
                "type": "closing_momentum",
                "description": "Late-session continuation strategy",
                "params": {"min_opening_return_pct": 0.4},
                "allowed_tickers": ["AAPL", "MSFT"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["type"] == "closing_momentum"
        assert data["params"]["min_opening_return_pct"] == 0.4

    async def test_update_strategy_execution_mode(self, client: AsyncClient, auth_headers: dict):
        create_resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Portfolio Toggle",
                "type": "buy_hold_core",
                "allowed_tickers": ["SPY", "QQQ", "IWM"],
                "eod_flatten": False,
            },
        )
        strategy_id = create_resp.json()["id"]

        update_resp = await client.patch(
            f"/v1/strategies/{strategy_id}",
            headers=auth_headers,
            json={"is_live": True, "params": {"capital_fraction": 0.35}},
        )
        assert update_resp.status_code == 400
        assert "promotion flow" in update_resp.json()["detail"].lower()

        safe_update_resp = await client.patch(
            f"/v1/strategies/{strategy_id}",
            headers=auth_headers,
            json={"params": {"capital_fraction": 0.35}},
        )
        assert safe_update_resp.status_code == 200
        data = safe_update_resp.json()
        assert data["is_live"] is False
        assert data["params"]["capital_fraction"] == 0.35

    async def test_get_strategy_promotion_status_defaults_to_dry_run(
        self, client: AsyncClient, auth_headers: dict
    ):
        create_resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Promotion Status",
                "type": "orb",
                "allowed_tickers": ["AAPL"],
                "params": {"orb_minutes": 15},
            },
        )
        strategy_id = create_resp.json()["id"]

        status_resp = await client.get(
            f"/v1/strategies/{strategy_id}/promotion-status", headers=auth_headers
        )
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["current_stage"] == "dry_run"
        assert data["broker_execution_enabled"] is False
        assert data["eligible_for_demo"] is False
        assert any(check["phase"] == "demo" for check in data["checks"])

    async def test_strategy_can_promote_from_dry_run_to_demo(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
    ):
        from app.db.models import Signal, Strategy

        create_resp = await client.post("/v1/strategies/presets/orb", headers=auth_headers, json={})
        assert create_resp.status_code == 201
        strategy_id = uuid.UUID(create_resp.json()["id"])

        strategy = await db.get(Strategy, strategy_id)
        now = datetime.now(UTC)
        db.add_all(
            [
                Signal(
                    id=uuid.uuid4(),
                    strategy_id=strategy_id,
                    ticker="AAPL",
                    side="buy",
                    signal_type="entry",
                    status="approved",
                    entry_price=Decimal("100"),
                    suggested_quantity=Decimal("1"),
                    confidence=Decimal("0.55"),
                    reason="dry-run sample",
                    generated_at=now - timedelta(minutes=15),
                ),
                Signal(
                    id=uuid.uuid4(),
                    strategy_id=strategy_id,
                    ticker="AAPL",
                    side="buy",
                    signal_type="entry",
                    status="approved",
                    entry_price=Decimal("101"),
                    suggested_quantity=Decimal("1"),
                    confidence=Decimal("0.58"),
                    reason="dry-run sample",
                    generated_at=now - timedelta(minutes=10),
                ),
                Signal(
                    id=uuid.uuid4(),
                    strategy_id=strategy_id,
                    ticker="AAPL",
                    side="buy",
                    signal_type="entry",
                    status="approved",
                    entry_price=Decimal("102"),
                    suggested_quantity=Decimal("1"),
                    confidence=Decimal("0.61"),
                    reason="dry-run sample",
                    generated_at=now - timedelta(minutes=5),
                ),
            ]
        )
        await db.commit()

        review_resp = await client.post(
            f"/v1/strategies/{strategy_id}/promotion",
            headers=auth_headers,
            json={"action": "record_dry_run_review"},
        )
        assert review_resp.status_code == 200, review_resp.text

        promote_resp = await client.post(
            f"/v1/strategies/{strategy_id}/promotion",
            headers=auth_headers,
            json={"action": "promote_to_demo"},
        )
        assert promote_resp.status_code == 200, promote_resp.text
        data = promote_resp.json()
        assert data["current_stage"] == "demo"
        assert data["demo_execution_enabled"] is True

        await db.refresh(strategy)
        assert strategy.is_live is True
        assert strategy.params["promotion"]["demo_promoted_at"]

    async def test_strategy_can_promote_from_demo_to_live_after_soak(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
        monkeypatch,
    ):
        from app.core.config import settings
        from app.db.models import Order, Signal, Strategy

        async def fake_live_readiness(self):
            return {
                "mode": "live",
                "live_execution_enabled": True,
                "live_trading_unlocked": False,
                "eligible_for_unlock": True,
                "ready_for_live": False,
                "blockers": [],
                "checks": [],
            }

        monkeypatch.setattr(settings, "STRATEGY_PROMOTION_MIN_DEMO_DAYS", 1)
        monkeypatch.setattr(
            "app.services.strategy_promotion.LiveReadinessService.evaluate", fake_live_readiness
        )

        create_resp = await client.post("/v1/strategies/presets/orb", headers=auth_headers, json={})
        assert create_resp.status_code == 201
        strategy_id = uuid.UUID(create_resp.json()["id"])

        strategy = await db.get(Strategy, strategy_id)
        now = datetime.now(UTC)
        dry_signals = []
        for minutes_ago in (30, 20, 10):
            dry_signals.append(
                Signal(
                    id=uuid.uuid4(),
                    strategy_id=strategy_id,
                    ticker="AAPL",
                    side="buy",
                    signal_type="entry",
                    status="approved",
                    entry_price=Decimal("100"),
                    suggested_quantity=Decimal("1"),
                    confidence=Decimal("0.60"),
                    reason="dry-run sample",
                    generated_at=now - timedelta(minutes=minutes_ago),
                )
            )
        db.add_all(dry_signals)
        await db.commit()

        review_resp = await client.post(
            f"/v1/strategies/{strategy_id}/promotion",
            headers=auth_headers,
            json={"action": "record_dry_run_review"},
        )
        assert review_resp.status_code == 200, review_resp.text

        demo_resp = await client.post(
            f"/v1/strategies/{strategy_id}/promotion",
            headers=auth_headers,
            json={"action": "promote_to_demo"},
        )
        assert demo_resp.status_code == 200, demo_resp.text

        strategy = await db.get(Strategy, strategy_id)
        demo_promoted_at = datetime.fromisoformat(strategy.params["promotion"]["demo_promoted_at"])
        if demo_promoted_at.tzinfo is None:
            demo_promoted_at = demo_promoted_at.replace(tzinfo=UTC)
        backdated_demo_start = now - timedelta(minutes=40)
        strategy.params = {
            **strategy.params,
            "promotion": {
                **strategy.params["promotion"],
                "demo_promoted_at": backdated_demo_start.isoformat(),
            },
        }
        await db.commit()

        demo_signals: list[Signal] = []
        demo_orders: list[Order] = []
        for index, minutes_ago in enumerate((30, 20, 10)):
            generated_at = now - timedelta(minutes=minutes_ago)
            signal = Signal(
                id=uuid.uuid4(),
                strategy_id=strategy_id,
                ticker="AAPL",
                side="buy",
                signal_type="entry",
                status="executed",
                entry_price=Decimal("103"),
                suggested_quantity=Decimal("1"),
                confidence=Decimal("0.67"),
                reason="demo sample",
                generated_at=generated_at,
            )
            demo_signals.append(signal)
            demo_orders.append(
                Order(
                    id=uuid.uuid4(),
                    signal_id=signal.id,
                    client_order_key=f"demo-{index}-{uuid.uuid4()}",
                    ticker="AAPL",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    status="filled",
                    broker_order_id=f"DEMO-{index}",
                    filled_quantity=Decimal("1"),
                    avg_fill_price=Decimal("103.25"),
                    is_dry_run=False,
                    created_at=generated_at,
                    updated_at=generated_at,
                )
            )
        db.add_all(demo_signals + demo_orders)
        await db.commit()

        demo_review_resp = await client.post(
            f"/v1/strategies/{strategy_id}/promotion",
            headers=auth_headers,
            json={"action": "record_demo_review"},
        )
        assert demo_review_resp.status_code == 200, demo_review_resp.text

        live_resp = await client.post(
            f"/v1/strategies/{strategy_id}/promotion",
            headers=auth_headers,
            json={"action": "promote_to_live"},
        )
        assert live_resp.status_code == 200, live_resp.text
        data = live_resp.json()
        assert data["current_stage"] == "live_approved"
        assert data["live_execution_approved"] is True
        assert data["eligible_for_live"] is True

    async def test_run_portfolio_strategy_dry(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        from app.services import portfolio_execution_service as portfolio_service_module

        monkeypatch.setattr(
            portfolio_service_module, "get_live_provider", lambda: FakePortfolioProvider()
        )

        create_resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Portfolio Dry Run",
                "type": "buy_hold_core",
                "allowed_tickers": ["SPY", "QQQ", "IWM"],
                "params": {
                    "capital_fraction": 0.24,
                    "min_trade_value": 25,
                    "min_weight_delta_pct": 0.5,
                },
                "eod_flatten": False,
            },
        )
        strategy_id = create_resp.json()["id"]

        enable_resp = await client.post(
            f"/v1/strategies/{strategy_id}/enable", headers=auth_headers
        )
        assert enable_resp.status_code == 200

        dry_resp = await client.post(f"/v1/strategies/{strategy_id}/run-dry", headers=auth_headers)
        assert dry_resp.status_code == 200
        data = dry_resp.json()
        assert data["is_live"] is False
        assert data["summary"]["dry_run_orders"] > 0

    async def test_run_strategy_dry_records_execution_metadata(
        self, client: AsyncClient, auth_headers: dict
    ):
        create_resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Dry Metadata",
                "type": "orb",
                "allowed_tickers": ["AAPL"],
                "params": {"orb_minutes": 15},
            },
        )
        assert create_resp.status_code == 201
        strategy_id = create_resp.json()["id"]

        dry_resp = await client.post(f"/v1/strategies/{strategy_id}/run-dry", headers=auth_headers)
        assert dry_resp.status_code == 200

        detail_resp = await client.get(f"/v1/strategies/{strategy_id}", headers=auth_headers)
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert (
            detail["params"]["execution_metadata"]["last_dry_run_requested_by"] == "admin@test.com"
        )
        assert detail["params"]["execution_metadata"]["last_dry_run_requested_at"]

    async def test_list_portfolio_monitoring(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        from app.services import portfolio_execution_service as portfolio_service_module

        monkeypatch.setattr(
            portfolio_service_module, "get_live_provider", lambda: FakePortfolioProvider()
        )

        create_resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Portfolio Monitor",
                "type": "buy_hold_core",
                "allowed_tickers": ["SPY", "QQQ", "IWM"],
                "params": {
                    "capital_fraction": 0.24,
                    "min_trade_value": 25,
                    "min_weight_delta_pct": 0.5,
                },
                "eod_flatten": False,
            },
        )
        strategy_id = create_resp.json()["id"]
        await client.post(f"/v1/strategies/{strategy_id}/enable", headers=auth_headers)
        await client.post(f"/v1/strategies/{strategy_id}/run-dry", headers=auth_headers)

        resp = await client.get("/v1/strategies/portfolio-monitoring", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["strategy_id"] == strategy_id
        assert data[0]["last_status"] == "rebalanced"
        assert len(data[0]["weights"]) > 0
        assert len(data[0]["recent_orders"]) > 0

    async def test_get_portfolio_strategy_monitoring_detail(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        from app.services import portfolio_execution_service as portfolio_service_module

        monkeypatch.setattr(
            portfolio_service_module, "get_live_provider", lambda: FakePortfolioProvider()
        )

        create_resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Portfolio Detail",
                "type": "buy_hold_core",
                "allowed_tickers": ["SPY", "QQQ", "IWM"],
                "params": {
                    "capital_fraction": 0.24,
                    "min_trade_value": 25,
                    "min_weight_delta_pct": 0.5,
                },
                "eod_flatten": False,
            },
        )
        strategy_id = create_resp.json()["id"]
        await client.post(f"/v1/strategies/{strategy_id}/enable", headers=auth_headers)
        await client.post(f"/v1/strategies/{strategy_id}/run-dry", headers=auth_headers)

        resp = await client.get(
            f"/v1/strategies/{strategy_id}/portfolio-monitoring", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["strategy_id"] == strategy_id
        assert data["last_dry_run_orders"] > 0
        assert any(weight["ticker"] == "SPY" for weight in data["weights"])
        assert data["recent_orders"][0]["is_dry_run"] is True

    async def test_get_portfolio_strategy_attribution_detail(
        self, client: AsyncClient, auth_headers: dict, db, monkeypatch
    ):
        from app.db.models import Order, Signal
        from app.services import portfolio_attribution_service as attribution_service_module

        monkeypatch.setattr(
            attribution_service_module, "get_live_provider", lambda: FakePortfolioProvider()
        )

        create_resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Portfolio Attribution",
                "type": "buy_hold_core",
                "allowed_tickers": ["SPY", "QQQ", "IWM"],
                "params": {"capital_fraction": 0.24},
                "eod_flatten": False,
            },
        )
        strategy_id = create_resp.json()["id"]

        base_day = datetime(2026, 4, 7, 15, 0, tzinfo=UTC)
        signal_buy = Signal(
            id=uuid.uuid4(),
            strategy_id=uuid.UUID(strategy_id),
            ticker="SPY",
            side="buy",
            signal_type="portfolio_rebalance",
            status="approved",
            entry_price=Decimal("500"),
            suggested_quantity=Decimal("2"),
            generated_at=base_day,
        )
        signal_sell = Signal(
            id=uuid.uuid4(),
            strategy_id=uuid.UUID(strategy_id),
            ticker="SPY",
            side="sell",
            signal_type="portfolio_rebalance",
            status="approved",
            entry_price=Decimal("510"),
            suggested_quantity=Decimal("1"),
            generated_at=base_day + timedelta(days=1),
        )
        db.add_all([signal_buy, signal_sell])
        db.add_all(
            [
                Order(
                    id=uuid.uuid4(),
                    signal_id=signal_buy.id,
                    client_order_key="api-portfolio-buy",
                    ticker="SPY",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("2"),
                    filled_quantity=Decimal("2"),
                    avg_fill_price=Decimal("500"),
                    status="filled",
                    is_dry_run=True,
                    created_at=base_day,
                    updated_at=base_day,
                ),
                Order(
                    id=uuid.uuid4(),
                    signal_id=signal_sell.id,
                    client_order_key="api-portfolio-sell",
                    ticker="SPY",
                    side="sell",
                    order_type="market",
                    quantity=Decimal("1"),
                    filled_quantity=Decimal("1"),
                    avg_fill_price=Decimal("510"),
                    status="filled",
                    is_dry_run=True,
                    created_at=base_day + timedelta(days=1),
                    updated_at=base_day + timedelta(days=1),
                ),
            ]
        )
        await db.commit()

        resp = await client.get(
            f"/v1/strategies/{strategy_id}/portfolio-attribution", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["strategy_id"] == strategy_id
        assert data["order_count"] == 2
        assert data["rebalance_days"] == 2
        assert data["benchmark_name"]
        assert "total_return_pct" in data
        assert "max_drawdown_pct" in data
        assert len(data["timeline"]) >= 2
        assert len(data["rebalance_events"]) == 2
        assert "before_weight" in data["rebalance_events"][0]["weights"][0]
        assert data["ticker_attribution"][0]["ticker"] == "SPY"

    async def test_list_portfolio_attribution(
        self, client: AsyncClient, auth_headers: dict, db, monkeypatch
    ):
        from app.db.models import Order, Signal
        from app.services import portfolio_attribution_service as attribution_service_module

        monkeypatch.setattr(
            attribution_service_module, "get_live_provider", lambda: FakePortfolioProvider()
        )

        create_resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Portfolio Attribution List",
                "type": "buy_hold_core",
                "allowed_tickers": ["SPY", "QQQ"],
                "params": {"capital_fraction": 0.24},
                "eod_flatten": False,
            },
        )
        strategy_id = create_resp.json()["id"]

        fill_time = datetime(2026, 4, 8, 15, 0, tzinfo=UTC)
        signal = Signal(
            id=uuid.uuid4(),
            strategy_id=uuid.UUID(strategy_id),
            ticker="QQQ",
            side="buy",
            signal_type="portfolio_rebalance",
            status="approved",
            entry_price=Decimal("430"),
            suggested_quantity=Decimal("1"),
            generated_at=fill_time,
        )
        order = Order(
            id=uuid.uuid4(),
            signal_id=signal.id,
            client_order_key="api-portfolio-list-buy",
            ticker="QQQ",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
            filled_quantity=Decimal("1"),
            avg_fill_price=Decimal("430"),
            status="filled",
            is_dry_run=True,
            created_at=fill_time,
            updated_at=fill_time,
        )
        db.add_all([signal, order])
        await db.commit()

        resp = await client.get("/v1/strategies/portfolio-attribution", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["strategy_id"] == strategy_id
        assert "benchmark_return_pct" in data[0]
        assert "alpha_vs_benchmark_pct" in data[0]
        assert len(data[0]["recent_timeline"]) >= 1

    async def test_enable_disable_strategy(self, client: AsyncClient, auth_headers: dict):
        create_resp = await client.post(
            "/v1/strategies",
            headers=auth_headers,
            json={
                "name": "Enable Test ORB",
                "type": "orb",
            },
        )
        sid = create_resp.json()["id"]

        enable_resp = await client.post(f"/v1/strategies/{sid}/enable", headers=auth_headers)
        assert enable_resp.status_code == 200
        assert enable_resp.json()["enabled"] is True

        disable_resp = await client.post(f"/v1/strategies/{sid}/disable", headers=auth_headers)
        assert disable_resp.status_code == 200
        assert disable_resp.json()["enabled"] is False


@pytest.mark.asyncio
class TestBacktestFlow:
    async def test_list_backtest_strategies(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/backtest/strategies", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert {item["type"] for item in data} >= {
            "orb",
            "opening_fade",
            "vwap_reclaim",
            "closing_momentum",
            "intraday_periodicity",
        }

    async def test_list_portfolio_backtest_strategies(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/v1/backtest/portfolio/strategies", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert {item["type"] for item in data} >= {
            "buy_hold_core",
            "equal_weight_rebalance",
            "cross_sectional_momentum",
            "low_volatility_tilt",
            "trend_following_tactical",
        }


@pytest.mark.asyncio
class TestRiskFlow:
    async def test_get_risk_profile(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/risk/profile", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_default"] is True
        assert float(data["max_daily_loss_pct"]) <= 20.0

    async def test_update_risk_profile(self, client: AsyncClient, auth_headers: dict):
        resp = await client.patch(
            "/v1/risk/profile",
            headers=auth_headers,
            json={
                "max_daily_loss_pct": "2.5",
                "max_open_positions": 3,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert float(data["max_daily_loss_pct"]) == 2.5
        assert data["max_open_positions"] == 3

    async def test_kill_switch_enable_disable(self, client: AsyncClient, auth_headers: dict):
        # Enable
        resp = await client.post("/v1/risk/kill-switch/enable", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["kill_switch_active"] is True

        # Disable
        resp = await client.post("/v1/risk/kill-switch/disable", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["kill_switch_active"] is False


@pytest.mark.asyncio
class TestOrderFlow:
    async def test_list_orders_empty(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/orders", headers=auth_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_place_order_mock_mode(self, client: AsyncClient, auth_headers: dict):
        """In mock mode, orders should be treated as dry-run and fill immediately."""
        # Make sure kill switch is off and auto-trading enabled
        await client.post("/v1/risk/kill-switch/disable", headers=auth_headers)
        await client.post("/v1/emergency/auto-trading/on", headers=auth_headers)

        resp = await client.post(
            "/v1/orders",
            headers=auth_headers,
            json={
                "ticker": "AAPL",
                "side": "buy",
                "order_type": "market",
                "quantity": "1",
            },
        )
        # May return 201 (placed) or 422 (risk violation)
        assert resp.status_code in (201, 422)

    async def test_kill_switch_blocks_manual_order_before_account_summary(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
        monkeypatch,
    ):
        """Kill-switch-active manual orders must not perform broker reads for placement."""
        from sqlalchemy import select

        from app.db.models import AuditLog

        class CountingBroker(FakeTrading212Adapter):
            account_summary_calls = 0

            async def get_account_summary(self):
                type(self).account_summary_calls += 1
                return await super().get_account_summary()

        async def fake_get_broker(**_kwargs):
            return CountingBroker("key", "secret", "demo")

        monkeypatch.setattr("app.api.v1.routes.orders.get_broker", fake_get_broker)

        await client.post("/v1/risk/kill-switch/enable", headers=auth_headers)

        resp = await client.post(
            "/v1/orders",
            headers=auth_headers,
            json={
                "ticker": "AAPL",
                "side": "buy",
                "order_type": "market",
                "quantity": "1",
            },
        )

        assert resp.status_code == 403
        assert "kill switch" in resp.json()["detail"].lower()
        assert CountingBroker.account_summary_calls == 0

        audits = (
            (
                await db.execute(
                    select(AuditLog).where(AuditLog.action == "order_blocked_by_kill_switch")
                )
            )
            .scalars()
            .all()
        )
        assert audits
        assert audits[-1].payload["source"] == "manual_order_route"
        assert audits[-1].payload["no_broker_order_sent"] is True

    async def test_order_validation_missing_limit_price(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/v1/orders",
            headers=auth_headers,
            json={
                "ticker": "AAPL",
                "side": "buy",
                "order_type": "limit",
                "quantity": "1",
                # Missing limit_price
            },
        )
        assert resp.status_code == 422

    async def test_order_quantity_must_be_positive(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/v1/orders",
            headers=auth_headers,
            json={
                "ticker": "AAPL",
                "side": "buy",
                "order_type": "market",
                "quantity": "-1",  # Must be positive from API (engine handles sign)
            },
        )
        assert resp.status_code == 422

    async def test_list_signals_includes_strategy_context(
        self, client: AsyncClient, auth_headers: dict, db
    ):
        from app.db.models import Signal, Strategy

        strategy = Strategy(
            id=uuid.uuid4(),
            name="Signal Context ORB",
            type="orb",
            params={},
            allowed_tickers=["AAPL"],
        )
        signal = Signal(
            id=uuid.uuid4(),
            strategy_id=strategy.id,
            ticker="AAPL",
            side="buy",
            signal_type="entry",
            status="rejected",
            confidence=Decimal("0.72"),
            reason="Opening range held with catalyst support.",
            risk_rejected=True,
            risk_rejection_reason="Feed health degraded for AAPL.",
            generated_at=datetime.now(UTC),
        )
        db.add_all([strategy, signal])
        await db.commit()

        resp = await client.get("/v1/signals", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        matching = next(item for item in data if item["id"] == str(signal.id))
        assert matching["strategy_name"] == "Signal Context ORB"
        assert matching["strategy_type_name"] == "orb"
        assert matching["risk_rejection_reason"] == "Feed health degraded for AAPL."

    async def test_list_orders_includes_signal_explainability(
        self, client: AsyncClient, auth_headers: dict, db
    ):
        from app.db.models import Order, Signal, Strategy

        strategy = Strategy(
            id=uuid.uuid4(),
            name="Order Context ORB",
            type="orb",
            params={},
            allowed_tickers=["MSFT"],
        )
        signal = Signal(
            id=uuid.uuid4(),
            strategy_id=strategy.id,
            ticker="MSFT",
            side="buy",
            signal_type="entry",
            status="approved",
            confidence=Decimal("0.81"),
            reason="Breakout confirmed above VWAP with elevated RVOL.",
            risk_rejected=False,
            generated_at=datetime.now(UTC),
        )
        order = Order(
            id=uuid.uuid4(),
            signal_id=signal.id,
            client_order_key="api-order-context",
            ticker="MSFT",
            side="buy",
            order_type="market",
            quantity=Decimal("2"),
            status="accepted",
            is_dry_run=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add_all([strategy, signal, order])
        await db.commit()

        resp = await client.get("/v1/orders", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        matching = next(item for item in data if item["id"] == str(order.id))
        assert matching["strategy_name"] == "Order Context ORB"
        assert matching["strategy_type_name"] == "orb"
        assert matching["signal_reason"] == "Breakout confirmed above VWAP with elevated RVOL."
        assert Decimal(str(matching["signal_confidence"])) == Decimal("0.81")
        assert matching["signal_risk_rejected"] is False

    async def test_get_order_detail_includes_signal_context_and_events(
        self, client: AsyncClient, auth_headers: dict, db
    ):
        from app.db.models import Order, OrderEvent, Signal, Strategy

        strategy = Strategy(
            id=uuid.uuid4(),
            name="Inspector ORB",
            type="orb",
            params={},
            allowed_tickers=["NVDA"],
        )
        signal = Signal(
            id=uuid.uuid4(),
            strategy_id=strategy.id,
            ticker="NVDA",
            side="buy",
            signal_type="entry",
            status="approved",
            confidence=Decimal("0.67"),
            reason="Opening range breakout confirmed above VWAP.",
            generated_at=datetime.now(UTC),
        )
        order = Order(
            id=uuid.uuid4(),
            signal_id=signal.id,
            client_order_key="api-order-detail-inspector",
            ticker="NVDA",
            side="buy",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price=Decimal("900"),
            status="accepted",
            is_dry_run=False,
            broker_request={"ticker": "NVDA", "quantity": 1},
            broker_response={"status": "WORKING"},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        event = OrderEvent(
            id=uuid.uuid4(),
            order_id=order.id,
            event_type="reconciled",
            from_status="submitted",
            to_status="accepted",
            payload={"source": "broker_poll"},
            occurred_at=datetime.now(UTC),
        )
        db.add_all([strategy, signal, order, event])
        await db.commit()

        resp = await client.get(f"/v1/orders/{order.id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["strategy_name"] == "Inspector ORB"
        assert data["signal_reason"] == "Opening range breakout confirmed above VWAP."
        assert data["signal_snapshot"]["ticker"] == "NVDA"
        assert data["signal_snapshot"]["signal_type"] == "entry"
        assert data["broker_request"]["ticker"] == "NVDA"
        assert data["broker_response"]["status"] == "WORKING"
        assert len(data["events"]) == 1
        assert data["events"][0]["event_type"] == "reconciled"
        assert data["events"][0]["to_status"] == "accepted"

    async def test_execution_quality_report_summarizes_slippage_and_rejections(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
    ):
        from app.db.models import Order, Signal, Strategy

        now = datetime.now(UTC)
        strategy = Strategy(
            id=uuid.uuid4(),
            name="Execution Quality Strategy",
            type="orb",
            params={},
            allowed_tickers=["AAPL", "MSFT"],
        )
        signal = Signal(
            id=uuid.uuid4(),
            strategy_id=strategy.id,
            ticker="AAPL",
            side="buy",
            signal_type="entry",
            status="executed",
            entry_price=Decimal("100"),
            suggested_quantity=Decimal("5"),
            confidence=Decimal("0.75"),
            generated_at=now - timedelta(minutes=10),
        )
        filled = Order(
            id=uuid.uuid4(),
            signal_id=signal.id,
            client_order_key="execution-quality-filled",
            ticker="AAPL",
            side="buy",
            order_type="market",
            quantity=Decimal("5"),
            filled_quantity=Decimal("5"),
            avg_fill_price=Decimal("101"),
            status="filled",
            is_dry_run=False,
            execution_environment="demo",
            expected_fill_price=Decimal("100"),
            submitted_at=now - timedelta(minutes=9),
            first_ack_at=now - timedelta(minutes=9, milliseconds=-120),
            filled_at=now - timedelta(minutes=9, milliseconds=-450),
            broker_latency_ms=120,
            fill_latency_ms=450,
            created_at=now - timedelta(minutes=10),
            updated_at=now - timedelta(minutes=9),
        )
        rejected = Order(
            id=uuid.uuid4(),
            client_order_key="execution-quality-rejected",
            ticker="MSFT",
            side="sell",
            order_type="limit",
            quantity=Decimal("2"),
            status="rejected",
            is_dry_run=False,
            execution_environment="demo",
            error_message="Broker rejected limit outside valid price band",
            submitted_at=now - timedelta(minutes=8),
            first_ack_at=now - timedelta(minutes=8, milliseconds=-200),
            broker_latency_ms=200,
            created_at=now - timedelta(minutes=8),
            updated_at=now - timedelta(minutes=8),
        )
        db.add_all([strategy, signal, filled, rejected])
        await db.commit()

        resp = await client.get("/v1/reports/execution-quality", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["summary"]["total_orders"] == 2
        assert data["summary"]["filled_orders"] == 1
        assert data["summary"]["rejected_orders"] == 1
        assert data["summary"]["status"] == "degraded"
        assert data["summary"]["avg_slippage_pct"] == 1.0
        assert data["summary"]["total_slippage_value"] == 5.0
        assert data["by_symbol_order_type"][0]["ticker"] in {"AAPL", "MSFT"}
        assert any(
            row["ticker"] == "AAPL" and row["avg_score"] == 82.0
            for row in data["by_symbol_order_type"]
        )
        assert data["reject_cancel_patterns"][0]["reason"].startswith("Broker rejected")
        assert data["worst_orders"][0]["ticker"] in {"AAPL", "MSFT"}


@pytest.mark.asyncio
class TestEmergencyFlow:
    async def test_emergency_kill_switch(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/v1/emergency/kill-switch", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "kill" in data["action"]

    async def test_emergency_disable_auto_trading(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/v1/emergency/auto-trading/off", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_cannot_enable_auto_trading_with_kill_switch(
        self, client: AsyncClient, auth_headers: dict
    ):
        # Activate kill switch
        await client.post("/v1/risk/kill-switch/enable", headers=auth_headers)

        # Try to enable auto-trading — should fail
        resp = await client.post("/v1/emergency/auto-trading/on", headers=auth_headers)
        assert resp.status_code == 400
        assert "kill switch" in resp.json()["detail"].lower()

        # Clean up
        await client.post("/v1/risk/kill-switch/disable", headers=auth_headers)


@pytest.mark.asyncio
class TestPositionsFlow:
    async def test_list_positions_mock(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/positions", headers=auth_headers)
        assert resp.status_code == 200
        positions = resp.json()
        assert isinstance(positions, list)
        # Mock mode should have pre-seeded positions
        assert len(positions) >= 1
        for pos in positions:
            assert "ticker" in pos
            assert "quantity" in pos
            assert pos["quantity"] > 0

    async def test_list_positions_returns_clear_broker_rate_limit(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        from app.api.deps import get_broker
        from app.main import app

        app.dependency_overrides[get_broker] = lambda: FakeRateLimitedBroker()
        try:
            resp = await client.get("/v1/positions", headers=auth_headers)
        finally:
            app.dependency_overrides.pop(get_broker, None)

        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "1"
        assert resp.json()["detail"]["code"] == "broker_rate_limited"


@pytest.mark.asyncio
class TestAuditFlow:
    async def test_audit_log_contains_login(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/audit", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        # Login should have been logged
        actions = [item["action"] for item in data["items"]]
        assert any("login" in a for a in actions)


@pytest.mark.asyncio
class TestAlertsFlow:
    async def test_list_alerts(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/alerts", headers=auth_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_send_test_alert(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/v1/alerts/test", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["sent"] is True


@pytest.mark.asyncio
class TestSettingsFlow:
    async def test_get_settings(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/settings", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "theme" in data
        assert "auto_trading_enabled" in data
        assert "kill_switch_active" in data

    async def test_update_settings_theme(self, client: AsyncClient, auth_headers: dict):
        resp = await client.patch("/v1/settings", headers=auth_headers, json={"theme": "light"})
        assert resp.status_code == 200
        assert resp.json()["theme"] == "light"

    async def test_get_live_readiness(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/settings/live-readiness", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready_for_live"] is False
        assert any(check["key"] == "app_mode_live" for check in data["checks"])

    async def test_cannot_unlock_live_without_prerequisites(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_MODE", "live")
        monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)

        resp = await client.post(
            "/v1/settings/live-readiness",
            headers=auth_headers,
            json={"action": "unlock_live"},
        )
        assert resp.status_code == 400
        assert "cannot be unlocked" in resp.json()["detail"].lower()

    async def test_can_unlock_live_after_all_checks(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
        monkeypatch,
    ):
        from sqlalchemy import select

        from app.core.config import settings
        from app.db.models import BrokerConnection, User

        monkeypatch.setattr(settings, "APP_MODE", "live")
        monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
        monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")

        user = (await db.execute(select(User).where(User.email == "admin@test.com"))).scalar_one()
        db.add(
            BrokerConnection(
                id=uuid.uuid4(),
                user_id=user.id,
                broker="trading212",
                environment="live",
                api_key_encrypted="enc-key",
                api_secret_encrypted="enc-secret",
                is_active=True,
                last_test_at=datetime.now(UTC),
                last_test_ok=True,
                account_id="LIVE-123",
                account_currency="USD",
            )
        )
        await db.commit()

        for action in (
            "record_demo_validation",
            "record_broker_test",
            "record_telegram_test",
            "record_kill_switch_test",
        ):
            resp = await client.post(
                "/v1/settings/live-readiness",
                headers=auth_headers,
                json={"action": action},
            )
            assert resp.status_code == 200, resp.text

        unlock_resp = await client.post(
            "/v1/settings/live-readiness",
            headers=auth_headers,
            json={"action": "unlock_live"},
        )
        assert unlock_resp.status_code == 200
        assert unlock_resp.json()["ready_for_live"] is True


@pytest.mark.asyncio
class TestTelegramFlow:
    async def test_telegram_status(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/v1/telegram/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["bot_configured"] is True
        assert data["control_enabled"] is True
        assert "/status" in data["supported_commands"]

    async def test_telegram_webhook_requires_secret(self, client: AsyncClient):
        resp = await client.post(
            "/v1/telegram/webhook",
            json={
                "message": {
                    "text": "/status",
                    "chat": {"id": 12345},
                    "from": {"id": 777},
                }
            },
        )
        assert resp.status_code == 401

    async def test_telegram_pause_confirmation_flow(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        sent_messages: list[tuple[str, str]] = []

        async def fake_send_message(self, chat_id: str, text: str) -> None:
            sent_messages.append((chat_id, text))

        monkeypatch.setattr(
            "app.services.telegram_control.TelegramControlService._send_message",
            fake_send_message,
        )

        await client.post("/v1/emergency/auto-trading/on", headers=auth_headers)

        request_headers = {"X-Telegram-Bot-Api-Secret-Token": "test-telegram-secret"}
        pause_resp = await client.post(
            "/v1/telegram/webhook",
            headers=request_headers,
            json={
                "message": {
                    "text": "/pause",
                    "chat": {"id": 12345},
                    "from": {"id": 777},
                }
            },
        )
        assert pause_resp.status_code == 200
        pause_data = pause_resp.json()
        assert pause_data["requires_confirmation"] is True

        confirmation_code = pause_data["reply_text"].split("/confirm ", 1)[1].split(" ", 1)[0]
        confirm_resp = await client.post(
            "/v1/telegram/webhook",
            headers=request_headers,
            json={
                "message": {
                    "text": f"/confirm {confirmation_code}",
                    "chat": {"id": 12345},
                    "from": {"id": 777},
                }
            },
        )
        assert confirm_resp.status_code == 200
        confirm_data = confirm_resp.json()
        assert confirm_data["executed"] is True
        assert "disabled" in confirm_data["reply_text"].lower()

        settings_resp = await client.get("/v1/settings", headers=auth_headers)
        assert settings_resp.status_code == 200
        assert settings_resp.json()["auto_trading_enabled"] is False
        assert len(sent_messages) == 2


@pytest.mark.asyncio
class TestLiveModeSafetyFlow:
    async def test_live_auto_trading_requires_readiness(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_MODE", "live")
        monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)

        resp = await client.post("/v1/emergency/auto-trading/on", headers=auth_headers)
        assert resp.status_code == 400
        assert "live auto-trading" in resp.json()["detail"].lower()

    async def test_live_order_endpoint_requires_live_readiness_unlock(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
        monkeypatch,
    ):
        from sqlalchemy import select

        from app.core.config import settings
        from app.db.models import AppSettings, BrokerConnection, User

        monkeypatch.setattr(settings, "APP_MODE", "live")
        monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
        monkeypatch.setattr(settings, "T212_ENVIRONMENT", "live")
        monkeypatch.setattr("app.broker.trading212.Trading212Adapter", FakeTrading212Adapter)

        user = (await db.execute(select(User).where(User.email == "admin@test.com"))).scalar_one()
        app_settings = (
            await db.execute(select(AppSettings).where(AppSettings.id == 1))
        ).scalar_one()
        app_settings.auto_trading_enabled = True
        app_settings.live_trading_unlocked = False
        db.add(
            BrokerConnection(
                id=uuid.uuid4(),
                user_id=user.id,
                broker="trading212",
                environment="live",
                api_key_encrypted="live-key",
                api_secret_encrypted="live-secret",
                is_active=True,
                last_test_ok=True,
                account_id="LIVE-BLOCKED",
                account_currency="USD",
            )
        )
        await db.commit()

        order_resp = await client.post(
            "/v1/orders",
            headers=auth_headers,
            json={
                "ticker": "MSFT",
                "side": "buy",
                "order_type": "market",
                "quantity": "1",
            },
        )

        assert order_resp.status_code == 403
        assert "live readiness" in order_resp.json()["detail"].lower()


@pytest.mark.asyncio
class TestBrokerExecutionFlow:
    async def test_mock_connect_ignores_credentials_and_does_not_persist_them(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
        monkeypatch,
    ):
        from sqlalchemy import select

        from app.core.config import settings
        from app.db.models import BrokerConnection

        monkeypatch.setattr(settings, "APP_MODE", "mock")

        resp = await client.post(
            "/v1/broker/trading212/connect",
            headers=auth_headers,
            json={
                "api_key": "should-not-be-stored",
                "api_secret": "should-not-be-stored-secret",
                "environment": "demo",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["environment"] == "mock"
        assert data["credential_state"] == "mock"
        assert data["is_active"] is True
        assert data["account_id"] == "MOCK-CREDENTIALS-IGNORED"
        assert data["recovery_hint"] is not None
        assert "credentials are ignored" in data["recovery_hint"].lower()
        assert "not stored" in data["recovery_hint"].lower()

        saved = (await db.execute(select(BrokerConnection))).scalars().all()
        assert saved == []

    async def test_mock_connect_does_not_call_broker_adapters(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        class ExplodingAdapter:
            def __init__(self, *args, **kwargs):
                raise AssertionError("mock connect must not instantiate broker adapters")

        monkeypatch.setattr(settings, "APP_MODE", "mock")
        monkeypatch.setattr("app.broker.mock_adapter.MockBrokerAdapter", ExplodingAdapter)
        monkeypatch.setattr("app.broker.trading212.Trading212Adapter", ExplodingAdapter)

        resp = await client.post(
            "/v1/broker/trading212/connect",
            headers=auth_headers,
            json={"api_key": "ignored-key", "api_secret": "ignored-secret", "environment": "demo"},
        )

        assert resp.status_code == 200
        assert resp.json()["credential_state"] == "mock"

    async def test_broker_status_surfaces_reconnect_required_state(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db,
        monkeypatch,
    ):
        from sqlalchemy import select

        from app.core.config import settings
        from app.db.models import BrokerConnection, User

        monkeypatch.setattr(settings, "APP_MODE", "demo")
        user = (await db.execute(select(User).where(User.email == "admin@test.com"))).scalar_one()
        conn = BrokerConnection(
            id=uuid.uuid4(),
            user_id=user.id,
            broker="trading212",
            environment="demo",
            api_key_encrypted="invalid-token",
            api_secret_encrypted="invalid-token",
            is_active=True,
            last_test_ok=True,
            account_id="DEMO-RECOVER",
            account_currency="USD",
        )
        db.add(conn)
        await db.commit()

        resp = await client.get("/v1/broker/trading212/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["credential_state"] == "reconnect_required"
        assert data["recovery_hint"] is not None
        assert "MASTER_KEY" in data["recovery_hint"]
        assert "Reconnect Trading 212" in data["recovery_hint"]

    async def test_connect_failure_returns_structured_broker_diagnostics(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_MODE", "demo")
        monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
        monkeypatch.setattr(
            "app.broker.trading212.Trading212Adapter", FakeRejectingTrading212Adapter
        )

        resp = await client.post(
            "/v1/broker/trading212/connect",
            headers=auth_headers,
            json={"api_key": "bad-key", "api_secret": "bad-secret", "environment": "demo"},
        )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["message"].startswith("Trading 212 rejected the demo API credentials")
        assert detail["diagnostics"]["code"] == "broker_auth_rejected"
        assert detail["diagnostics"]["environment"] == "demo"
        assert detail["diagnostics"]["http_status"] == 401
        assert detail["diagnostics"]["causes"][0]["key"] == "wrong_environment"

    async def test_demo_broker_status_reports_missing_demo_credentials_safely(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_MODE", "demo")
        monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
        monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")
        monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key")
        monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret")

        resp = await client.get("/v1/broker/trading212/status", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["environment"] == "demo"
        assert data["credential_state"] == "not_connected"
        assert data["is_active"] is False
        assert "demo credentials" in data["recovery_hint"].lower()
        assert "live-key" not in str(data)

    async def test_demo_broker_execution_end_to_end(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_MODE", "demo")
        monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
        monkeypatch.setattr("app.broker.trading212.Trading212Adapter", FakeTrading212Adapter)

        connect_resp = await client.post(
            "/v1/broker/trading212/connect",
            headers=auth_headers,
            json={"api_key": "demo-key", "api_secret": "demo-secret", "environment": "demo"},
        )
        assert connect_resp.status_code == 200
        assert connect_resp.json()["environment"] == "demo"

        test_resp = await client.post("/v1/broker/trading212/test", headers=auth_headers)
        assert test_resp.status_code == 200
        assert test_resp.json()["is_ok"] is True

        enable_resp = await client.post("/v1/emergency/auto-trading/on", headers=auth_headers)
        assert enable_resp.status_code == 200

        order_resp = await client.post(
            "/v1/orders",
            headers=auth_headers,
            json={
                "ticker": "AAPL",
                "side": "buy",
                "order_type": "market",
                "quantity": "1",
            },
        )
        assert order_resp.status_code == 201, order_resp.text
        order = order_resp.json()
        assert order["is_dry_run"] is False
        assert order["status"] == "filled"
        assert order["broker_order_id"].startswith("DEMO-")

    async def test_live_broker_execution_requires_readiness_then_succeeds(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_MODE", "live")
        monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
        monkeypatch.setattr(settings, "T212_ENVIRONMENT", "live")
        monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "configured-live-key")
        monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "polygon")
        monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")
        monkeypatch.setattr("app.broker.trading212.Trading212Adapter", FakeTrading212Adapter)

        startup_resp = await client.get("/v1/health/startup")
        assert startup_resp.status_code == 200
        assert startup_resp.json()["status"] == "pass"

        connect_resp = await client.post(
            "/v1/broker/trading212/connect",
            headers=auth_headers,
            json={"api_key": "live-key", "api_secret": "live-secret", "environment": "live"},
        )
        assert connect_resp.status_code == 200
        assert connect_resp.json()["environment"] == "live"

        blocked_resp = await client.post("/v1/emergency/auto-trading/on", headers=auth_headers)
        assert blocked_resp.status_code == 400
        assert "live auto-trading" in blocked_resp.json()["detail"].lower()

        for action in (
            "record_demo_validation",
            "record_broker_test",
            "record_telegram_test",
            "record_kill_switch_test",
            "unlock_live",
        ):
            readiness_resp = await client.post(
                "/v1/settings/live-readiness",
                headers=auth_headers,
                json={"action": action},
            )
            assert readiness_resp.status_code == 200, readiness_resp.text

        enable_resp = await client.post("/v1/emergency/auto-trading/on", headers=auth_headers)
        assert enable_resp.status_code == 200

        order_resp = await client.post(
            "/v1/orders",
            headers=auth_headers,
            json={
                "ticker": "MSFT",
                "side": "buy",
                "order_type": "market",
                "quantity": "1",
            },
        )
        assert order_resp.status_code == 201, order_resp.text
        order = order_resp.json()
        assert order["is_dry_run"] is False
        assert order["status"] == "filled"
        assert order["broker_order_id"].startswith("LIVE-")
