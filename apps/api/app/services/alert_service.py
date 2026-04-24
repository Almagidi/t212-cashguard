"""
Alert service.
Delivers alerts to in-app DB, email (SMTP), and Telegram.
All channels are optional — configured via environment variables.
"""
from __future__ import annotations

import smtplib
import ssl
import uuid
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any

import structlog

from app.core.config import settings
from app.db.models import Alert

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

# Severity → emoji for notifications
SEVERITY_EMOJI = {
    "info": "INFO",
    "warning": "⚠️",
    "error": "🔴",
    "critical": "🚨",
}


class AlertService:
    """
    Send alerts to all configured channels simultaneously.
    Channels are silently skipped when not configured.
    DB storage always happens regardless of other channel failures.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def send(
        self,
        *,
        alert_type: str,
        title: str,
        message: str,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> Alert:
        """
        Persist alert and dispatch to all configured channels.
        Returns the persisted Alert record.
        """
        alert = Alert(
            id=uuid.uuid4(),
            alert_type=alert_type,
            channel="in_app",
            title=title,
            message=message,
            severity=severity,
            is_read=False,
            payload=payload,
            created_at=datetime.now(UTC),
        )
        self.db.add(alert)
        await self.db.flush()

        # Fire-and-forget external channels — don't fail the request on delivery errors
        if settings.ALERT_EMAIL_TO and settings.SMTP_HOST:
            try:
                self._send_email(title, message, severity)
                log.info("alert.email_sent", alert_id=str(alert.id))
            except Exception as exc:
                log.warning("alert.email_failed", error=str(exc))

        if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
            try:
                await self._send_telegram(title, message, severity)
                log.info("alert.telegram_sent", alert_id=str(alert.id))
            except Exception as exc:
                log.warning("alert.telegram_failed", error=str(exc))

        if settings.DISCORD_WEBHOOK_URL:
            try:
                await self._send_discord(title, message, severity)
                log.info("alert.discord_sent", alert_id=str(alert.id))
            except Exception as exc:
                log.warning("alert.discord_failed", error=str(exc))

        if settings.SLACK_WEBHOOK_URL:
            try:
                await self._send_slack(title, message, severity)
                log.info("alert.slack_sent", alert_id=str(alert.id))
            except Exception as exc:
                log.warning("alert.slack_failed", error=str(exc))

        return alert

    def _send_email(self, title: str, message: str, severity: str) -> None:
        """Send email alert via SMTP."""
        emoji = SEVERITY_EMOJI.get(severity, "")
        subject = f"{emoji} CashGuard [{severity.upper()}]: {title}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.ALERT_EMAIL_FROM or settings.SMTP_USER
        msg["To"] = settings.ALERT_EMAIL_TO

        text_body = f"""
T212 CashGuard Trader Alert
===========================
Type:     {title}
Severity: {severity.upper()}
Time:     {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}

{message}
        """.strip()

        html_body = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto">
<div style="background:#1a1a2e;color:#e2e8f0;padding:24px;border-radius:8px">
  <h2 style="margin:0 0 16px;color:#{'ef4444' if severity in ('error','critical') else 'f59e0b' if severity == 'warning' else '60a5fa'}">
    {emoji} {title}
  </h2>
  <p style="color:#94a3b8;margin:0 0 8px">Severity: <strong>{severity.upper()}</strong></p>
  <p style="color:#94a3b8;margin:0 0 16px">Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
  <div style="background:#0f172a;padding:16px;border-radius:6px;color:#e2e8f0">{message}</div>
  <p style="color:#475569;font-size:12px;margin:16px 0 0">
    T212 CashGuard Trader — Cash-Only Mode
  </p>
</div>
</body></html>
        """

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        context = ssl.create_default_context()
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=context)
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(
                msg["From"],
                [settings.ALERT_EMAIL_TO],
                msg.as_string(),
            )

    async def _send_telegram(self, title: str, message: str, severity: str) -> None:
        """Send Telegram message via Bot API."""
        import httpx

        emoji = SEVERITY_EMOJI.get(severity, "")
        text = (
            f"{emoji} *{title}*\n"
            f"Severity: `{severity.upper()}`\n"
            f"Time: `{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
            f"{message}"
        )
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={
                "chat_id": settings.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })
            resp.raise_for_status()

    async def _send_discord(self, title: str, message: str, severity: str) -> None:
        """Send Discord embed via webhook."""
        import httpx

        emoji = SEVERITY_EMOJI.get(severity, "")
        color_map = {"info": 0x3B82F6, "warning": 0xF59E0B, "error": 0xEF4444, "critical": 0xDC2626}
        color = color_map.get(severity, 0x6B7280)

        payload = {
            "embeds": [{
                "title": f"{emoji} {title}",
                "description": message,
                "color": color,
                "footer": {"text": "T212 CashGuard Trader"},
                "timestamp": datetime.now(UTC).isoformat(),
                "fields": [{"name": "Severity", "value": severity.upper(), "inline": True}],
            }]
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.DISCORD_WEBHOOK_URL, json=payload)
            resp.raise_for_status()

    async def _send_slack(self, title: str, message: str, severity: str) -> None:
        """Send Slack message via incoming webhook."""
        import httpx

        emoji = SEVERITY_EMOJI.get(severity, "")
        color_map = {"info": "#3B82F6", "warning": "#F59E0B", "error": "#EF4444", "critical": "#DC2626"}
        color = color_map.get(severity, "#6B7280")

        payload = {
            "attachments": [{
                "color": color,
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": message}},
                    {"type": "context", "elements": [
                        {"type": "mrkdwn", "text": f"*Severity:* {severity.upper()}  |  *Time:* {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"}
                    ]},
                ],
            }]
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.SLACK_WEBHOOK_URL, json=payload)
            resp.raise_for_status()


