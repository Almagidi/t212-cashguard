"""
Prometheus metrics for CashGuard Trader.
Exposes /metrics endpoint for scraping by Prometheus.

The kill switches and auto-trading switches are read from the database at every scrape
(``app.api.metrics_state``), so they show what is saved, whichever process saved it, and
"unknown" when it cannot be read. The counters and histograms below belong to this process
only: they start at zero on every restart, other API replicas and the Celery workers keep
their own, and most have no producer yet. Account cash, account value and open positions
are not exported: no producer here can name the account and the age of those figures.
"""

from __future__ import annotations

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        Info,
        generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

router = APIRouter(tags=["metrics"])

if PROMETHEUS_AVAILABLE:
    # ── Trading activity ──────────────────────────────────────────────────────
    orders_placed = Counter(
        "cashguard_orders_placed_total",
        "Total orders placed",
        ["side", "order_type", "status", "is_dry_run"],
    )

    orders_filled = Counter(
        "cashguard_orders_filled_total",
        "Total orders filled",
        ["side", "ticker"],
    )

    cash_guard_blocks = Counter(
        "cashguard_cash_guard_blocks_total",
        "Orders blocked by cash guard",
    )

    risk_violations = Counter(
        "cashguard_risk_violations_total",
        "Risk engine violations",
        ["violation_type"],
    )

    # No producer. Activations are recorded in risk_events by whichever process makes them;
    # cashguard_kill_switch_last_activation_timestamp_seconds exports the newest of them.
    kill_switch_activations = Counter(
        "cashguard_kill_switch_activations_total",
        "Kill switch activation events",
        ["actor"],
    )

    signals_generated = Counter(
        "cashguard_signals_generated_total",
        "Strategy signals generated",
        ["strategy_type", "side", "is_live"],
    )

    # ── Broker / connectivity ─────────────────────────────────────────────────
    broker_request_duration = Histogram(
        "cashguard_broker_request_duration_seconds",
        "Trading 212 API request duration",
        ["endpoint"],
        buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )

    broker_errors = Counter(
        "cashguard_broker_errors_total",
        "Broker API errors",
        ["endpoint", "status_code"],
    )

    circuit_breaker_state = Gauge(
        "cashguard_circuit_breaker_open",
        "Whether the broker circuit breaker is open (1=open, 0=closed)",
        ["name"],
    )

    # ── App health ────────────────────────────────────────────────────────────
    app_info = Info(
        "cashguard_app",
        "Application info",
    )
    app_info.info({"version": "1.0.0", "mode": "unknown"})

    # ── Worker health ─────────────────────────────────────────────────────────
    task_failures = Counter(
        "cashguard_task_failures_total",
        "Celery tasks that exhausted all retries and were dead-lettered",
        ["task_name"],
    )


def get_metrics_session_factory() -> async_sessionmaker[AsyncSession]:
    """Sessions for the scrape-time state read (overridden in tests)."""
    from app.db.session import AsyncSessionLocal

    return AsyncSessionLocal


@router.get("/metrics")
async def metrics(
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_metrics_session_factory),
) -> Response:
    """Prometheus metrics endpoint."""
    if not PROMETHEUS_AVAILABLE:
        return Response(
            content="# prometheus_client not installed\n",
            media_type="text/plain",
        )
    from app.api.metrics_state import render_safety_state, scrape_safety_state

    state = await scrape_safety_state(session_factory)
    return Response(
        content=generate_latest() + render_safety_state(state),
        media_type=CONTENT_TYPE_LATEST,
    )


# ── Helper functions called from other modules ────────────────────────────────


def record_order_placed(
    side: str,
    order_type: str,
    status: str,
    is_dry_run: bool,
) -> None:
    if PROMETHEUS_AVAILABLE:
        orders_placed.labels(
            side=side,
            order_type=order_type,
            status=status,
            is_dry_run=str(is_dry_run),
        ).inc()


def record_risk_violation(violation_type: str) -> None:
    if PROMETHEUS_AVAILABLE:
        risk_violations.labels(violation_type=violation_type).inc()
        if violation_type == "cash_guard_block":
            cash_guard_blocks.inc()


def record_signal(strategy_type: str, side: str, is_live: bool) -> None:
    if PROMETHEUS_AVAILABLE:
        signals_generated.labels(
            strategy_type=strategy_type,
            side=side,
            is_live=str(is_live),
        ).inc()


def record_task_failure(task_name: str) -> None:
    if PROMETHEUS_AVAILABLE:
        task_failures.labels(task_name=task_name).inc()
