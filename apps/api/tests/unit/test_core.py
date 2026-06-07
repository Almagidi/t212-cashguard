"""
Unit tests: risk engine, ORB strategy, security helpers, sell quantity convention.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _replace_jwt_segment(token: str, index: int, value: dict[str, object]) -> str:
    parts = token.split(".")
    parts[index] = _b64url_encode(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    return ".".join(parts)


def _sign_jwt(header: dict[str, object], payload: dict[str, object]) -> str:
    from app.core.config import settings

    signing_input = ".".join(
        [
            _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    )
    return f"{signing_input}.{_b64url_encode(signature.digest())}"


# ─── Security ────────────────────────────────────────────────────────────────


class TestSecurity:
    def test_password_hash_and_verify(self):
        from app.core.security import hash_password, verify_password

        hashed = hash_password("mypassword123")
        assert verify_password("mypassword123", hashed)
        assert not verify_password("wrongpassword", hashed)

    def test_create_and_decode_token(self):
        from app.core.security import create_access_token, decode_access_token

        token = create_access_token("user-123")
        assert isinstance(token, str)

        payload = decode_access_token(token)
        assert payload["sub"] == "user-123"
        assert payload["type"] == "access"

    def test_decode_expired_token_raises_token_decode_error(self):
        from app.core.security import TokenDecodeError, decode_access_token

        now = datetime.now(UTC)
        token = _sign_jwt(
            {"alg": "HS256", "typ": "JWT"},
            {
                "sub": "user-123",
                "iat": int(now.timestamp()),
                "exp": int((now - timedelta(minutes=1)).timestamp()),
                "type": "access",
            },
        )

        with pytest.raises(TokenDecodeError):
            decode_access_token(token)

    def test_decode_token_rejects_tampered_payload(self):
        from app.core.security import TokenDecodeError, create_access_token, decode_access_token

        token = create_access_token("user-123")
        payload = json.loads(_b64url_decode(token.split(".")[1]))
        payload["sub"] = "attacker"
        tampered = _replace_jwt_segment(token, 1, payload)

        with pytest.raises(TokenDecodeError):
            decode_access_token(tampered)

    def test_decode_token_rejects_tampered_signature(self):
        from app.core.security import TokenDecodeError, create_access_token, decode_access_token

        token = create_access_token("user-123")
        header, payload, signature = token.split(".")
        signature_bytes = bytearray(_b64url_decode(signature))
        # Changing only the final base64url character can alter unused padding bits,
        # so mutate the decoded signature bytes instead.
        signature_bytes[0] ^= 0x01
        tampered = ".".join([header, payload, _b64url_encode(bytes(signature_bytes))])

        assert tampered != token

        with pytest.raises(TokenDecodeError):
            decode_access_token(tampered)

    def test_decode_token_rejects_alg_none(self):
        from app.core.security import TokenDecodeError, decode_access_token

        now = datetime.now(UTC)
        token = _sign_jwt(
            {"alg": "none", "typ": "JWT"},
            {
                "sub": "user-123",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "type": "access",
            },
        )

        with pytest.raises(TokenDecodeError):
            decode_access_token(token)

    def test_decode_token_rejects_wrong_alg(self):
        from app.core.security import TokenDecodeError, decode_access_token

        now = datetime.now(UTC)
        token = _sign_jwt(
            {"alg": "HS512", "typ": "JWT"},
            {
                "sub": "user-123",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "type": "access",
            },
        )

        with pytest.raises(TokenDecodeError):
            decode_access_token(token)

    def test_decode_token_rejects_malformed_token(self):
        from app.core.security import TokenDecodeError, decode_access_token

        with pytest.raises(TokenDecodeError):
            decode_access_token("not-a-jwt")

    def test_decode_token_rejects_missing_typ_header(self):
        from app.core.security import TokenDecodeError, decode_access_token

        now = datetime.now(UTC)
        token = _sign_jwt(
            {"alg": "HS256"},
            {
                "sub": "user-123",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "type": "access",
            },
        )

        with pytest.raises(TokenDecodeError):
            decode_access_token(token)

    def test_decode_token_rejects_missing_access_type(self):
        from app.core.security import TokenDecodeError, decode_access_token

        now = datetime.now(UTC)
        token = _sign_jwt(
            {"alg": "HS256", "typ": "JWT"},
            {
                "sub": "user-123",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "type": "refresh",
            },
        )

        with pytest.raises(TokenDecodeError):
            decode_access_token(token)

    def test_extra_non_reserved_claim_is_preserved(self):
        from app.core.security import create_access_token, decode_access_token

        token = create_access_token("user-123", extra={"role": "admin"})
        payload = decode_access_token(token)
        assert payload["role"] == "admin"

    def test_extra_cannot_override_sub(self):
        from app.core.security import create_access_token, decode_access_token

        token = create_access_token("user-123", extra={"sub": "attacker"})
        payload = decode_access_token(token)
        assert payload["sub"] == "user-123"

    def test_extra_cannot_override_type(self):
        from app.core.security import create_access_token, decode_access_token

        token = create_access_token("user-123", extra={"type": "refresh"})
        payload = decode_access_token(token)
        assert payload["type"] == "access"

    def test_extra_cannot_override_exp(self):
        from app.core.security import create_access_token, decode_access_token

        past = int((datetime.now(UTC) - timedelta(minutes=10)).timestamp())
        token = create_access_token("user-123", extra={"exp": past})
        # token must still decode successfully (exp was not overridden)
        payload = decode_access_token(token)
        assert payload["exp"] > int(datetime.now(UTC).timestamp())

    def test_decode_token_rejects_missing_exp(self):
        from app.core.security import TokenDecodeError, decode_access_token

        now = datetime.now(UTC)
        token = _sign_jwt(
            {"alg": "HS256", "typ": "JWT"},
            {
                "sub": "user-123",
                "iat": int(now.timestamp()),
                "type": "access",
                # no "exp"
            },
        )
        with pytest.raises(TokenDecodeError):
            decode_access_token(token)

    def test_decode_token_rejects_bool_exp(self):
        from app.core.security import TokenDecodeError, decode_access_token

        now = datetime.now(UTC)
        token = _sign_jwt(
            {"alg": "HS256", "typ": "JWT"},
            {
                "sub": "user-123",
                "iat": int(now.timestamp()),
                "exp": True,
                "type": "access",
            },
        )
        with pytest.raises(TokenDecodeError):
            decode_access_token(token)

    def test_field_encryption_roundtrip(self):
        from app.core.security import decrypt_field, encrypt_field

        secret = "my-api-secret-key-12345"
        encrypted = encrypt_field(secret)
        assert encrypted != secret
        decrypted = decrypt_field(encrypted)
        assert decrypted == secret

    def test_different_values_encrypt_differently(self):
        from app.core.security import encrypt_field

        e1 = encrypt_field("key-one")
        e2 = encrypt_field("key-two")
        assert e1 != e2


# ─── Sell Quantity Convention ─────────────────────────────────────────────────


class TestSellQuantityConvention:
    """
    Critical: Trading 212 requires negative quantity for sell orders.
    """

    def test_make_sell_quantity_positive_input(self):
        from app.broker.trading212 import make_sell_quantity

        result = make_sell_quantity(Decimal("10.5"))
        assert result == Decimal("-10.5")
        assert result < 0

    def test_make_sell_quantity_already_negative(self):
        from app.broker.trading212 import make_sell_quantity

        result = make_sell_quantity(Decimal("-10.5"))
        assert result == Decimal("-10.5")
        assert result < 0

    def test_make_sell_quantity_never_positive(self):
        from app.broker.trading212 import make_sell_quantity

        for qty in [Decimal("1"), Decimal("100"), Decimal("0.5"), Decimal("-5")]:
            assert (
                make_sell_quantity(qty) < 0
            ), f"Sell quantity must be negative, got: {make_sell_quantity(qty)}"

    def test_buy_quantity_positive(self):
        """Buy quantities should be positive."""
        buy_qty = Decimal("10")
        assert buy_qty > 0


# ─── ORB Strategy ─────────────────────────────────────────────────────────────


class TestORBStrategy:
    def _make_candle(self, open_=100, high=105, low=95, close=102, ts=None):
        from app.strategies.orb import OHLCV

        return OHLCV(
            timestamp=ts or datetime.now(UTC),
            open=Decimal(str(open_)),
            high=Decimal(str(high)),
            low=Decimal(str(low)),
            close=Decimal(str(close)),
            volume=Decimal("100000"),
        )

    def test_compute_opening_range_sufficient_candles(self):
        from app.strategies.orb import OpeningRangeBreakoutStrategy

        strat = OpeningRangeBreakoutStrategy({"orb_minutes": 15})
        # 15 min / 5 min candles = 3 candles
        candles = [
            self._make_candle(100, 110, 95, 105),
            self._make_candle(105, 112, 98, 108),
            self._make_candle(108, 115, 100, 112),
        ]
        result = strat.compute_opening_range(candles)
        assert result is not None
        orb_high, orb_low = result
        assert orb_high == Decimal("115")
        assert orb_low == Decimal("95")

    def test_compute_opening_range_insufficient_candles(self):
        from app.strategies.orb import OpeningRangeBreakoutStrategy

        strat = OpeningRangeBreakoutStrategy({"orb_minutes": 15})
        candles = [self._make_candle()]  # only 1, need 3
        result = strat.compute_opening_range(candles)
        assert result is None

    def test_validate_range_valid(self):
        from app.strategies.orb import OpeningRangeBreakoutStrategy

        strat = OpeningRangeBreakoutStrategy()
        valid, reason = strat.validate_range(Decimal("105"), Decimal("100"), Decimal("100"))
        assert valid
        assert "valid" in reason.lower()

    def test_validate_range_too_narrow(self):
        from app.strategies.orb import OpeningRangeBreakoutStrategy

        strat = OpeningRangeBreakoutStrategy({"min_range_pct": 1.0})
        valid, reason = strat.validate_range(Decimal("100.05"), Decimal("100.00"), Decimal("100"))
        assert not valid
        assert "narrow" in reason.lower()

    def test_validate_range_too_wide(self):
        from app.strategies.orb import OpeningRangeBreakoutStrategy

        strat = OpeningRangeBreakoutStrategy({"max_range_pct": 2.0})
        valid, reason = strat.validate_range(Decimal("110"), Decimal("100"), Decimal("100"))
        assert not valid
        assert "wide" in reason.lower()

    def test_calculate_quantity_respects_risk_pct(self):
        from app.strategies.orb import OpeningRangeBreakoutStrategy

        strat = OpeningRangeBreakoutStrategy({"max_risk_per_trade_pct": 1.0})
        qty = strat.calculate_quantity(
            entry_price=Decimal("100"),
            stop_price=Decimal("98"),  # $2 risk per share
            account_value=Decimal("10000"),  # 1% = $100 risk
            available_cash=Decimal("5000"),
        )
        # $100 risk / $2 per share = 50 shares
        assert qty == Decimal("50.00")

    def test_calculate_quantity_capped_by_cash(self):
        from app.strategies.orb import OpeningRangeBreakoutStrategy

        strat = OpeningRangeBreakoutStrategy({"max_risk_per_trade_pct": 50.0})
        qty = strat.calculate_quantity(
            entry_price=Decimal("100"),
            stop_price=Decimal("50"),
            account_value=Decimal("10000"),
            available_cash=Decimal("200"),  # Only $200 cash → max 2 shares
        )
        assert qty <= Decimal("2.00")

    def test_generate_signal_long_breakout(self):
        from app.strategies.orb import OpeningRangeBreakoutStrategy

        strat = OpeningRangeBreakoutStrategy({"orb_minutes": 15})
        orb_candles = [
            self._make_candle(100, 105, 98, 103),
            self._make_candle(103, 106, 99, 104),
            self._make_candle(104, 107, 100, 106),
        ]
        # Current price breaking above ORB high (107)
        breakout_candle = self._make_candle(107, 110, 106, 109)
        signal = strat.generate_signal(
            ticker="AAPL",
            current_price=Decimal("109"),
            current_candle=breakout_candle,
            opening_range_candles=orb_candles,
            account_value=Decimal("10000"),
            available_cash=Decimal("5000"),
            session_candle_index=4,
        )
        assert signal is not None
        assert signal.side == "buy"
        assert signal.signal_type == "entry"
        assert signal.entry_price > Decimal("107")
        assert signal.stop_price < signal.entry_price
        assert signal.take_profit_price > signal.entry_price

    def test_generate_signal_no_breakout(self):
        from app.strategies.orb import OpeningRangeBreakoutStrategy

        strat = OpeningRangeBreakoutStrategy({"orb_minutes": 15})
        orb_candles = [
            self._make_candle(100, 105, 98, 103),
            self._make_candle(103, 106, 99, 104),
            self._make_candle(104, 107, 100, 106),
        ]
        # Current price within range — no signal
        no_breakout_candle = self._make_candle(104, 106, 103, 104)
        signal = strat.generate_signal(
            ticker="AAPL",
            current_price=Decimal("104"),
            current_candle=no_breakout_candle,
            opening_range_candles=orb_candles,
            account_value=Decimal("10000"),
            available_cash=Decimal("5000"),
            session_candle_index=4,
        )
        assert signal is None


# ─── Mock Broker ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestMockBroker:
    async def test_test_connection(self):
        from app.broker.mock_adapter import MockBrokerAdapter

        async with MockBrokerAdapter() as broker:
            result = await broker.test_connection()
        assert result["is_ok"] is True
        assert result["account_id"] is not None
        assert result["error"] is None

    async def test_get_account_summary(self):
        from app.broker.mock_adapter import MockBrokerAdapter

        async with MockBrokerAdapter() as broker:
            summary = await broker.get_account_summary()
        assert "cash" in summary
        assert "total" in summary
        assert summary["cash"] > 0

    async def test_place_market_buy_order(self):
        from app.broker.mock_adapter import MockBrokerAdapter

        async with MockBrokerAdapter() as broker:
            order = await broker.place_market_order("GOOGL", Decimal("5"))
        assert order["ticker"] == "GOOGL"
        assert order["status"] == "FILLED"
        assert order["filledQuantity"] == 5.0

    async def test_sell_order_uses_negative_quantity(self):
        """T212 sell orders must use negative quantity."""
        from app.broker.mock_adapter import MockBrokerAdapter
        from app.broker.trading212 import make_sell_quantity

        async with MockBrokerAdapter() as broker:
            # Ensure we have a position first
            await broker.place_market_order("AAPL", Decimal("5"))
            sell_qty = make_sell_quantity(Decimal("3"))
            assert sell_qty == Decimal("-3")
            order = await broker.place_market_order("AAPL", sell_qty)
        assert order["quantity"] == -3.0

    async def test_get_positions(self):
        from app.broker.mock_adapter import MockBrokerAdapter

        async with MockBrokerAdapter() as broker:
            positions = await broker.get_positions()
        assert isinstance(positions, list)
        assert len(positions) >= 1
        for p in positions:
            assert "ticker" in p
            assert "quantity" in p


class TestBrokerSchemasAndAdapter:
    def test_broker_connect_request_trims_whitespace(self):
        from app.api.schemas import BrokerConnectRequest

        payload = BrokerConnectRequest(
            api_key="  demo-key  ",
            api_secret="\n demo-secret \t",
            environment="demo",
        )

        assert payload.api_key == "demo-key"
        assert payload.api_secret == "demo-secret"

    @pytest.mark.asyncio
    async def test_trading212_auth_failure_returns_actionable_guidance(self, monkeypatch):
        from app.broker.trading212 import T212AuthError, Trading212Adapter
        from app.core.config import settings

        async def fake_get_account_metadata(self):
            raise T212AuthError(401, "Unauthorized")

        monkeypatch.setattr(settings, "APP_MODE", "demo")
        monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
        monkeypatch.setattr(Trading212Adapter, "get_account_metadata", fake_get_account_metadata)

        async with Trading212Adapter("demo-key", "demo-secret", "demo") as broker:
            result = await broker.test_connection()

        assert result["is_ok"] is False
        assert "demo API credentials" in result["error"]
        assert "same environment" in result["error"]
        assert "whitespace" in result["error"]
        assert "public IP" in result["error"]


# ─── Risk Engine ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRiskEngine:
    async def test_cash_guard_blocks_overspend(self, db):
        from app.risk.engine import RiskEngine, RiskViolation

        engine = RiskEngine(db)
        with pytest.raises(RiskViolation) as exc_info:
            await engine.check_cash_guard(
                ticker="AAPL",
                quantity=Decimal("1000"),
                estimated_price=Decimal("200"),
                available_cash=Decimal("100"),  # Only $100 available
            )
        assert "cash guard" in exc_info.value.reason.lower()

    async def test_cash_guard_allows_affordable_order(self, db):
        from app.risk.engine import RiskEngine

        engine = RiskEngine(db)
        # Should not raise
        await engine.check_cash_guard(
            ticker="AAPL",
            quantity=Decimal("1"),
            estimated_price=Decimal("100"),
            available_cash=Decimal("1000"),
        )

    async def test_cash_guard_ignores_sell_orders(self, db):
        from app.risk.engine import RiskEngine

        engine = RiskEngine(db)
        # Sells don't need cash — should not raise regardless
        await engine.check_cash_guard(
            ticker="AAPL",
            quantity=Decimal("-100"),  # Sell
            estimated_price=Decimal("200"),
            available_cash=Decimal("0"),  # Zero cash — still OK for sells
        )

    async def test_kill_switch_blocks_all_trades(self, db):
        from app.db.models import AppSettings
        from app.risk.engine import RiskEngine, RiskViolation

        # Activate kill switch
        result = await db.execute(
            __import__("sqlalchemy", fromlist=["select"])
            .select(AppSettings)
            .where(AppSettings.id == 1)
        )
        s = result.scalar_one_or_none()
        if s:
            s.kill_switch_active = True
        else:
            db.add(AppSettings(id=1, kill_switch_active=True, auto_trading_enabled=True))
        await db.flush()

        engine = RiskEngine(db)
        with pytest.raises(RiskViolation) as exc_info:
            await engine.check_kill_switch()
        assert "kill switch" in exc_info.value.reason.lower()

    def _make_profile(self, **overrides):
        """Return a minimal RiskProfile with sensible defaults."""
        from app.db.models import RiskProfile

        defaults = {
            "id": uuid.uuid4(),
            "name": "Test Profile",
            "max_risk_per_trade_pct": Decimal("1.0"),
            "max_daily_loss_pct": Decimal("3.0"),
            "max_open_positions": 5,
            "max_position_size_pct": Decimal("10.0"),
            "max_trades_per_day": 20,
            "stop_after_consecutive_losses": 3,
            "symbol_cooldown_seconds": 0,
            "force_flat_eod": False,
            "is_default": True,
        }
        defaults.update(overrides)
        return RiskProfile(**defaults)

    async def test_daily_loss_limit_blocks_when_breached(self, db):
        from app.risk.engine import RiskEngine, RiskViolation

        profile = self._make_profile(max_daily_loss_pct=Decimal("3.0"))
        db.add(profile)
        await db.flush()

        engine = RiskEngine(db)
        with pytest.raises(RiskViolation) as exc_info:
            # -3.5% loss on a $10 000 account → breaches 3% limit
            await engine.check_daily_loss_limit(
                realized_pnl_today=Decimal("-350"),
                account_value=Decimal("10000"),
                risk_profile=profile,
            )
        assert "daily loss" in exc_info.value.reason.lower()

    async def test_daily_loss_limit_passes_when_profitable(self, db):
        from app.risk.engine import RiskEngine

        profile = self._make_profile(max_daily_loss_pct=Decimal("3.0"))
        db.add(profile)
        await db.flush()

        engine = RiskEngine(db)
        await engine.check_daily_loss_limit(
            realized_pnl_today=Decimal("50"),
            account_value=Decimal("10000"),
            risk_profile=profile,
        )  # must not raise

    async def test_max_open_positions_blocks_at_limit(self, db):
        from app.risk.engine import RiskEngine, RiskViolation

        profile = self._make_profile(max_open_positions=3)
        db.add(profile)
        await db.flush()

        engine = RiskEngine(db)
        with pytest.raises(RiskViolation):
            await engine.check_max_open_positions(current_open=3, risk_profile=profile)

    async def test_max_open_positions_passes_below_limit(self, db):
        from app.risk.engine import RiskEngine

        profile = self._make_profile(max_open_positions=5)
        db.add(profile)
        await db.flush()

        engine = RiskEngine(db)
        await engine.check_max_open_positions(current_open=2, risk_profile=profile)

    async def test_duplicate_order_blocks_active_order(self, db):
        from app.db.models import Order
        from app.risk.engine import RiskEngine, RiskViolation

        order = Order(
            id=uuid.uuid4(),
            client_order_key="key-dup-test",
            ticker="AAPL",
            side="buy",
            order_type="market",
            quantity=Decimal("5"),
            status="accepted",
            time_validity="DAY",
            is_dry_run=False,
        )
        db.add(order)
        await db.flush()

        engine = RiskEngine(db)
        with pytest.raises(RiskViolation) as exc_info:
            await engine.check_duplicate_order("AAPL", "buy")
        assert "duplicate" in exc_info.value.reason.lower()

    async def test_duplicate_order_passes_with_no_active_order(self, db):
        from app.risk.engine import RiskEngine

        engine = RiskEngine(db)
        await engine.check_duplicate_order("AAPL", "buy")  # must not raise

    async def test_position_size_blocks_oversized_trade(self, db):
        from app.risk.engine import RiskEngine, RiskViolation

        profile = self._make_profile(max_position_size_pct=Decimal("10.0"))
        db.add(profile)
        await db.flush()

        engine = RiskEngine(db)
        with pytest.raises(RiskViolation):
            # $2 000 position on a $10 000 account = 20% > 10% limit
            await engine.check_position_size(
                ticker="AAPL",
                estimated_cost=Decimal("2000"),
                account_value=Decimal("10000"),
                risk_profile=profile,
            )


# ── Drawdown sizing — sync, no DB needed ─────────────────────────────────────


class TestDrawdownSizing:
    def test_full_at_zero_loss(self):
        from app.risk.engine import RiskEngine

        engine = RiskEngine(None)
        factor, tier = engine.get_drawdown_size_factor(Decimal("0"), Decimal("10000"))
        assert factor == Decimal("1.0")
        assert tier == "full"

    def test_reduced_between_tier1_and_tier2(self):
        from app.risk.engine import RiskEngine

        engine = RiskEngine(None)
        # -0.6% loss: above TIER1 (0.5%) but below TIER2 (1.0%) → reduced
        factor, tier = engine.get_drawdown_size_factor(Decimal("-60"), Decimal("10000"))
        assert factor == Decimal("0.75")
        assert tier == "reduced"

    def test_half_at_tier2(self):
        from app.risk.engine import RiskEngine

        engine = RiskEngine(None)
        # -1.2% loss (≥ TIER2=1.0%, < TIER3=1.5%) → half
        factor, tier = engine.get_drawdown_size_factor(Decimal("-120"), Decimal("10000"))
        assert factor == Decimal("0.50")
        assert tier == "half"

    def test_quarter_at_tier3(self):
        from app.risk.engine import RiskEngine

        engine = RiskEngine(None)
        # -2% loss (≥ TIER3=1.5%) → quarter
        factor, tier = engine.get_drawdown_size_factor(Decimal("-200"), Decimal("10000"))
        assert factor == Decimal("0.25")
        assert tier == "quarter"


def test_account_summary_normaliser_handles_nested_trading212_cash_without_top_level_total_leak():
    from app.api.v1.routes.account import _normalise_account_summary

    summary = {
        "cash": {
            "availableToTrade": 5000.0,
            "blockedForPendingOrders": 300.0,
        },
        "invested": 4700.0,
        "result": 0.0,
        "total": 10000.0,
        "currencyCode": "GBP",
    }

    normalised = _normalise_account_summary(summary)

    assert normalised["total_value"] == 10000.0
    assert normalised["cash"] == 5300.0
    assert normalised["free_funds"] == 5000.0
    assert normalised["currency"] == "GBP"
