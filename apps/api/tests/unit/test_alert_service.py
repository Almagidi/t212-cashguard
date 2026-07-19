"""
Unit tests for alert_service.py.
Covers DB persistence, channel routing, failure isolation, and all helpers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from app.db.models import Alert
from app.services.alert_service import (
    AlertService,
    alert_abnormal_slippage,
    alert_broker_disconnected,
    alert_daily_loss_breach,
    alert_daily_summary,
    alert_feed_health_issue,
    alert_feed_health_recovered,
    alert_kill_switch_activated,
    alert_order_failed,
    alert_regime_shift,
    alert_stale_data,
    alert_stop_out,
    alert_take_profit,
    alert_trade_submitted,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _no_channels_ctx():
    """Context manager that disables all external channels."""
    return patch(
        "app.services.alert_service.settings",
        **{
            "ALERT_EMAIL_TO": "",
            "SMTP_HOST": "",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "DISCORD_WEBHOOK_URL": "",
            "SLACK_WEBHOOK_URL": "",
        },
    )


def _all_channels_ctx():
    return patch(
        "app.services.alert_service.settings",
        **{
            "ALERT_EMAIL_TO": "admin@example.com",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": 587,
            "SMTP_USER": "user@example.com",
            "SMTP_PASSWORD": "password",
            "ALERT_EMAIL_FROM": "from@example.com",
            "TELEGRAM_BOT_TOKEN": "bot123",
            "TELEGRAM_CHAT_ID": "999",
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test",
            "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/test",
        },
    )


def _mock_smtp_server():
    srv = MagicMock()
    srv.__enter__ = MagicMock(return_value=srv)
    srv.__exit__ = MagicMock(return_value=False)
    return srv


def _mock_httpx_resp():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    return resp


# ── AlertService.send — DB persistence ───────────────────────────────────────


class TestAlertServiceDB:
    async def test_alert_persisted_to_db(self, db):
        with _no_channels_ctx():
            svc = AlertService(db)
            alert = await svc.send(
                alert_type="test_type",
                title="Test Alert",
                message="Test message",
                severity="info",
            )
        await db.commit()

        saved = (await db.execute(select(Alert).where(Alert.id == alert.id))).scalar_one()
        assert saved.alert_type == "test_type"
        assert saved.title == "Test Alert"
        assert saved.message == "Test message"
        assert saved.severity == "info"
        assert saved.is_read is False

    async def test_returns_alert_with_payload(self, db):
        with _no_channels_ctx():
            svc = AlertService(db)
            alert = await svc.send(
                alert_type="order_failed",
                title="Fail",
                message="Msg",
                payload={"ticker": "AAPL"},
            )
        assert isinstance(alert, Alert)
        assert alert.payload == {"ticker": "AAPL"}

    async def test_db_persists_even_when_all_channels_fail(self, db):
        with _all_channels_ctx():
            import httpx

            with (
                patch("smtplib.SMTP", side_effect=ConnectionRefusedError("no server")),
                patch.object(
                    httpx.AsyncClient,
                    "post",
                    new_callable=AsyncMock,
                    side_effect=Exception("network error"),
                ),
            ):
                svc = AlertService(db)
                alert = await svc.send(alert_type="test", title="T", message="M")
        assert alert is not None
        assert alert.id is not None


# ── Email channel ─────────────────────────────────────────────────────────────


class TestEmailChannel:
    async def test_skipped_when_email_not_configured(self, db):
        with (
            _no_channels_ctx(),
            patch("smtplib.SMTP") as mock_smtp,
        ):
            svc = AlertService(db)
            await svc.send(alert_type="t", title="T", message="M")
            mock_smtp.assert_not_called()

    async def test_skipped_when_smtp_host_missing(self, db):
        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "a@b.com",
                    "SMTP_HOST": "",  # missing
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                    "DISCORD_WEBHOOK_URL": "",
                    "SLACK_WEBHOOK_URL": "",
                },
            ),
            patch("smtplib.SMTP") as mock_smtp,
        ):
            svc = AlertService(db)
            await svc.send(alert_type="t", title="T", message="M")
            mock_smtp.assert_not_called()

    async def test_fires_when_fully_configured(self, db):
        srv = _mock_smtp_server()
        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "a@b.com",
                    "SMTP_HOST": "smtp.example.com",
                    "SMTP_PORT": 587,
                    "SMTP_USER": "u",
                    "SMTP_PASSWORD": "p",
                    "ALERT_EMAIL_FROM": "f@example.com",
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                    "DISCORD_WEBHOOK_URL": "",
                    "SLACK_WEBHOOK_URL": "",
                },
            ),
            patch("smtplib.SMTP", return_value=srv),
        ):
            svc = AlertService(db)
            await svc.send(alert_type="t", title="Title", message="Msg", severity="error")
        srv.sendmail.assert_called_once()

    async def test_failure_does_not_propagate(self, db):
        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "a@b.com",
                    "SMTP_HOST": "smtp.example.com",
                    "SMTP_PORT": 587,
                    "SMTP_USER": "u",
                    "SMTP_PASSWORD": "p",
                    "ALERT_EMAIL_FROM": "f@example.com",
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                    "DISCORD_WEBHOOK_URL": "",
                    "SLACK_WEBHOOK_URL": "",
                },
            ),
            patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")),
        ):
            svc = AlertService(db)
            alert = await svc.send(alert_type="t", title="T", message="M")
        assert alert is not None

    async def test_subject_contains_severity(self, db):
        subjects: list[str] = []

        def capture_sendmail(from_addr, to_addrs, msg_string):
            import re

            m = re.search(r"Subject: (.+)", msg_string)
            if m:
                subjects.append(m.group(1))

        srv = _mock_smtp_server()
        srv.sendmail.side_effect = capture_sendmail

        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "a@b.com",
                    "SMTP_HOST": "smtp.example.com",
                    "SMTP_PORT": 587,
                    "SMTP_USER": "u",
                    "SMTP_PASSWORD": "p",
                    "ALERT_EMAIL_FROM": "f@example.com",
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                    "DISCORD_WEBHOOK_URL": "",
                    "SLACK_WEBHOOK_URL": "",
                },
            ),
            patch("smtplib.SMTP", return_value=srv),
        ):
            svc = AlertService(db)
            await svc.send(alert_type="t", title="T", message="M", severity="critical")

        assert subjects
        # Subject may be MIME-encoded (base64) due to emoji — decode before checking
        from email.header import decode_header

        decoded_parts = decode_header(subjects[0])
        decoded = "".join(
            part.decode(enc or "utf-8") if isinstance(part, bytes) else part
            for part, enc in decoded_parts
        )
        assert "CRITICAL" in decoded


# ── Telegram channel ──────────────────────────────────────────────────────────


class TestTelegramChannel:
    async def test_skipped_when_chat_id_empty(self, db):
        import httpx

        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "",
                    "SMTP_HOST": "",
                    "TELEGRAM_BOT_TOKEN": "bot",
                    "TELEGRAM_CHAT_ID": "",  # falsy → skip
                    "DISCORD_WEBHOOK_URL": "",
                    "SLACK_WEBHOOK_URL": "",
                },
            ),
            patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mp,
        ):
            svc = AlertService(db)
            await svc.send(alert_type="t", title="T", message="M")
            mp.assert_not_called()

    async def test_fires_when_configured(self, db):
        import httpx

        resp = _mock_httpx_resp()
        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "",
                    "SMTP_HOST": "",
                    "TELEGRAM_BOT_TOKEN": "bot123",
                    "TELEGRAM_CHAT_ID": "999",
                    "DISCORD_WEBHOOK_URL": "",
                    "SLACK_WEBHOOK_URL": "",
                },
            ),
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
            ) as mp,
        ):
            svc = AlertService(db)
            await svc.send(alert_type="t", title="T", message="M", severity="critical")
            mp.assert_called_once()
            url = mp.call_args[0][0]
            assert "sendMessage" in url

    async def test_failure_does_not_propagate(self, db):
        import httpx

        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "",
                    "SMTP_HOST": "",
                    "TELEGRAM_BOT_TOKEN": "bot123",
                    "TELEGRAM_CHAT_ID": "999",
                    "DISCORD_WEBHOOK_URL": "",
                    "SLACK_WEBHOOK_URL": "",
                },
            ),
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=Exception("timeout")
            ),
        ):
            svc = AlertService(db)
            alert = await svc.send(alert_type="t", title="T", message="M")
        assert alert is not None


# ── Discord channel ───────────────────────────────────────────────────────────


class TestDiscordChannel:
    async def test_fires_with_embed_payload(self, db):
        import httpx

        resp = _mock_httpx_resp()
        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "",
                    "SMTP_HOST": "",
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test",
                    "SLACK_WEBHOOK_URL": "",
                },
            ),
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
            ) as mp,
        ):
            svc = AlertService(db)
            await svc.send(alert_type="t", title="T", message="M", severity="warning")
            mp.assert_called_once()
            assert "embeds" in mp.call_args[1]["json"]

    async def test_skipped_when_not_configured(self, db):
        import httpx

        with (
            _no_channels_ctx(),
            patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mp,
        ):
            svc = AlertService(db)
            await svc.send(alert_type="t", title="T", message="M")
            mp.assert_not_called()

    async def test_failure_does_not_propagate(self, db):
        import httpx

        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "",
                    "SMTP_HOST": "",
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test",
                    "SLACK_WEBHOOK_URL": "",
                },
            ),
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=Exception("boom")
            ),
        ):
            svc = AlertService(db)
            alert = await svc.send(alert_type="t", title="T", message="M")
        assert alert is not None


# ── Slack channel ─────────────────────────────────────────────────────────────


class TestSlackChannel:
    async def test_fires_with_attachments_payload(self, db):
        import httpx

        resp = _mock_httpx_resp()
        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "",
                    "SMTP_HOST": "",
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                    "DISCORD_WEBHOOK_URL": "",
                    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/test",
                },
            ),
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
            ) as mp,
        ):
            svc = AlertService(db)
            await svc.send(alert_type="t", title="T", message="M", severity="info")
            mp.assert_called_once()
            assert "attachments" in mp.call_args[1]["json"]

    async def test_failure_does_not_propagate(self, db):
        import httpx

        with (
            patch(
                "app.services.alert_service.settings",
                **{
                    "ALERT_EMAIL_TO": "",
                    "SMTP_HOST": "",
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                    "DISCORD_WEBHOOK_URL": "",
                    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/test",
                },
            ),
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=Exception("timeout")
            ),
        ):
            svc = AlertService(db)
            alert = await svc.send(alert_type="t", title="T", message="M")
        assert alert is not None


# ── All channels simultaneously ───────────────────────────────────────────────


class TestAllChannels:
    async def test_three_httpx_calls_one_smtp(self, db):
        import httpx

        srv = _mock_smtp_server()
        resp = _mock_httpx_resp()
        with (
            _all_channels_ctx(),
            patch("smtplib.SMTP", return_value=srv),
            patch.object(
                httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
            ) as mp,
        ):
            svc = AlertService(db)
            alert = await svc.send(alert_type="t", title="T", message="M", severity="critical")
        assert alert is not None
        srv.sendmail.assert_called_once()
        assert mp.call_count == 3  # Telegram + Discord + Slack


# ── Helper functions ──────────────────────────────────────────────────────────


class TestAlertHelpers:
    async def test_alert_kill_switch_activated(self, db):
        with _no_channels_ctx():
            await alert_kill_switch_activated(db, actor="test_runner")
        await db.commit()
        a = (await db.execute(select(Alert).where(Alert.alert_type == "kill_switch"))).scalar_one()
        assert "test_runner" in a.message
        assert a.severity == "critical"

    async def test_alert_order_failed(self, db):
        with _no_channels_ctx():
            await alert_order_failed(db, ticker="AAPL", error="Insufficient funds")
        await db.commit()
        a = (await db.execute(select(Alert).where(Alert.alert_type == "order_failed"))).scalar_one()
        assert "AAPL" in a.title
        assert a.severity == "error"

    async def test_alert_abnormal_slippage_warning(self, db):
        with _no_channels_ctx():
            await alert_abnormal_slippage(
                db,
                order_id="o1",
                ticker="TSLA",
                side="buy",
                expected_price=100.0,
                fill_price=101.0,
                slippage_pct=1.0,
                slippage_value=1.0,
            )
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "abnormal_slippage"))
        ).scalar_one()
        assert a.severity == "warning"  # slippage_pct < 1.5

    async def test_alert_abnormal_slippage_critical(self, db):
        with _no_channels_ctx():
            await alert_abnormal_slippage(
                db,
                order_id="o2",
                ticker="TSLA",
                side="buy",
                expected_price=100.0,
                fill_price=102.0,
                slippage_pct=2.0,
                slippage_value=2.0,
            )
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "abnormal_slippage"))
        ).scalar_one()
        assert a.severity == "critical"  # slippage_pct >= 1.5

    async def test_alert_daily_loss_breach(self, db):
        with _no_channels_ctx():
            await alert_daily_loss_breach(db, loss_pct=4.5, limit_pct=3.0)
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "daily_loss_breach"))
        ).scalar_one()
        assert a.severity == "critical"
        assert "4.50%" in a.message

    async def test_alert_broker_disconnected(self, db):
        with _no_channels_ctx():
            await alert_broker_disconnected(db)
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "broker_disconnected"))
        ).scalar_one()
        assert a.severity == "critical"

    async def test_alert_stale_data(self, db):
        with _no_channels_ctx():
            await alert_stale_data(db, ticker="NVDA", age_seconds=120)
        await db.commit()
        a = (await db.execute(select(Alert).where(Alert.alert_type == "stale_data"))).scalar_one()
        assert a.severity == "warning"
        assert "NVDA" in a.title
        assert a.payload["age_seconds"] == 120

    async def test_alert_trade_submitted_buy(self, db):
        with _no_channels_ctx():
            await alert_trade_submitted(
                db,
                strategy_name="ORB",
                ticker="AMD",
                side="buy",
                qty=10,
                price=150.0,
                confidence=0.85,
                reason="breakout",
            )
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "trade_submitted"))
        ).scalar_one()
        assert "AMD" in a.title
        assert a.severity == "info"

    async def test_alert_trade_submitted_sell(self, db):
        with _no_channels_ctx():
            await alert_trade_submitted(
                db,
                strategy_name="ORB",
                ticker="AMD",
                side="sell",
                qty=10,
                price=155.0,
            )
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "trade_submitted"))
        ).scalar_one()
        assert "SELL" in a.title or "sell" in a.message.lower()

    async def test_alert_stop_out(self, db):
        with _no_channels_ctx():
            await alert_stop_out(
                db,
                strategy_name="ORB",
                ticker="SPY",
                exit_price=100.0,
                entry_price=102.0,
                pnl=-2.0,
            )
        await db.commit()
        a = (await db.execute(select(Alert).where(Alert.alert_type == "stop_out"))).scalar_one()
        assert a.severity == "warning"
        assert "SPY" in a.title

    async def test_alert_take_profit_full(self, db):
        with _no_channels_ctx():
            await alert_take_profit(
                db,
                strategy_name="ORB",
                ticker="AAPL",
                exit_price=110.0,
                entry_price=100.0,
                pnl=10.0,
            )
        await db.commit()
        a = (await db.execute(select(Alert).where(Alert.alert_type == "take_profit"))).scalar_one()
        assert "Take Profit" in a.title
        assert a.severity == "info"

    async def test_alert_take_profit_partial(self, db):
        with _no_channels_ctx():
            await alert_take_profit(
                db,
                strategy_name="ORB",
                ticker="AAPL",
                exit_price=105.0,
                entry_price=100.0,
                pnl=5.0,
                signal_type="partial_exit",
            )
        await db.commit()
        a = (await db.execute(select(Alert).where(Alert.alert_type == "take_profit"))).scalar_one()
        assert "Partial Exit" in a.title

    async def test_alert_daily_summary_clean(self, db):
        with _no_channels_ctx():
            await alert_daily_summary(
                db,
                date_str="2026-04-24",
                total_trades=10,
                orders_submitted=8,
                risk_blocks=2,
                errors=[],
            )
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "daily_summary"))
        ).scalar_one()
        assert a.severity == "info"

    async def test_alert_daily_summary_with_errors(self, db):
        with _no_channels_ctx():
            await alert_daily_summary(
                db,
                date_str="2026-04-24",
                total_trades=5,
                orders_submitted=3,
                risk_blocks=2,
                errors=["order timeout"],
            )
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "daily_summary"))
        ).scalar_one()
        assert a.severity == "warning"

    async def test_alert_feed_health_issue_stale(self, db):
        with _no_channels_ctx():
            await alert_feed_health_issue(
                db,
                status="stale",
                provider="polygon",
                detail="data >5m old",
                affected_symbols=["AAPL", "TSLA"],
            )
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "feed_health_issue"))
        ).scalar_one()
        assert a.severity == "critical"

    async def test_alert_feed_health_issue_degraded(self, db):
        with _no_channels_ctx():
            await alert_feed_health_issue(
                db,
                status="degraded",
                provider="polygon",
                detail="slow",
                affected_symbols=[],
            )
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "feed_health_issue"))
        ).scalar_one()
        assert a.severity == "warning"

    async def test_alert_feed_health_recovered(self, db):
        with _no_channels_ctx():
            await alert_feed_health_recovered(db, provider="polygon")
        await db.commit()
        a = (
            await db.execute(select(Alert).where(Alert.alert_type == "feed_health_recovered"))
        ).scalar_one()
        assert a.severity == "info"

    async def test_alert_regime_shift_risk_off(self, db):
        with _no_channels_ctx():
            await alert_regime_shift(
                db,
                previous_regime="normal",
                current_regime="risk_off",
                detail="VIX spike",
                suppressed_strategies=["ORB"],
            )
        await db.commit()
        a = (await db.execute(select(Alert).where(Alert.alert_type == "regime_shift"))).scalar_one()
        assert a.severity == "warning"

    async def test_alert_regime_shift_unsafe(self, db):
        with _no_channels_ctx():
            await alert_regime_shift(
                db,
                previous_regime="risk_off",
                current_regime="unsafe",
                detail="circuit break",
                suppressed_strategies=["ORB", "VWAP"],
            )
        await db.commit()
        a = (await db.execute(select(Alert).where(Alert.alert_type == "regime_shift"))).scalar_one()
        assert a.severity == "critical"

    async def test_alert_regime_shift_info(self, db):
        with _no_channels_ctx():
            await alert_regime_shift(
                db,
                previous_regime="risk_off",
                current_regime="normal",
                detail="VIX calmed",
                suppressed_strategies=[],
            )
        await db.commit()
        a = (await db.execute(select(Alert).where(Alert.alert_type == "regime_shift"))).scalar_one()
        assert a.severity == "info"
