"""
Hard risk engine.
Enforces all safety rules before any order is placed.
Every check is logged to risk_events for full auditability.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from app.db.models import AppSettings, Order, RiskEvent, RiskProfile, Trade
from app.services.feed_health import get_feed_health_snapshot

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


class RiskViolation(Exception):
    """Raised when a risk rule blocks an action."""

    def __init__(self, reason: str, event_type: str = "risk_block"):
        self.reason = reason
        self.event_type = event_type
        super().__init__(reason)


class RiskEngine:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _get_settings(self) -> AppSettings:
        result = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        settings = result.scalar_one_or_none()
        if not settings:
            raise RuntimeError("App settings not initialized — run seed")
        return settings

    async def _get_default_risk_profile(self) -> RiskProfile | None:
        result = await self.db.execute(
            select(RiskProfile).where(RiskProfile.is_default == True)  # noqa: E712
        )
        return result.scalar_one_or_none()

    async def _log(
        self,
        event_type: str,
        message: str,
        ticker: str | None = None,
        signal_id: UUID | None = None,
        order_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.db.add(
            RiskEvent(
                event_type=event_type,
                ticker=ticker,
                signal_id=signal_id,
                order_id=order_id,
                message=message,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.flush()

    # ── Individual checks ─────────────────────────────────────────────────────

    async def check_kill_switch(self) -> None:
        s = await self._get_settings()
        if s.kill_switch_active:
            await self._log("kill_switch_block", "Kill switch active — all trading blocked")
            raise RiskViolation(
                "Kill switch is active. Disable it from Emergency Controls.",
                "kill_switch_block",
            )

    async def check_auto_trading_enabled(self) -> None:
        s = await self._get_settings()
        if not s.auto_trading_enabled:
            raise RiskViolation(
                "Auto-trading is disabled. Enable it from the dashboard.",
                "auto_trading_disabled",
            )

    async def check_cash_guard(
        self,
        ticker: str,
        quantity: Decimal,
        estimated_price: Decimal,
        available_cash: Decimal,
        signal_id: UUID | None = None,
    ) -> None:
        """HARD BLOCK: Never spend more than available cash."""
        if quantity <= 0:
            return  # Sell — no cash needed

        estimated_cost = quantity * estimated_price
        if estimated_cost > available_cash:
            msg = (
                f"Cash guard: estimated cost {estimated_cost:.2f} "
                f"exceeds available cash {available_cash:.2f} for {ticker}"
            )
            await self._log(
                "cash_guard_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={"cost": float(estimated_cost), "available": float(available_cash)},
            )
            raise RiskViolation(msg, "cash_guard_block")

    async def check_duplicate_order(
        self, ticker: str, side: str, signal_id: UUID | None = None
    ) -> None:
        """Block if an active order for this ticker+side already exists."""
        result = await self.db.execute(
            select(Order)
            .where(
                Order.ticker == ticker,
                Order.side == side,
                Order.status.in_(["pending_intent", "submitted", "accepted"]),
            )
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            msg = f"Duplicate order blocked: active {side} for {ticker} (id={existing.id})"
            await self._log(
                "duplicate_order_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={"existing_order_id": str(existing.id)},
            )
            raise RiskViolation(msg, "duplicate_order_block")

    async def check_daily_loss_limit(
        self,
        realized_pnl_today: Decimal,
        account_value: Decimal,
        risk_profile: RiskProfile | None = None,
    ) -> None:
        rp = risk_profile or await self._get_default_risk_profile()
        if not rp or account_value <= 0:
            return

        if realized_pnl_today < 0:
            loss_pct = abs(realized_pnl_today) / account_value * 100
            if loss_pct >= rp.max_daily_loss_pct:
                msg = f"Daily loss {loss_pct:.2f}% ≥ limit {rp.max_daily_loss_pct}%"
                await self._log("daily_loss_breach", msg)
                raise RiskViolation(msg, "daily_loss_breach")

    async def check_max_open_positions(
        self,
        current_open: int,
        risk_profile: RiskProfile | None = None,
    ) -> None:
        rp = risk_profile or await self._get_default_risk_profile()
        if not rp:
            return
        if current_open >= rp.max_open_positions:
            msg = f"Max open positions: {current_open} ≥ {rp.max_open_positions}"
            await self._log("max_positions_block", msg)
            raise RiskViolation(msg, "max_positions_block")

    async def check_position_size(
        self,
        ticker: str,
        estimated_cost: Decimal,
        account_value: Decimal,
        signal_id: UUID | None = None,
        risk_profile: RiskProfile | None = None,
    ) -> None:
        rp = risk_profile or await self._get_default_risk_profile()
        if not rp or account_value <= 0:
            return
        size_pct = estimated_cost / account_value * 100
        if size_pct > rp.max_position_size_pct:
            msg = f"Position size {size_pct:.2f}% for {ticker} > max {rp.max_position_size_pct}%"
            await self._log("position_size_block", msg, ticker=ticker, signal_id=signal_id)
            raise RiskViolation(msg, "position_size_block")

    async def check_max_trades_today(self, risk_profile: RiskProfile | None = None) -> None:
        rp = risk_profile or await self._get_default_risk_profile()
        if not rp:
            return
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        from sqlalchemy import func

        result = await self.db.execute(
            select(func.count(Order.id)).where(
                Order.created_at >= today,
                Order.is_dry_run == False,  # noqa: E712
                Order.status.not_in(["rejected", "cancelled"]),
            )
        )
        count: int = result.scalar_one()
        if count >= rp.max_trades_per_day:
            msg = f"Max trades/day: {count} ≥ {rp.max_trades_per_day}"
            await self._log("max_trades_block", msg)
            raise RiskViolation(msg, "max_trades_block")

    async def check_consecutive_losses(self, risk_profile: RiskProfile | None = None) -> None:
        """
        Stop trading after N consecutive losing trades.
        Uses the trades table to look at the last N closed trades.
        """
        rp = risk_profile or await self._get_default_risk_profile()
        if not rp or rp.stop_after_consecutive_losses <= 0:
            return

        max_losses = rp.stop_after_consecutive_losses
        result = await self.db.execute(
            select(Trade)
            .where(
                Trade.closed_at.isnot(None),
                Trade.is_dry_run == False,  # noqa: E712
                Trade.realized_pnl.isnot(None),
            )
            .order_by(desc(Trade.closed_at))
            .limit(max_losses)
        )
        recent_trades = result.scalars().all()

        if len(recent_trades) < max_losses:
            return  # Not enough history yet

        # All N must be losses
        all_losses = all((t.realized_pnl or Decimal("0")) < 0 for t in recent_trades)
        if all_losses:
            msg = f"Stopped: {max_losses} consecutive losing trades"
            await self._log(
                "consecutive_loss_stop", msg, payload={"consecutive_losses": max_losses}
            )
            raise RiskViolation(msg, "consecutive_loss_stop")

    async def check_symbol_cooldown(
        self, ticker: str, risk_profile: RiskProfile | None = None
    ) -> None:
        """
        Prevent re-entering a symbol within cooldown_seconds of the last trade.
        """
        rp = risk_profile or await self._get_default_risk_profile()
        if not rp or rp.symbol_cooldown_seconds <= 0:
            return

        cutoff = datetime.now(UTC) - timedelta(seconds=rp.symbol_cooldown_seconds)
        result = await self.db.execute(
            select(Trade.closed_at)
            .where(
                Trade.ticker == ticker,
                Trade.closed_at >= cutoff,
                Trade.is_dry_run == False,  # noqa: E712
            )
            .limit(1)
        )
        recent = result.scalar_one_or_none()
        if recent:
            wait_s = int(
                (
                    recent + timedelta(seconds=rp.symbol_cooldown_seconds) - datetime.now(UTC)
                ).total_seconds()
            )
            msg = f"{ticker} is in cooldown for {wait_s}s more"
            await self._log("cooldown_block", msg, ticker=ticker, payload={"wait_seconds": wait_s})
            raise RiskViolation(msg, "cooldown_block")

    async def check_portfolio_heat(
        self,
        ticker: str,
        new_position_risk: Decimal,
        account_value: Decimal,
        open_positions: list[dict],
        signal_id: UUID | None = None,
        risk_profile: RiskProfile | None = None,
        max_heat_pct: float = 5.0,
    ) -> None:
        """
        Portfolio heat guard — cap total simultaneous risk exposure.

        'Heat' = sum of (entry_price - stop_price) * quantity across all open
        positions, expressed as a percentage of account_value.

        Scientific basis: Markowitz (1952) portfolio variance; Lopez de Prado
        (2018) position sizing. A portfolio heat cap of 5% means the account
        can never lose more than 5% in a single adverse sweep of all stops
        at the same time.

        Args:
            new_position_risk: Dollar risk of the proposed trade
                               = abs(entry_price - stop_price) * quantity.
            open_positions:    Broker position list; each dict may carry a
                               'riskDollars' / 'risk_dollars' key populated
                               by the position monitor when tracking stops.
            max_heat_pct:      Maximum total portfolio heat as % of account.
                               Default 5.0 — override via RiskProfile.
        """
        rp = risk_profile or await self._get_default_risk_profile()
        if rp and hasattr(rp, "max_portfolio_heat_pct") and rp.max_portfolio_heat_pct:
            max_heat_pct = float(rp.max_portfolio_heat_pct)

        if account_value <= 0:
            return

        # Sum existing heat from open positions that carry risk metadata
        existing_heat = Decimal("0")
        for pos in open_positions:
            rd = pos.get("riskDollars") or pos.get("risk_dollars")
            if rd is not None:
                existing_heat += Decimal(str(rd))

        total_heat = existing_heat + new_position_risk
        heat_pct = float(total_heat / account_value * 100)

        if heat_pct > max_heat_pct:
            msg = (
                f"Portfolio heat {heat_pct:.2f}% would exceed limit {max_heat_pct:.1f}% "
                f"(existing={float(existing_heat):.2f}, "
                f"new={float(new_position_risk):.2f}, "
                f"account={float(account_value):.2f}) for {ticker}"
            )
            await self._log(
                "portfolio_heat_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={
                    "heat_pct": round(heat_pct, 4),
                    "max_heat_pct": max_heat_pct,
                    "existing_heat": float(existing_heat),
                    "new_risk": float(new_position_risk),
                },
            )
            raise RiskViolation(msg, "portfolio_heat_block")

    async def check_sector_and_correlation(
        self,
        ticker: str,
        estimated_value: Decimal,
        account_value: Decimal,
        current_positions: list[dict],
        signal_id: UUID | None = None,
    ) -> None:
        """Check sector concentration and position correlation."""
        from app.risk.correlation import get_correlation_checker

        checker = get_correlation_checker()
        float_value = float(estimated_value)
        float_account = float(account_value)

        # Sector check
        ok, reason = checker.check_sector_exposure(
            ticker, float_value, current_positions, float_account
        )
        if not ok:
            await self._log("sector_limit_block", reason, ticker=ticker, signal_id=signal_id)
            raise RiskViolation(reason, "sector_limit_block")

        # Correlation check (requires price history — fetch from positions if available)
        price_history: dict[str, list[float]] = {}
        for pos in current_positions:
            t = pos.get("ticker", "")
            # Use avg_price as a stub; real implementation uses historical closes
            if t:
                price_history[t] = [float(pos.get("averagePrice", pos.get("avg_price", 100)))]

        allowed, violations = checker.check_correlation(ticker, current_positions, price_history)
        if not allowed:
            msgs = "; ".join(v.message for v in violations)
            await self._log("correlation_block", msgs, ticker=ticker, signal_id=signal_id)
            raise RiskViolation(
                f"Correlation limit: {violations[0].message}"
                if violations
                else "Correlation check failed",
                "correlation_block",
            )

    async def check_market_conditions(
        self,
        *,
        ticker: str,
        strategy_type: str,
        market_regime: dict[str, Any] | None,
        watchlist_context: dict[str, Any] | None = None,
        signal_id: UUID | None = None,
    ) -> None:
        regime_payload = market_regime or {}
        regime_name = str(regime_payload.get("regime") or "unknown")

        feed_snapshot = get_feed_health_snapshot()
        feed_status = str(feed_snapshot.get("status") or "unknown")
        symbol_status: str | None = None
        symbol_detail: str | None = None
        for symbol in feed_snapshot.get("symbols", []):
            if str(symbol.get("ticker") or "").upper() == ticker.upper():
                symbol_status = str(symbol.get("status") or "unknown")
                symbol_detail = str(symbol.get("detail") or "")
                break

        if feed_status in {"stale", "error"}:
            msg = f"Primary market data is {feed_status}; new entries are blocked until feed health recovers."
            await self._log(
                "feed_health_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={"feed_status": feed_status, "provider": feed_snapshot.get("provider")},
            )
            raise RiskViolation(msg, "feed_health_block")

        if symbol_status and symbol_status not in {"ok", "fallback"}:
            msg = (
                f"Feed validation blocked {ticker}: {symbol_status}. "
                f"{symbol_detail or 'Cross-source data mismatch persists.'}"
            )
            await self._log(
                "feed_symbol_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={
                    "feed_status": feed_status,
                    "symbol_status": symbol_status,
                    "provider": feed_snapshot.get("provider"),
                },
            )
            raise RiskViolation(msg, "feed_symbol_block")

        trusted_regimes = {
            "trending_up",
            "trending_down",
            "ranging",
            "risk_off",
            "unsafe",
            "high_volatility",
        }
        if regime_name == "unknown":
            msg = "Strategy entries blocked: unknown market regime."
            await self._log(
                "regime_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={"regime": regime_name, "strategy_type": strategy_type},
            )
            raise RiskViolation(msg, "regime_block")
        if regime_name not in trusted_regimes:
            msg = f"Strategy entries blocked: invalid market regime {regime_name}."
            await self._log(
                "regime_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={"regime": regime_name, "strategy_type": strategy_type},
            )
            raise RiskViolation(msg, "regime_block")
        if regime_name == "high_volatility":
            msg = "Strategy entries blocked in high_volatility regime."
            await self._log(
                "regime_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={"regime": regime_name, "strategy_type": strategy_type},
            )
            raise RiskViolation(msg, "regime_block")

        suppressed = {str(item) for item in regime_payload.get("suppressed_strategies", []) if item}
        if strategy_type in suppressed:
            msg = f"Strategy {strategy_type} blocked in {regime_name} regime."
            await self._log(
                "regime_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={
                    "regime": regime_name,
                    "strategy_type": strategy_type,
                    "suppressed_strategies": sorted(suppressed),
                },
            )
            raise RiskViolation(msg, "regime_block")

        context = watchlist_context or {}
        catalyst_score = float(context.get("catalyst_score", 0.0) or 0.0)
        event_type = str(context.get("catalyst_event_type") or "")
        if (
            strategy_type in {"opening_fade", "vwap_reclaim"}
            and catalyst_score >= 0.7
            and event_type in {"earnings", "guidance", "m&a", "legal_regulatory"}
        ):
            msg = (
                f"{strategy_type} blocked for {ticker}: fresh {event_type} catalyst "
                f"({catalyst_score:.2f}) makes mean-reversion unreliable."
            )
            await self._log(
                "event_risk_block",
                msg,
                ticker=ticker,
                signal_id=signal_id,
                payload={
                    "strategy_type": strategy_type,
                    "catalyst_score": catalyst_score,
                    "event_type": event_type,
                },
            )
            raise RiskViolation(msg, "event_risk_block")

    # ── Master check ──────────────────────────────────────────────────────────

    async def check_cfd_limits(
        self,
        *,
        ticker: str,
        quantity: Decimal,
        estimated_price: Decimal,
        account_value: Decimal,
        realized_pnl_today: Decimal,
        free_margin_pct: Decimal | None = None,
        signal_id: UUID | None = None,
        risk_profile: RiskProfile | None = None,
    ) -> None:
        """
        Additional risk checks applied only to CFD instruments.

        1. Stricter per-trade risk (cfd_max_risk_per_trade_pct if set)
        2. Stricter daily loss limit (cfd_max_daily_loss_pct if set)
        3. Leverage cap: (quantity x price) / account_value <= cfd_max_leverage
        4. Free margin guard: free_margin_pct >= min_free_margin_pct

        Scientific basis:
          FCA/ESMA (2018): retail CFD leverage capped at 5-30x by asset class.
          Chan (2013): leverage magnifies drawdowns - a 10% adverse move at 10x
          leverage results in a 100% margin loss.
        """
        rp = risk_profile or await self._get_default_risk_profile()
        if not rp:
            return

        notional = quantity * estimated_price

        # 1. Stricter per-trade risk (uses CFD override if set)
        effective_risk_pct = rp.cfd_max_risk_per_trade_pct or (
            rp.max_risk_per_trade_pct * Decimal("0.5")
        )
        if account_value > 0:
            risk_pct = notional / account_value * 100
            if risk_pct > effective_risk_pct:
                msg = (
                    f"CFD position {ticker}: notional {risk_pct:.2f}% of equity "
                    f"> CFD limit {effective_risk_pct:.2f}%"
                )
                await self._log("cfd_size_block", msg, ticker=ticker, signal_id=signal_id)
                raise RiskViolation(msg, "cfd_size_block")

        # 2. Stricter daily loss limit for CFDs
        effective_daily_loss = rp.cfd_max_daily_loss_pct or (
            rp.max_daily_loss_pct * Decimal("0.67")
        )
        if account_value > 0 and realized_pnl_today < 0:
            loss_pct = abs(realized_pnl_today) / account_value * 100
            if loss_pct >= float(effective_daily_loss):
                msg = f"CFD daily loss {loss_pct:.2f}% ≥ CFD limit {effective_daily_loss:.2f}%"
                await self._log("cfd_daily_loss_block", msg, signal_id=signal_id)
                raise RiskViolation(msg, "cfd_daily_loss_block")

        # 3. Leverage cap
        if account_value > 0:
            leverage = notional / account_value
            if leverage > rp.cfd_max_leverage:
                msg = (
                    f"CFD leverage {leverage:.1f}x for {ticker} exceeds cap "
                    f"{rp.cfd_max_leverage:.1f}x"
                )
                await self._log("cfd_leverage_block", msg, ticker=ticker, signal_id=signal_id)
                raise RiskViolation(msg, "cfd_leverage_block")

        # 4. Free margin guard (only when broker provides it)
        if free_margin_pct is not None and free_margin_pct < rp.min_free_margin_pct:
            msg = (
                f"Free margin {float(free_margin_pct):.1f}% < minimum "
                f"{float(rp.min_free_margin_pct):.1f}% — CFD blocked"
            )
            await self._log("cfd_margin_block", msg, ticker=ticker, signal_id=signal_id)
            raise RiskViolation(msg, "cfd_margin_block")

    async def run_all_checks(
        self,
        *,
        ticker: str,
        side: str,
        quantity: Decimal,
        estimated_price: Decimal,
        available_cash: Decimal,
        account_value: Decimal,
        realized_pnl_today: Decimal,
        current_open_positions: int,
        signal_id: UUID | None = None,
        risk_profile: RiskProfile | None = None,
        skip_auto_trading_check: bool = False,
        # Portfolio heat inputs (optional — skipped if not provided)
        stop_price: Decimal | None = None,
        open_positions: list[dict] | None = None,
        # CFD-specific inputs (optional — checks run only when is_cfd=True)
        is_cfd: bool = False,
        free_margin_pct: Decimal | None = None,
    ) -> None:
        """
        Run ALL checks in priority order.
        Raises RiskViolation on first failure — nothing proceeds past it.

        New optional args:
            stop_price:      The proposed stop loss for the trade. When
                             provided together with open_positions, the
                             portfolio heat check (check 11) is run.
            open_positions:  Full broker position list with optional
                             'riskDollars' fields for existing heat.
        """
        # 1. Kill switch — absolute stop
        await self.check_kill_switch()

        # 2. Auto-trading gate
        if not skip_auto_trading_check:
            await self.check_auto_trading_enabled()

        # 3. Cash guard — hardcoded safety (buy orders only)
        if side == "buy":
            await self.check_cash_guard(
                ticker, quantity, estimated_price, available_cash, signal_id
            )

        # 4. Duplicate order dedup
        await self.check_duplicate_order(ticker, side, signal_id)

        # 5. Daily loss limit
        await self.check_daily_loss_limit(realized_pnl_today, account_value, risk_profile)

        # 6. Max open positions
        await self.check_max_open_positions(current_open_positions, risk_profile)

        # 7. Position size
        if side == "buy":
            estimated_cost = quantity * estimated_price
            await self.check_position_size(
                ticker, estimated_cost, account_value, signal_id, risk_profile
            )

        # 8. Max trades today
        await self.check_max_trades_today(risk_profile)

        # 9. Consecutive losses
        await self.check_consecutive_losses(risk_profile)

        # 10. Symbol cooldown
        await self.check_symbol_cooldown(ticker, risk_profile)

        # 11. Portfolio heat — only when stop price is known (entry orders)
        if side == "buy" and stop_price is not None and open_positions is not None:
            new_risk = abs(estimated_price - stop_price) * quantity
            await self.check_portfolio_heat(
                ticker=ticker,
                new_position_risk=new_risk,
                account_value=account_value,
                open_positions=open_positions,
                signal_id=signal_id,
                risk_profile=risk_profile,
            )

        # 12. CFD-specific checks (leverage, tighter limits, margin guard)
        if is_cfd:
            await self.check_cfd_limits(
                ticker=ticker,
                quantity=quantity,
                estimated_price=estimated_price,
                account_value=account_value,
                realized_pnl_today=realized_pnl_today,
                free_margin_pct=free_margin_pct,
                signal_id=signal_id,
                risk_profile=risk_profile,
            )

    def get_drawdown_size_factor(
        self,
        realized_pnl_today: Decimal,
        account_value: Decimal,
    ) -> tuple[Decimal, str]:
        """
        Drawdown-adaptive position sizing.

        Returns (size_factor, tier_label) where size_factor ∈ [0.25, 1.0].

        Tiers (configurable via env):
          Full size   (1.00) — loss < DRAWDOWN_TIER1_PCT % of account
          Reduced     (0.75) — loss ≥ TIER1 and < TIER2
          Half size   (0.50) — loss ≥ TIER2 and < TIER3
          Quarter     (0.25) — loss ≥ TIER3 (soft stop before kill switch)
        """
        from app.core.config import settings as cfg

        if account_value <= 0:
            return Decimal("1.0"), "full"

        loss_pct = float(abs(min(realized_pnl_today, Decimal("0"))) / account_value * 100)

        if loss_pct >= cfg.DRAWDOWN_TIER3_PCT:
            return Decimal("0.25"), "quarter"
        if loss_pct >= cfg.DRAWDOWN_TIER2_PCT:
            return Decimal("0.50"), "half"
        if loss_pct >= cfg.DRAWDOWN_TIER1_PCT:
            return Decimal("0.75"), "reduced"
        return Decimal("1.0"), "full"

    async def apply_drawdown_sizing(
        self,
        quantity: Decimal,
        realized_pnl_today: Decimal,
        account_value: Decimal,
        signal_id: UUID | None = None,
        ticker: str | None = None,
    ) -> Decimal:
        """
        Scale quantity by the current drawdown factor.
        Logs a risk event when sizing is reduced.
        Minimum quantity is 1 share.
        """
        factor, tier = self.get_drawdown_size_factor(realized_pnl_today, account_value)
        if factor < Decimal("1.0"):
            scaled = max(Decimal("1"), (quantity * factor).quantize(Decimal("1")))
            await self._log(
                "drawdown_size_reduction",
                f"Position size reduced to {int(factor * 100)}% ({tier}) due to daily drawdown. "
                f"{quantity} → {scaled} shares.",
                ticker=ticker,
                signal_id=signal_id,
                payload={"factor": float(factor), "tier": tier, "original_qty": float(quantity)},
            )
            return scaled
        return quantity


# ── Module-level helpers ──────────────────────────────────────────────────────


async def activate_kill_switch(db: AsyncSession, actor: str = "system") -> None:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if s:
        s.kill_switch_active = True
        s.auto_trading_enabled = False
    db.add(
        RiskEvent(
            event_type="kill_switch_on",
            message=f"Kill switch activated by {actor}",
            payload={"actor": actor},
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def deactivate_kill_switch(db: AsyncSession, actor: str = "system") -> None:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if s:
        s.kill_switch_active = False
    db.add(
        RiskEvent(
            event_type="kill_switch_off",
            message=f"Kill switch deactivated by {actor}",
            payload={"actor": actor},
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
