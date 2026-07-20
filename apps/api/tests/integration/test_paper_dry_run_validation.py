"""Automated paper-trade dry-run safety-path validation.

This module is a cohesive, scenario-oriented proof that the current system can
exercise an *automated paper-mode dry run* end to end **without** enabling live
trading or invoking a real broker. It walks the documented safety chain:

    signal / dry-run trigger
    -> risk / safety gate
    -> paper-only order path
    -> audit / event visibility
    -> reconciliation visibility (and its paper-order isolation boundary)
    -> operator status visibility
    -> kill switch blocks the path

It complements (does not replace) the existing focused suites in
``test_paper_execution.py`` (HTTP surface), ``test_operator_status_api.py``
(operator status), ``test_demo_reconciliation.py`` (demo reconciler) and
``test_strategy_runner_provider_equivalence.py`` (automated runner gates). The
tests here assert on the *service layer* directly so the safety narrative is
provable without a live server, network, credentials, or timing sleeps.

Everything runs against the in-memory SQLite ``db`` fixture in ``APP_MODE=mock``.
No Trading 212 or Kraken adapter is constructed, and no order-placement method is
ever called.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import AppSettings, AuditLog, Order, PositionSnapshot, User
from app.execution.paper_engine import (
    PAPER_EXECUTION_ENVIRONMENT,
    PaperExecutionEngine,
    PaperExecutionError,
    paper_execution_summary,
)
from app.services.safety_policy import SafetyPolicyViolation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ─── Local test helpers (kept under tests/, no runtime changes) ───────────────


async def _seed_user(db: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="paper-dry-run@test.com",
        hashed_password=hash_password("testpassword123"),
        is_active=True,
        is_admin=True,
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_settings(db: AsyncSession, *, kill_switch_active: bool = False) -> AppSettings:
    app_settings = AppSettings(
        id=1,
        theme="dark",
        timezone="UTC",
        auto_trading_enabled=True,
        kill_switch_active=kill_switch_active,
        live_trading_unlocked=False,
    )
    db.add(app_settings)
    await db.flush()
    return app_settings


def _paper_body(**overrides):
    """Build a validated PaperOrderCreate (schema enforces paper_only=True)."""
    from app.api.schemas import PaperOrderCreate

    payload = {
        "ticker": "PAPERDRY",
        "side": "buy",
        "quantity": Decimal("2"),
        "estimated_price": Decimal("25.50"),
        "source": "automation_dry_run",
        "strategy": "paper-dry-run-validation",
        "venue": "paper",
        "paper_only": True,
    }
    payload.update(overrides)
    return PaperOrderCreate(**payload)


def _no_broker_adapters_constructed(monkeypatch) -> list[str]:
    """Trip a marker if any real broker adapter is instantiated.

    Returns a list that stays empty unless a Trading 212 / Kraken adapter or the
    provider factory is constructed during the exercised path.
    """
    constructed: list[str] = []

    from app.broker import kraken, provider, trading212

    original_t212_init = trading212.Trading212Adapter.__init__
    original_kraken_init = kraken.KrakenAdapter.__init__

    def _trip_t212(self, *args, **kwargs):
        constructed.append("Trading212Adapter")
        return original_t212_init(self, *args, **kwargs)

    def _trip_kraken(self, *args, **kwargs):
        constructed.append("KrakenAdapter")
        return original_kraken_init(self, *args, **kwargs)

    def _trip_provider(*args, **kwargs):
        constructed.append("create_trading212_provider_adapter")
        raise AssertionError("provider factory must not be called on the paper path")

    monkeypatch.setattr(trading212.Trading212Adapter, "__init__", _trip_t212)
    monkeypatch.setattr(kraken.KrakenAdapter, "__init__", _trip_kraken)
    monkeypatch.setattr(
        provider,
        "create_trading212_provider_adapter",
        _trip_provider,
    )
    return constructed


# ─── Scenario 1 — Paper-only happy path (automated/dry-run trigger) ───────────


@pytest.mark.asyncio
async def test_paper_happy_path_produces_local_dry_run_order_without_broker(
    db: AsyncSession,
    monkeypatch,
):
    """An automation-sourced signal flows into the paper-only order path.

    Proves: order is explicit paper-only + dry-run, live mode stays off, no real
    broker adapter/provider is constructed, a filled paper Order is produced, and
    a full audit + order-event trail is written.
    """
    user = await _seed_user(db)
    await _seed_settings(db)
    constructed = _no_broker_adapters_constructed(monkeypatch)

    engine = PaperExecutionEngine(db)
    order = await engine.execute(_paper_body(source="automation_dry_run"), user=user)

    # Paper-only + dry-run is explicit and persisted.
    assert order.is_dry_run is True
    assert order.execution_environment == PAPER_EXECUTION_ENVIRONMENT
    assert order.status == "filled"
    assert order.broker_order_id is None
    assert order.broker_request["no_broker_order_sent"] is True
    assert order.broker_response["status"] == "PAPER_FILLED"

    # Live trading remained disabled; no broker adapter/provider was constructed.
    settings_row = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalar_one()
    assert settings_row.live_trading_unlocked is False
    assert constructed == []

    # Audit + event trail exists and is uniformly paper-only / no-broker.
    audits = (await db.execute(select(AuditLog))).scalars().all()
    actions = {audit.action for audit in audits}
    assert actions >= {
        "paper_signal_accepted",
        "paper_risk_check_result",
        "paper_order_created",
        "paper_fill_simulated",
        "paper_position_updated",
    }
    paper_audits = [audit for audit in audits if audit.action.startswith("paper_")]
    assert paper_audits
    assert all(audit.payload["paper_only"] is True for audit in paper_audits)
    assert all(audit.payload["no_broker_order_sent"] is True for audit in paper_audits)


# ─── Scenario 2 — Kill switch blocks the path (endpoint + automation) ─────────


@pytest.mark.asyncio
async def test_kill_switch_blocks_paper_execution_and_creates_no_order(
    db: AsyncSession,
    monkeypatch,
):
    """With the global kill switch active, the paper path fails closed.

    Proves: paper execution raises before any Order is created, the block is
    audited with the ``kill_switch_block`` decision code, and no broker adapter
    is constructed.
    """
    user = await _seed_user(db)
    await _seed_settings(db, kill_switch_active=True)
    constructed = _no_broker_adapters_constructed(monkeypatch)

    engine = PaperExecutionEngine(db)
    with pytest.raises(PaperExecutionError, match="Kill switch"):
        await engine.execute(_paper_body(ticker="BLOCKED"), user=user)

    assert (await db.execute(select(Order))).scalars().all() == []
    assert constructed == []

    audits = (await db.execute(select(AuditLog))).scalars().all()
    actions = [audit.action for audit in audits]
    assert "paper_signal_rejected" in actions
    risk_audit = next(a for a in audits if a.action == "paper_risk_check_result")
    assert risk_audit.payload["result"] == "blocked"
    assert risk_audit.payload["decision_code"] == "kill_switch_block"


@pytest.mark.asyncio
async def test_kill_switch_skips_automated_strategy_runner_before_broker_lookup(
    db: AsyncSession,
    monkeypatch,
):
    """The automated (scheduled) strategy runner also fails closed on kill switch.

    Proves: ``run_all_enabled`` short-circuits with ``skipped=kill_switch`` and
    never reaches broker construction — the automated trigger cannot bypass the
    kill switch. Guards against regression by tripping if a broker is built.
    """
    await _seed_settings(db, kill_switch_active=True)
    constructed = _no_broker_adapters_constructed(monkeypatch)

    from app.services.strategy_runner import StrategyRunner

    runner = StrategyRunner(db)

    async def _fail_if_called() -> None:
        raise AssertionError("kill switch must skip before broker lookup")

    monkeypatch.setattr(runner, "_get_broker", _fail_if_called)

    summary = await runner.run_all_enabled()

    assert summary["skipped"] == "kill_switch"
    assert summary["orders_submitted"] == 0
    assert constructed == []


# ─── Scenario 3 — Risk / safety gate blocks invalid or unsafe orders ──────────


@pytest.mark.asyncio
async def test_paper_execution_refused_outside_mock_mode(
    db: AsyncSession,
    monkeypatch,
):
    """Paper execution is only available in APP_MODE=mock and fails closed elsewhere.

    Proves the ``PAPER_MODE_BLOCK`` boundary: even a well-formed paper-only order
    is refused (403) when APP_MODE is not mock, with no Order created.
    """
    from app.core.config import settings as app_config

    user = await _seed_user(db)
    await _seed_settings(db)
    monkeypatch.setattr(app_config, "APP_MODE", "demo")

    engine = PaperExecutionEngine(db)
    with pytest.raises(PaperExecutionError) as exc_info:
        await engine.execute(_paper_body(ticker="WRONGMODE"), user=user)

    assert exc_info.value.status_code == 403
    assert (await db.execute(select(Order))).scalars().all() == []
    audits = (await db.execute(select(AuditLog))).scalars().all()
    rejected = next(a for a in audits if a.action == "paper_signal_rejected")
    assert rejected.payload["decision_code"] == "PAPER_MODE_BLOCK"


@pytest.mark.asyncio
async def test_paper_sell_exceeding_position_is_blocked_before_fill(
    db: AsyncSession,
):
    """An unsafe paper sell (oversell) is rejected before any fill.

    Proves the risk/position gate: selling more than the open paper quantity is
    refused with ``paper_oversell_block`` and leaves the buy position intact.
    """
    user = await _seed_user(db)
    await _seed_settings(db)
    engine = PaperExecutionEngine(db)

    await engine.execute(
        _paper_body(ticker="OVERSELL", side="buy", quantity=Decimal("2")), user=user
    )

    with pytest.raises(PaperExecutionError, match="exceeds available paper quantity"):
        await engine.execute(
            _paper_body(ticker="OVERSELL", side="sell", quantity=Decimal("3")),
            user=user,
        )

    orders = (await db.execute(select(Order).where(Order.ticker == "OVERSELL"))).scalars().all()
    assert len(orders) == 1  # only the buy exists; the oversell created no order
    audits = (await db.execute(select(AuditLog))).scalars().all()
    oversell = [
        a
        for a in audits
        if a.action == "paper_signal_rejected"
        and a.payload.get("decision_code") == "paper_oversell_block"
    ]
    assert oversell
    assert oversell[-1].payload["no_broker_order_sent"] is True


def test_paper_order_schema_forbids_non_paper_payloads():
    """The request schema pins paper_only=True and rejects live-ish payloads.

    Proves the safety gate at the boundary: ``paper_only=False`` and unknown
    fields (e.g. ``live``) are rejected by validation before any service runs.
    """
    from pydantic import ValidationError

    from app.api.schemas import PaperOrderCreate

    with pytest.raises(ValidationError):
        PaperOrderCreate(ticker="X", side="buy", quantity=Decimal("1"), paper_only=False)
    with pytest.raises(ValidationError):
        PaperOrderCreate(
            ticker="X",
            side="buy",
            quantity=Decimal("1"),
            live=True,  # type: ignore[call-arg]
        )


# ─── Scenario 4 — Reconciliation + operator visibility (and paper isolation) ──


@pytest.mark.asyncio
async def test_demo_reconciler_refuses_paper_dry_run_order(
    db: AsyncSession,
    monkeypatch,
):
    """The real-broker demo reconciler must never touch a paper/dry-run order.

    This is the reconciliation *isolation* boundary: a paper order is filled
    locally and is intentionally NOT eligible for Trading 212 demo history
    reconciliation. A paper order carries ``execution_environment='paper_mock'``,
    so the reconciler refuses it at the environment gate
    (``demo_reconciliation_order_environment_block``) — which is evaluated before
    any broker read. The stricter ``demo_reconciliation_dry_run_block`` gate then
    also guards demo-environment orders that are dry-run; both keep paper fills
    off the real reconciliation path.
    """
    from app.core.config import settings as app_config
    from app.services.demo_order_reconciliation import DemoOrderReconciler

    user = await _seed_user(db)
    await _seed_settings(db)
    paper_order = await PaperExecutionEngine(db).execute(_paper_body(ticker="NORECON"), user=user)

    # Put the reconciler's environment gates into their "demo" happy state so the
    # ONLY thing that can block is the paper/dry-run nature of the order itself.
    monkeypatch.setattr(app_config, "APP_MODE", "demo")
    monkeypatch.setattr(app_config, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(app_config, "LIVE_TRADING_ENABLED", False)

    class _BrokerShouldNotBeRead:
        environment = "demo"

        async def get_historical_orders(self, *args, **kwargs):
            raise AssertionError("paper order must be refused before any broker read")

    reconciler = DemoOrderReconciler(db, _BrokerShouldNotBeRead())

    with pytest.raises(SafetyPolicyViolation) as exc_info:
        await reconciler.reconcile_order(paper_order)

    # Paper orders are refused at the environment gate (paper_mock != demo),
    # before the dry-run gate and before any broker history read.
    assert exc_info.value.decision_code == "demo_reconciliation_order_environment_block"


@pytest.mark.asyncio
async def test_operator_paper_execution_summary_reflects_paper_fill(
    db: AsyncSession,
):
    """Operator visibility: the read-only paper-execution summary sees the fill.

    Proves that after a paper dry-run fill, ``paper_execution_summary`` (the same
    aggregation the operator status endpoint exposes) reports the order and open
    position as paper-only with no broker order sent — read-only, no controls.
    """
    user = await _seed_user(db)
    await _seed_settings(db)
    await PaperExecutionEngine(db).execute(_paper_body(ticker="VISIBLE"), user=user)

    summary = await paper_execution_summary(db)

    assert summary["paper_only"] is True
    assert summary["no_broker_order_sent"] is True
    assert summary["total_paper_orders"] == 1
    assert summary["last_paper_execution_status"] == "filled"
    assert summary["open_paper_positions_count"] == 1
    assert "No broker order sent." in summary["safety_notes"]

    # The position snapshot itself is tagged paper-only.
    snapshot = (
        await db.execute(select(PositionSnapshot).where(PositionSnapshot.ticker == "VISIBLE"))
    ).scalar_one()
    assert snapshot.raw["paper_only"] is True
    assert snapshot.raw["no_broker_order_sent"] is True
