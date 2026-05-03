"""
Tests for DcaPlanStateRepository and dca_state_from_row.

Coverage:
  - ORM model shape is usable through SQLAlchemy (SQLite in-memory)
  - Repository can create and fetch a new state row
  - upsert creates when absent, updates when present
  - All updatable fields (last_buy_at, last_decision_at, total_allocated_usd,
    executions_count, last_decision_code, last_reason) round-trip correctly
  - UniqueConstraint on (ticker, venue) is enforced
  - Decimal/money fields round-trip safely
  - DCA remains non-runnable after this pass
  - No scheduler task was introduced

Mock-based tests verify the interface without a real DB.
DB-backed tests (using the conftest 'db' fixture) verify actual persistence.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import DcaPlanState
from app.db.repositories.dca_plan_state_repo import DcaPlanStateRepository, dca_state_from_row


# ── Mock helpers (mirrors test_repositories.py pattern) ───────────────────────

def _db_with_result(result: MagicMock) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalars_result(values):
    scalars = MagicMock()
    scalars.all.return_value = values
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


# ── Mock-based interface tests ─────────────────────────────────────────────────

class TestDcaPlanStateRepositoryMock:
    @pytest.mark.asyncio
    async def test_get_by_ticker_venue_returns_scalar(self):
        row = MagicMock(spec=DcaPlanState)
        repo = DcaPlanStateRepository(_db_with_result(_scalar_result(row)))
        assert await repo.get_by_ticker_venue("BTC/USD", "kraken") is row

    @pytest.mark.asyncio
    async def test_get_by_ticker_venue_returns_none_when_absent(self):
        repo = DcaPlanStateRepository(_db_with_result(_scalar_result(None)))
        assert await repo.get_by_ticker_venue("BTC/USD", "kraken") is None

    @pytest.mark.asyncio
    async def test_create_adds_and_flushes(self):
        db = _db_with_result(MagicMock())
        repo = DcaPlanStateRepository(db)
        state = MagicMock(spec=DcaPlanState)

        result = await repo.create(state)

        assert result is state
        db.add.assert_called_once_with(state)
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_creates_when_row_absent(self):
        db = _db_with_result(_scalar_result(None))
        repo = DcaPlanStateRepository(db)
        updates = {"last_decision_code": "BUY_DUE", "executions_count": 1}

        result = await repo.upsert("BTC/USD", "kraken", updates)

        db.add.assert_called_once()
        db.flush.assert_awaited_once()
        assert isinstance(result, DcaPlanState)
        assert result.ticker == "BTC/USD"
        assert result.venue == "kraken"

    @pytest.mark.asyncio
    async def test_upsert_updates_when_row_present(self):
        existing = MagicMock(spec=DcaPlanState)
        db = _db_with_result(_scalar_result(existing))
        repo = DcaPlanStateRepository(db)
        updates = {"last_decision_code": "SKIP_ALREADY_BOUGHT_THIS_WINDOW", "executions_count": 3}

        result = await repo.upsert("BTC/USD", "kraken", updates)

        db.add.assert_not_called()
        db.flush.assert_awaited_once()
        assert result is existing

    @pytest.mark.asyncio
    async def test_list_all_returns_scalars(self):
        rows = [MagicMock(spec=DcaPlanState), MagicMock(spec=DcaPlanState)]
        repo = DcaPlanStateRepository(_db_with_result(_scalars_result(rows)))
        assert await repo.list_all() == rows


# ── DB-backed persistence tests (uses conftest 'db' SQLite fixture) ────────────

class TestDcaPlanStateRepositoryDB:
    @pytest.mark.asyncio
    async def test_create_and_fetch_round_trip(self, db):
        repo = DcaPlanStateRepository(db)
        state = DcaPlanState(
            ticker="BTC/USD",
            venue="kraken",
            total_allocated_usd=Decimal("0"),
            executions_count=0,
        )
        created = await repo.create(state)
        await db.commit()

        fetched = await repo.get_by_ticker_venue("BTC/USD", "kraken")
        assert fetched is not None
        assert fetched.ticker == "BTC/USD"
        assert fetched.venue == "kraken"
        assert fetched.executions_count == 0
        assert fetched.total_allocated_usd == Decimal("0")
        assert fetched.last_buy_at is None
        assert fetched.last_decision_at is None
        assert fetched.last_decision_code is None
        assert fetched.last_reason is None
        assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_upsert_creates_row_when_absent(self, db):
        repo = DcaPlanStateRepository(db)
        updates = {
            "last_buy_at": date(2026, 4, 29),
            "last_decision_at": date(2026, 4, 29),
            "total_allocated_usd": Decimal("100.00000000"),
            "executions_count": 1,
            "last_decision_code": "BUY_DUE",
            "last_reason": "Scheduled accumulation: 100 USD for BTC/USD",
        }

        await repo.upsert("BTC/USD", "kraken", updates)
        await db.commit()

        fetched = await repo.get_by_ticker_venue("BTC/USD", "kraken")
        assert fetched is not None
        assert fetched.executions_count == 1
        assert fetched.last_decision_code == "BUY_DUE"
        assert fetched.last_buy_at == date(2026, 4, 29)

    @pytest.mark.asyncio
    async def test_upsert_updates_all_fields_on_existing_row(self, db):
        repo = DcaPlanStateRepository(db)

        await repo.upsert("ETH/USD", "kraken", {
            "total_allocated_usd": Decimal("100"),
            "executions_count": 1,
            "last_buy_at": date(2026, 4, 22),
            "last_decision_at": date(2026, 4, 22),
            "last_decision_code": "BUY_DUE",
            "last_reason": "First buy",
        })
        await db.commit()

        await repo.upsert("ETH/USD", "kraken", {
            "last_buy_at": date(2026, 4, 29),
            "last_decision_at": date(2026, 4, 29),
            "total_allocated_usd": Decimal("200"),
            "executions_count": 2,
            "last_decision_code": "BUY_DUE",
            "last_reason": "Second buy",
        })
        await db.commit()

        fetched = await repo.get_by_ticker_venue("ETH/USD", "kraken")
        assert fetched is not None
        assert fetched.last_buy_at == date(2026, 4, 29)
        assert fetched.last_decision_at == date(2026, 4, 29)
        assert fetched.total_allocated_usd == Decimal("200")
        assert fetched.executions_count == 2
        assert fetched.last_decision_code == "BUY_DUE"
        assert fetched.last_reason == "Second buy"

    @pytest.mark.asyncio
    async def test_upsert_skip_decision_does_not_change_last_buy_at(self, db):
        repo = DcaPlanStateRepository(db)

        await repo.upsert("BTC/USD", "kraken", {
            "last_buy_at": date(2026, 4, 22),
            "last_decision_at": date(2026, 4, 22),
            "total_allocated_usd": Decimal("100"),
            "executions_count": 1,
            "last_decision_code": "BUY_DUE",
            "last_reason": "First buy",
        })
        await db.commit()

        # Skip evaluation — only decision metadata updated, last_buy_at not in updates dict
        await repo.upsert("BTC/USD", "kraken", {
            "last_decision_at": date(2026, 4, 25),
            "last_decision_code": "SKIP_ALREADY_BOUGHT_THIS_WINDOW",
            "last_reason": "Next scheduled buy is 2026-04-29 (4 days away)",
        })
        await db.commit()

        fetched = await repo.get_by_ticker_venue("BTC/USD", "kraken")
        assert fetched.last_buy_at == date(2026, 4, 22)       # unchanged
        assert fetched.executions_count == 1                   # unchanged
        assert fetched.total_allocated_usd == Decimal("100")  # unchanged
        assert fetched.last_decision_at == date(2026, 4, 25)
        assert fetched.last_decision_code == "SKIP_ALREADY_BOUGHT_THIS_WINDOW"

    @pytest.mark.asyncio
    async def test_decimal_money_fields_round_trip_safely(self, db):
        repo = DcaPlanStateRepository(db)
        precise_amount = Decimal("12345.67890123")

        await repo.upsert("BTC/USD", "kraken", {
            "total_allocated_usd": precise_amount,
            "executions_count": 0,
        })
        await db.commit()

        fetched = await repo.get_by_ticker_venue("BTC/USD", "kraken")
        assert fetched.total_allocated_usd == precise_amount

    @pytest.mark.asyncio
    async def test_uniqueness_constraint_ticker_venue(self, db):
        repo = DcaPlanStateRepository(db)

        state1 = DcaPlanState(ticker="BTC/USD", venue="kraken")
        state2 = DcaPlanState(ticker="BTC/USD", venue="kraken")

        await repo.create(state1)
        await db.commit()

        with pytest.raises((IntegrityError, Exception)):
            await repo.create(state2)
            await db.commit()

    @pytest.mark.asyncio
    async def test_different_tickers_same_venue_are_independent_rows(self, db):
        repo = DcaPlanStateRepository(db)

        await repo.upsert("BTC/USD", "kraken", {"executions_count": 5})
        await repo.upsert("ETH/USD", "kraken", {"executions_count": 2})
        await db.commit()

        btc = await repo.get_by_ticker_venue("BTC/USD", "kraken")
        eth = await repo.get_by_ticker_venue("ETH/USD", "kraken")
        assert btc.executions_count == 5
        assert eth.executions_count == 2
        assert btc.id != eth.id

    @pytest.mark.asyncio
    async def test_same_ticker_different_venues_are_independent_rows(self, db):
        repo = DcaPlanStateRepository(db)

        await repo.upsert("BTC/USD", "kraken", {"executions_count": 10})
        await repo.upsert("BTC/USD", "binance", {"executions_count": 3})
        await db.commit()

        kraken_row = await repo.get_by_ticker_venue("BTC/USD", "kraken")
        binance_row = await repo.get_by_ticker_venue("BTC/USD", "binance")
        assert kraken_row.executions_count == 10
        assert binance_row.executions_count == 3

    @pytest.mark.asyncio
    async def test_list_all_returns_all_rows_ordered_by_ticker(self, db):
        repo = DcaPlanStateRepository(db)

        await repo.upsert("ETH/USD", "kraken", {"executions_count": 1})
        await repo.upsert("BTC/USD", "kraken", {"executions_count": 2})
        await db.commit()

        rows = await repo.list_all()
        tickers = [r.ticker for r in rows]
        assert tickers.index("BTC/USD") < tickers.index("ETH/USD")

    @pytest.mark.asyncio
    async def test_get_returns_none_for_unknown_ticker(self, db):
        repo = DcaPlanStateRepository(db)
        result = await repo.get_by_ticker_venue("SOL/USD", "kraken")
        assert result is None


# ── dca_state_from_row conversion helper ──────────────────────────────────────

class TestDcaStateFromRow:
    def test_converts_orm_row_to_dca_state_dataclass(self):
        from app.strategies.kraken_dca_planner import DCAState

        row = DcaPlanState(
            ticker="BTC/USD",
            venue="kraken",
            last_buy_at=date(2026, 4, 22),
            last_decision_at=date(2026, 4, 29),
            total_allocated_usd=Decimal("300.00000000"),
            executions_count=3,
            last_decision_code="BUY_DUE",
            last_reason="Scheduled accumulation: 100 USD for BTC/USD",
        )

        state = dca_state_from_row(row)

        assert isinstance(state, DCAState)
        assert state.ticker == "BTC/USD"
        assert state.venue == "kraken"
        assert state.last_buy_at == date(2026, 4, 22)
        assert state.last_decision_at == date(2026, 4, 29)
        assert state.total_allocated_usd == Decimal("300.00000000")
        assert state.executions_count == 3
        assert state.last_decision_code == "BUY_DUE"
        assert state.last_reason == "Scheduled accumulation: 100 USD for BTC/USD"

    def test_converts_row_with_null_optional_fields(self):
        from app.strategies.kraken_dca_planner import DCAState

        row = DcaPlanState(ticker="ETH/USD", venue="kraken")
        state = dca_state_from_row(row)

        assert isinstance(state, DCAState)
        assert state.last_buy_at is None
        assert state.last_decision_at is None
        assert state.last_decision_code is None
        assert state.last_reason is None

    def test_converted_state_is_accepted_by_planner(self):
        """A state converted from an ORM row is accepted directly by evaluate_plan()."""
        from app.strategies.kraken_dca_planner import (
            DCAConfig,
            DCADecisionCode,
            KrakenDCAPlanner,
        )

        # last_buy_at = 7 days before evaluation → cadence elapsed → BUY_DUE
        row = DcaPlanState(
            ticker="BTC/USD",
            venue="kraken",
            last_buy_at=date(2026, 4, 22),
            total_allocated_usd=Decimal("100"),
            executions_count=1,
        )
        state = dca_state_from_row(row)
        config = DCAConfig(ticker="BTC/USD", cadence_days=7)

        decision = KrakenDCAPlanner().evaluate_plan(
            config=config,
            state=state,
            current_price=Decimal("50000"),
            available_cash=Decimal("10000"),
            account_value=Decimal("100000"),
            now=date(2026, 4, 29),
        )

        assert decision.code == DCADecisionCode.BUY_DUE


# ── Architecture guards: DCA non-runnability after this pass ──────────────────

class TestDcaRemainsNonRunnable:
    def test_runnable_flag_is_false(self):
        from app.strategies.kraken_dca_planner import KrakenDCAPlanner
        assert KrakenDCAPlanner.RUNNABLE is False

    def test_only_dedicated_paper_scheduler_in_beat_schedule(self):
        from app.workers.celery_app import celery_app
        for key, cfg in celery_app.conf.beat_schedule.items():
            task_path = cfg.get("task", "")
            if "dca" in task_path.lower():
                assert key == "dca-paper-evaluate"
                assert task_path == "app.workers.tasks_dca.evaluate_due_plans_task"

    def test_dca_not_constructible_via_make_engine(self):
        from app.services.strategy_runner import StrategyRunner
        runner = StrategyRunner(MagicMock())
        for type_name in ("kraken_dca_planner", "kraken_dca", "dca"):
            strategy = MagicMock()
            strategy.type = type_name
            strategy.params = {}
            assert runner._make_engine(strategy) is None, (
                f"_make_engine must return None for type {type_name!r}"
            )
