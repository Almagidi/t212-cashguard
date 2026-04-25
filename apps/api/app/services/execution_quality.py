"""Execution-quality analytics for broker orders."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from app.db.models import Order, Signal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TERMINAL_STATUSES = {"filled", "cancelled", "rejected", "error"}
ABNORMAL_SLIPPAGE_PCT = Decimal("0.75")
ABNORMAL_SLIPPAGE_VALUE = Decimal("25.00")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _avg(values: list[float | int | None]) -> float | None:
    filtered = [float(value) for value in values if value is not None]
    if not filtered:
        return None
    return sum(filtered) / len(filtered)


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _aware_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def milliseconds_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, int((end - start).total_seconds() * 1000))


def infer_execution_environment(order: Order) -> str:
    if order.execution_environment:
        return order.execution_environment
    if order.is_dry_run:
        return "dry_run"
    return "broker"


def infer_expected_fill_price(order: Order) -> Decimal | None:
    expected = _decimal(order.expected_fill_price)
    if expected and expected > 0:
        return expected
    if order.signal and order.signal.entry_price and order.signal.entry_price > 0:
        return order.signal.entry_price
    if order.limit_price and order.limit_price > 0:
        return order.limit_price
    if order.stop_price and order.stop_price > 0:
        return order.stop_price
    return None


def _infer_terminal_time(order: Order, status: str) -> datetime | None:
    if status == "filled":
        return order.filled_at or order.first_ack_at or order.updated_at
    if status == "cancelled":
        return order.cancelled_at or order.updated_at
    if status in {"rejected", "error"}:
        return order.rejected_at or order.updated_at
    return None


def calculate_order_execution_quality(order: Order) -> dict[str, Any]:
    """Return normalized execution-quality metrics without mutating the order."""
    expected = infer_expected_fill_price(order)
    actual = _decimal(order.avg_fill_price)
    filled_qty = _decimal(order.filled_quantity) or _decimal(order.quantity)
    status = str(order.status or "").lower()
    submitted_at = order.submitted_at
    first_ack_at = order.first_ack_at
    filled_at = order.filled_at or (order.first_ack_at if status == "filled" else None)
    cancelled_at = order.cancelled_at
    rejected_at = order.rejected_at
    terminal_at = _infer_terminal_time(order, status)

    slippage_pct: Decimal | None = _decimal(order.slippage_pct)
    slippage_value: Decimal | None = _decimal(order.slippage_value)
    if (
        slippage_pct is None
        and expected is not None
        and actual is not None
        and filled_qty is not None
        and expected > 0
        and actual > 0
        and filled_qty != 0
    ):
        if order.side == "sell":
            adverse_per_share = expected - actual
        else:
            adverse_per_share = actual - expected
        slippage_pct = (adverse_per_share / expected * Decimal("100")).quantize(Decimal("0.0001"))
        slippage_value = (adverse_per_share * abs(filled_qty)).quantize(Decimal("0.0001"))

    broker_latency_ms = order.broker_latency_ms
    if broker_latency_ms is None:
        broker_latency_ms = milliseconds_between(submitted_at, first_ack_at)

    fill_latency_ms = order.fill_latency_ms
    if fill_latency_ms is None:
        fill_latency_ms = milliseconds_between(submitted_at, filled_at)

    reconciliation_latency_ms = order.reconciliation_latency_ms
    if reconciliation_latency_ms is None and terminal_at is not None:
        reconciliation_latency_ms = milliseconds_between(submitted_at, terminal_at)

    score, grade, notes = score_execution_quality(
        status=status,
        order_type=order.order_type,
        slippage_pct=slippage_pct,
        broker_latency_ms=broker_latency_ms,
        fill_latency_ms=fill_latency_ms,
        reconciliation_latency_ms=reconciliation_latency_ms,
        existing_notes=order.execution_quality_notes,
    )

    return {
        "execution_environment": infer_execution_environment(order),
        "expected_fill_price": expected,
        "slippage_pct": slippage_pct,
        "slippage_value": slippage_value,
        "broker_latency_ms": broker_latency_ms,
        "fill_latency_ms": fill_latency_ms,
        "reconciliation_latency_ms": reconciliation_latency_ms,
        "execution_quality_score": score,
        "execution_quality_grade": grade,
        "execution_quality_notes": notes,
    }


def score_execution_quality(
    *,
    status: str,
    order_type: str,
    slippage_pct: Decimal | None,
    broker_latency_ms: int | None,
    fill_latency_ms: int | None,
    reconciliation_latency_ms: int | None,
    existing_notes: dict[str, Any] | None = None,
) -> tuple[Decimal | None, str, dict[str, Any]]:
    notes: dict[str, Any] = dict(existing_notes or {})
    notes["status"] = status
    notes.pop("penalties", None)
    penalties: dict[str, float] = {}

    if status not in TERMINAL_STATUSES:
        notes["pending"] = True
        return None, "pending", notes

    if status == "error":
        penalties["terminal_error"] = 75.0
    elif status == "rejected":
        penalties["broker_rejection"] = 70.0
    elif status == "cancelled":
        penalties["cancelled"] = 35.0

    adverse_slippage = float(slippage_pct) if slippage_pct is not None and slippage_pct > 0 else 0.0
    if adverse_slippage > 0:
        penalties["slippage"] = min(45.0, adverse_slippage * 18.0)

    if broker_latency_ms is not None and broker_latency_ms > 1500:
        penalties["first_ack_latency"] = min(15.0, (broker_latency_ms - 1500) / 500)

    fill_threshold_ms = 5_000 if order_type == "market" else 60_000
    if fill_latency_ms is not None and fill_latency_ms > fill_threshold_ms:
        penalties["fill_latency"] = min(20.0, (fill_latency_ms - fill_threshold_ms) / 3000)

    if reconciliation_latency_ms is not None and reconciliation_latency_ms > 60_000:
        penalties["reconciliation_latency"] = min(10.0, (reconciliation_latency_ms - 60_000) / 10_000)

    raw_score = max(0.0, 100.0 - sum(penalties.values()))
    score = Decimal(str(round(raw_score, 2))).quantize(Decimal("0.01"))
    grade = grade_execution_quality(float(score))
    notes["penalties"] = {key: round(value, 2) for key, value in penalties.items()}
    return score, grade, notes


def grade_execution_quality(score: float | None) -> str:
    if score is None:
        return "pending"
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 60:
        return "watch"
    if score >= 40:
        return "degraded"
    return "poor"


def apply_order_execution_quality(order: Order) -> dict[str, Any]:
    metrics = calculate_order_execution_quality(order)
    order.execution_environment = metrics["execution_environment"]
    order.expected_fill_price = metrics["expected_fill_price"]
    order.slippage_pct = metrics["slippage_pct"]
    order.slippage_value = metrics["slippage_value"]
    order.broker_latency_ms = metrics["broker_latency_ms"]
    order.fill_latency_ms = metrics["fill_latency_ms"]
    order.reconciliation_latency_ms = metrics["reconciliation_latency_ms"]
    order.execution_quality_score = metrics["execution_quality_score"]
    order.execution_quality_grade = metrics["execution_quality_grade"]
    order.execution_quality_notes = metrics["execution_quality_notes"]
    return metrics


def should_alert_abnormal_slippage(order: Order) -> bool:
    if order.is_dry_run or order.status != "filled":
        return False
    notes = dict(order.execution_quality_notes or {})
    if notes.get("slippage_alerted"):
        return False
    slippage_pct = _decimal(order.slippage_pct)
    slippage_value = _decimal(order.slippage_value)
    return (
        slippage_pct is not None
        and slippage_pct > 0
        and (
            slippage_pct >= ABNORMAL_SLIPPAGE_PCT
            or (slippage_value is not None and slippage_value >= ABNORMAL_SLIPPAGE_VALUE)
        )
    )


def mark_slippage_alerted(order: Order) -> None:
    notes = dict(order.execution_quality_notes or {})
    notes["slippage_alerted"] = True
    order.execution_quality_notes = notes


class ExecutionQualityService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def report(self, *, days: int = 30, include_dry_run: bool = False) -> dict[str, Any]:
        since = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(Order)
            .where(Order.created_at >= since)
            .options(selectinload(Order.signal).selectinload(Signal.strategy))
            .order_by(desc(Order.created_at))
        )
        if not include_dry_run:
            stmt = stmt.where(Order.is_dry_run == False)  # noqa: E712

        result = await self.db.execute(stmt)
        orders = list(result.scalars().all())
        snapshots = [
            self._snapshot(order)
            for order in orders
        ]

        return {
            "period_days": days,
            "generated_at": datetime.now(UTC),
            "include_dry_run": include_dry_run,
            "summary": self._summary(snapshots, since=since, days=days),
            "by_symbol_order_type": self._bucket_by_symbol_order_type(snapshots),
            "reject_cancel_patterns": self._reject_cancel_patterns(snapshots),
            "worst_orders": self._worst_orders(snapshots),
        }

    def _snapshot(self, order: Order) -> dict[str, Any]:
        metrics = calculate_order_execution_quality(order)
        return {
            "id": str(order.id),
            "ticker": order.ticker,
            "side": order.side,
            "order_type": order.order_type,
            "status": order.status,
            "environment": metrics["execution_environment"],
            "is_dry_run": order.is_dry_run,
            "expected_fill_price": _float(metrics["expected_fill_price"]),
            "avg_fill_price": _float(order.avg_fill_price),
            "slippage_pct": _float(metrics["slippage_pct"]),
            "slippage_value": _float(metrics["slippage_value"]),
            "broker_latency_ms": metrics["broker_latency_ms"],
            "fill_latency_ms": metrics["fill_latency_ms"],
            "reconciliation_latency_ms": metrics["reconciliation_latency_ms"],
            "score": _float(metrics["execution_quality_score"]),
            "grade": metrics["execution_quality_grade"],
            "error_message": order.error_message,
            "created_at": _aware_datetime(order.created_at),
        }

    def _summary(self, snapshots: list[dict[str, Any]], *, since: datetime, days: int) -> dict[str, Any]:
        total = len(snapshots)
        filled = sum(1 for row in snapshots if row["status"] == "filled")
        rejected = sum(1 for row in snapshots if row["status"] == "rejected")
        cancelled = sum(1 for row in snapshots if row["status"] == "cancelled")
        errored = sum(1 for row in snapshots if row["status"] == "error")
        scores = [row["score"] for row in snapshots if row["score"] is not None]
        avg_score = _avg(scores)
        adverse_slips = [
            row["slippage_pct"]
            for row in snapshots
            if row["slippage_pct"] is not None and row["slippage_pct"] > 0
        ]
        abnormal_slippage_count = sum(
            1
            for row in snapshots
            if row["slippage_pct"] is not None
            and row["slippage_pct"] >= float(ABNORMAL_SLIPPAGE_PCT)
        )

        midpoint = since + timedelta(days=days / 2)
        recent_scores = [
            row["score"]
            for row in snapshots
            if row["score"] is not None and row["created_at"] >= midpoint
        ]
        previous_scores = [
            row["score"]
            for row in snapshots
            if row["score"] is not None and row["created_at"] < midpoint
        ]
        recent_avg = _avg(recent_scores)
        previous_avg = _avg(previous_scores)
        score_delta = None if recent_avg is None or previous_avg is None else recent_avg - previous_avg
        status, reason = self._status_and_reason(
            total=total,
            avg_score=avg_score,
            score_delta=score_delta,
            reject_rate=_pct(rejected, total),
            cancel_rate=_pct(cancelled, total),
            error_rate=_pct(errored, total),
            abnormal_slippage_count=abnormal_slippage_count,
        )

        environments = sorted({row["environment"] for row in snapshots})

        return {
            "status": status,
            "status_reason": reason,
            "total_orders": total,
            "filled_orders": filled,
            "rejected_orders": rejected,
            "cancelled_orders": cancelled,
            "error_orders": errored,
            "fill_rate": _pct(filled, total),
            "reject_rate": _pct(rejected, total),
            "cancel_rate": _pct(cancelled, total),
            "error_rate": _pct(errored, total),
            "avg_score": _round(avg_score),
            "score_delta": _round(score_delta),
            "avg_slippage_pct": _round(_avg(adverse_slips), 4),
            "total_slippage_value": _round(sum(row["slippage_value"] or 0 for row in snapshots), 2),
            "adverse_slippage_rate": _pct(len(adverse_slips), filled),
            "abnormal_slippage_count": abnormal_slippage_count,
            "avg_broker_latency_ms": _round(_avg([row["broker_latency_ms"] for row in snapshots]), 0),
            "avg_fill_latency_ms": _round(_avg([row["fill_latency_ms"] for row in snapshots]), 0),
            "avg_reconciliation_latency_ms": _round(
                _avg([row["reconciliation_latency_ms"] for row in snapshots]),
                0,
            ),
            "environments": environments,
        }

    def _status_and_reason(
        self,
        *,
        total: int,
        avg_score: float | None,
        score_delta: float | None,
        reject_rate: float,
        cancel_rate: float,
        error_rate: float,
        abnormal_slippage_count: int,
    ) -> tuple[str, str]:
        if total == 0:
            return "no_data", "No broker execution data in this window."
        if avg_score is not None and avg_score < 65:
            return "degraded", "Execution score is below the production trust threshold."
        if reject_rate >= 0.10 or error_rate >= 0.05:
            return "degraded", "Broker rejection or error rate is elevated."
        if abnormal_slippage_count >= 2:
            return "degraded", "Multiple abnormal adverse slippage events were detected."
        if score_delta is not None and score_delta <= -8:
            return "watch", "Recent execution score is falling versus the previous half of the window."
        if avg_score is not None and avg_score < 80:
            return "watch", "Execution quality is usable but below the preferred score band."
        if cancel_rate >= 0.10:
            return "watch", "Cancel rate is elevated; check order type and venue behavior."
        return "ok", "Execution quality is stable in this window."

    def _bucket_by_symbol_order_type(self, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in snapshots:
            buckets[(row["environment"], row["ticker"], row["order_type"])].append(row)

        output = []
        for (environment, ticker, order_type), rows in buckets.items():
            total = len(rows)
            filled = sum(1 for row in rows if row["status"] == "filled")
            rejected = sum(1 for row in rows if row["status"] == "rejected")
            cancelled = sum(1 for row in rows if row["status"] == "cancelled")
            errored = sum(1 for row in rows if row["status"] == "error")
            adverse = [row["slippage_pct"] for row in rows if row["slippage_pct"] is not None and row["slippage_pct"] > 0]
            output.append({
                "environment": environment,
                "ticker": ticker,
                "order_type": order_type,
                "order_count": total,
                "filled_count": filled,
                "rejected_count": rejected,
                "cancelled_count": cancelled,
                "error_count": errored,
                "fill_rate": _pct(filled, total),
                "avg_score": _round(_avg([row["score"] for row in rows])),
                "avg_slippage_pct": _round(_avg(adverse), 4),
                "total_slippage_value": _round(sum(row["slippage_value"] or 0 for row in rows), 2),
                "avg_broker_latency_ms": _round(_avg([row["broker_latency_ms"] for row in rows]), 0),
                "avg_fill_latency_ms": _round(_avg([row["fill_latency_ms"] for row in rows]), 0),
                "worst_slippage_pct": _round(max(adverse), 4) if adverse else None,
            })

        output.sort(key=lambda row: (row["avg_score"] is None, row["avg_score"] or 0, row["ticker"]))
        return output

    def _reject_cancel_patterns(self, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in snapshots:
            if row["status"] not in {"rejected", "cancelled", "error"}:
                continue
            reason = (row["error_message"] or row["status"]).strip()[:160]
            key = (row["status"], row["ticker"], row["order_type"], reason)
            existing = grouped.setdefault(
                key,
                {
                    "status": row["status"],
                    "ticker": row["ticker"],
                    "order_type": row["order_type"],
                    "reason": reason,
                    "count": 0,
                    "last_seen_at": row["created_at"],
                },
            )
            existing["count"] += 1
            if row["created_at"] > existing["last_seen_at"]:
                existing["last_seen_at"] = row["created_at"]

        patterns = list(grouped.values())
        patterns.sort(key=lambda row: (-row["count"], row["status"], row["ticker"]))
        return patterns[:12]

    def _worst_orders(self, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = [
            row
            for row in snapshots
            if row["status"] == "filled" and (row["score"] is not None or row["slippage_pct"] is not None)
        ]
        candidates.sort(
            key=lambda row: (
                row["score"] if row["score"] is not None else 101,
                -(row["slippage_pct"] or 0),
            )
        )
        return [
            {
                "id": row["id"],
                "ticker": row["ticker"],
                "side": row["side"],
                "order_type": row["order_type"],
                "environment": row["environment"],
                "status": row["status"],
                "expected_fill_price": row["expected_fill_price"],
                "avg_fill_price": row["avg_fill_price"],
                "slippage_pct": _round(row["slippage_pct"], 4),
                "slippage_value": _round(row["slippage_value"], 2),
                "broker_latency_ms": row["broker_latency_ms"],
                "fill_latency_ms": row["fill_latency_ms"],
                "score": _round(row["score"]),
                "grade": row["grade"],
                "created_at": row["created_at"],
            }
            for row in candidates[:10]
        ]
