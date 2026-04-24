"""
Strategy runner service — production complete.

Pipeline per Celery tick (every 5 min):
  1. Kill switch + auto-trading gates
  2. Morning watchlist override (09:15 ET daily scan)
  3. For each strategy x ticker:
     - Exit check if position open
     - Entry signal if flat
     - Full risk checks (cash, dedup, sector, correlation, consecutive losses,
       portfolio heat)
     - Submit order via execution engine

Supported strategy types:
  "orb"          — Opening Range Breakout (continuation; trending sessions)
  "vwap_reclaim" — VWAP Reclaim (mean-reversion / momentum hybrid)
  "opening_fade" — Opening Fade (gap mean-reversion; choppy sessions)
  "closing_momentum"     — Late-session continuation after a strong first half-hour
  "intraday_periodicity" — Same-slot continuation with recent session confirmation
"""
from __future__ import annotations

import inspect
import uuid
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import desc, select

from app.backtest.portfolio_strategies import is_portfolio_strategy_type
from app.core.config import settings
from app.db.models import AppSettings, AuditLog, BrokerConnection, Signal, Strategy
from app.execution.engine import ExecutionEngine
from app.risk.engine import RiskEngine, RiskViolation
from app.services.alert_service import (
    alert_daily_summary,
    alert_order_failed,
    alert_stop_out,
    alert_take_profit,
    alert_trade_submitted,
)
from app.services.broker_connection_recovery import mark_broker_connection_reconnect_required
from app.services.market_intelligence_monitor import MarketIntelligenceMonitor
from app.services.signal_allocator import AllocationState, AllocatorCandidate, SignalAllocator
from app.services.strategy_promotion import StrategyPromotionService
from app.strategies.indicators import Bar, atr
from app.strategies.orb_production import OpeningRangeBreakoutStrategy, ORBState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


