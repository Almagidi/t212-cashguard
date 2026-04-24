"""
Prometheus metrics for CashGuard Trader.
Exposes /metrics endpoint for scraping by Prometheus.

Key metrics:
- orders_placed_total        - trading activity
- cash_guard_blocks_total    - safety signal
- risk_violations_total      - risk engine health
- broker_request_duration    - latency
- kill_switch_activations    - critical safety event
- circuit_breaker_state      - broker connectivity
"""
from __future__ import annotations

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        Info,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

from fastapi import APIRouter, Response

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

    # ── Account ───────────────────────────────────────────────────────────────
    account_cash = Gauge(
        "cashguard_account_cash_available",
        "Available cash balance for trading",
    )

    account_total_value = Gauge(
        "cashguard_account_total_value",
        "Total account value",
    )

    open_positions_count = Gauge(
        "cashguard_open_positions_count",
        "Number of currently open positions",
    )

    # ── App health ────────────────────────────────────────────────────────────
    app_info = Info(
        "cashguard_app",
        "Application info",
    )
    app_info.info({"version": "1.0.0", "mode": "unknown"})

    auto_trading_enabled = Gauge(
        "cashguard_auto_trading_enabled",
        "Whether auto trading is enabled (1=yes, 0=no)",
    )

    kill_switch_active = Gauge(
        "cashguard_kill_switch_active",
        "Whether kill switch is active (1=active, 0=inactive)",
    )


@router.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    if not PROMETHEUS_AVAILABLE:
        return Response(
            content="# prometheus_client not installed\n",
            media_type="text/plain",
        )
    return Response(
        content=generate_latest(),
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


def record_kill_switch(actor: str) -> None:
    if PROMETHEUS_AVAILABLE:
        kill_switch_activations.labels(actor=actor).inc()
        kill_switch_active.set(1)


def record_kill_switch_deactivated() -> None:
    if PROMETHEUS_AVAILABLE:
        kill_switch_active.set(0)


def record_signal(strategy_type: str, side: str, is_live: bool) -> None:
    if PROMETHEUS_AVAILABLE:
        signals_generated.labels(
            strategy_type=strategy_type,
            side=side,
            is_live=str(is_live),
        ).inc()


def update_account_metrics(cash: float, total: float, positions: int) -> None:
    if PROMETHEUS_AVAILABLE:
        account_cash.set(cash)
        account_total_value.set(total)
        open_positions_count.set(positions)