# ── Convenience helpers called from risk engine + workers ────────────────────

async def alert_kill_switch_activated(db: AsyncSession, actor: str) -> None:
    svc = AlertService(db)
    await svc.send(
        alert_type="kill_switch",
        title="Kill Switch Activated",
        message=f"Kill switch was activated by {actor}. All automated trading is halted.",
        severity="critical",
        payload={"actor": actor},
    )


async def alert_order_failed(
    db: AsyncSession, ticker: str, error: str
) -> None:
    svc = AlertService(db)
    await svc.send(
        alert_type="order_failed",
        title=f"Order Failed: {ticker}",
        message=f"An order for {ticker} failed to submit: {error}",
        severity="error",
        payload={"ticker": ticker, "error": error},
    )


async def alert_daily_loss_breach(
    db: AsyncSession, loss_pct: float, limit_pct: float
) -> None:
    svc = AlertService(db)
    await svc.send(
        alert_type="daily_loss_breach",
        title="Daily Loss Limit Breached",
        message=(
            f"Daily loss of {loss_pct:.2f}% has exceeded the limit of {limit_pct:.2f}%. "
            f"No further trades will be placed today."
        ),
        severity="critical",
        payload={"loss_pct": loss_pct, "limit_pct": limit_pct},
    )


async def alert_broker_disconnected(db: AsyncSession) -> None:
    svc = AlertService(db)
    await svc.send(
        alert_type="broker_disconnected",
        title="Broker Connection Lost",
        message="The circuit breaker has opened due to repeated broker API failures. "
                "Kill switch activated automatically. Check broker status.",
        severity="critical",
    )


async def alert_stale_data(db: AsyncSession, ticker: str, age_seconds: int) -> None:
    svc = AlertService(db)
    await svc.send(
        alert_type="stale_data",
        title=f"Stale Market Data: {ticker}",
        message=f"Market data for {ticker} is {age_seconds}s old. Strategy signals paused.",
        severity="warning",
        payload={"ticker": ticker, "age_seconds": age_seconds},
    )


async def alert_trade_submitted(
    db: AsyncSession,
    strategy_name: str,
    ticker: str,
    side: str,
    qty: float,
    price: float,
    order_type: str = "limit",
    confidence: float | None = None,
    reason: str = "",
) -> None:
    """Fire when a live order is successfully placed."""
    svc = AlertService(db)
    direction = "🟢 BUY" if side == "buy" else "🔴 SELL"
    conf_str = f"  |  Confidence: {confidence:.0%}" if confidence else ""
    await svc.send(
        alert_type="trade_submitted",
        title=f"Trade Placed: {direction} {ticker}",
        message=(
            f"Strategy: {strategy_name}\n"
            f"Ticker:   {ticker}\n"
            f"Side:     {side.upper()}\n"
            f"Qty:      {qty}\n"
            f"Price:    ${price:.2f}  ({order_type}){conf_str}\n"
            f"Signal:   {reason}"
        ),
        severity="info",
        payload={
            "strategy": strategy_name, "ticker": ticker,
            "side": side, "qty": qty, "price": price,
            "order_type": order_type, "confidence": confidence, "reason": reason,
        },
    )


