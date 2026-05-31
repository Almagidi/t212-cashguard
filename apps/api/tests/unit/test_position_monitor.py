# mypy: disable-error-code="no-untyped-def"
"""
Unit tests for position_monitor.py.
Covers early-exit branches, daily-loss halt, eod_flatten, and
_check_daily_loss_with_unrealized logic.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.config import settings
from app.db.models import AppSettings, RiskProfile
from app.services.position_monitor import PositionMonitor, _DailyLossOutcome

# ── Shared broker mock ────────────────────────────────────────────────────────


class _FakeBroker:
    """Async context-manager broker that returns configurable data."""

    def __init__(
        self,
        positions: list | None = None,
        account: dict | None = None,
        raise_on_fetch: Exception | None = None,
    ):
        self._positions = positions if positions is not None else []
        self._account = account or {"total": 10_000}
        self._raise_on_fetch = raise_on_fetch

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get_positions(self):
        if self._raise_on_fetch:
            raise self._raise_on_fetch
        return self._positions

    async def get_account_summary(self):
        return self._account

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        return {
            "id": f"BROKER-{ticker}",
            "status": "FILLED",
            "filledQuantity": float(quantity),
            "filledPrice": 100.0,
        }


async def _make_app_settings(
    db,
    kill_switch_active: bool = False,
    auto_trading_enabled: bool = True,
) -> AppSettings:
    s = AppSettings(
        id=1,
        theme="dark",
        timezone="UTC",
        auto_trading_enabled=auto_trading_enabled,
        kill_switch_active=kill_switch_active,
        live_trading_unlocked=False,
    )
    db.add(s)
    await db.commit()
    return s


async def _make_risk_profile(db, max_daily_loss_pct: float = 3.0) -> RiskProfile:
    rp = RiskProfile(
        id=uuid.uuid4(),
        name="Default",
        max_risk_per_trade_pct=Decimal("1.0"),
        max_daily_loss_pct=Decimal(str(max_daily_loss_pct)),
        max_open_positions=5,
        max_position_size_pct=Decimal("10.0"),
        max_trades_per_day=20,
        stop_after_consecutive_losses=3,
        symbol_cooldown_seconds=300,
        force_flat_eod=True,
        is_default=True,
    )
    db.add(rp)
    await db.commit()
    return rp


# ── run() early-exit branches ─────────────────────────────────────────────────


class TestRunEarlyExits:
    async def test_no_app_settings_returns_empty(self, db):
        monitor = PositionMonitor(db)
        result = await monitor.run()
        assert result["positions_checked"] == 0
        assert "skipped" not in result

    async def test_kill_switch_active(self, db):
        await _make_app_settings(db, kill_switch_active=True)
        monitor = PositionMonitor(db)
        result = await monitor.run()
        assert result.get("skipped") == "kill_switch_active"

    async def test_auto_trading_disabled(self, db):
        await _make_app_settings(db, auto_trading_enabled=False)
        monitor = PositionMonitor(db)
        result = await monitor.run()
        assert result.get("skipped") == "auto_trading_disabled"

    async def test_no_broker_returns_skipped(self, db):
        await _make_app_settings(db)
        monitor = PositionMonitor(db)
        with patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=None):
            result = await monitor.run()
        assert result.get("skipped") == "no_broker"

    async def test_broker_fetch_error_captured(self, db):
        await _make_app_settings(db)
        broker = _FakeBroker(raise_on_fetch=RuntimeError("connection refused"))
        monitor = PositionMonitor(db)
        with patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=broker):
            result = await monitor.run()
        assert any("broker_fetch" in e for e in result["errors"])

    async def test_no_positions_returns_empty_summary(self, db):
        await _make_app_settings(db)
        broker = _FakeBroker(positions=[], account={"total": 10_000})
        monitor = PositionMonitor(db)
        with patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=broker):
            result = await monitor.run()
        assert result["positions_checked"] == 0
        assert result["exits_submitted"] == 0


# ── run() daily-loss halt ─────────────────────────────────────────────────────


class TestRunDailyLossHalt:
    async def test_daily_loss_breach_halts_and_fires_kill_switch(self, db):
        await _make_app_settings(db)
        await _make_risk_profile(db, max_daily_loss_pct=3.0)

        # Position with large negative unrealized P&L — 40% drawdown on 10k account
        broker = _FakeBroker(
            positions=[{"ticker": "AAPL", "quantity": 10, "ppl": -4000}],
            account={"total": 10_000},
        )
        monitor = PositionMonitor(db)
        with (
            patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=broker),
            patch(
                "app.services.position_monitor.activate_kill_switch",
                new_callable=AsyncMock,
            ) as mock_ks,
            patch(
                "app.services.position_monitor.alert_kill_switch_activated",
                new_callable=AsyncMock,
            ),
        ):
            result = await monitor.run()

        assert result.get("halted") == "daily_loss_breach"
        mock_ks.assert_called_once()

    async def test_block_trading_snapshot_failure_halts_without_kill_switch(self, db, monkeypatch):
        await _make_app_settings(db)
        await _make_risk_profile(db, max_daily_loss_pct=3.0)
        monkeypatch.setattr(
            settings,
            "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
            "block_trading",
        )

        class SnapshotFailureBroker(_FakeBroker):
            def __init__(self):
                super().__init__(
                    positions=[{"ticker": "AAPL", "quantity": 10, "ppl": 0}],
                    account={"total": 10_000},
                )
                self.position_calls = 0

            async def get_positions(self):
                self.position_calls += 1
                if self.position_calls == 1:
                    return self._positions
                raise RuntimeError("snapshot unavailable")

        broker = SnapshotFailureBroker()
        monitor = PositionMonitor(db)
        with (
            patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=broker),
            patch(
                "app.services.position_monitor.activate_kill_switch",
                new_callable=AsyncMock,
            ) as mock_ks,
            patch(
                "app.services.position_monitor.alert_kill_switch_activated",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            result = await monitor.run()

        assert result.get("halted") == "unrealized_pnl_failure_block_trading"
        assert result.get("failure_policy") == "block_trading"
        assert result.get("fail_closed") is True
        assert broker.position_calls == 2
        mock_ks.assert_not_called()
        mock_alert.assert_not_called()

    async def test_activate_kill_switch_snapshot_failure_halts_with_single_policy_activation(
        self, db, monkeypatch
    ):
        await _make_app_settings(db)
        await _make_risk_profile(db, max_daily_loss_pct=3.0)
        monkeypatch.setattr(
            settings,
            "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
            "activate_kill_switch",
        )

        class SnapshotFailureBroker(_FakeBroker):
            def __init__(self):
                super().__init__(
                    positions=[{"ticker": "AAPL", "quantity": 10, "ppl": 0}],
                    account={"total": 10_000},
                )
                self.position_calls = 0
                self.write_calls = []

            async def get_positions(self):
                self.position_calls += 1
                if self.position_calls == 1:
                    return self._positions
                raise RuntimeError("snapshot unavailable")

            async def place_market_order(self, *args, **kwargs):
                self.write_calls.append(("place_market_order", args, kwargs))
                raise AssertionError("unexpected direct broker write")

        broker = SnapshotFailureBroker()
        monitor = PositionMonitor(db)
        with (
            patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=broker),
            patch(
                "app.services.position_monitor.activate_kill_switch",
                new_callable=AsyncMock,
            ) as mock_ks,
            patch(
                "app.services.position_monitor.alert_kill_switch_activated",
                new_callable=AsyncMock,
            ) as mock_alert,
        ):
            result = await monitor.run()

        assert result.get("halted") == "unrealized_pnl_failure_kill_switch"
        assert result.get("failure_policy") == "activate_kill_switch"
        assert result.get("fail_closed") is True
        assert broker.position_calls == 2
        assert broker.write_calls == []
        mock_ks.assert_awaited_once_with(
            db,
            actor="position_monitor:unrealized_pnl_failure",
        )
        mock_alert.assert_awaited_once_with(
            db,
            actor="position_monitor:unrealized_pnl_failure",
        )

    async def test_daily_loss_within_limit_continues(self, db):
        await _make_app_settings(db)
        await _make_risk_profile(db, max_daily_loss_pct=3.0)

        # Small positive P&L — no breach
        broker = _FakeBroker(
            positions=[{"ticker": "AAPL", "quantity": 10, "ppl": 50}],
            account={"total": 10_000},
        )
        monitor = PositionMonitor(db)
        with (
            patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=broker),
            patch.object(
                monitor,
                "_monitor_position",
                new_callable=AsyncMock,
                return_value={"exits": 0, "partial": 0, "stops": 0, "tps": 0},
            ),
        ):
            result = await monitor.run()

        assert "halted" not in result


# ── broker and market-data helpers ────────────────────────────────────────────


class TestBrokerAndMarketDataHelpers:
    async def test_get_broker_returns_mock_adapter_in_mock_mode(self, db, monkeypatch):
        monkeypatch.setattr(settings, "APP_MODE", "mock")

        broker = await PositionMonitor(db)._get_broker()

        assert broker is not None

    async def test_get_broker_returns_none_when_live_connection_missing(self, db, monkeypatch):
        monkeypatch.setattr(settings, "APP_MODE", "demo")

        broker = await PositionMonitor(db)._get_broker()

        assert broker is None

    async def test_get_broker_marks_reconnect_required_for_bad_credentials(self, monkeypatch):
        from app.core.security import CredentialDecryptionError

        conn = SimpleNamespace(
            api_key_encrypted="bad-key",
            api_secret_encrypted="bad-secret",
            environment="demo",
        )
        result = MagicMock()
        result.scalar_one_or_none.return_value = conn
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)

        def fake_decrypt(_value):
            raise CredentialDecryptionError("cannot decrypt")

        reconnect = AsyncMock()
        monkeypatch.setattr(settings, "APP_MODE", "demo")
        monkeypatch.setattr("app.core.security.decrypt_field", fake_decrypt)
        monkeypatch.setattr(
            "app.services.position_monitor.mark_broker_connection_reconnect_required",
            reconnect,
        )

        broker = await PositionMonitor(db)._get_broker()

        assert broker is None
        reconnect.assert_awaited_once_with(
            db,
            conn,
            "cannot decrypt",
            actor="position_monitor",
        )

    async def test_get_market_data_reads_async_provider(self, db, monkeypatch):
        class AsyncProvider:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get_bars(self, *_args, **_kwargs):
                return [
                    SimpleNamespace(
                        open=Decimal("99"),
                        high=Decimal("101"),
                        low=Decimal("98"),
                        close=Decimal("100"),
                        volume=Decimal("1000"),
                    )
                ]

        monkeypatch.setattr("app.market_data.get_live_provider", lambda: AsyncProvider())

        bars, current_price = await PositionMonitor(db)._get_market_data("AAPL")

        assert len(bars) == 1
        assert current_price == Decimal("100")

    async def test_get_market_data_reads_sync_provider(self, db, monkeypatch):
        class SyncProvider:
            def get_ohlcv(self, *_args, **_kwargs):
                return [
                    {
                        "open": "49",
                        "high": "51",
                        "low": "48",
                        "close": "50",
                        "volume": "500",
                    }
                ]

        monkeypatch.setattr("app.market_data.get_live_provider", lambda: SyncProvider())

        bars, current_price = await PositionMonitor(db)._get_market_data("MSFT")

        assert len(bars) == 1
        assert current_price == Decimal("50")

    async def test_get_market_data_returns_empty_on_provider_error(self, db, monkeypatch):
        def broken_provider():
            raise RuntimeError("provider down")

        monkeypatch.setattr("app.market_data.get_live_provider", broken_provider)

        bars, current_price = await PositionMonitor(db)._get_market_data("TSLA")

        assert bars == []
        assert current_price == Decimal("0")


# ── _check_daily_loss_with_unrealized ────────────────────────────────────────


class TestCheckDailyLoss:
    async def test_no_risk_profile_returns_false(self, db):
        # No RiskProfile in DB
        monitor = PositionMonitor(db)
        app_settings = AppSettings(
            id=1,
            theme="dark",
            timezone="UTC",
            auto_trading_enabled=True,
            kill_switch_active=False,
            live_trading_unlocked=False,
        )
        broker = _FakeBroker(positions=[])
        result = await monitor._check_daily_loss_with_unrealized(
            app_settings, broker, Decimal("10000")
        )
        assert result is _DailyLossOutcome.NO_BREACH

    async def test_zero_account_value_returns_false(self, db):
        await _make_risk_profile(db)
        monitor = PositionMonitor(db)
        app_settings = MagicMock()
        broker = _FakeBroker(positions=[])
        result = await monitor._check_daily_loss_with_unrealized(app_settings, broker, Decimal("0"))
        assert result is _DailyLossOutcome.NO_BREACH

    async def test_breach_returns_true(self, db):
        await _make_risk_profile(db, max_daily_loss_pct=3.0)
        monitor = PositionMonitor(db)
        app_settings = MagicMock()
        # Unrealized loss = -500 on 10k account = 5% → exceeds 3%
        broker = _FakeBroker(positions=[{"ppl": -500}])
        result = await monitor._check_daily_loss_with_unrealized(
            app_settings, broker, Decimal("10000")
        )
        assert result is _DailyLossOutcome.BREACH_KILL_SWITCH

    async def test_within_limit_returns_false(self, db):
        await _make_risk_profile(db, max_daily_loss_pct=3.0)
        monitor = PositionMonitor(db)
        app_settings = MagicMock()
        # Unrealized loss = -200 on 10k = 2% → within 3%
        broker = _FakeBroker(positions=[{"ppl": -200}])
        result = await monitor._check_daily_loss_with_unrealized(
            app_settings, broker, Decimal("10000")
        )
        assert result is _DailyLossOutcome.NO_BREACH

    async def test_positive_pnl_returns_false(self, db):
        await _make_risk_profile(db, max_daily_loss_pct=3.0)
        monitor = PositionMonitor(db)
        app_settings = MagicMock()
        # Profit — no loss
        broker = _FakeBroker(positions=[{"ppl": 300}])
        result = await monitor._check_daily_loss_with_unrealized(
            app_settings, broker, Decimal("10000")
        )
        assert result is _DailyLossOutcome.NO_BREACH

    async def test_broker_error_defaults_to_fail_closed_block(self, db, monkeypatch):
        await _make_risk_profile(db, max_daily_loss_pct=3.0)
        monkeypatch.setattr(
            settings,
            "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
            "block_trading",
        )
        monitor = PositionMonitor(db)
        app_settings = MagicMock()
        # Broker raises — default policy blocks trading instead of assuming zero
        broker = _FakeBroker(raise_on_fetch=RuntimeError("no data"))
        result = await monitor._check_daily_loss_with_unrealized(
            app_settings, broker, Decimal("10000")
        )
        assert result is _DailyLossOutcome.POLICY_BLOCK_TRADING


# ── eod_flatten ───────────────────────────────────────────────────────────────


class TestEodFlatten:
    async def test_no_broker_returns_early(self, db):
        monitor = PositionMonitor(db)
        with patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=None):
            result = await monitor.eod_flatten()
        assert result == {"flattened": 0, "reason": "no_broker"}

    async def test_no_positions_flattens_zero(self, db):
        broker = _FakeBroker(positions=[], account={"total": 10_000})
        monitor = PositionMonitor(db)
        with (
            patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=broker),
            patch("app.services.position_monitor.ExecutionEngine") as MockEngine,
        ):
            instance = MockEngine.return_value
            instance.create_order_intent = AsyncMock()
            instance.submit_order = AsyncMock()
            result = await monitor.eod_flatten()
        assert result["flattened"] == 0
        instance.create_order_intent.assert_not_called()

    async def test_flattens_positive_qty_positions(self, db):
        positions = [
            {"ticker": "AAPL", "quantity": 10, "currentPrice": 150.0, "averagePrice": 148.0},
            {"ticker": "TSLA", "quantity": 5, "currentPrice": 200.0, "averagePrice": 195.0},
            {"ticker": "ZERO", "quantity": 0, "currentPrice": 50.0},  # skipped
        ]
        broker = _FakeBroker(positions=positions, account={"total": 10_000})

        mock_order = MagicMock()
        mock_order.id = uuid.uuid4()

        monitor = PositionMonitor(db)
        with (
            patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=broker),
            patch("app.services.position_monitor.ExecutionEngine") as MockEngine,
        ):
            instance = MockEngine.return_value
            instance.create_order_intent = AsyncMock(return_value=mock_order)
            instance.submit_order = AsyncMock(return_value=mock_order)
            result = await monitor.eod_flatten()

        assert result["flattened"] == 2  # AAPL + TSLA, ZERO skipped
        assert instance.create_order_intent.call_count == 2

    async def test_eod_flatten_writes_audit_log(self, db):
        from sqlalchemy import select

        from app.db.models import AuditLog

        broker = _FakeBroker(
            positions=[{"ticker": "SPY", "quantity": 3, "currentPrice": 500.0}],
            account={"total": 10_000},
        )
        mock_order = MagicMock()
        mock_order.id = uuid.uuid4()

        monitor = PositionMonitor(db)
        with (
            patch.object(monitor, "_get_broker", new_callable=AsyncMock, return_value=broker),
            patch("app.services.position_monitor.ExecutionEngine") as MockEngine,
        ):
            instance = MockEngine.return_value
            instance.create_order_intent = AsyncMock(return_value=mock_order)
            instance.submit_order = AsyncMock(return_value=mock_order)
            await monitor.eod_flatten()

        await db.commit()
        logs = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "eod_flatten_executed")))
            .scalars()
            .all()
        )
        assert len(logs) == 1
        assert logs[0].payload["flattened"] == 1
