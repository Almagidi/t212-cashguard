"""
Position monitor service.
Runs every 60 seconds (more frequent than signal generation).
Checks all open positions for:
  1. Trailing stop triggers
  2. Take-profit targets
  3. Partial exit conditions
  4. EOD flatten
  5. Daily loss limit breach (including unrealized)

This is the automation core — it fires sell orders without human intervention.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import desc, select

from app.core.config import settings
from app.db.models import (
    AppSettings,
    AuditLog,
    BrokerConnection,
    Signal,
    Strategy,
    Trade,
)
from app.execution.engine import ExecutionEngine
from app.risk.engine import activate_kill_switch
from app.services.alert_service import AlertService, alert_kill_switch_activated
from app.services.broker_connection_recovery import mark_broker_connection_reconnect_required
from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment
from app.strategies.indicators import Bar, atr
from app.strategies.orb_production import OpeningRangeBreakoutStrategy, ORBState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


class _DailyLossOutcome(Enum):
    NO_BREACH = "no_breach"
    BREACH_KILL_SWITCH = "breach_kill_switch"
    POLICY_BLOCK_TRADING = "policy_block_trading"
    POLICY_KILL_SWITCH = "policy_kill_switch"


class PositionMonitor:
    """
    Monitors all open positions and fires automated exit orders.
    Called by Celery every 60 seconds during market hours.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_settings(self) -> AppSettings | None:
        result = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        return result.scalar_one_or_none()

    async def _get_broker(self) -> Any | None:
        if settings.APP_MODE == "mock":
            from app.broker.mock_adapter import MockBrokerAdapter

            return MockBrokerAdapter()
        result = await self.db.execute(
            select(BrokerConnection)
            .where(BrokerConnection.is_active == True)  # noqa: E712
            .where(BrokerConnection.environment == settings.APP_MODE)
            .limit(1)
        )
        conn = result.scalar_one_or_none()
        if not conn:
            return None
        from app.broker.trading212 import Trading212Adapter
        from app.core.security import CredentialDecryptionError, decrypt_field

        try:
            api_key = decrypt_field(conn.api_key_encrypted)
            api_secret = decrypt_field(conn.api_secret_encrypted)
        except CredentialDecryptionError as exc:
            log.error("position_monitor.credentials_invalid", error=str(exc))
            await mark_broker_connection_reconnect_required(
                self.db,
                conn,
                str(exc),
                actor="position_monitor",
            )
            return None
        try:
            require_broker_environment(conn.environment, action="position monitor broker access")
        except SafetyPolicyViolation as exc:
            log.error("position_monitor.broker_policy_block", reason=exc.reason)
            return None
        return Trading212Adapter(
            api_key,
            api_secret,
            conn.environment,
        )

    async def _get_market_data(self, ticker: str) -> tuple[list[Bar], Decimal]:
        """Returns (bars, current_price). Falls back to empty on error."""
        try:
            from app.market_data import get_live_provider

            provider = get_live_provider()

            if hasattr(provider, "__aenter__"):
                async with provider as md:
                    raw_bars = await md.get_bars(ticker, multiplier=5, timespan="minute", limit=50)
                bars = [
                    Bar(open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
                    for b in raw_bars
                ]
            else:
                raw = provider.get_ohlcv(ticker, interval_minutes=5, bars=50)
                bars = [
                    Bar(
                        open=Decimal(str(b["open"])),
                        high=Decimal(str(b["high"])),
                        low=Decimal(str(b["low"])),
                        close=Decimal(str(b["close"])),
                        volume=Decimal(str(b["volume"])),
                    )
                    for b in raw
                ]

            current_price = bars[-1].close if bars else Decimal("0")
            return bars, current_price
        except Exception as exc:
            log.warning("position_monitor.data_error", error=str(exc))
            return [], Decimal("0")

    async def _check_daily_loss_with_unrealized(
        self,
        app_settings: AppSettings,
        broker: Any,
        account_value: Decimal,
    ) -> _DailyLossOutcome:
        """
        Check daily loss including unrealized P&L from open positions.
        Returns the halt/continue outcome for the caller.
        """
        from app.db.models import RiskProfile

        rp_result = await self.db.execute(
            select(RiskProfile).where(RiskProfile.is_default == True)  # noqa: E712
        )
        rp = rp_result.scalar_one_or_none()
        if not rp or account_value <= 0:
            return _DailyLossOutcome.NO_BREACH

        # Get unrealized P&L from broker
        try:
            async with broker as b:
                positions = await b.get_positions()
            unrealized = sum(float(p.get("ppl", 0)) for p in positions)
        except Exception as exc:
            configured_policy = getattr(
                settings,
                "POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY",
                "block_trading",
            )
            policy = str(configured_policy or "block_trading").strip().lower()

            if policy == "assume_zero":
                log.error(
                    "position_monitor.unrealized_pnl_error",
                    error=str(exc),
                    exc_info=True,
                    unrealized_assumed=0.0,
                    failure_policy="assume_zero",
                )
                unrealized = 0.0
            elif policy == "block_trading":
                log.error(
                    "position_monitor.unrealized_pnl_error",
                    error=str(exc),
                    exc_info=True,
                    failure_policy="block_trading",
                    fail_closed=True,
                )
                log.warning(
                    "position_monitor.realized_pnl_skipped",
                    reason="unrealized_pnl_failure_policy_halt",
                    failure_policy="block_trading",
                )
                return _DailyLossOutcome.POLICY_BLOCK_TRADING
            elif policy == "activate_kill_switch":
                log.error(
                    "position_monitor.unrealized_pnl_error",
                    error=str(exc),
                    exc_info=True,
                    failure_policy="activate_kill_switch",
                    fail_closed=True,
                    kill_switch_activated=True,
                )
                await activate_kill_switch(self.db, actor="position_monitor:unrealized_pnl_failure")
                await alert_kill_switch_activated(
                    self.db,
                    actor="position_monitor:unrealized_pnl_failure",
                )
                log.warning(
                    "position_monitor.realized_pnl_skipped",
                    reason="unrealized_pnl_failure_policy_halt",
                    failure_policy="activate_kill_switch",
                )
                return _DailyLossOutcome.POLICY_KILL_SWITCH
            else:
                log.error(
                    "position_monitor.unrealized_pnl_failure_policy_invalid",
                    configured_policy=configured_policy,
                    fallback_policy="block_trading",
                    fail_closed=True,
                    error=str(exc),
                    exc_info=True,
                )
                log.warning(
                    "position_monitor.realized_pnl_skipped",
                    reason="unrealized_pnl_failure_policy_halt",
                    failure_policy="block_trading",
                )
                return _DailyLossOutcome.POLICY_BLOCK_TRADING

        # Get today's realized P&L from closed trades; negative values mean losses.
        from sqlalchemy import func

        # Daily loss uses the UTC day boundary, matching existing behavior.
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.db.execute(
            select(func.sum(Trade.realized_pnl)).where(
                Trade.closed_at >= today,
                Trade.is_dry_run == False,  # noqa: E712
                Trade.realized_pnl.isnot(None),
            )
        )
        realized = float(result.scalar_one() or 0)

        total_loss = unrealized + realized
        if total_loss < 0:
            loss_pct = abs(total_loss) / float(account_value) * 100
            if loss_pct >= float(rp.max_daily_loss_pct):
                log.warning(
                    "position_monitor.daily_loss_breach",
                    loss_pct=loss_pct,
                    limit=float(rp.max_daily_loss_pct),
                )
                return _DailyLossOutcome.BREACH_KILL_SWITCH

        return _DailyLossOutcome.NO_BREACH

    async def run(self) -> dict[str, Any]:
        """
        Main monitoring loop.
        Returns summary dict for logging.
        """
        summary: dict[str, Any] = {
            "positions_checked": 0,
            "exits_submitted": 0,
            "partial_exits": 0,
            "stops_hit": 0,
            "take_profits": 0,
            "errors": [],
        }

        app_settings = await self._get_settings()
        if not app_settings:
            return summary

        # Hard gates
        if app_settings.kill_switch_active:
            return {**summary, "skipped": "kill_switch_active"}
        if not app_settings.auto_trading_enabled:
            return {**summary, "skipped": "auto_trading_disabled"}

        broker = await self._get_broker()
        if broker is None:
            return {**summary, "skipped": "no_broker"}

        # Fetch current positions
        try:
            async with broker as b:
                positions = await b.get_positions()
                account_data = await b.get_account_summary()
        except Exception as exc:
            summary["errors"].append(f"broker_fetch: {exc}")
            return summary

        if not positions:
            return summary

        account_value = Decimal(str(account_data.get("total", 0)))

        # Check daily loss breach (including unrealized) — halt if exceeded
        daily_loss_outcome = await self._check_daily_loss_with_unrealized(
            app_settings, broker, account_value
        )
        if daily_loss_outcome is _DailyLossOutcome.POLICY_BLOCK_TRADING:
            log.critical(
                "position_monitor.daily_loss_halt",
                failure_policy="block_trading",
                fail_closed=True,
            )
            return {
                **summary,
                "halted": "unrealized_pnl_failure_block_trading",
                "failure_policy": "block_trading",
                "fail_closed": True,
            }
        if daily_loss_outcome is _DailyLossOutcome.POLICY_KILL_SWITCH:
            log.critical(
                "position_monitor.daily_loss_halt",
                failure_policy="activate_kill_switch",
                fail_closed=True,
            )
            return {
                **summary,
                "halted": "unrealized_pnl_failure_kill_switch",
                "failure_policy": "activate_kill_switch",
                "fail_closed": True,
            }
        if daily_loss_outcome is _DailyLossOutcome.BREACH_KILL_SWITCH:
            log.critical("position_monitor.daily_loss_halt")
            await activate_kill_switch(self.db, actor="position_monitor:daily_loss")
            await alert_kill_switch_activated(self.db, actor="position_monitor:daily_loss")
            return {**summary, "halted": "daily_loss_breach"}

        # Get all live strategies for context
        strat_result = await self.db.execute(
            select(Strategy).where(
                Strategy.is_enabled == True,  # noqa: E712
                Strategy.is_live == True,  # noqa: E712
            )
        )
        strategies = list(strat_result.scalars().all())

        # Monitor each open position
        for pos in positions:
            ticker = pos.get("ticker", "")
            pos_qty = Decimal(str(pos.get("quantity", 0)))
            if pos_qty <= 0 or not ticker:
                continue

            summary["positions_checked"] += 1

            try:
                result = await self._monitor_position(
                    ticker=ticker,
                    pos_qty=pos_qty,
                    pos_data=pos,
                    broker=broker,
                    strategies=strategies,
                    account_value=account_value,
                )
                summary["exits_submitted"] += result.get("exits", 0)
                summary["partial_exits"] += result.get("partial", 0)
                summary["stops_hit"] += result.get("stops", 0)
                summary["take_profits"] += result.get("tps", 0)
            except Exception as exc:
                summary["errors"].append(f"{ticker}: {exc}")
                log.error("position_monitor.ticker_error", ticker=ticker, error=str(exc))

        await self.db.commit()
        return summary

    async def _monitor_position(
        self,
        ticker: str,
        pos_qty: Decimal,
        pos_data: dict[str, Any],
        broker: Any,
        strategies: list[Strategy],
        account_value: Decimal,
    ) -> dict[str, Any]:
        """Monitor a single position for exit conditions."""
        result = {"exits": 0, "partial": 0, "stops": 0, "tps": 0}

        bars, current_price = await self._get_market_data(ticker)
        if current_price <= 0 or not bars:
            return result

        avg_price = Decimal(str(pos_data.get("averagePrice", 0)))
        max_sell_qty = Decimal(str(pos_data.get("maxSell", float(pos_qty))))

        # Find the last executed entry signal for this ticker
        sig_result = await self.db.execute(
            select(Signal)
            .where(
                Signal.ticker == ticker,
                Signal.side == "buy",
                Signal.status == "executed",
                Signal.stop_price.isnot(None),
            )
            .order_by(desc(Signal.generated_at))
            .limit(1)
        )
        last_signal = sig_result.scalar_one_or_none()

        if not last_signal:
            return result

        atr_val = atr(bars, 14) if len(bars) >= 15 else Decimal("0")
        if atr_val <= 0:
            return result

        # Find the active strategy to get params
        strategy = next((s for s in strategies if str(s.id) == str(last_signal.strategy_id)), None)
        # Build ORBState for exit evaluation
        initial_stop = last_signal.stop_price
        take_profit_2r = last_signal.take_profit_price

        if not initial_stop or not take_profit_2r:
            return result

        # Calculate risk at entry for 1R target
        risk_per_share = avg_price - initial_stop
        take_profit_1r = avg_price + risk_per_share if risk_per_share > 0 else take_profit_2r

        # Check if we've already taken partial exit
        partial_result = await self.db.execute(
            select(Signal)
            .where(
                Signal.ticker == ticker,
                Signal.signal_type == "partial_exit",
                Signal.status == "executed",
                Signal.strategy_id == last_signal.strategy_id,
            )
            .order_by(desc(Signal.generated_at))
            .limit(1)
        )
        partial_done = partial_result.scalar_one_or_none() is not None

        state = ORBState(
            ticker=ticker,
            strategy_id=str(last_signal.strategy_id),
            side="buy",
            entry_price=avg_price,
            quantity=pos_qty,
            remaining_quantity=max_sell_qty,
            initial_stop=initial_stop,
            current_stop=initial_stop,
            take_profit_1r=take_profit_1r,
            take_profit_2r=take_profit_2r,
            partial_exit_done=partial_done,
            atr_at_entry=atr_val,
        )

        # Use the strategy's exit logic
        orb = OpeningRangeBreakoutStrategy(strategy.params if strategy else None)
        exit_signal = orb.check_exit_conditions(ticker, state, current_price, bars)

        if exit_signal is None:
            return result

        # Determine sell qty
        sell_qty = abs(exit_signal.suggested_quantity)
        if sell_qty > max_sell_qty:
            sell_qty = max_sell_qty

        if sell_qty <= 0:
            return result

        # Submit the exit order
        async with broker as b:
            engine = ExecutionEngine(self.db, b)
            order = await engine.create_order_intent(
                ticker=ticker,
                side="sell",
                order_type="market",
                quantity=sell_qty,
                signal_id=last_signal.id,
                is_dry_run=(settings.APP_MODE == "mock"),
                estimated_price=current_price,
            )
            order = await engine.submit_order(order)

        # Record the exit signal
        exit_sig_record = Signal(
            id=uuid.uuid4(),
            strategy_id=last_signal.strategy_id,
            ticker=ticker,
            side="sell",
            signal_type=exit_signal.signal_type,
            status="executed",
            entry_price=current_price,
            stop_price=exit_signal.stop_price,
            take_profit_price=exit_signal.take_profit_price,
            suggested_quantity=-sell_qty,
            confidence=exit_signal.confidence,
            reason=exit_signal.reason,
            generated_at=datetime.now(UTC),
            executed_at=datetime.now(UTC),
        )
        self.db.add(exit_sig_record)

        self.db.add(
            AuditLog(
                action="position_exit_automated",
                entity_type="order",
                entity_id=str(order.id),
                actor="position_monitor",
                payload={
                    "ticker": ticker,
                    "exit_type": exit_signal.signal_type,
                    "price": float(current_price),
                    "qty": float(sell_qty),
                    "reason": exit_signal.reason,
                },
                occurred_at=datetime.now(UTC),
            )
        )

        log.info(
            "position_monitor.exit_submitted",
            ticker=ticker,
            exit_type=exit_signal.signal_type,
            price=float(current_price),
            qty=float(sell_qty),
            order_id=str(order.id),
        )

        result["exits"] = 1
        if exit_signal.signal_type == "partial_exit":
            result["partial"] = 1
        elif exit_signal.signal_type in ("stop", "trailing_stop"):
            result["stops"] = 1
        elif exit_signal.signal_type == "take_profit":
            result["tps"] = 1

        # Send alert for notable exits
        svc = AlertService(self.db)
        if exit_signal.signal_type in ("stop", "trailing_stop"):
            await svc.send(
                alert_type="stop_hit",
                title=f"Stop hit: {ticker}",
                message=exit_signal.reason,
                severity="warning",
                payload={"ticker": ticker, "price": float(current_price)},
            )
        elif exit_signal.signal_type == "take_profit":
            unrealized = float(current_price - avg_price) * float(sell_qty)
            await svc.send(
                alert_type="take_profit",
                title=f"Take profit: {ticker} +{unrealized:.2f}",
                message=exit_signal.reason,
                severity="info",
                payload={"ticker": ticker, "pnl": unrealized},
            )

        return result

    async def eod_flatten(self) -> dict[str, Any]:
        """
        Force-close all positions at end of session.
        Only runs for strategies with eod_flatten=True.
        """
        broker = await self._get_broker()
        if broker is None:
            return {"flattened": 0, "reason": "no_broker"}

        async with broker as b:
            positions = await b.get_positions()
            engine = ExecutionEngine(self.db, b)
            flattened = 0
            for pos in positions:
                qty = Decimal(str(pos.get("quantity", 0)))
                if qty > 0:
                    order = await engine.create_order_intent(
                        ticker=pos["ticker"],
                        side="sell",
                        order_type="market",
                        quantity=qty,
                        is_dry_run=(settings.APP_MODE == "mock"),
                        estimated_price=Decimal(
                            str(pos.get("currentPrice", 0) or pos.get("averagePrice", 0) or 0)
                        ),
                    )
                    await engine.submit_order(order)
                    flattened += 1

        self.db.add(
            AuditLog(
                action="eod_flatten_executed",
                actor="position_monitor",
                payload={"flattened": flattened},
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.commit()

        log.info("position_monitor.eod_flatten", positions=flattened)
        return {"flattened": flattened}
