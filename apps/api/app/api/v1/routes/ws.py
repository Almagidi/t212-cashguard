"""
WebSocket live feed — real-time account, positions, orders, signals, regime.
Clients connect at /v1/ws/live, then send {"type":"auth","token":"<jwt>"} as
the first message (within 5 s).  The token never appears in query params or
Nginx access logs.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError
from sqlalchemy import select

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.models import (
    AppSettings,
    BrokerAccountSnapshot,
    Order,
    PositionSnapshot,
    RiskProfile,
    Signal,
    User,
)
from app.db.session import AsyncSessionLocal
from app.services.market_regime import MarketRegimeService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["websocket"])
log = structlog.get_logger()

# Seconds between application-level ping frames sent to the client.
_HEARTBEAT_INTERVAL = 30
# Seconds the server waits for any client message before declaring the
# connection dead. Must be > 2 * _HEARTBEAT_INTERVAL so a single missed
# pong doesn't trigger a false positive.
_HEARTBEAT_TIMEOUT = 65
# Seconds the server waits for the initial auth message after accepting.
_AUTH_TIMEOUT = 5


# ── Connection manager ────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self._active: dict[str, WebSocket] = {}

    async def connect(self, ws: WebSocket, client_id: str) -> None:
        # Caller must already have called ws.accept() (done before auth).
        self._active[client_id] = ws
        log.info("ws.connected", client=client_id, total=len(self._active))

    def disconnect(self, client_id: str) -> None:
        self._active.pop(client_id, None)
        log.info("ws.disconnected", client=client_id, total=len(self._active))

    async def send(self, ws: WebSocket, data: dict) -> bool:
        try:
            await ws.send_text(json.dumps(data, default=_json_serial))
            return True
        except Exception:
            return False

    @property
    def count(self) -> int:
        return len(self._active)


manager = ConnectionManager()


def _json_serial(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    raise TypeError(f"Not serializable: {type(obj)}")


# ── Auth helpers ──────────────────────────────────────────────────────────────

async def _auth_first_message(websocket: WebSocket, db: AsyncSession) -> User | None:
    """Await the mandatory first-message auth frame; close 4001 on any failure."""
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=_AUTH_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            await websocket.close(code=4001, reason="auth_timeout")
        return None
    except Exception:
        with contextlib.suppress(Exception):
            await websocket.close(code=4001, reason="auth_error")
        return None

    if not isinstance(raw, dict) or raw.get("type") != "auth":
        with contextlib.suppress(Exception):
            await websocket.close(code=4001, reason="expected_auth_message")
        return None

    user = await _authenticate(raw.get("token"), db)
    if not user:
        with contextlib.suppress(Exception):
            await websocket.close(code=4001, reason="Unauthorized")
        return None
    return user


async def _authenticate(token: str | None, db: AsyncSession) -> User | None:
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user_id_raw = payload.get("sub", "")
        if not user_id_raw:
            return None
        user_id = uuid.UUID(str(user_id_raw))
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        return user if user and user.is_active else None
    except (JWTError, Exception):
        return None


# ── Live data assembler ───────────────────────────────────────────────────────

async def _build_payload(db: AsyncSession) -> dict:
    """Assemble the live snapshot from the database (or mock data)."""

    if settings.APP_MODE == "mock":
        return await _mock_payload()

    # Real DB queries
    snap_result = await db.execute(
        select(BrokerAccountSnapshot).order_by(BrokerAccountSnapshot.snapshotted_at.desc()).limit(1)
    )
    snap = snap_result.scalar_one_or_none()

    positions_result = await db.execute(select(PositionSnapshot))
    positions = positions_result.scalars().all()

    signals_result = await db.execute(
        select(Signal).order_by(Signal.generated_at.desc()).limit(6)
    )
    signals = signals_result.scalars().all()

    orders_result = await db.execute(
        select(Order).order_by(Order.created_at.desc()).limit(8)
    )
    orders = orders_result.scalars().all()

    settings_result = await db.execute(select(AppSettings).limit(1))
    app_settings = settings_result.scalar_one_or_none()

    risk_result = await db.execute(select(RiskProfile).where(RiskProfile.is_default).limit(1))
    risk = risk_result.scalar_one_or_none()

    unrealized = sum(float(p.unrealized_pnl or 0) for p in positions)

    return {
        "type": "snapshot",
        "ts": datetime.now(UTC).isoformat(),
        "account": {
            "total_value": float(snap.total_value) if snap else 0,
            "free_cash": float(snap.free_funds) if snap else 0,
            "invested": float(snap.invested) if snap else 0,
            "currency": snap.currency if snap else "GBP",
            "unrealized_pnl": unrealized,
        },
        "positions": [
            {
                "ticker": p.ticker,
                "quantity": float(p.quantity),
                "average_price": float(p.avg_price),
                "current_price": float(p.current_price or 0),
                "unrealized_pnl": float(p.unrealized_pnl or 0),
                "market_value": float(p.quantity) * float(p.current_price or p.avg_price),
            }
            for p in positions
        ],
        "signals": [
            {
                "id": str(s.id),
                "ticker": s.ticker,
                "side": s.side,
                "signal_type": s.signal_type,
                "status": s.status,
                "confidence": float(s.confidence or 0),
                "generated_at": s.generated_at.isoformat(),
            }
            for s in signals
        ],
        "orders": [
            {
                "id": str(o.id),
                "ticker": o.ticker,
                "side": o.side,
                "order_type": o.order_type,
                "quantity": float(o.quantity),
                "status": o.status,
                "created_at": o.created_at.isoformat(),
            }
            for o in orders
        ],
        "system": {
            "auto_trading_enabled": app_settings.auto_trading_enabled if app_settings else False,
            "kill_switch_active": app_settings.kill_switch_active if app_settings else False,
            "max_daily_loss_pct": float(risk.max_daily_loss_pct) if risk else 2.0,
        },
        "regime": await MarketRegimeService().evaluate(),
    }


async def _mock_payload() -> dict:
    """Realistic mock data for demo mode."""
    import math
    import time

    t = time.time()
    # Simulate gentle oscillating P&L
    unrealized = round(142.50 + 30 * math.sin(t / 60), 2)
    total_value = round(10_847.30 + unrealized, 2)

    return {
        "type": "snapshot",
        "ts": datetime.now(UTC).isoformat(),
        "account": {
            "total_value": total_value,
            "free_cash": 8_234.80,
            "invested": round(total_value - 8_234.80, 2),
            "currency": "GBP",
            "unrealized_pnl": unrealized,
        },
        "positions": [
            {"ticker": "AAPL", "quantity": 5.0, "average_price": 178.50, "current_price": 181.20, "unrealized_pnl": 13.50, "market_value": 906.00},
            {"ticker": "MSFT", "quantity": 3.0, "average_price": 415.00, "current_price": 421.80, "unrealized_pnl": 20.40, "market_value": 1265.40},
            {"ticker": "NVDA", "quantity": 2.0, "average_price": 880.00, "current_price": 894.50, "unrealized_pnl": 29.00, "market_value": 1789.00},
        ],
        "signals": [
            {"id": str(uuid.uuid4()), "ticker": "AAPL", "side": "buy", "signal_type": "orb_breakout", "status": "executed", "confidence": 0.82, "generated_at": datetime.now(UTC).isoformat()},
            {"id": str(uuid.uuid4()), "ticker": "TSLA", "side": "sell", "signal_type": "vwap_reclaim", "status": "rejected", "confidence": 0.61, "generated_at": datetime.now(UTC).isoformat()},
        ],
        "orders": [
            {"id": str(uuid.uuid4()), "ticker": "AAPL", "side": "buy", "order_type": "limit", "quantity": 5.0, "status": "filled", "created_at": datetime.now(UTC).isoformat()},
            {"id": str(uuid.uuid4()), "ticker": "MSFT", "side": "buy", "order_type": "limit", "quantity": 3.0, "status": "filled", "created_at": datetime.now(UTC).isoformat()},
        ],
        "system": {
            "auto_trading_enabled": True,
            "kill_switch_active": False,
            "max_daily_loss_pct": 2.0,
        },
        "regime": await MarketRegimeService().evaluate(),
    }


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/v1/ws/live")
async def websocket_live(websocket: WebSocket) -> None:
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        user = await _auth_first_message(websocket, db)
    if not user:
        return

    client_id = str(uuid.uuid4())[:8]
    await manager.connect(websocket, client_id)

    try:
        # Send initial snapshot immediately
        async with AsyncSessionLocal() as db:
            payload = await _build_payload(db)
        await manager.send(websocket, payload)

        async def _broadcast_loop() -> None:
            """Push a fresh snapshot to the client every 2 seconds."""
            while True:
                await asyncio.sleep(2)
                async with AsyncSessionLocal() as db:
                    snapshot = await _build_payload(db)
                if not await manager.send(websocket, snapshot):
                    return

        async def _heartbeat_loop() -> None:
            """Send an application-level ping every _HEARTBEAT_INTERVAL seconds."""
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                if not await manager.send(
                    websocket,
                    {"type": "ping", "ts": datetime.now(UTC).isoformat()},
                ):
                    return

        async def _receive_loop() -> None:
            """Drain client messages (pong responses).
            Closes the connection if no message arrives within _HEARTBEAT_TIMEOUT
            seconds — this catches silent TCP half-open drops that the OS never
            surfaces as a clean disconnect.
            """
            while True:
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=_HEARTBEAT_TIMEOUT,
                    )
                    try:
                        data = json.loads(raw)
                        if data.get("type") != "pong":
                            log.debug("ws.unexpected_message", client=client_id, type=data.get("type"))
                    except (json.JSONDecodeError, Exception):
                        pass
                except asyncio.TimeoutError:
                    log.warning("ws.heartbeat_timeout", client=client_id)
                    with contextlib.suppress(Exception):
                        await websocket.close(code=1001, reason="heartbeat_timeout")
                    return
                except Exception:
                    return

        broadcast_task = asyncio.create_task(_broadcast_loop())
        heartbeat_task = asyncio.create_task(_heartbeat_loop())
        receive_task   = asyncio.create_task(_receive_loop())

        try:
            done, pending = await asyncio.wait(
                {broadcast_task, heartbeat_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Propagate any unexpected exception from the finished task.
            for task in done:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    task.result()
        finally:
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("ws.error", client=client_id, error=str(exc))
    finally:
        manager.disconnect(client_id)
