"""
Unit tests for KrakenDCAPlanner — typed evaluate_plan() API and architecture contracts.

Covers all DCADecisionCode paths, cadence boundary conditions, cash/position gates,
dip-enhancement logic, and architecture guards (RUNNABLE=False, PAPER_ONLY=True,
not constructible via _make_engine, not in beat_schedule).

The legacy evaluate() API is covered in test_kraken_strategies.py.
This file tests the typed evaluate_plan(config, state, ...) primary interface.
"""
from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.strategies.indicators import Bar
from app.strategies.kraken_dca_planner import (
    APPROVED_TICKERS,
    DCAConfig,
    DCADecision,
    DCADecisionCode,
    DCAState,
    DcaSchedulerContract,
    KrakenDCAPlanner,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _bar(close: float, *, volume: float = 1000.0) -> Bar:
    c = Decimal(str(close))
    return Bar(
        open=c * Decimal("0.998"),
        high=c * Decimal("1.005"),
        low=c * Decimal("0.995"),
        close=c,
        volume=Decimal(str(volume)),
    )


def _config(**overrides) -> DCAConfig:
    defaults: dict = dict(
        ticker="BTC/USD",
        cadence_days=7,
        base_allocation_usd=Decimal("100"),
        enable_dip_enhancement=True,
        dip_threshold_pct=5.0,
        dip_multiplier=2.0,
        dip_ema_period=20,
        min_cash_reserve_usd=Decimal("500"),
        max_position_pct=25.0,
        paper_only=True,
        enabled=True,
        venue="kraken",
    )
    defaults.update(overrides)
    return DCAConfig(**defaults)


def _state(**overrides) -> DCAState:
    defaults: dict = dict(
        ticker="BTC/USD",
        venue="kraken",
        last_buy_at=None,
        last_decision_at=None,
        total_allocated_usd=Decimal("0"),
        executions_count=0,
        last_decision_code=None,
        last_reason=None,
    )
    defaults.update(overrides)
    return DCAState(**defaults)


def _planner() -> KrakenDCAPlanner:
    return KrakenDCAPlanner()


# Fixed evaluation date: avoids date.today() non-determinism in tests.
TODAY = date(2026, 4, 29)
PRICE = Decimal("50000")
CASH = Decimal("10000")
ACCOUNT = Decimal("100000")


def _eval_default(**config_overrides) -> DCADecision:
    """Convenience: evaluate a single plan with default state and fixed date."""
    return _planner().evaluate_plan(
        config=_config(**config_overrides),
        state=_state(),
        current_price=PRICE,
        available_cash=CASH,
        account_value=ACCOUNT,
        now=TODAY,
    )


# ── Architecture contracts ─────────────────────────────────────────────────────

class TestArchitectureContracts:
    def test_runnable_is_false(self):
        """Deployment gate: must remain False until all prerequisites are met."""
        assert KrakenDCAPlanner.RUNNABLE is False

    def test_paper_only_is_true(self):
        assert KrakenDCAPlanner.PAPER_ONLY is True

    def test_venue_is_kraken(self):
        assert KrakenDCAPlanner.VENUE == "kraken"

    def test_does_not_implement_generate_signal(self):
        """DCA planner must not implement the bar-triggered signal interface."""
        assert not hasattr(KrakenDCAPlanner, "generate_signal"), (
            "KrakenDCAPlanner must never implement generate_signal — "
            "it is schedule-driven, not bar-triggered"
        )

    def test_not_constructible_via_make_engine_dca_planner(self):
        """strategy_runner._make_engine must return None for 'kraken_dca_planner'."""
        from app.services.strategy_runner import StrategyRunner
        runner = StrategyRunner(MagicMock())
        strategy = MagicMock()
        strategy.type = "kraken_dca_planner"
        strategy.params = {}
        result = runner._make_engine(strategy)
        assert result is None, (
            "_make_engine must return None for 'kraken_dca_planner'; "
            "DCA must not be constructible through the signal runner"
        )

    def test_not_constructible_via_make_engine_kraken_dca(self):
        """strategy_runner._make_engine must return None for 'kraken_dca'."""
        from app.services.strategy_runner import StrategyRunner
        runner = StrategyRunner(MagicMock())
        strategy = MagicMock()
        strategy.type = "kraken_dca"
        strategy.params = {}
        result = runner._make_engine(strategy)
        assert result is None, (
            "_make_engine must return None for 'kraken_dca'"
        )

    def test_only_dedicated_paper_scheduler_in_beat_schedule(self):
        """DCA must only appear in the dedicated daily paper scheduler."""
        from app.workers.celery_app import celery_app
        for task_key, task_cfg in celery_app.conf.beat_schedule.items():
            task_path = task_cfg.get("task", "")
            if "dca" in task_path.lower():
                assert task_key == "dca-paper-evaluate"
                assert task_path == "app.workers.tasks_dca.evaluate_due_plans_task"

    def test_dca_scheduler_contract_exists_and_has_evaluate_method(self):
        """DcaSchedulerContract Protocol must exist as documentation of future interface."""
        assert DcaSchedulerContract is not None
        assert hasattr(DcaSchedulerContract, "evaluate_due_plans")

    def test_approved_tickers_are_btc_and_eth_only(self):
        assert APPROVED_TICKERS == frozenset({"BTC/USD", "ETH/USD"})


# ── DCADecision type contract ─────────────────────────────────────────────────

class TestDCADecisionTypeContract:
    def test_code_is_first_field_and_required(self):
        """code must be the first field and required (no default)."""
        fields = dataclasses.fields(DCADecision)
        assert fields[0].name == "code", "code must be the first DCADecision field"
        assert fields[0].default is dataclasses.MISSING, "code must be required (no default)"

    def test_next_scheduled_date_is_optional(self):
        fields = {f.name: f for f in dataclasses.fields(DCADecision)}
        assert fields["next_scheduled_date"].default is None

    def test_dca_decision_has_no_signal_fields(self):
        """DCADecision must never carry signal-style fields."""
        result = _eval_default()
        assert not hasattr(result, "signal_type"), "DCADecision must not have signal_type"
        assert not hasattr(result, "stop_price"), "DCADecision must not have stop_price"
        assert not hasattr(result, "take_profit_price"), "DCADecision must not have take_profit_price"

    def test_every_evaluation_returns_dca_decision(self):
        result = _eval_default()
        assert isinstance(result, DCADecision)

    def test_every_evaluation_has_explicit_decision_code(self):
        result = _eval_default()
        assert isinstance(result.code, DCADecisionCode), (
            "Every DCA evaluation must set an explicit DCADecisionCode"
        )


# ── All DCADecisionCode paths via evaluate_plan() ─────────────────────────────

class TestDecisionCodePaths:
    """Each path through evaluate_plan() must produce the correct DCADecisionCode."""

    def test_blocked_policy_when_plan_disabled(self):
        result = _eval_default(enabled=False)
        assert result.code == DCADecisionCode.BLOCKED_POLICY
        assert result.should_accumulate is False
        assert result.amount_usd == Decimal("0")

    def test_blocked_policy_when_ticker_not_approved(self):
        result = _eval_default(ticker="SOL/USD")
        assert result.code == DCADecisionCode.BLOCKED_POLICY
        assert result.should_accumulate is False
        assert "SOL/USD" in result.reason

    def test_blocked_policy_when_price_is_zero(self):
        result = _planner().evaluate_plan(
            config=_config(),
            state=_state(),
            current_price=Decimal("0"),
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BLOCKED_POLICY
        assert result.should_accumulate is False

    def test_blocked_policy_when_price_is_negative(self):
        result = _planner().evaluate_plan(
            config=_config(),
            state=_state(),
            current_price=Decimal("-1"),
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BLOCKED_POLICY
        assert result.should_accumulate is False

    def test_blocked_low_cash_when_below_reserve(self):
        # Cash 400, reserve 500 → blocked
        result = _planner().evaluate_plan(
            config=_config(min_cash_reserve_usd=Decimal("500")),
            state=_state(),
            current_price=PRICE,
            available_cash=Decimal("400"),
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BLOCKED_LOW_CASH
        assert result.should_accumulate is False
        assert result.amount_usd == Decimal("0")

    def test_skip_already_bought_this_window_when_cadence_not_elapsed(self):
        # Bought 3 days ago, cadence 7 → skip
        last_buy = date(2026, 4, 26)  # 3 days before TODAY
        result = _planner().evaluate_plan(
            config=_config(cadence_days=7),
            state=_state(last_buy_at=last_buy),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.SKIP_ALREADY_BOUGHT_THIS_WINDOW
        assert result.should_accumulate is False
        assert result.next_scheduled_date is not None

    def test_blocked_policy_when_allocation_exceeds_max_position_cap(self):
        # account=1000, max_pct=5 → cap=50; base=100 > 50 → blocked
        result = _planner().evaluate_plan(
            config=_config(
                base_allocation_usd=Decimal("100"),
                max_position_pct=5.0,
            ),
            state=_state(),
            current_price=PRICE,
            available_cash=Decimal("5000"),
            account_value=Decimal("1000"),
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BLOCKED_POLICY
        assert result.should_accumulate is False

    def test_buy_due_scheduled_on_first_buy(self):
        result = _planner().evaluate_plan(
            config=_config(),
            state=_state(last_buy_at=None),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.should_accumulate is True
        assert result.amount_usd > Decimal("0")
        assert result.mode in ("scheduled", "dip_enhanced")

    def test_buy_due_scheduled_when_cadence_exactly_elapsed(self):
        # Bought exactly 7 days ago, cadence=7 → due
        last_buy = date(2026, 4, 22)
        result = _planner().evaluate_plan(
            config=_config(cadence_days=7),
            state=_state(last_buy_at=last_buy),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.should_accumulate is True
        assert result.mode == "scheduled"
        assert result.amount_usd == Decimal("100")

    def test_buy_due_dip_enhanced_when_price_significantly_below_ema(self):
        # 25 flat bars at 3000; price=2700 is ~10% below EMA20≈3000; threshold=5% → qualifies
        bars = [_bar(3000.0) for _ in range(25)]
        result = _planner().evaluate_plan(
            config=_config(
                ticker="ETH/USD",
                base_allocation_usd=Decimal("100"),
                dip_threshold_pct=5.0,
                dip_multiplier=2.0,
                dip_ema_period=20,
                enable_dip_enhancement=True,
            ),
            state=_state(ticker="ETH/USD"),
            current_price=Decimal("2700"),
            available_cash=CASH,
            account_value=ACCOUNT,
            bars=bars,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.mode == "dip_enhanced"
        assert result.amount_usd == Decimal("200")  # 100 * 2.0

    def test_buy_due_scheduled_when_price_at_ema_no_dip(self):
        # Price at EMA — dip threshold not met → plain scheduled
        bars = [_bar(3000.0) for _ in range(25)]
        result = _planner().evaluate_plan(
            config=_config(
                ticker="ETH/USD",
                base_allocation_usd=Decimal("100"),
                dip_threshold_pct=5.0,
            ),
            state=_state(ticker="ETH/USD"),
            current_price=Decimal("3000"),
            available_cash=CASH,
            account_value=ACCOUNT,
            bars=bars,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.mode == "scheduled"
        assert result.amount_usd == Decimal("100")


# ── Cadence boundary conditions ────────────────────────────────────────────────

class TestCadenceBoundary:
    """Exact boundary: day N-1 is blocked; day N is due."""

    def test_one_day_before_cadence_is_blocked(self):
        # 6 days since last buy, cadence=7 → still in window
        last_buy = date(2026, 4, 23)  # TODAY - 6 days
        result = _planner().evaluate_plan(
            config=_config(cadence_days=7),
            state=_state(last_buy_at=last_buy),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.SKIP_ALREADY_BOUGHT_THIS_WINDOW
        assert result.should_accumulate is False

    def test_exact_cadence_day_is_due(self):
        # 7 days since last buy, cadence=7 → exactly due
        last_buy = date(2026, 4, 22)  # TODAY - 7 days
        result = _planner().evaluate_plan(
            config=_config(cadence_days=7),
            state=_state(last_buy_at=last_buy),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.should_accumulate is True

    def test_one_day_past_cadence_is_still_due(self):
        # 8 days since last buy, cadence=7 → overdue; still fires
        last_buy = date(2026, 4, 21)  # TODAY - 8 days
        result = _planner().evaluate_plan(
            config=_config(cadence_days=7),
            state=_state(last_buy_at=last_buy),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.should_accumulate is True

    def test_skip_response_contains_correct_next_scheduled_date(self):
        last_buy = date(2026, 4, 26)  # 3 days ago; cadence=7 → next due May 3
        result = _planner().evaluate_plan(
            config=_config(cadence_days=7),
            state=_state(last_buy_at=last_buy),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.next_scheduled_date == "2026-05-03"

    def test_buy_response_contains_next_scheduled_date(self):
        # On buy day, next_scheduled is today + cadence_days
        result = _planner().evaluate_plan(
            config=_config(cadence_days=7),
            state=_state(last_buy_at=None),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.next_scheduled_date == date(2026, 5, 6).isoformat()


# ── Cash reserve gate ─────────────────────────────────────────────────────────

class TestCashReserveGate:
    """Gate uses strict less-than: available_cash < reserve blocks; == reserve passes."""

    def test_cash_equal_to_reserve_passes(self):
        result = _planner().evaluate_plan(
            config=_config(min_cash_reserve_usd=Decimal("10000")),
            state=_state(),
            current_price=PRICE,
            available_cash=Decimal("10000"),
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code != DCADecisionCode.BLOCKED_LOW_CASH

    def test_cash_one_cent_below_reserve_is_blocked(self):
        result = _planner().evaluate_plan(
            config=_config(min_cash_reserve_usd=Decimal("10000")),
            state=_state(),
            current_price=PRICE,
            available_cash=Decimal("9999.99"),
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BLOCKED_LOW_CASH
        assert result.should_accumulate is False

    def test_zero_cash_is_blocked(self):
        result = _planner().evaluate_plan(
            config=_config(min_cash_reserve_usd=Decimal("500")),
            state=_state(),
            current_price=PRICE,
            available_cash=Decimal("0"),
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BLOCKED_LOW_CASH


# ── Max position gate ─────────────────────────────────────────────────────────

class TestMaxPositionGate:
    def test_allocation_within_cap_is_allowed(self):
        # account=1000, max_pct=25 → cap=250; base=100 < 250 → passes gate
        result = _planner().evaluate_plan(
            config=_config(
                base_allocation_usd=Decimal("100"),
                max_position_pct=25.0,
            ),
            state=_state(),
            current_price=PRICE,
            available_cash=Decimal("5000"),
            account_value=Decimal("1000"),
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE

    def test_allocation_exceeding_cap_is_blocked(self):
        # account=1000, max_pct=5 → cap=50; base=100 > 50 → blocked
        result = _planner().evaluate_plan(
            config=_config(
                base_allocation_usd=Decimal("100"),
                max_position_pct=5.0,
            ),
            state=_state(),
            current_price=PRICE,
            available_cash=Decimal("5000"),
            account_value=Decimal("1000"),
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BLOCKED_POLICY

    def test_zero_account_value_skips_cap_check(self):
        # account_value=0 → cap check bypassed (guard against zero division)
        result = _planner().evaluate_plan(
            config=_config(
                base_allocation_usd=Decimal("100"),
                max_position_pct=5.0,
            ),
            state=_state(),
            current_price=PRICE,
            available_cash=CASH,
            account_value=Decimal("0"),
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE


# ── Dip enhancement ───────────────────────────────────────────────────────────

class TestDipEnhancement:
    def test_disabled_dip_enhancement_yields_scheduled_mode(self):
        bars = [_bar(3000.0) for _ in range(25)]
        result = _planner().evaluate_plan(
            config=_config(
                ticker="ETH/USD",
                enable_dip_enhancement=False,
                base_allocation_usd=Decimal("100"),
            ),
            state=_state(ticker="ETH/USD"),
            current_price=Decimal("2700"),
            available_cash=CASH,
            account_value=ACCOUNT,
            bars=bars,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.mode == "scheduled"
        assert result.amount_usd == Decimal("100")

    def test_insufficient_bars_skips_dip_check(self):
        # Fewer bars than dip_ema_period → dip check skipped → plain scheduled
        bars = [_bar(3000.0) for _ in range(5)]  # need 20, have 5
        result = _planner().evaluate_plan(
            config=_config(
                ticker="ETH/USD",
                enable_dip_enhancement=True,
                dip_ema_period=20,
            ),
            state=_state(ticker="ETH/USD"),
            current_price=Decimal("2700"),
            available_cash=CASH,
            account_value=ACCOUNT,
            bars=bars,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.mode == "scheduled"

    def test_no_bars_skips_dip_check(self):
        result = _planner().evaluate_plan(
            config=_config(ticker="ETH/USD", enable_dip_enhancement=True),
            state=_state(ticker="ETH/USD"),
            current_price=Decimal("2700"),
            available_cash=CASH,
            account_value=ACCOUNT,
            bars=None,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.mode == "scheduled"

    def test_dip_enhancement_amount_is_base_times_multiplier(self):
        bars = [_bar(3000.0) for _ in range(25)]
        result = _planner().evaluate_plan(
            config=_config(
                ticker="ETH/USD",
                base_allocation_usd=Decimal("150"),
                dip_multiplier=3.0,
                dip_threshold_pct=5.0,
                dip_ema_period=20,
                enable_dip_enhancement=True,
            ),
            state=_state(ticker="ETH/USD"),
            current_price=Decimal("2700"),  # ~10% below EMA≈3000
            available_cash=CASH,
            account_value=ACCOUNT,
            bars=bars,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BUY_DUE
        assert result.mode == "dip_enhanced"
        assert result.amount_usd == Decimal("450")  # 150 * 3.0


# ── Determinism ────────────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_inputs_produce_identical_outputs(self):
        """Planner is a pure function — identical inputs must produce identical outputs."""
        p = _planner()
        config = _config()
        state = _state()
        a = p.evaluate_plan(config, state, PRICE, CASH, ACCOUNT, now=TODAY)
        b = p.evaluate_plan(config, state, PRICE, CASH, ACCOUNT, now=TODAY)
        assert a.code == b.code
        assert a.should_accumulate == b.should_accumulate
        assert a.amount_usd == b.amount_usd
        assert a.mode == b.mode
        assert a.reason == b.reason
        assert a.next_scheduled_date == b.next_scheduled_date

    def test_different_dates_produce_different_cadence_outcomes(self):
        """Outcome is date-sensitive — different now values must produce different results."""
        last_buy = date(2026, 4, 22)
        state = _state(last_buy_at=last_buy)
        config = _config(cadence_days=7)
        # 6 days later → skip; 7 days later → buy
        day6 = _planner().evaluate_plan(
            config=config, state=state,
            current_price=PRICE, available_cash=CASH, account_value=ACCOUNT,
            now=date(2026, 4, 28),
        )
        day7 = _planner().evaluate_plan(
            config=config, state=state,
            current_price=PRICE, available_cash=CASH, account_value=ACCOUNT,
            now=date(2026, 4, 29),
        )
        assert day6.code == DCADecisionCode.SKIP_ALREADY_BOUGHT_THIS_WINDOW
        assert day7.code == DCADecisionCode.BUY_DUE


# ── DCAState persistence contract ─────────────────────────────────────────────

class TestDCAStateContract:
    """
    DCAState defines the persistence contract for the future dca_plan_states table.
    These tests assert the correct field set is present — not DB integration tests.
    """

    REQUIRED_FIELDS = frozenset({
        "ticker", "venue", "last_buy_at", "last_decision_at",
        "total_allocated_usd", "executions_count",
        "last_decision_code", "last_reason",
    })

    def test_all_required_persistence_fields_are_present(self):
        state = _state()
        for field_name in self.REQUIRED_FIELDS:
            assert hasattr(state, field_name), (
                f"DCAState is missing required persistence field: {field_name!r}"
            )

    def test_zero_safe_defaults(self):
        state = DCAState(ticker="BTC/USD", venue="kraken")
        assert state.last_buy_at is None
        assert state.total_allocated_usd == Decimal("0")
        assert state.executions_count == 0
        assert state.last_decision_code is None
        assert state.last_reason is None

    def test_state_ticker_and_venue_roundtrip(self):
        state = DCAState(ticker="ETH/USD", venue="kraken")
        assert state.ticker == "ETH/USD"
        assert state.venue == "kraken"

    def test_state_is_mutable_for_caller_update_after_buy_due(self):
        """Caller updates DCAState after a BUY_DUE decision; fields must accept mutation."""
        state = _state()
        state.last_buy_at = TODAY
        state.executions_count += 1
        state.total_allocated_usd += Decimal("100")
        state.last_decision_code = DCADecisionCode.BUY_DUE
        assert state.last_buy_at == TODAY
        assert state.executions_count == 1
        assert state.total_allocated_usd == Decimal("100")
        assert state.last_decision_code == DCADecisionCode.BUY_DUE

    def test_updated_state_enforces_cadence_on_next_evaluation(self):
        """After simulating a buy (updating state.last_buy_at), the next call skips."""
        state = _state()
        # Simulate caller updating state after BUY_DUE
        state.last_buy_at = TODAY
        state.executions_count = 1

        # Same-day re-evaluation must be blocked
        result = _planner().evaluate_plan(
            config=_config(cadence_days=7),
            state=state,
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.SKIP_ALREADY_BOUGHT_THIS_WINDOW
        assert result.should_accumulate is False


# ── DCAConfig contract ────────────────────────────────────────────────────────

class TestDCAConfigContract:
    def test_paper_only_default_is_true(self):
        cfg = DCAConfig(ticker="BTC/USD")
        assert cfg.paper_only is True

    def test_enabled_default_is_true(self):
        cfg = DCAConfig(ticker="BTC/USD")
        assert cfg.enabled is True

    def test_venue_default_is_kraken(self):
        cfg = DCAConfig(ticker="BTC/USD")
        assert cfg.venue == "kraken"

    def test_unapproved_ticker_rejected_at_evaluation_not_construction(self):
        """DCAConfig construction allows any ticker; the gate is enforced in evaluate_plan()."""
        cfg = DCAConfig(ticker="DOGE/USD")  # not in APPROVED_TICKERS
        result = _planner().evaluate_plan(
            config=cfg,
            state=_state(ticker="DOGE/USD"),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BLOCKED_POLICY

    def test_disabled_plan_always_blocked_regardless_of_other_conditions(self):
        cfg = DCAConfig(ticker="BTC/USD", enabled=False)
        result = _planner().evaluate_plan(
            config=cfg,
            state=_state(last_buy_at=None),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert result.code == DCADecisionCode.BLOCKED_POLICY

    @pytest.mark.parametrize("ticker", sorted(APPROVED_TICKERS))
    def test_approved_tickers_pass_ticker_gate(self, ticker: str):
        result = _planner().evaluate_plan(
            config=_config(ticker=ticker),
            state=_state(ticker=ticker),
            current_price=PRICE,
            available_cash=CASH,
            account_value=ACCOUNT,
            now=TODAY,
        )
        assert "not in the approved" not in result.reason
