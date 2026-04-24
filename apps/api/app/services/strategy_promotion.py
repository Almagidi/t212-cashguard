"""
Per-strategy promotion pipeline: dry-run -> demo -> live approval.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from app.core.config import settings
from app.db.models import AuditLog, Order, Signal, Strategy
from app.services.live_readiness import LiveReadinessService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


PromotionAction = Literal[
    "record_dry_run_review",
    "promote_to_demo",
    "record_demo_review",
    "promote_to_live",
    "demote_to_dry_run",
    "revoke_live_promotion",
]

PromotionStage = Literal["dry_run", "demo", "live_approved"]

_PROMOTION_KEY = "promotion"


class StrategyPromotionError(Exception):
    """Raised when a promotion action is unsafe or invalid."""


class StrategyPromotionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    @staticmethod
    def _promotion_state(strategy: Strategy) -> dict[str, Any]:
        params = strategy.params or {}
        promotion = params.get(_PROMOTION_KEY)
        return dict(promotion) if isinstance(promotion, dict) else {}

    @staticmethod
    def _set_promotion_state(strategy: Strategy, promotion: dict[str, Any]) -> None:
        params = dict(strategy.params or {})
        params[_PROMOTION_KEY] = promotion
        strategy.params = params

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not value or not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _normalize_timestamp(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _serialize_timestamp(value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat() if value is not None else None

    @staticmethod
    def _status(passed: bool) -> Literal["pass", "fail"]:
        return "pass" if passed else "fail"

    @staticmethod
    def _check(
        *,
        phase: Literal["demo", "live"],
        key: str,
        label: str,
        passed: bool,
        detail: str,
        verified_at: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "phase": phase,
            "key": key,
            "label": label,
            "status": StrategyPromotionService._status(passed),
            "detail": detail,
            "verified_at": verified_at,
        }

    async def _get_strategy(self, strategy_id: uuid.UUID) -> Strategy:
        result = await self.db.execute(select(Strategy).where(Strategy.id == strategy_id))
        strategy = result.scalar_one_or_none()
        if strategy is None:
            raise StrategyPromotionError("Strategy not found.")
        return strategy

    async def _load_strategy_activity(
        self,
        strategy_id: uuid.UUID,
    ) -> tuple[list[Signal], list[Order]]:
        signal_result = await self.db.execute(
            select(Signal)
            .where(Signal.strategy_id == strategy_id)
            .order_by(Signal.generated_at.asc())
        )
        signals = list(signal_result.scalars().all())

        order_result = await self.db.execute(
            select(Order)
            .join(Signal, Order.signal_id == Signal.id)
            .where(Signal.strategy_id == strategy_id)
            .order_by(Order.created_at.asc())
        )
        orders = list(order_result.scalars().all())
        return signals, orders

    def _strategy_stage(self, promotion: dict[str, Any]) -> PromotionStage:
        if self._parse_timestamp(promotion.get("live_approved_at")) is not None:
            return "live_approved"
        if self._parse_timestamp(promotion.get("demo_promoted_at")) is not None:
            return "demo"
        return "dry_run"

    async def evaluate(self, strategy_id: uuid.UUID) -> dict[str, Any]:
        strategy = await self._get_strategy(strategy_id)
        return await self.evaluate_strategy(strategy)

    async def evaluate_strategy(self, strategy: Strategy) -> dict[str, Any]:
        promotion = self._promotion_state(strategy)
        signals, orders = await self._load_strategy_activity(strategy.id)

        dry_run_reviewed_at = self._parse_timestamp(promotion.get("dry_run_reviewed_at"))
        demo_promoted_at = self._parse_timestamp(promotion.get("demo_promoted_at"))
        demo_reviewed_at = self._parse_timestamp(promotion.get("demo_reviewed_at"))
        live_approved_at = self._parse_timestamp(promotion.get("live_approved_at"))

        dry_phase_signals = [
            signal for signal in signals
            if demo_promoted_at is None or self._normalize_timestamp(signal.generated_at) < demo_promoted_at
        ]
        dry_phase_orders = [
            order for order in orders
            if order.is_dry_run and (
                demo_promoted_at is None or self._normalize_timestamp(order.created_at) < demo_promoted_at
            )
        ]
        demo_phase_signals = [
            signal for signal in signals
            if demo_promoted_at is not None
            and self._normalize_timestamp(signal.generated_at) >= demo_promoted_at
            and (
                live_approved_at is None
                or self._normalize_timestamp(signal.generated_at) < live_approved_at
            )
        ]
        demo_phase_orders = [
            order for order in orders
            if demo_promoted_at is not None
            and not order.is_dry_run
            and self._normalize_timestamp(order.created_at) >= demo_promoted_at
            and (
                live_approved_at is None
                or self._normalize_timestamp(order.created_at) < live_approved_at
            )
        ]

        dry_dates = {
            self._normalize_timestamp(signal.generated_at).date().isoformat()
            for signal in dry_phase_signals
        }
        dry_dates.update(
            self._normalize_timestamp(order.created_at).date().isoformat()
            for order in dry_phase_orders
        )
        demo_dates = {
            self._normalize_timestamp(signal.generated_at).date().isoformat()
            for signal in demo_phase_signals
        }
        demo_dates.update(
            self._normalize_timestamp(order.created_at).date().isoformat()
            for order in demo_phase_orders
        )

        demo_order_total = len(demo_phase_orders)
        demo_filled = sum(1 for order in demo_phase_orders if order.status == "filled")
        demo_rejected = sum(1 for order in demo_phase_orders if order.status == "rejected")
        demo_error = sum(1 for order in demo_phase_orders if order.status == "error")
        demo_cancelled = sum(1 for order in demo_phase_orders if order.status == "cancelled")
        demo_error_like = demo_rejected + demo_error
        demo_fill_rate = (demo_filled / demo_order_total) if demo_order_total else 0.0
        demo_error_rate = (demo_error_like / demo_order_total) if demo_order_total else 0.0
        demo_risk_blocks = sum(1 for signal in demo_phase_signals if signal.risk_rejected)
        demo_risk_block_rate = (
            demo_risk_blocks / len(demo_phase_signals)
            if demo_phase_signals else 0.0
        )

        live_readiness = await LiveReadinessService(self.db).evaluate()
        live_readiness_ready = bool(live_readiness["eligible_for_unlock"])
        live_readiness_detail = (
            "Global live-readiness checklist has passed."
            if live_readiness_ready
            else (live_readiness["blockers"][0] if live_readiness["blockers"] else "Global live-readiness is incomplete.")
        )

        checks = [
            self._check(
                phase="demo",
                key="risk_profile",
                label="Risk profile attached",
                passed=strategy.risk_profile_id is not None,
                detail=(
                    "Risk profile attached."
                    if strategy.risk_profile_id is not None
                    else "Attach a risk profile before promoting beyond dry-run."
                ),
            ),
            self._check(
                phase="demo",
                key="ticker_universe",
                label="Ticker universe configured",
                passed=bool(strategy.allowed_tickers),
                detail=(
                    f"{len(strategy.allowed_tickers)} symbols configured."
                    if strategy.allowed_tickers
                    else "Add at least one allowed ticker before promotion."
                ),
            ),
            self._check(
                phase="demo",
                key="dry_run_signals",
                label="Minimum dry-run sample reached",
                passed=len(dry_phase_signals) >= settings.STRATEGY_PROMOTION_MIN_DRY_RUN_SIGNALS,
                detail=(
                    f"{len(dry_phase_signals)} dry-run signals recorded."
                    f" Need at least {settings.STRATEGY_PROMOTION_MIN_DRY_RUN_SIGNALS}."
                ),
            ),
            self._check(
                phase="demo",
                key="dry_run_days",
                label="Dry-run soak duration reached",
                passed=len(dry_dates) >= settings.STRATEGY_PROMOTION_MIN_DRY_RUN_DAYS,
                detail=(
                    f"Dry-run activity seen on {len(dry_dates)} trading day(s)."
                    f" Need at least {settings.STRATEGY_PROMOTION_MIN_DRY_RUN_DAYS}."
                ),
            ),
            self._check(
                phase="demo",
                key="dry_run_review",
                label="Dry-run manually reviewed",
                passed=dry_run_reviewed_at is not None,
                detail=(
                    f"Dry-run review recorded by {promotion.get('dry_run_reviewed_by', 'an admin')}."
                    if dry_run_reviewed_at is not None
                    else "Record a dry-run review before enabling demo broker execution."
                ),
                verified_at=dry_run_reviewed_at,
            ),
            self._check(
                phase="demo",
                key="app_not_live",
                label="App not currently in live mode",
                passed=settings.APP_MODE != "live",
                detail=(
                    f"Current app mode is `{settings.APP_MODE}`."
                    if settings.APP_MODE != "live"
                    else "Switch the app out of live mode before using demo promotion."
                ),
            ),
            self._check(
                phase="live",
                key="demo_promoted",
                label="Demo broker execution was promoted",
                passed=demo_promoted_at is not None,
                detail=(
                    f"Demo promotion recorded by {promotion.get('demo_promoted_by', 'an admin')}."
                    if demo_promoted_at is not None
                    else "Promote the strategy to demo execution before live approval."
                ),
                verified_at=demo_promoted_at,
            ),
            self._check(
                phase="live",
                key="demo_review",
                label="Demo execution manually reviewed",
                passed=demo_reviewed_at is not None,
                detail=(
                    f"Demo review recorded by {promotion.get('demo_reviewed_by', 'an admin')}."
                    if demo_reviewed_at is not None
                    else "Record a manual demo review after observing broker-side demo behavior."
                ),
                verified_at=demo_reviewed_at,
            ),
            self._check(
                phase="live",
                key="demo_orders",
                label="Minimum demo order sample reached",
                passed=demo_order_total >= settings.STRATEGY_PROMOTION_MIN_DEMO_ORDERS,
                detail=(
                    f"{demo_order_total} demo broker orders recorded."
                    f" Need at least {settings.STRATEGY_PROMOTION_MIN_DEMO_ORDERS}."
                ),
            ),
            self._check(
                phase="live",
                key="demo_days",
                label="Demo soak duration reached",
                passed=len(demo_dates) >= settings.STRATEGY_PROMOTION_MIN_DEMO_DAYS,
                detail=(
                    f"Demo activity seen on {len(demo_dates)} trading day(s)."
                    f" Need at least {settings.STRATEGY_PROMOTION_MIN_DEMO_DAYS}."
                ),
            ),
            self._check(
                phase="live",
                key="demo_fill_rate",
                label="Demo fill rate acceptable",
                passed=demo_order_total > 0 and demo_fill_rate >= settings.STRATEGY_PROMOTION_MIN_DEMO_FILL_RATE,
                detail=(
                    f"Fill rate {demo_fill_rate:.0%} across {demo_order_total} demo orders."
                    f" Need at least {settings.STRATEGY_PROMOTION_MIN_DEMO_FILL_RATE:.0%}."
                ),
            ),
            self._check(
                phase="live",
                key="demo_error_rate",
                label="Demo order error rate acceptable",
                passed=demo_order_total > 0 and demo_error_rate <= settings.STRATEGY_PROMOTION_MAX_DEMO_ERROR_RATE,
                detail=(
                    f"Error/reject rate {demo_error_rate:.0%}."
                    f" Must stay at or below {settings.STRATEGY_PROMOTION_MAX_DEMO_ERROR_RATE:.0%}."
                ),
            ),
            self._check(
                phase="live",
                key="demo_risk_block_rate",
                label="Demo risk-block rate acceptable",
                passed=(
                    len(demo_phase_signals) > 0
                    and demo_risk_block_rate <= settings.STRATEGY_PROMOTION_MAX_DEMO_RISK_BLOCK_RATE
                ),
                detail=(
                    f"Risk blocks on {demo_risk_blocks}/{len(demo_phase_signals)} demo signals"
                    f" ({demo_risk_block_rate:.0%}). Must stay at or below"
                    f" {settings.STRATEGY_PROMOTION_MAX_DEMO_RISK_BLOCK_RATE:.0%}."
                ),
            ),
            self._check(
                phase="live",
                key="global_live_readiness",
                label="Global live-readiness eligible",
                passed=live_readiness_ready,
                detail=live_readiness_detail,
            ),
        ]

        demo_checks = [check for check in checks if check["phase"] == "demo"]
        live_checks = [check for check in checks if check["phase"] == "live"]
        eligible_for_demo = all(check["status"] == "pass" for check in demo_checks)
        eligible_for_live = all(check["status"] == "pass" for check in live_checks)

        blockers: list[str] = []
        if strategy.is_live and demo_promoted_at is None:
            blockers.append(
                "Broker execution is enabled without a recorded demo promotion. Move the strategy back to dry-run and re-promote through the gated flow."
            )
        blockers.extend(check["detail"] for check in demo_checks if check["status"] != "pass")
        if demo_promoted_at is not None or live_approved_at is not None:
            blockers.extend(check["detail"] for check in live_checks if check["status"] != "pass")

        recommended_next_action: PromotionAction | None = None
        if dry_run_reviewed_at is None and len(dry_phase_signals) > 0:
            recommended_next_action = "record_dry_run_review"
        elif demo_promoted_at is None and eligible_for_demo:
            recommended_next_action = "promote_to_demo"
        elif demo_promoted_at is not None and demo_reviewed_at is None and demo_order_total > 0:
            recommended_next_action = "record_demo_review"
        elif live_approved_at is None and eligible_for_live:
            recommended_next_action = "promote_to_live"

        return {
            "strategy_id": strategy.id,
            "strategy_name": strategy.name,
            "current_stage": self._strategy_stage(promotion),
            "broker_execution_enabled": bool(strategy.is_live),
            "demo_execution_enabled": bool(strategy.is_live and demo_promoted_at is not None),
            "live_execution_approved": live_approved_at is not None,
            "eligible_for_demo": eligible_for_demo,
            "eligible_for_live": eligible_for_live,
            "recommended_next_action": recommended_next_action,
            "blockers": blockers,
            "checks": checks,
            "metrics": {
                "dry_run_signal_count": len(dry_phase_signals),
                "dry_run_order_count": len(dry_phase_orders),
                "dry_run_days": len(dry_dates),
                "dry_run_reviewed_at": self._serialize_timestamp(dry_run_reviewed_at),
                "demo_order_count": demo_order_total,
                "demo_filled_count": demo_filled,
                "demo_rejected_count": demo_rejected,
                "demo_error_count": demo_error,
                "demo_cancelled_count": demo_cancelled,
                "demo_days": len(demo_dates),
                "demo_fill_rate": demo_fill_rate,
                "demo_error_rate": demo_error_rate,
                "demo_signal_count": len(demo_phase_signals),
                "demo_risk_block_count": demo_risk_blocks,
                "demo_risk_block_rate": demo_risk_block_rate,
                "demo_promoted_at": self._serialize_timestamp(demo_promoted_at),
                "demo_reviewed_at": self._serialize_timestamp(demo_reviewed_at),
                "live_approved_at": self._serialize_timestamp(live_approved_at),
            },
        }

    async def execution_gate(self, strategy: Strategy) -> tuple[bool, str | None]:
        promotion = self._promotion_state(strategy)
        demo_promoted_at = self._parse_timestamp(promotion.get("demo_promoted_at"))
        live_approved_at = self._parse_timestamp(promotion.get("live_approved_at"))

        if not strategy.is_live or settings.APP_MODE == "mock":
            return True, None
        if settings.APP_MODE == "demo" and demo_promoted_at is None:
            return False, "strategy_not_promoted_to_demo"
        if settings.APP_MODE == "live" and live_approved_at is None:
            return False, "strategy_not_approved_for_live"
        return True, None

    async def apply_action(
        self,
        *,
        strategy_id: uuid.UUID,
        action: PromotionAction,
        actor: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        strategy = await self._get_strategy(strategy_id)
        promotion = self._promotion_state(strategy)
        now = datetime.now(UTC)
        status = await self.evaluate_strategy(strategy)

        if action == "record_dry_run_review":
            if status["metrics"]["dry_run_signal_count"] <= 0 and status["metrics"]["dry_run_order_count"] <= 0:
                raise StrategyPromotionError("Run the strategy in dry-run first so there is evidence to review.")
            promotion["dry_run_reviewed_at"] = now.isoformat()
            promotion["dry_run_reviewed_by"] = actor
            if notes:
                promotion["dry_run_review_notes"] = notes
            audit_action = "strategy_dry_run_review_recorded"
        elif action == "promote_to_demo":
            if not status["eligible_for_demo"]:
                raise StrategyPromotionError("This strategy has not yet satisfied the dry-run promotion checklist.")
            strategy.is_live = True
            promotion["demo_promoted_at"] = now.isoformat()
            promotion["demo_promoted_by"] = actor
            if notes:
                promotion["demo_promotion_notes"] = notes
            audit_action = "strategy_promoted_to_demo"
        elif action == "record_demo_review":
            if status["metrics"]["demo_order_count"] <= 0:
                raise StrategyPromotionError("Demo broker execution must produce at least one order before review can be recorded.")
            promotion["demo_reviewed_at"] = now.isoformat()
            promotion["demo_reviewed_by"] = actor
            if notes:
                promotion["demo_review_notes"] = notes
            audit_action = "strategy_demo_review_recorded"
        elif action == "promote_to_live":
            if not status["eligible_for_live"]:
                raise StrategyPromotionError("This strategy has not yet satisfied the live promotion checklist.")
            strategy.is_live = True
            promotion["live_approved_at"] = now.isoformat()
            promotion["live_approved_by"] = actor
            if notes:
                promotion["live_approval_notes"] = notes
            audit_action = "strategy_promoted_to_live"
        elif action == "demote_to_dry_run":
            strategy.is_live = False
            promotion["demoted_to_dry_run_at"] = now.isoformat()
            promotion["demoted_to_dry_run_by"] = actor
            if notes:
                promotion["demoted_to_dry_run_notes"] = notes
            audit_action = "strategy_demoted_to_dry_run"
        elif action == "revoke_live_promotion":
            promotion["live_approved_at"] = None
            promotion["live_approved_by"] = None
            if notes:
                promotion["live_approval_revoked_notes"] = notes
            audit_action = "strategy_live_promotion_revoked"
        else:
            raise StrategyPromotionError("Unsupported promotion action.")

        self._set_promotion_state(strategy, promotion)
        self.db.add(
            AuditLog(
                action=audit_action,
                entity_type="strategy",
                entity_id=str(strategy.id),
                actor=actor,
                payload={
                    "action": action,
                    "notes": notes,
                },
                occurred_at=now,
            )
        )
        await self.db.flush()
        await self.db.refresh(strategy)
        return await self.evaluate_strategy(strategy)
