"""Unit tests for the paper-only DCA scheduler task."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.schedules import crontab
from sqlalchemy import func, select

from app.db.models import AuditLog, DcaPlanState, Order
from app.db.models import DcaConfig as DcaConfigRow
from app.db.seed import seed_dca_configs
from app.strategies.kraken_dca_planner import (
    DCAConfig,
    DCADecision,
    DCADecisionCode,
    KrakenDCAPlanner,
)
from app.workers import tasks_dca

TODAY = date(2026, 4, 29)


class FakeKrakenProvider:
    def __init__(self, *, price: Decimal = Decimal("50000")) -> None:
        self.price = price
        self.get_quote = AsyncMock(
            return_value=SimpleNamespace(last=price)
        )
        self.get_bars = AsyncMock(
            return_value=[
                SimpleNamespace(
                    open=Decimal("51000"),
                    high=Decimal("52000"),
                    low=Decimal("49000"),
                    close=Decimal("50500"),
                    volume=Decimal("10"),
                )
                for _ in range(20)
            ]
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _provider_factory(provider: FakeKrakenProvider):
    def factory():
        return provider

    return factory


def _single_config(**overrides) -> list[DCAConfig]:
    params = {
        "ticker": "BTC/USD",
        "cadence_days": 7,
        "base_allocation_usd": Decimal("100"),
        "min_cash_reserve_usd": Decimal("500"),
        "max_position_pct": 25.0,
        "paper_only": True,
        "enabled": True,
        "venue": "kraken",
    }
    params.update(overrides)
    return [DCAConfig(**params)]


class TestDcaBeatSchedule:
    def test_dca_task_is_registered_on_daily_cadence(self):
        from app.workers.celery_app import celery_app

        cfg = celery_app.conf.beat_schedule["dca-paper-evaluate"]
        assert cfg["task"] == "app.workers.tasks_dca.evaluate_due_plans_task"
        assert isinstance(cfg["schedule"], crontab)
        assert cfg["schedule"]._orig_hour == 1
        assert cfg["schedule"]._orig_minute == 0

    def test_dca_task_is_separate_from_main_strategy_runner(self):
        from app.workers.celery_app import celery_app

        assert celery_app.conf.beat_schedule["strategy-signals"]["task"] == (
            "app.workers.tasks.run_strategy_signals"
        )
        assert celery_app.conf.beat_schedule["dca-paper-evaluate"]["task"] != (
            "app.workers.tasks.run_strategy_signals"
        )
        assert celery_app.conf.beat_schedule["strategy-signals"]["schedule"] == 300.0
        assert isinstance(celery_app.conf.beat_schedule["dca-paper-evaluate"]["schedule"], crontab)


class TestDcaConfigLoading:
    @pytest.mark.asyncio
    async def test_scheduler_loads_persisted_enabled_configs_by_default(self, db):
        provider = FakeKrakenProvider()
        db.add(
            DcaConfigRow(
                ticker="BTC/USD",
                venue="kraken",
                cadence_days=11,
                fixed_cash_amount=Decimal("75.00000000"),
                dip_buy_enabled=False,
                dip_threshold_pct=Decimal("6.5000"),
                dip_buy_multiplier=Decimal("1.5000"),
                dip_ema_period=12,
                min_cash_reserve=Decimal("250.00000000"),
                max_position_percent=Decimal("9.0000"),
                paper_only=True,
                enabled=True,
            )
        )
        await db.commit()

        summary = await tasks_dca.evaluate_due_plans(
            db,
            now=TODAY,
            provider_factory=_provider_factory(provider),
            available_cash=Decimal("10000"),
            account_value=Decimal("100000"),
        )

        row = (
            await db.execute(select(DcaPlanState).where(DcaPlanState.ticker == "BTC/USD"))
        ).scalar_one()
        assert summary["evaluated"] == 1
        assert row.last_decision_at == TODAY
        assert row.total_allocated_usd == Decimal("75.00000000")
        provider.get_bars.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scheduler_ignores_disabled_configs_by_default(self, db):
        provider = FakeKrakenProvider()
        db.add(
            DcaConfigRow(
                ticker="BTC/USD",
                venue="kraken",
                paper_only=True,
                enabled=False,
            )
        )
        await db.commit()

        summary = await tasks_dca.evaluate_due_plans(
            db,
            now=TODAY,
            provider_factory=_provider_factory(provider),
            available_cash=Decimal("10000"),
            account_value=Decimal("100000"),
        )

        assert summary == {"evaluated": 0, "buy_due": 0, "non_buy": 0, "errors": [], "paper_only": True}
        provider.get_quote.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scheduler_does_nothing_safely_when_no_configs_exist(self, db):
        provider = FakeKrakenProvider()

        summary = await tasks_dca.evaluate_due_plans(
            db,
            now=TODAY,
            provider_factory=_provider_factory(provider),
        )

        assert summary == {"evaluated": 0, "buy_due": 0, "non_buy": 0, "errors": [], "paper_only": True}
        provider.get_quote.assert_not_awaited()
        assert not hasattr(tasks_dca, "load_default_dca_configs")


class TestDcaEvaluationPersistence:
    @pytest.mark.asyncio
    async def test_missing_state_becomes_zero_default_state_and_buy_due_persists(self, db):
        provider = FakeKrakenProvider()

        summary = await tasks_dca.evaluate_due_plans(
            db,
            now=TODAY,
            provider_factory=_provider_factory(provider),
            config_loader=_single_config,
            available_cash=Decimal("10000"),
            account_value=Decimal("100000"),
        )

        row = (
            await db.execute(select(DcaPlanState).where(DcaPlanState.ticker == "BTC/USD"))
        ).scalar_one()
        assert summary == {"evaluated": 1, "buy_due": 1, "non_buy": 0, "errors": [], "paper_only": True}
        assert row.last_buy_at == TODAY
        assert row.last_decision_at == TODAY
        assert row.total_allocated_usd == Decimal("100.00000000")
        assert row.executions_count == 1
        assert row.last_decision_code == "BUY_DUE"
        provider.get_quote.assert_awaited_once_with("BTC/USD")
        provider.get_bars.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_existing_state_is_loaded_before_evaluation(self, db):
        db.add(
            DcaPlanState(
                ticker="BTC/USD",
                venue="kraken",
                last_buy_at=date(2026, 4, 20),
                last_decision_at=date(2026, 4, 20),
                total_allocated_usd=Decimal("300"),
                executions_count=3,
                last_decision_code="BUY_DUE",
                last_reason="previous",
            )
        )
        await db.commit()

        await tasks_dca.evaluate_due_plans(
            db,
            now=TODAY,
            provider_factory=_provider_factory(FakeKrakenProvider()),
            config_loader=_single_config,
            available_cash=Decimal("10000"),
            account_value=Decimal("100000"),
        )

        row = (
            await db.execute(select(DcaPlanState).where(DcaPlanState.ticker == "BTC/USD"))
        ).scalar_one()
        assert row.total_allocated_usd == Decimal("400.00000000")
        assert row.executions_count == 4

    @pytest.mark.asyncio
    async def test_non_buy_decision_persists_decision_metadata_only(self, db):
        db.add(
            DcaPlanState(
                ticker="BTC/USD",
                venue="kraken",
                last_buy_at=date(2026, 4, 27),
                total_allocated_usd=Decimal("100"),
                executions_count=1,
            )
        )
        await db.commit()

        await tasks_dca.evaluate_due_plans(
            db,
            now=TODAY,
            provider_factory=_provider_factory(FakeKrakenProvider()),
            config_loader=_single_config,
            available_cash=Decimal("10000"),
            account_value=Decimal("100000"),
        )

        row = (
            await db.execute(select(DcaPlanState).where(DcaPlanState.ticker == "BTC/USD"))
        ).scalar_one()
        assert row.last_buy_at == date(2026, 4, 27)
        assert row.total_allocated_usd == Decimal("100.00000000")
        assert row.executions_count == 1
        assert row.last_decision_at == TODAY
        assert row.last_decision_code == "SKIP_ALREADY_BOUGHT_THIS_WINDOW"
        assert row.last_reason is not None

    @pytest.mark.asyncio
    async def test_audit_log_records_paper_decision_without_orders(self, db, monkeypatch):
        monkeypatch.setattr(tasks_dca, "AuditLog", MagicMock(wraps=AuditLog))

        await tasks_dca.evaluate_due_plans(
            db,
            now=TODAY,
            provider_factory=_provider_factory(FakeKrakenProvider()),
            config_loader=_single_config,
            available_cash=Decimal("10000"),
            account_value=Decimal("100000"),
        )

        audit = (await db.execute(select(AuditLog))).scalar_one()
        assert audit.action == "dca_paper_decision"
        assert audit.entity_type == "dca_plan_state"
        assert audit.payload["decision_code"] == "BUY_DUE"
        assert audit.payload["paper_only"] is True
        tasks_dca.AuditLog.assert_called_once()
        assert not hasattr(tasks_dca, "ExecutionEngine")
        assert not hasattr(tasks_dca, "OrderRepository")

    @pytest.mark.asyncio
    async def test_custom_planner_returning_non_buy_still_persists_metadata(self, db):
        planner = MagicMock()
        planner.evaluate_plan.return_value = DCADecision(
            code=DCADecisionCode.BLOCKED_LOW_CASH,
            should_accumulate=False,
            amount_usd=Decimal("0"),
            mode="skip",
            reason="cash blocked",
        )

        await tasks_dca.evaluate_due_plans(
            db,
            now=TODAY,
            provider_factory=_provider_factory(FakeKrakenProvider()),
            config_loader=_single_config,
            planner_factory=lambda: planner,
            available_cash=Decimal("10000"),
            account_value=Decimal("100000"),
        )

        row = (
            await db.execute(select(DcaPlanState).where(DcaPlanState.ticker == "BTC/USD"))
        ).scalar_one()
        assert row.last_buy_at is None
        assert row.executions_count == 0
        assert row.last_decision_code == "BLOCKED_LOW_CASH"
        planner.evaluate_plan.assert_called_once()

    @pytest.mark.asyncio
    async def test_persisted_enabled_seed_config_evaluates_without_orders_or_execution(
        self,
        db,
        monkeypatch,
    ):
        from app.execution.engine import ExecutionEngine
        from app.services.strategy_runner import StrategyRunner

        await seed_dca_configs(db)
        btc_config = (
            await db.execute(
                select(DcaConfigRow).where(
                    DcaConfigRow.ticker == "BTC/USD",
                    DcaConfigRow.venue == "kraken",
                )
            )
        ).scalar_one()
        btc_config.enabled = True
        btc_config.paper_only = True
        await db.commit()

        create_order_intent = MagicMock()
        monkeypatch.setattr(ExecutionEngine, "create_order_intent", create_order_intent)
        provider = FakeKrakenProvider()

        summary = await tasks_dca.evaluate_due_plans(
            db,
            now=TODAY,
            provider_factory=_provider_factory(provider),
            available_cash=Decimal("10000"),
            account_value=Decimal("100000"),
        )

        plan_state = (
            await db.execute(
                select(DcaPlanState).where(
                    DcaPlanState.ticker == "BTC/USD",
                    DcaPlanState.venue == "kraken",
                )
            )
        ).scalar_one()
        audit = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "dca_paper_decision")
            )
        ).scalar_one()
        order_count = (await db.execute(select(func.count()).select_from(Order))).scalar_one()

        assert summary == {"evaluated": 1, "buy_due": 1, "non_buy": 0, "errors": [], "paper_only": True}
        assert plan_state.last_buy_at == TODAY
        assert plan_state.last_decision_at == TODAY
        assert plan_state.last_decision_code == "BUY_DUE"
        assert audit.entity_id == "kraken:BTC/USD"
        assert audit.payload["paper_only"] is True
        assert audit.payload["decision_code"] == "BUY_DUE"
        assert order_count == 0
        create_order_intent.assert_not_called()
        provider.get_quote.assert_awaited_once_with("BTC/USD")
        provider.get_bars.assert_awaited_once()

        runner = StrategyRunner(MagicMock())
        strategy = MagicMock()
        strategy.type = "kraken_dca"
        strategy.params = {}
        assert runner._make_engine(strategy) is None


class TestDcaFailSafeGuards:
    @pytest.mark.asyncio
    async def test_fails_safe_if_runnable_becomes_true(self, db, monkeypatch):
        monkeypatch.setattr(KrakenDCAPlanner, "RUNNABLE", True)

        with pytest.raises(RuntimeError, match="RUNNABLE must remain False"):
            await tasks_dca.evaluate_due_plans(
                db,
                provider_factory=_provider_factory(FakeKrakenProvider()),
                config_loader=_single_config,
                available_cash=Decimal("10000"),
                account_value=Decimal("100000"),
            )

    @pytest.mark.asyncio
    async def test_fails_safe_if_paper_only_becomes_false(self, db, monkeypatch):
        monkeypatch.setattr(KrakenDCAPlanner, "PAPER_ONLY", False)

        with pytest.raises(RuntimeError, match="PAPER_ONLY must remain True"):
            await tasks_dca.evaluate_due_plans(
                db,
                provider_factory=_provider_factory(FakeKrakenProvider()),
                config_loader=_single_config,
                available_cash=Decimal("10000"),
                account_value=Decimal("100000"),
            )

    def test_dca_remains_non_runnable_through_make_engine(self):
        from app.services.strategy_runner import StrategyRunner

        runner = StrategyRunner(MagicMock())
        strategy = MagicMock()
        strategy.type = "kraken_dca"
        strategy.params = {}
        assert runner._make_engine(strategy) is None