class StrategyRunner:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Infrastructure ────────────────────────────────────────────────────────

    async def _get_settings(self) -> AppSettings | None:
        r = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        return r.scalar_one_or_none()

    async def _get_broker(self) -> Any | None:
        if settings.APP_MODE == "mock":
            from app.broker.mock_adapter import MockBrokerAdapter
            return MockBrokerAdapter()
        r = await self.db.execute(
            select(BrokerConnection).where(BrokerConnection.is_active == True).limit(1)  # noqa: E712
        )
        conn = r.scalar_one_or_none()
        if not conn:
            return None
        from app.broker.trading212 import Trading212Adapter
        from app.core.security import CredentialDecryptionError, decrypt_field
        try:
            api_key = decrypt_field(conn.api_key_encrypted)
            api_secret = decrypt_field(conn.api_secret_encrypted)
        except CredentialDecryptionError as exc:
            log.error("runner.credentials_invalid", error=str(exc))
            await mark_broker_connection_reconnect_required(
                self.db,
                conn,
                str(exc),
                actor="strategy_runner",
            )
            return None
        return Trading212Adapter(
            api_key,
            api_secret,
            conn.environment,
        )

    def _parse_session_open(self, value: str) -> time:
        hours, minutes = map(int, value.split(":"))
        return time(hour=hours, minute=minutes, tzinfo=UTC)

    def _coerce_bar_time(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                return None
        return None

    def _extract_session_context(
        self,
        bars: list[Bar],
        bar_times: list[datetime],
        *,
        session_open_utc: str,
    ) -> tuple[list[Bar], list[datetime], Decimal | None]:
        if not bars or not bar_times or len(bars) != len(bar_times):
            return bars, bar_times, None

        session_clock = self._parse_session_open(session_open_utc)
        candidate_dates = sorted({bar_time.date() for bar_time in bar_times}, reverse=True)
        for candidate_date in candidate_dates:
            session_start = datetime.combine(candidate_date, session_clock)
            session_pairs = [
                (bar, bar_time)
                for bar, bar_time in zip(bars, bar_times, strict=True)
                if bar_time >= session_start and bar_time.date() == candidate_date
            ]
            if not session_pairs:
                continue
            session_bars = [bar for bar, _ in session_pairs]
            session_times = [bar_time for _, bar_time in session_pairs]
            previous_bars = [
                bar
                for bar, bar_time in zip(bars, bar_times, strict=True)
                if bar_time < session_start
            ]
            prev_close = previous_bars[-1].close if previous_bars else None
            return session_bars, session_times, prev_close

        return bars, bar_times, None

    async def _fetch_market_context(
        self,
        ticker: str,
        *,
        session_open_utc: str,
        history_days: int,
        max_bars: int,
    ) -> tuple[list[Bar], list[datetime], list[Bar], list[datetime], Decimal | None, str]:
        """
        Returns:
          session_bars, session_times, history_bars, history_bar_times, prev_close, current_utc_hhmm

        The session slice is derived from timestamped bars so opening-range and
        gap-based strategies operate on the real current session instead of an
        arbitrary rolling window.
        """
        now_utc = datetime.now(UTC).strftime("%H:%M")
        try:
            from app.market_data import get_live_provider
            provider = get_live_provider()
            raw_bars: list[Any]
            latest_quote: Any | None = None

            if hasattr(provider, '__aenter__'):
                # Async context manager (Alpaca / Polygon)
                async with provider as md:
                    from_date = date.today() - timedelta(days=max(history_days + 2, 5))
                    raw_bars = await md.get_bars(
                        ticker,
                        multiplier=5,
                        timespan="minute",
                        from_date=from_date,
                        to_date=date.today(),
                        limit=max_bars,
                    )
                    with suppress(Exception):
                        latest_quote = await md.get_quote(ticker)
                    if hasattr(md, "is_trade_safe") and latest_quote is not None and not md.is_trade_safe(ticker):
                        log.warning("runner.feed_unhealthy", ticker=ticker)
                        return [], [], [], [], None, now_utc
            else:
                # Sync mock provider
                raw_bars = provider.get_ohlcv(ticker, interval_minutes=5, bars=max_bars)

            bars: list[Bar] = []
            bar_times: list[datetime] = []
            for raw_bar in raw_bars:
                timestamp = self._coerce_bar_time(getattr(raw_bar, "timestamp", None))
                if timestamp is None and isinstance(raw_bar, dict):
                    timestamp = self._coerce_bar_time(raw_bar.get("timestamp"))
                if timestamp is None:
                    continue

                if isinstance(raw_bar, dict):
                    bars.append(
                        Bar(
                            Decimal(str(raw_bar["open"])),
                            Decimal(str(raw_bar["high"])),
                            Decimal(str(raw_bar["low"])),
                            Decimal(str(raw_bar["close"])),
                            Decimal(str(raw_bar["volume"])),
                        )
                    )
                else:
                    bars.append(
                        Bar(
                            Decimal(str(raw_bar.open)),
                            Decimal(str(raw_bar.high)),
                            Decimal(str(raw_bar.low)),
                            Decimal(str(raw_bar.close)),
                            Decimal(str(raw_bar.volume)),
                        )
                    )
                bar_times.append(timestamp)

            session_bars, session_times, prev_close = self._extract_session_context(
                bars,
                bar_times,
                session_open_utc=session_open_utc,
            )
            if session_times:
                now_utc = session_times[-1].strftime("%H:%M")
            return session_bars, session_times, bars, bar_times, prev_close, now_utc
        except Exception as exc:
            log.warning("runner.data_error", ticker=ticker, error=str(exc))
            return [], [], [], [], None, now_utc

    def _get_tickers(self, strategy: Strategy) -> list[str]:
        """Use today's scanned watchlist if available and fresh, else static list."""
        params = strategy.params
        todays = params.get("todays_watchlist")
        watchlist_candidates = params.get("watchlist_candidates")
        updated = params.get("watchlist_updated_at", "")
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if todays and isinstance(todays, list) and today in updated:
            if isinstance(watchlist_candidates, dict) and watchlist_candidates:
                ranked = sorted(
                    (str(item).upper() for item in todays),
                    key=lambda ticker: float(
                        (watchlist_candidates.get(ticker) or {}).get("score", 0.0)
                    ),
                    reverse=True,
                )
                log.debug("runner.using_ranked_watchlist", strategy=strategy.name, n=len(ranked))
                return ranked
            log.debug("runner.using_scanned_watchlist", strategy=strategy.name, n=len(todays))
            return todays
        return strategy.allowed_tickers

    def _watchlist_context(self, strategy: Strategy, ticker: str) -> dict[str, Any]:
        raw = (strategy.params or {}).get("watchlist_candidates")
        if not isinstance(raw, dict):
            return {}
        context = raw.get(ticker.upper()) or raw.get(ticker) or {}
        return context if isinstance(context, dict) else {}

    def _apply_signal_intelligence_overlay(
        self,
        *,
        strategy: Strategy,
        ticker: str,
        signal_obj: Any,
        regime_payload: dict[str, Any],
        watchlist_context: dict[str, Any],
    ) -> None:
        if not hasattr(signal_obj, "params_snapshot"):
            return

        params_snapshot = dict(getattr(signal_obj, "params_snapshot", {}) or {})
        confidence = Decimal(str(getattr(signal_obj, "confidence", Decimal("0"))))
        notes: list[str] = []
        catalyst_score = float(watchlist_context.get("catalyst_score", 0.0) or 0.0)
        regime_name = str(regime_payload.get("regime") or "unknown")
        momentum_strategies = {"orb", "closing_momentum", "intraday_periodicity"}
        mean_reversion_strategies = {"opening_fade", "vwap_reclaim"}

        if watchlist_context:
            params_snapshot["watchlist_context"] = watchlist_context
            if catalyst_score >= 0.65 and strategy.type in momentum_strategies:
                confidence += Decimal("0.05")
                notes.append(f"fresh catalyst {catalyst_score:.2f}")
            elif catalyst_score >= 0.50 and strategy.type in mean_reversion_strategies:
                confidence -= Decimal("0.03")
                notes.append(f"event risk dampener {catalyst_score:.2f}")

        if regime_name == "trending_up" and strategy.type in momentum_strategies:
            confidence += Decimal("0.03")
            notes.append("regime tailwind")
        elif regime_name == "ranging" and strategy.type in mean_reversion_strategies:
            confidence += Decimal("0.02")
            notes.append("range regime fit")

        confidence = max(Decimal("0.01"), min(confidence, Decimal("0.98")))
        signal_obj.confidence = confidence
        signal_obj.params_snapshot = params_snapshot
        if notes:
            signal_obj.reason = f"{signal_obj.reason} | {'; '.join(notes)}"

    def _make_engine(self, strategy: Strategy) -> Any:
        """Return the correct strategy engine for the given strategy type."""
        if strategy.type == "orb":
            return OpeningRangeBreakoutStrategy(strategy.params)
        if strategy.type == "vwap_reclaim":
            from app.strategies.vwap_reclaim import VWAPReclaimStrategy
            return VWAPReclaimStrategy(strategy.params)
        if strategy.type == "opening_fade":
            from app.strategies.opening_fade import OpeningFadeStrategy
            return OpeningFadeStrategy(strategy.params)
        if strategy.type == "closing_momentum":
            from app.strategies.closing_momentum import ClosingMomentumStrategy
            return ClosingMomentumStrategy(strategy.params)
        if strategy.type == "intraday_periodicity":
            from app.strategies.intraday_periodicity import IntradayPeriodicityStrategy
            return IntradayPeriodicityStrategy(strategy.params)
        return None

    def _build_signal_kwargs(
        self,
        engine: Any,
        *,
        ticker: str,
        bars: list[Bar],
        bar_times: list[datetime],
        history_bars: list[Bar],
        history_bar_times: list[datetime],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None,
    ) -> dict[str, Any]:
        signature = inspect.signature(engine.generate_signal)
        kwargs: dict[str, Any] = {
            "ticker": ticker,
            "bars": bars,
            "account_value": account_value,
            "available_cash": available_cash,
            "current_time_utc": current_time_utc,
        }
        if "prev_close" in signature.parameters:
            kwargs["prev_close"] = prev_close
        if "session_open" in signature.parameters and bars:
            kwargs["session_open"] = bars[0].open
        if "bar_times" in signature.parameters:
            kwargs["bar_times"] = bar_times
        if "history_bars" in signature.parameters:
            kwargs["history_bars"] = history_bars
        if "history_bar_times" in signature.parameters:
            kwargs["history_bar_times"] = history_bar_times
        return kwargs

    # ── Main entry ────────────────────────────────────────────────────────────

    async def run_all_enabled(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "strategies_run": 0, "signals_generated": 0,
            "orders_submitted": 0, "risk_blocks": 0, "errors": [],
        }

        app_cfg = await self._get_settings()
        if not app_cfg:
            return {**summary, "skipped": "no_settings"}
        if app_cfg.kill_switch_active:
            return {**summary, "skipped": "kill_switch"}
        if not app_cfg.auto_trading_enabled:
            return {**summary, "skipped": "auto_trading_off"}

        strategies = (await self.db.execute(
            select(Strategy).where(Strategy.is_enabled == True)  # noqa: E712
        )).scalars().all()
        strategies = [strategy for strategy in strategies if not is_portfolio_strategy_type(strategy.type)]
        if not strategies:
            return summary

        intelligence = await MarketIntelligenceMonitor(self.db).evaluate_and_alert()

        broker = await self._get_broker()
        if broker is None:
            return {**summary, "skipped": "no_broker"}

        async with broker as b:
            account = await b.get_account_summary()
            positions = await b.get_positions()

        cash = Decimal(str(account.get("free", 0)))
        total = Decimal(str(account.get("total", 0)))
        n_open = len(positions)
        pos_map = {p["ticker"]: p for p in positions}
        allocator = SignalAllocator()
        allocation_state = allocator.new_state()

        for strategy in strategies:
            try:
                g, s, b_ = await self._run_strategy(
                    strategy=strategy, broker=broker,
                    cash=cash, total=total, n_open=n_open,
                    pos_map=pos_map, all_positions=positions,
                    intelligence=intelligence,
                    allocator=allocator,
                    allocation_state=allocation_state,
                )
                summary["strategies_run"] += 1
                summary["signals_generated"] += g
                summary["orders_submitted"] += s
                summary["risk_blocks"] += b_
            except Exception as exc:
                summary["errors"].append(f"{strategy.name}: {exc}")
                log.error("runner.strategy_error", name=strategy.name, error=str(exc))

        # Daily summary alert (fires after all strategies have run)
        try:
            from datetime import date
            await alert_daily_summary(
                self.db,
                date_str=date.today().isoformat(),
                total_trades=summary["signals_generated"],
                orders_submitted=summary["orders_submitted"],
                risk_blocks=summary["risk_blocks"],
                errors=summary["errors"],
            )
        except Exception:
            pass

        return summary

    # ── Per-strategy ──────────────────────────────────────────────────────────

    async def _run_strategy(
        self, *, strategy: Strategy, broker: Any,
        cash: Decimal, total: Decimal, n_open: int,
        pos_map: dict, all_positions: list,
        intelligence: dict[str, Any],
        allocator: SignalAllocator,
        allocation_state: AllocationState,
    ) -> tuple[int, int, int]:
        engine = self._make_engine(strategy)
        if engine is None:
            return 0, 0, 0

        if strategy.is_live:
            allowed, reason = await StrategyPromotionService(self.db).execution_gate(strategy)
            if not allowed:
                log.info("runner.promotion_block", strategy=strategy.name, reason=reason)
                self.db.add(AuditLog(
                    action="strategy_execution_blocked_by_promotion",
                    entity_type="strategy",
                    entity_id=str(strategy.id),
                    actor="strategy_runner",
                    payload={"reason": reason, "mode": settings.APP_MODE},
                    occurred_at=datetime.now(UTC),
                ))
                return 0, 0, 0

        risk = RiskEngine(self.db)
        tickers = self._get_tickers(strategy)
        if not tickers:
            return 0, 0, 0

        gen = sub = blocks = 0
        for ticker in tickers:
            try:
                g, s, b = await self._process_ticker(
                    ticker=ticker, strategy=strategy, engine=engine,
                    risk=risk, broker=broker, cash=cash, total=total,
                    n_open=n_open, pos_map=pos_map, all_positions=all_positions,
                    intelligence=intelligence,
                    allocator=allocator,
                    allocation_state=allocation_state,
                )
                gen += g
                sub += s
                blocks += b
            except Exception as exc:
                log.warning("runner.ticker_error",
                            strategy=strategy.name, ticker=ticker, error=str(exc))
        return gen, sub, blocks

    # ── Per-ticker ────────────────────────────────────────────────────────────

    async def _process_ticker(
        self, *, ticker: str, strategy: Strategy, engine: Any,
        risk: RiskEngine, broker: Any, cash: Decimal, total: Decimal,
        n_open: int, pos_map: dict, all_positions: list,
        intelligence: dict[str, Any],
        allocator: SignalAllocator,
        allocation_state: AllocationState,
    ) -> tuple[int, int, int]:
        session_open_utc = str(engine.params.get("session_open_utc", "14:30"))
        history_days = int(getattr(engine, "history_days", 5))
        max_history_bars = int(getattr(engine, "max_history_bars", 180))
        session_bars, session_times, history_bars, history_bar_times, prev_close, now_utc = await self._fetch_market_context(
            ticker,
            session_open_utc=session_open_utc,
            history_days=history_days,
            max_bars=max_history_bars,
        )
        if len(session_bars) < max(4, int(getattr(engine, "required_bars", 4))):
            return 0, 0, 0

        watchlist_context = self._watchlist_context(strategy, ticker)
        try:
            await risk.check_market_conditions(
                ticker=ticker,
                strategy_type=strategy.type,
                market_regime=dict(intelligence.get("regime") or {}),
                watchlist_context=watchlist_context,
            )
        except RiskViolation as exc:
            log.info("runner.intelligence_block", ticker=ticker, reason=exc.reason, strategy=strategy.name)
            return 0, 0, 1

        # Exit check if position open
        if ticker in pos_map:
            pos = pos_map[ticker]
            qty = Decimal(str(pos.get("quantity", 0)))
            avg = Decimal(str(pos.get("averagePrice", pos.get("avg_price", 0))))
            if qty > 0 and avg > 0:
                submitted = await self._check_exit(
                    ticker=ticker, strategy=strategy, bars=session_bars,
                    pos_qty=qty, avg_price=avg,
                    max_sell=Decimal(str(pos.get("maxSell", float(qty)))),
                    broker=broker, risk=risk,
                )
                return (1 if submitted is not None else 0), (submitted or 0), 0
            return 0, 0, 0

        # Entry signal
        signal_obj = engine.generate_signal(**self._build_signal_kwargs(
            engine,
            ticker=ticker,
            bars=session_bars,
            bar_times=session_times,
            history_bars=history_bars,
            history_bar_times=history_bar_times,
            account_value=total,
            available_cash=cash,
            current_time_utc=now_utc,
            prev_close=prev_close,
        ))
        if signal_obj is None:
            return 0, 0, 0

        self._apply_signal_intelligence_overlay(
            strategy=strategy,
            ticker=ticker,
            signal_obj=signal_obj,
            regime_payload=dict(intelligence.get("regime") or {}),
            watchlist_context=watchlist_context,
        )

        qty = abs(signal_obj.suggested_quantity)
        price = signal_obj.entry_price

        # Persist signal
        sig = Signal(
            id=uuid.uuid4(), strategy_id=strategy.id,
            ticker=ticker, side=signal_obj.side,
            signal_type=signal_obj.signal_type, status="pending",
            entry_price=price, stop_price=signal_obj.stop_price,
            take_profit_price=signal_obj.take_profit_price,
            suggested_quantity=signal_obj.suggested_quantity,
            confidence=signal_obj.confidence, reason=signal_obj.reason,
            params_snapshot={**signal_obj.params_snapshot, "strategy_type": strategy.type},
            generated_at=datetime.now(UTC),
        )
        self.db.add(sig)
        await self.db.flush()
        strategy.last_signal_at = datetime.now(UTC)

        allocation = allocator.allocate_one(
            AllocatorCandidate(
                ticker=ticker,
                side=signal_obj.side,
                strategy_id=str(strategy.id),
                strategy_name=strategy.name,
                strategy_type=strategy.type,
                signal_type=signal_obj.signal_type,
                confidence=signal_obj.confidence,
                entry_price=price,
                quantity=qty,
                stop_price=signal_obj.stop_price,
                take_profit_price=signal_obj.take_profit_price,
                watchlist_context=watchlist_context,
            ),
            account_value=total,
            available_cash=cash,
            current_positions=all_positions,
            regime=dict(intelligence.get("regime") or {}),
            state=allocation_state,
        )
        sig.params_snapshot = {
            **dict(sig.params_snapshot or {}),
            "allocation": allocation.to_payload(),
        }
        if allocation.status == "rejected":
            sig.status = "rejected"
            sig.risk_rejected = True
            sig.risk_rejection_reason = allocation.reason
            self.db.add(AuditLog(
                action="strategy_allocation_blocked",
                entity_type="signal",
                entity_id=str(sig.id),
                actor="strategy_runner",
                payload=allocation.to_payload(),
                occurred_at=datetime.now(UTC),
            ))
            log.info(
                "runner.allocation_block",
                strategy=strategy.name,
                ticker=ticker,
                score=allocation.score,
                reason=allocation.reason,
            )
            return 1, 0, 1

        # Dry-run: log only
        if not strategy.is_live:
            sig.status = "approved"
            log.info("runner.dry_run", strategy=strategy.name, ticker=ticker,
                     side=signal_obj.side, conf=float(signal_obj.confidence))
            return 1, 0, 0

        # Risk checks
        # Determine if the strategy is operating as a CFD (allow_short = True
        # means the strategy will short, which requires a margin/CFD account).
        is_cfd = bool(strategy.params.get("allow_short", False))
        try:
            await risk.run_all_checks(
                ticker=ticker, side=signal_obj.side, quantity=qty,
                estimated_price=price, available_cash=cash, account_value=total,
                realized_pnl_today=Decimal("0"), current_open_positions=n_open,
                signal_id=sig.id, skip_auto_trading_check=True,
                # Portfolio heat check: pass stop price so the engine can
                # compute new_risk = |entry - stop| * qty and cap total exposure.
                stop_price=signal_obj.stop_price,
                open_positions=all_positions,
                # CFD-specific checks (leverage cap, tighter sizing, margin guard)
                is_cfd=is_cfd,
            )
            await risk.check_sector_and_correlation(
                ticker=ticker, estimated_value=qty * price,
                account_value=total, current_positions=all_positions, signal_id=sig.id,
            )
        except RiskViolation as exc:
            sig.status = "rejected"
            sig.risk_rejected = True
            sig.risk_rejection_reason = exc.reason
            log.info("runner.risk_block", ticker=ticker, reason=exc.reason)
            return 1, 0, 1

        # Submit — use limit orders to reduce slippage (Almgren & Chriss 2001)
        # Place a marketable limit slightly through the touch: buy at ask+0.1%,
        # sell at bid-0.1%.  The execution engine will cancel and re-submit as
        # market if the limit is not filled within 30 seconds.
        try:
            limit_offset = Decimal("0.001")  # 0.1 % through the touch
            if signal_obj.side == "buy":
                limit_price = price * (1 + limit_offset)
            else:
                limit_price = price * (1 - limit_offset)
            limit_price = limit_price.quantize(Decimal("0.01"))

            async with broker as b:
                exec_engine = ExecutionEngine(self.db, b)
                order = await exec_engine.create_order_intent(
                    ticker=ticker, side=signal_obj.side,
                    order_type="limit", quantity=qty, signal_id=sig.id,
                    available_cash=cash, estimated_price=price,
                    limit_price=limit_price,
                )
                order = await exec_engine.submit_order(order)

            sig.status = "executed"
            sig.executed_at = datetime.now(UTC)
            self.db.add(AuditLog(
                action="strategy_order_placed", entity_type="order",
                entity_id=str(order.id), actor=f"strategy:{strategy.name}",
                payload={"ticker": ticker, "side": signal_obj.side,
                         "qty": float(qty), "reason": signal_obj.reason},
                occurred_at=datetime.now(UTC),
            ))
            log.info("runner.order_submitted", strategy=strategy.name,
                     ticker=ticker, side=signal_obj.side, order=str(order.id))
            # Alert on live trade submission
            with suppress(Exception):
                await alert_trade_submitted(
                    self.db, strategy_name=strategy.name, ticker=ticker,
                    side=signal_obj.side, qty=float(qty), price=float(price),
                    order_type="limit",
                    confidence=float(signal_obj.confidence),
                    reason=signal_obj.reason or "",
                )
            return 1, 1, 0
        except Exception as exc:
            sig.status = "error"
            sig.risk_rejection_reason = str(exc)
            log.error("runner.submit_error", ticker=ticker, error=str(exc))
            with suppress(Exception):
                await alert_order_failed(self.db, ticker, str(exc))
            return 1, 0, 0

    # ── Exit logic ────────────────────────────────────────────────────────────

    async def _check_exit(
        self, *, ticker: str, strategy: Strategy, bars: list[Bar],
        pos_qty: Decimal, avg_price: Decimal, max_sell: Decimal,
        broker: Any, risk: RiskEngine,
    ) -> int | None:
        current_price = bars[-1].close
        atr_val = atr(bars, 14) if len(bars) >= 15 else Decimal("0")

        # Find last entry signal
        r = await self.db.execute(
            select(Signal).where(
                Signal.strategy_id == strategy.id, Signal.ticker == ticker,
                Signal.side == "buy", Signal.status == "executed",
                Signal.stop_price.isnot(None), Signal.take_profit_price.isnot(None),
            ).order_by(desc(Signal.generated_at)).limit(1)
        )
        last_sig = r.scalar_one_or_none()
        if not last_sig:
            return None

        # Check if partial exit done
        pr = await self.db.execute(
            select(Signal).where(
                Signal.ticker == ticker, Signal.signal_type == "partial_exit",
                Signal.status == "executed", Signal.strategy_id == strategy.id,
            ).order_by(desc(Signal.generated_at)).limit(1)
        )
        partial_done = pr.scalar_one_or_none() is not None

        risk_at_entry = avg_price - (last_sig.stop_price or avg_price * Decimal("0.98"))
        state = ORBState(
            ticker=ticker, strategy_id=str(strategy.id), side="buy",
            entry_price=avg_price, quantity=pos_qty,
            remaining_quantity=min(max_sell, pos_qty),
            initial_stop=last_sig.stop_price or avg_price * Decimal("0.98"),
            current_stop=last_sig.stop_price or avg_price * Decimal("0.98"),
            take_profit_1r=avg_price + risk_at_entry,
            take_profit_2r=last_sig.take_profit_price or avg_price * Decimal("1.04"),
            partial_exit_done=partial_done, atr_at_entry=atr_val,
        )

        exit_engine = OpeningRangeBreakoutStrategy(strategy.params)
        exit_sig = exit_engine.check_exit_conditions(ticker, state, current_price, bars)
        if exit_sig is None:
            return None

        sell_qty = min(abs(exit_sig.suggested_quantity), max_sell)
        if sell_qty <= 0:
            return None

        if not strategy.is_live:
            log.info("runner.dry_run_exit", ticker=ticker, reason=exit_sig.signal_type)
            return 0

        try:
            await risk.check_kill_switch()
        except RiskViolation:
            return None

        async with broker as b:
            exec_engine = ExecutionEngine(self.db, b)
            order = await exec_engine.create_order_intent(
                ticker=ticker, side="sell", order_type="market",
                quantity=sell_qty, signal_id=last_sig.id,
            )
            order = await exec_engine.submit_order(order)

        self.db.add(Signal(
            id=uuid.uuid4(), strategy_id=strategy.id, ticker=ticker,
            side="sell", signal_type=exit_sig.signal_type, status="executed",
            entry_price=current_price, stop_price=exit_sig.stop_price,
            take_profit_price=exit_sig.take_profit_price,
            suggested_quantity=-sell_qty, confidence=exit_sig.confidence,
            reason=exit_sig.reason, generated_at=datetime.now(UTC),
            executed_at=datetime.now(UTC),
        ))
        self.db.add(AuditLog(
            action="strategy_exit_placed", entity_type="order",
            entity_id=str(order.id), actor=f"strategy:{strategy.name}",
            payload={"ticker": ticker, "exit_type": exit_sig.signal_type,
                     "price": float(current_price), "qty": float(sell_qty)},
            occurred_at=datetime.now(UTC),
        ))
        log.info("runner.exit_submitted", strategy=strategy.name,
                 ticker=ticker, exit_type=exit_sig.signal_type, qty=float(sell_qty))

        # Alerts for exits
        try:
            pnl_est = float((current_price - avg_price) * sell_qty)
            if exit_sig.signal_type == "stop":
                await alert_stop_out(
                    self.db, strategy_name=strategy.name, ticker=ticker,
                    exit_price=float(current_price), entry_price=float(avg_price),
                    pnl=pnl_est,
                )
            elif exit_sig.signal_type in ("take_profit", "partial_exit"):
                await alert_take_profit(
                    self.db, strategy_name=strategy.name, ticker=ticker,
                    exit_price=float(current_price), entry_price=float(avg_price),
                    pnl=pnl_est, signal_type=exit_sig.signal_type,
                )
        except Exception:
            pass

        return 1