async def alert_stop_out(
    db: AsyncSession,
    strategy_name: str,
    ticker: str,
    exit_price: float,
    entry_price: float,
    pnl: float | None = None,
) -> None:
    """Fire when a position is stopped out."""
    svc = AlertService(db)
    pnl_str = f"  |  P&L: ${pnl:+.2f}" if pnl is not None else ""
    await svc.send(
        alert_type="stop_out",
        title=f"Stop-Out: {ticker}",
        message=(
            f"Strategy: {strategy_name}\n"
            f"Ticker:   {ticker}\n"
            f"Exit:     ${exit_price:.2f} (stop hit)\n"
            f"Entry:    ${entry_price:.2f}{pnl_str}"
        ),
        severity="warning",
        payload={
            "strategy": strategy_name, "ticker": ticker,
            "exit_price": exit_price, "entry_price": entry_price, "pnl": pnl,
        },
    )


async def alert_take_profit(
    db: AsyncSession,
    strategy_name: str,
    ticker: str,
    exit_price: float,
    entry_price: float,
    pnl: float | None = None,
    signal_type: str = "take_profit",
) -> None:
    """Fire when a take-profit (full or partial) is hit."""
    svc = AlertService(db)
    label = "Partial Exit" if signal_type == "partial_exit" else "Take Profit"
    pnl_str = f"  |  P&L: ${pnl:+.2f}" if pnl is not None else ""
    await svc.send(
        alert_type="take_profit",
        title=f"{label}: {ticker}",
        message=(
            f"Strategy: {strategy_name}\n"
            f"Ticker:   {ticker}\n"
            f"Exit:     ${exit_price:.2f} ({label.lower()})\n"
            f"Entry:    ${entry_price:.2f}{pnl_str}"
        ),
        severity="info",
        payload={
            "strategy": strategy_name, "ticker": ticker,
            "exit_price": exit_price, "entry_price": entry_price,
            "pnl": pnl, "signal_type": signal_type,
        },
    )


async def alert_daily_summary(
    db: AsyncSession,
    date_str: str,
    total_trades: int,
    orders_submitted: int,
    risk_blocks: int,
    errors: list[str],
) -> None:
    """End-of-day runner summary alert."""
    svc = AlertService(db)
    error_str = "\n".join(f"  • {e}" for e in errors) if errors else "  None"
    severity = "warning" if errors else "info"
    await svc.send(
        alert_type="daily_summary",
        title=f"Daily Summary — {date_str}",
        message=(
            f"Signals:   {total_trades}\n"
            f"Orders:    {orders_submitted}\n"
            f"Blocked:   {risk_blocks}\n"
            f"Errors:\n{error_str}"
        ),
        severity=severity,
        payload={
            "date": date_str,
            "signals": total_trades,
            "orders": orders_submitted,
            "blocks": risk_blocks,
            "errors": errors,
        },
    )


async def alert_feed_health_issue(
    db: AsyncSession,
    *,
    status: str,
    provider: str,
    detail: str,
    affected_symbols: list[str],
) -> None:
    svc = AlertService(db)
    await svc.send(
        alert_type="feed_health_issue",
        title=f"Market Data {status.title()}",
        message=(
            f"Provider: {provider or 'unknown'}\n"
            f"Status:   {status}\n"
            f"Symbols:  {', '.join(affected_symbols) if affected_symbols else 'market-wide'}\n"
            f"Detail:   {detail}"
        ),
        severity="critical" if status in {"stale", "error"} else "warning",
        payload={
            "status": status,
            "provider": provider,
            "detail": detail,
            "affected_symbols": affected_symbols,
        },
    )


async def alert_feed_health_recovered(
    db: AsyncSession,
    *,
    provider: str,
) -> None:
    svc = AlertService(db)
    await svc.send(
        alert_type="feed_health_recovered",
        title="Market Data Recovered",
        message=f"Feed validation returned to a healthy state on {provider or 'the active provider'}.",
        severity="info",
        payload={"provider": provider},
    )


async def alert_regime_shift(
    db: AsyncSession,
    *,
    previous_regime: str | None,
    current_regime: str,
    detail: str,
    suppressed_strategies: list[str],
) -> None:
    svc = AlertService(db)
    await svc.send(
        alert_type="regime_shift",
        title="Market Regime Shift",
        message=(
            f"Previous: {previous_regime or 'unknown'}\n"
            f"Current:  {current_regime}\n"
            f"Suppressed: {', '.join(suppressed_strategies) if suppressed_strategies else 'none'}\n"
            f"Detail: {detail}"
        ),
        severity="warning" if current_regime in {"risk_off", "high_volatility"} else "critical" if current_regime == "unsafe" else "info",
        payload={
            "previous_regime": previous_regime,
            "current_regime": current_regime,
            "detail": detail,
            "suppressed_strategies": suppressed_strategies,
        },
    )
