"""
Portfolio execution and rebalance service.

This promotes the new longer-horizon portfolio strategies from research-only
backtests into controlled automation:
  - dry-run order journaling when a strategy is not yet live
  - demo-broker execution when the app runs in demo mode and the strategy is live
  - live-broker execution only when the app is in live mode and the global
    live-trading readiness gates have already been unlocked elsewhere
"""
from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.backtest.portfolio_engine import SHARE_QUANT, _align_histories, _rebalance_due
from app.backtest.portfolio_strategies import (
    PORTFOLIO_STRATEGY_TYPES,
    get_portfolio_backtest_strategy,
    is_portfolio_strategy_type,
)
from app.core.config import settings
from app.db.models import AppSettings, AuditLog, BrokerConnection, Instrument, Signal, Strategy
from app.execution.engine import ExecutionEngine
from app.market_data import get_live_provider, get_provider_name
from app.risk.engine import RiskEngine, RiskViolation
from app.services.broker_connection_recovery import mark_broker_connection_reconnect_required
from app.services.market_regime import MarketRegimeService
from app.services.signal_allocator import AllocationState, AllocatorCandidate, SignalAllocator
from app.services.strategy_promotion import StrategyPromotionService
from app.strategies.indicators import Bar

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

PORTFOLIO_STATE_KEY = "portfolio_execution"
DEFAULT_MIN_TRADE_VALUE = Decimal("25")
DEFAULT_MIN_WEIGHT_DELTA_PCT = Decimal("1.0")
DEFAULT_CAPITAL_FRACTION = Decimal("1.0")


@dataclass
class MarketSnapshot:
    histories: dict[str, tuple[list[Bar], list[datetime]]]
    latest_prices: dict[str, Decimal]
    latest_quote_times: dict[str, datetime]
    market_open: bool | None
    provider_name: str


class PortfolioExecutionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def run_strategy_by_id(
        self,
        strategy_id: uuid.UUID,
        *,
        force: bool = False,
        actor: str = "system",
        override_is_live: bool | None = None,
    ) -> dict[str, Any]:
        app_cfg = await self._get_settings()
        if not app_cfg:
            return {"status": "skipped", "reason": "no_settings"}
        if app_cfg.kill_switch_active:
            return {"status": "skipped", "reason": "kill_switch"}
        if not app_cfg.auto_trading_enabled and not force:
            return {"status": "skipped", "reason": "auto_trading_off"}
        if settings.APP_MODE == "live" and override_is_live is not False and not app_cfg.live_trading_unlocked:
            return {"status": "skipped", "reason": "live_not_unlocked"}

        result = await self.db.execute(
            select(Strategy)
            .where(Strategy.id == strategy_id)
            .options(selectinload(Strategy.risk_profile))
        )
        strategy = result.scalar_one_or_none()
        if strategy is None or not is_portfolio_strategy_type(strategy.type):
            return {"status": "skipped", "reason": "strategy_not_portfolio"}

        broker = await self._get_broker()
        if broker is None:
            return {"status": "skipped", "reason": "no_broker"}

        async with broker as broker_client:
            account = await broker_client.get_account_summary()
            positions = await broker_client.get_positions()
            allocator = SignalAllocator()
            return await self.run_strategy_once(
                strategy,
                broker=broker_client,
                account_value=Decimal(str(account.get("total", 0))),
                available_cash=Decimal(str(account.get("free", account.get("cash", 0)))),
                broker_positions=list(positions),
                force=force,
                actor=actor,
                override_is_live=override_is_live,
                allocator_state=allocator.new_state(),
            )

    async def run_all_enabled(
        self,
        *,
        force: bool = False,
        actor: str = "worker:run_portfolio_rebalance",
        strategy_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "strategies_seen": 0,
            "strategies_due": 0,
            "strategies_rebalanced": 0,
            "signals_created": 0,
            "orders_submitted": 0,
            "dry_run_orders": 0,
            "risk_blocks": 0,
            "allocation_blocks": 0,
            "skipped": [],
            "errors": [],
        }

        app_cfg = await self._get_settings()
        if not app_cfg:
            return {**summary, "skipped_reason": "no_settings"}
        if app_cfg.kill_switch_active:
            return {**summary, "skipped_reason": "kill_switch"}
        if not app_cfg.auto_trading_enabled and not force:
            return {**summary, "skipped_reason": "auto_trading_off"}
        if settings.APP_MODE == "live" and not app_cfg.live_trading_unlocked:
            return {**summary, "skipped_reason": "live_not_unlocked"}

        strategies = await self._list_enabled_portfolio_strategies(strategy_id=strategy_id)
        if not strategies:
            return {**summary, "skipped_reason": "no_portfolio_strategies"}

        allocation_check = self._validate_capital_allocations(strategies)
        if allocation_check is not None:
            summary["skipped"].append(allocation_check)
            return {**summary, "skipped_reason": "capital_fraction_conflict"}

        broker = await self._get_broker()
        if broker is None:
            return {**summary, "skipped_reason": "no_broker"}

        async with broker as broker_client:
            account = await broker_client.get_account_summary()
            positions = await broker_client.get_positions()

            total_equity = Decimal(str(account.get("total", 0)))
            current_cash = Decimal(str(account.get("free", account.get("cash", 0))))
            current_positions = list(positions)
            allocator = SignalAllocator()
            allocation_state = allocator.new_state()

            for strategy in strategies:
                summary["strategies_seen"] += 1
                try:
                    strategy_summary = await self.run_strategy_once(
                        strategy,
                        broker=broker_client,
                        account_value=total_equity,
                        available_cash=current_cash,
                        broker_positions=current_positions,
                        force=force,
                        actor=actor,
                        allocator_state=allocation_state,
                    )
                except Exception as exc:
                    log.exception("portfolio_execution.strategy_failed", strategy=strategy.name, error=str(exc))
                    summary["errors"].append(f"{strategy.name}: {exc}")
                    continue

                if strategy_summary.get("status") in {"due", "rebalanced"}:
                    summary["strategies_due"] += 1
                if strategy_summary.get("status") == "rebalanced":
                    summary["strategies_rebalanced"] += 1
                if "available_cash" in strategy_summary:
                    current_cash = Decimal(str(strategy_summary["available_cash"]))
                if "positions" in strategy_summary:
                    current_positions = list(strategy_summary["positions"])
                summary["signals_created"] += int(strategy_summary.get("signals_created", 0))
                summary["orders_submitted"] += int(strategy_summary.get("orders_submitted", 0))
                summary["dry_run_orders"] += int(strategy_summary.get("dry_run_orders", 0))
                summary["risk_blocks"] += int(strategy_summary.get("risk_blocks", 0))
                summary["allocation_blocks"] += int(strategy_summary.get("allocation_blocks", 0))
                if strategy_summary.get("status") == "skipped":
                    summary["skipped"].append(
                        {
                            "strategy": strategy.name,
                            "reason": strategy_summary.get("reason", "unknown"),
                        }
                    )
        return summary

    async def run_strategy_once(
        self,
        strategy: Strategy,
        *,
        broker: Any,
        account_value: Decimal,
        available_cash: Decimal,
        broker_positions: list[dict[str, Any]],
        force: bool = False,
        actor: str = "system",
        override_is_live: bool | None = None,
        allocator_state: AllocationState | None = None,
    ) -> dict[str, Any]:
        if not is_portfolio_strategy_type(strategy.type):
            return {"status": "skipped", "reason": "not_portfolio_strategy"}

        if not strategy.allowed_tickers:
            await self._update_strategy_state(
                strategy,
                status="skipped",
                reason="no_universe",
                actor=actor,
            )
            return {"status": "skipped", "reason": "no_universe"}

        config = get_portfolio_backtest_strategy(strategy.type)
        strategy_impl = config["strategy_class"](strategy.params or {})
        min_history = max(int(config["min_history_bars"]), int(strategy.params.get("lookback_bars", 0))) + 5

        market_snapshot = await self._load_market_snapshot(strategy.allowed_tickers, min_history)
        if settings.APP_MODE == "live" and market_snapshot.provider_name == "polygon_delayed":
            await self._update_strategy_state(
                strategy,
                status="skipped",
                reason="delayed_market_data",
                actor=actor,
            )
            return {"status": "skipped", "reason": "delayed_market_data"}
        if market_snapshot.market_open is False and not force:
            await self._update_strategy_state(
                strategy,
                status="skipped",
                reason="market_closed",
                actor=actor,
            )
            return {"status": "skipped", "reason": "market_closed"}

        if not market_snapshot.histories:
            await self._update_strategy_state(
                strategy,
                status="skipped",
                reason="insufficient_market_data",
                actor=actor,
            )
            return {"status": "skipped", "reason": "insufficient_market_data"}

        decision_date, aligned_history = self._aligned_decision_history(market_snapshot.histories)
        if decision_date is None or not aligned_history:
            await self._update_strategy_state(
                strategy,
                status="skipped",
                reason="alignment_failed",
                actor=actor,
            )
            return {"status": "skipped", "reason": "alignment_failed"}

        previous_rebalance = self._parse_last_rebalance_date(strategy)
        rebalance_frequency = str(getattr(strategy_impl, "rebalance_frequency", "monthly"))
        if not force and not _rebalance_due(previous_rebalance, decision_date, rebalance_frequency):
            await self._update_strategy_state(
                strategy,
                status="skipped",
                reason="not_due",
                actor=actor,
                decision_date=decision_date,
            )
            return {"status": "skipped", "reason": "not_due"}

        target_weights = strategy_impl.target_weights(aligned_history, as_of_index=len(next(iter(aligned_history.values()))) - 1)
        is_live = strategy.is_live if override_is_live is None else override_is_live
        execute_live = is_live and settings.APP_MODE != "mock"
        if execute_live:
            promotion_allowed, promotion_reason = await StrategyPromotionService(self.db).execution_gate(strategy)
            if not promotion_allowed:
                await self._update_strategy_state(
                    strategy,
                    status="skipped",
                    reason=promotion_reason or "promotion_blocked",
                    actor=actor,
                    decision_date=decision_date,
                )
                return {"status": "skipped", "reason": promotion_reason or "promotion_blocked"}
        sleeve_fraction = self._strategy_capital_fraction(strategy)
        sleeve_value = (account_value * sleeve_fraction).quantize(Decimal("0.01"))

        instruments = await self._load_instruments(strategy.allowed_tickers)
        position_map = {
            str(position.get("ticker", "")).upper(): position
            for position in broker_positions
            if str(position.get("ticker", "")).upper() in {ticker.upper() for ticker in strategy.allowed_tickers}
        }

        plan = self._build_rebalance_plan(
            strategy=strategy,
            target_weights=target_weights,
            sleeve_value=sleeve_value,
            position_map=position_map,
            latest_prices=market_snapshot.latest_prices,
            instruments=instruments,
        )

        if not plan:
            await self._update_strategy_state(
                strategy,
                status="rebalanced",
                reason="already_aligned",
                actor=actor,
                decision_date=decision_date,
                target_weights=target_weights,
                current_weights=self._current_weights(position_map, market_snapshot.latest_prices, sleeve_value),
                mode="dry_run" if not execute_live else settings.APP_MODE,
            )
            return {
                "status": "rebalanced",
                "signals_created": 0,
                "orders_submitted": 0,
                "dry_run_orders": 0,
                "risk_blocks": 0,
                "available_cash": float(available_cash),
                "positions": broker_positions,
            }

        risk_engine = RiskEngine(self.db)
        allocator = SignalAllocator()
        active_allocation_state = allocator_state or allocator.new_state()
        regime_payload = await self._load_regime_payload()
        current_cash = available_cash
        current_positions = list(broker_positions)
        allocation_base_positions = list(broker_positions)
        open_positions_count = len([position for position in current_positions if Decimal(str(position.get("quantity", 0))) > 0])
        signals_created = 0
        orders_submitted = 0
        dry_run_orders = 0
        risk_blocks = 0
        allocation_blocks = 0
        allocation_decisions: list[dict[str, Any]] = []

        for order_plan in plan:
            signal = await self._create_rebalance_signal(strategy, order_plan, actor=actor)
            signals_created += 1

            allocation = allocator.allocate_one(
                AllocatorCandidate(
                    ticker=order_plan["ticker"],
                    side=order_plan["side"],
                    strategy_id=str(strategy.id),
                    strategy_name=strategy.name,
                    strategy_type=strategy.type,
                    signal_type="portfolio_rebalance",
                    confidence=Decimal("1.0"),
                    entry_price=order_plan["price"],
                    quantity=order_plan["quantity"],
                    target_weight=order_plan["target_weight"],
                    delta_value=order_plan["delta_value"],
                ),
                account_value=account_value,
                available_cash=current_cash,
                current_positions=allocation_base_positions,
                regime=regime_payload,
                state=active_allocation_state,
            )
            allocation_payload = allocation.to_payload()
            allocation_decisions.append(allocation_payload)
            signal.params_snapshot = {
                **dict(signal.params_snapshot or {}),
                "allocation": allocation_payload,
            }
            if allocation.status == "rejected":
                signal.status = "rejected"
                signal.risk_rejected = True
                signal.risk_rejection_reason = allocation.reason
                risk_blocks += 1
                allocation_blocks += 1
                await self._add_audit_log(
                    action="portfolio_allocation_blocked",
                    actor=actor,
                    entity_type="signal",
                    entity_id=str(signal.id),
                    payload=allocation_payload,
                )
                continue

            if order_plan["side"] == "buy":
                effective_open_positions = open_positions_count
                if order_plan["ticker"] in position_map:
                    effective_open_positions = max(0, open_positions_count - 1)
                try:
                    await risk_engine.run_all_checks(
                        ticker=order_plan["ticker"],
                        side="buy",
                        quantity=order_plan["quantity"],
                        estimated_price=order_plan["price"],
                        available_cash=current_cash,
                        account_value=account_value,
                        realized_pnl_today=Decimal("0"),
                        current_open_positions=effective_open_positions,
                        signal_id=signal.id,
                        risk_profile=strategy.risk_profile,
                        skip_auto_trading_check=True,
                        open_positions=current_positions,
                    )
                    await risk_engine.check_sector_and_correlation(
                        ticker=order_plan["ticker"],
                        estimated_value=order_plan["quantity"] * order_plan["price"],
                        account_value=account_value,
                        current_positions=current_positions,
                        signal_id=signal.id,
                    )
                except RiskViolation as exc:
                    signal.status = "rejected"
                    signal.risk_rejected = True
                    signal.risk_rejection_reason = exc.reason
                    risk_blocks += 1
                    await self._add_audit_log(
                        action="portfolio_rebalance_blocked",
                        actor=actor,
                        entity_type="strategy",
                        entity_id=str(strategy.id),
                        payload={
                            "strategy": strategy.name,
                            "ticker": order_plan["ticker"],
                            "reason": exc.reason,
                        },
                    )
                    continue

            exec_engine = ExecutionEngine(self.db, broker)
            order = await exec_engine.create_order_intent(
                ticker=order_plan["ticker"],
                side=order_plan["side"],
                order_type="market",
                quantity=order_plan["quantity"],
                signal_id=signal.id,
                is_dry_run=not execute_live,
                available_cash=current_cash,
                estimated_price=order_plan["price"],
            )
            order = await exec_engine.submit_order(order)

            signal.status = "approved" if order.is_dry_run else "executed"
            signal.executed_at = None if order.is_dry_run else datetime.now(UTC)
            current_cash = self._advance_cash_balance(current_cash, order_plan["side"], order_plan["quantity"], order_plan["price"])
            current_positions = self._advance_positions(
                current_positions,
                ticker=order_plan["ticker"],
                side=order_plan["side"],
                quantity=order_plan["quantity"],
                price=order_plan["price"],
            )
            open_positions_count = len([position for position in current_positions if Decimal(str(position.get("quantity", 0))) > 0])

            if order.is_dry_run:
                dry_run_orders += 1
            else:
                orders_submitted += 1

            await self._add_audit_log(
                action="portfolio_rebalance_order",
                actor=actor,
                entity_type="order",
                entity_id=str(order.id),
                payload={
                    "strategy": strategy.name,
                    "ticker": order_plan["ticker"],
                    "side": order_plan["side"],
                    "quantity": float(order_plan["quantity"]),
                    "price": float(order_plan["price"]),
                    "target_weight": float(order_plan["target_weight"]),
                    "mode": "dry_run" if order.is_dry_run else settings.APP_MODE,
                },
            )

        final_status = "rebalanced"
        await self._update_strategy_state(
            strategy,
            status=final_status,
            reason=None,
            actor=actor,
            decision_date=decision_date,
            target_weights=target_weights,
            current_weights=self._current_weights(
                {
                    str(position.get("ticker", "")).upper(): position
                    for position in current_positions
                    if str(position.get("ticker", "")).upper() in {ticker.upper() for ticker in strategy.allowed_tickers}
                },
                market_snapshot.latest_prices,
                sleeve_value,
            ),
            mode="dry_run" if not execute_live else settings.APP_MODE,
            orders_submitted=orders_submitted,
            dry_run_orders=dry_run_orders,
            risk_blocks=risk_blocks,
            allocation_decisions=allocation_decisions,
            allocation_blocks=allocation_blocks,
        )
        strategy.last_signal_at = datetime.now(UTC)

        return {
            "status": "rebalanced",
            "signals_created": signals_created,
            "orders_submitted": orders_submitted,
            "dry_run_orders": dry_run_orders,
            "risk_blocks": risk_blocks,
            "allocation_blocks": allocation_blocks,
            "available_cash": float(current_cash),
            "positions": current_positions,
        }

    async def _get_settings(self) -> AppSettings | None:
        result = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        return result.scalar_one_or_none()

    async def _list_enabled_portfolio_strategies(self, *, strategy_id: uuid.UUID | None = None) -> list[Strategy]:
        stmt = (
            select(Strategy)
            .where(Strategy.is_enabled == True)  # noqa: E712
            .options(selectinload(Strategy.risk_profile))
        )
        if strategy_id is not None:
            stmt = stmt.where(Strategy.id == strategy_id)
        result = await self.db.execute(stmt.order_by(Strategy.created_at.asc()))
        return [
            strategy
            for strategy in result.scalars().all()
            if strategy.type in PORTFOLIO_STRATEGY_TYPES
        ]

    async def _get_broker(self) -> Any | None:
        if settings.APP_MODE == "mock":
            from app.broker.mock_adapter import MockBrokerAdapter

            return MockBrokerAdapter()

        result = await self.db.execute(
            select(BrokerConnection)
            .where(BrokerConnection.is_active == True)  # noqa: E712
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
            await mark_broker_connection_reconnect_required(
                self.db,
                conn,
                str(exc),
                actor="portfolio_execution_service",
            )
            return None

        return Trading212Adapter(api_key, api_secret, conn.environment)

    async def _load_market_snapshot(self, tickers: list[str], bars_needed: int) -> MarketSnapshot:
        provider = get_live_provider()
        if hasattr(provider, "__aenter__"):
            async with provider as active_provider:
                return await self._snapshot_from_provider(active_provider, tickers, bars_needed)
        return await self._snapshot_from_provider(provider, tickers, bars_needed)

    async def _snapshot_from_provider(self, provider: Any, tickers: list[str], bars_needed: int) -> MarketSnapshot:
        histories: dict[str, tuple[list[Bar], list[datetime]]] = {}
        latest_prices: dict[str, Decimal] = {}
        latest_quote_times: dict[str, datetime] = {}

        for raw_ticker in tickers:
            ticker = raw_ticker.upper()
            try:
                bars, bar_times = await self._fetch_daily_bars(provider, ticker, bars_needed)
            except Exception as exc:
                log.warning("portfolio_execution.history_failed", ticker=ticker, error=str(exc))
                continue

            if bars:
                histories[ticker] = (bars, bar_times)
                latest_prices[ticker] = bars[-1].close
                latest_quote_times[ticker] = bar_times[-1]

            try:
                quote = await self._maybe_await(provider.get_quote(ticker))
            except Exception:
                quote = None
            if quote is not None:
                quote_price = Decimal(str(getattr(quote, "last", 0) or 0))
                quote_time = getattr(quote, "timestamp", None)
                if quote_price > 0:
                    latest_prices[ticker] = quote_price
                if isinstance(quote_time, datetime):
                    latest_quote_times[ticker] = quote_time

        market_open: bool | None = None
        if hasattr(provider, "is_market_open"):
            try:
                market_open = bool(await self._maybe_await(provider.is_market_open()))
            except Exception:
                market_open = None

        return MarketSnapshot(
            histories=histories,
            latest_prices=latest_prices,
            latest_quote_times=latest_quote_times,
            market_open=market_open,
            provider_name=get_provider_name(),
        )

    async def _fetch_daily_bars(self, provider: Any, ticker: str, bars_needed: int) -> tuple[list[Bar], list[datetime]]:
        if hasattr(provider, "get_bars"):
            raw_bars = await self._maybe_await(
                provider.get_bars(
                    ticker,
                    multiplier=1,
                    timespan="day",
                    limit=bars_needed,
                )
            )
            bars = [
                Bar(
                    open=Decimal(str(bar.open)),
                    high=Decimal(str(bar.high)),
                    low=Decimal(str(bar.low)),
                    close=Decimal(str(bar.close)),
                    volume=Decimal(str(bar.volume)),
                )
                for bar in raw_bars
            ]
            bar_times = [
                getattr(bar, "timestamp", datetime.now(UTC))
                for bar in raw_bars
            ]
            return bars, bar_times

        raw = provider.get_ohlcv(ticker, interval_minutes=1440, bars=bars_needed)
        bars = [
            Bar(
                open=Decimal(str(bar["open"])),
                high=Decimal(str(bar["high"])),
                low=Decimal(str(bar["low"])),
                close=Decimal(str(bar["close"])),
                volume=Decimal(str(bar["volume"])),
            )
            for bar in raw
        ]
        bar_times = [
            datetime.fromisoformat(str(bar["timestamp"]))
            for bar in raw
        ]
        return bars, bar_times

    async def _load_regime_payload(self) -> dict[str, Any]:
        if settings.APP_MODE == "mock":
            return {
                "regime": "mock",
                "active_strategies": [],
                "suppressed_strategies": [],
                "detail": "Mock mode uses neutral allocator regime assumptions.",
            }
        try:
            return await MarketRegimeService().evaluate()
        except Exception as exc:
            log.warning("portfolio_execution.regime_failed", error=str(exc))
            return {"regime": "unknown", "active_strategies": [], "suppressed_strategies": []}

    def _aligned_decision_history(
        self,
        histories: dict[str, tuple[list[Bar], list[datetime]]],
    ) -> tuple[date | None, dict[str, list[Bar]]]:
        try:
            aligned_dates, aligned_history = _align_histories(histories)
        except ValueError:
            return None, {}

        if not aligned_dates:
            return None, {}

        decision_index = len(aligned_dates) - 1
        today = datetime.now(UTC).date()
        if aligned_dates[-1] >= today and len(aligned_dates) >= 2:
            decision_index -= 1

        if decision_index < 0:
            return None, {}

        sliced_history = {
            ticker: bars[: decision_index + 1]
            for ticker, bars in aligned_history.items()
        }
        return aligned_dates[decision_index], sliced_history

    def _build_rebalance_plan(
        self,
        *,
        strategy: Strategy,
        target_weights: dict[str, Decimal],
        sleeve_value: Decimal,
        position_map: dict[str, dict[str, Any]],
        latest_prices: dict[str, Decimal],
        instruments: dict[str, Instrument],
    ) -> list[dict[str, Any]]:
        min_trade_value = Decimal(str(strategy.params.get("min_trade_value", DEFAULT_MIN_TRADE_VALUE)))
        min_weight_delta_pct = Decimal(
            str(strategy.params.get("min_weight_delta_pct", DEFAULT_MIN_WEIGHT_DELTA_PCT))
        )
        managed_tickers = sorted(set(position_map) | set(target_weights))
        plan: list[dict[str, Any]] = []

        for ticker in managed_tickers:
            price = latest_prices.get(ticker, Decimal("0"))
            if price <= 0:
                continue
            current_qty = Decimal(str(position_map.get(ticker, {}).get("quantity", 0)))
            current_value = (current_qty * price).quantize(Decimal("0.01"))
            target_weight = target_weights.get(ticker, Decimal("0"))
            target_value = (sleeve_value * target_weight).quantize(Decimal("0.01"))
            delta_value = target_value - current_value
            abs_delta_value = abs(delta_value)
            weight_delta_pct = Decimal("0")
            if sleeve_value > 0:
                weight_delta_pct = (abs_delta_value / sleeve_value) * Decimal("100")

            if abs_delta_value < min_trade_value or weight_delta_pct < min_weight_delta_pct:
                continue

            side = "buy" if delta_value > 0 else "sell"
            raw_quantity = (abs_delta_value / price).quantize(SHARE_QUANT, rounding=ROUND_DOWN)
            quantity = self._normalize_quantity(raw_quantity, instruments.get(ticker))
            if side == "sell":
                quantity = min(quantity, current_qty)
            if quantity <= 0:
                continue
            plan.append(
                {
                    "ticker": ticker,
                    "side": side,
                    "quantity": quantity,
                    "price": price.quantize(Decimal("0.0001")),
                    "target_weight": target_weight.quantize(Decimal("0.0001")),
                    "delta_value": delta_value.quantize(Decimal("0.01")),
                }
            )

        plan.sort(key=lambda item: (0 if item["side"] == "sell" else 1, item["ticker"]))
        return plan

    def _normalize_quantity(self, quantity: Decimal, instrument: Instrument | None) -> Decimal:
        normalized = quantity.quantize(SHARE_QUANT, rounding=ROUND_DOWN)
        if instrument and instrument.buy_lot_size:
            lot = Decimal(str(instrument.buy_lot_size))
            if lot > 0:
                normalized = ((normalized / lot).to_integral_value(rounding=ROUND_DOWN) * lot).quantize(
                    SHARE_QUANT,
                    rounding=ROUND_DOWN,
                )
        return normalized

    async def _create_rebalance_signal(
        self,
        strategy: Strategy,
        order_plan: dict[str, Any],
        *,
        actor: str,
    ) -> Signal:
        signal = Signal(
            id=uuid.uuid4(),
            strategy_id=strategy.id,
            ticker=order_plan["ticker"],
            side=order_plan["side"],
            signal_type="portfolio_rebalance",
            status="pending",
            entry_price=order_plan["price"],
            suggested_quantity=order_plan["quantity"],
            confidence=Decimal("1.0"),
            reason=(
                f"Portfolio rebalance toward target weight "
                f"{order_plan['target_weight']:.4f}"
            ),
            params_snapshot={
                "strategy_type": strategy.type,
                "target_weight": float(order_plan["target_weight"]),
                "estimated_price": float(order_plan["price"]),
                "actor": actor,
            },
            generated_at=datetime.now(UTC),
        )
        self.db.add(signal)
        await self.db.flush()
        return signal

    async def _load_instruments(self, tickers: list[str]) -> dict[str, Instrument]:
        result = await self.db.execute(
            select(Instrument).where(Instrument.ticker.in_([ticker.upper() for ticker in tickers]))
        )
        return {
            instrument.ticker.upper(): instrument
            for instrument in result.scalars().all()
        }

    def _current_weights(
        self,
        position_map: dict[str, dict[str, Any]],
        latest_prices: dict[str, Decimal],
        sleeve_value: Decimal,
    ) -> dict[str, float]:
        weights: dict[str, float] = {}
        if sleeve_value <= 0:
            return weights
        for ticker, position in position_map.items():
            price = latest_prices.get(ticker, Decimal("0"))
            if price <= 0:
                continue
            quantity = Decimal(str(position.get("quantity", 0)))
            value = quantity * price
            if value <= 0:
                continue
            weights[ticker] = float((value / sleeve_value).quantize(Decimal("0.0001")))
        return weights

    def _parse_last_rebalance_date(self, strategy: Strategy) -> date | None:
        raw = (strategy.params or {}).get(PORTFOLIO_STATE_KEY, {}).get("last_rebalance_signal_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw)).date()
        except ValueError:
            return None

    def _strategy_capital_fraction(self, strategy: Strategy) -> Decimal:
        raw = Decimal(str(strategy.params.get("capital_fraction", DEFAULT_CAPITAL_FRACTION)))
        return max(Decimal("0"), min(raw, Decimal("1")))

    def _validate_capital_allocations(self, strategies: list[Strategy]) -> dict[str, Any] | None:
        total = sum((self._strategy_capital_fraction(strategy) for strategy in strategies), Decimal("0"))
        if total <= Decimal("1.0001"):
            return None
        return {
            "reason": "capital_fraction_conflict",
            "detail": f"Enabled portfolio strategies claim {float(total):.2f}x of account capital.",
        }

    def _advance_cash_balance(
        self,
        current_cash: Decimal,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> Decimal:
        delta = quantity * price
        if side == "buy":
            return current_cash - delta
        return current_cash + delta

    def _advance_positions(
        self,
        current_positions: list[dict[str, Any]],
        *,
        ticker: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> list[dict[str, Any]]:
        updated = [dict(position) for position in current_positions]
        for position in updated:
            if str(position.get("ticker", "")).upper() != ticker:
                continue
            current_qty = Decimal(str(position.get("quantity", 0)))
            if side == "buy":
                new_qty = current_qty + quantity
                old_avg = Decimal(str(position.get("averagePrice", price)))
                position["quantity"] = float(new_qty)
                if new_qty > 0:
                    position["averagePrice"] = float(
                        ((old_avg * current_qty) + (price * quantity)) / new_qty
                    )
            else:
                new_qty = max(Decimal("0"), current_qty - quantity)
                position["quantity"] = float(new_qty)
            return [position for position in updated if Decimal(str(position.get("quantity", 0))) > 0]

        if side == "buy":
            updated.append(
                {
                    "ticker": ticker,
                    "quantity": float(quantity),
                    "averagePrice": float(price),
                    "currentPrice": float(price),
                    "maxSell": float(quantity),
                }
            )
        return updated

    async def _update_strategy_state(
        self,
        strategy: Strategy,
        *,
        status: str,
        actor: str,
        reason: str | None = None,
        decision_date: date | None = None,
        target_weights: dict[str, Decimal] | None = None,
        current_weights: dict[str, float] | None = None,
        mode: str | None = None,
        orders_submitted: int | None = None,
        dry_run_orders: int | None = None,
        risk_blocks: int | None = None,
        allocation_decisions: list[dict[str, Any]] | None = None,
        allocation_blocks: int | None = None,
    ) -> None:
        params = dict(strategy.params or {})
        state = dict(params.get(PORTFOLIO_STATE_KEY, {}))
        state["last_run_at"] = datetime.now(UTC).isoformat()
        state["last_status"] = status
        state["last_actor"] = actor
        if reason is not None:
            state["last_reason"] = reason
        if decision_date is not None:
            state["last_rebalance_signal_at"] = datetime.combine(decision_date, datetime.min.time(), tzinfo=UTC).isoformat()
        if target_weights is not None:
            state["last_target_weights"] = {ticker: float(weight) for ticker, weight in target_weights.items()}
        if current_weights is not None:
            state["last_current_weights"] = current_weights
        if mode is not None:
            state["last_mode"] = mode
        if orders_submitted is not None:
            state["last_orders_submitted"] = orders_submitted
        if dry_run_orders is not None:
            state["last_dry_run_orders"] = dry_run_orders
        if risk_blocks is not None:
            state["last_risk_blocks"] = risk_blocks
        if allocation_decisions is not None:
            state["last_allocation_decisions"] = allocation_decisions[-10:]
        if allocation_blocks is not None:
            state["last_allocation_blocks"] = allocation_blocks
        params[PORTFOLIO_STATE_KEY] = state
        strategy.params = params

        await self._add_audit_log(
            action="portfolio_rebalance_state",
            actor=actor,
            entity_type="strategy",
            entity_id=str(strategy.id),
            payload={
                "strategy": strategy.name,
                "status": status,
                "reason": reason,
                "mode": mode,
                "decision_date": decision_date.isoformat() if decision_date else None,
            },
        )

    async def _add_audit_log(
        self,
        *,
        action: str,
        actor: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> None:
        self.db.add(
            AuditLog(
                id=uuid.uuid4(),
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.flush()

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value
