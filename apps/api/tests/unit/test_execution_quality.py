"""
Unit tests for the execution-quality analytics helpers.

All tested functions are pure Python (no DB, no I/O).
The Order/Signal ORM objects are replaced with lightweight fakes.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.execution_quality import (
    ABNORMAL_SLIPPAGE_PCT,
    ABNORMAL_SLIPPAGE_VALUE,
    TERMINAL_STATUSES,
    _avg,
    _decimal,
    _float,
    _pct,
    _round,
    _aware_datetime,
    calculate_order_execution_quality,
    grade_execution_quality,
    infer_execution_environment,
    infer_expected_fill_price,
    mark_slippage_alerted,
    milliseconds_between,
    score_execution_quality,
    should_alert_abnormal_slippage,
)


# ── tiny helpers ──────────────────────────────────────────────────────────────

class TestDecimalHelper:
    def test_none_returns_none(self):
        assert _decimal(None) is None

    def test_int_converts(self):
        assert _decimal(10) == Decimal("10")

    def test_float_converts(self):
        assert _decimal(1.5) == Decimal("1.5")

    def test_string_decimal_converts(self):
        assert _decimal("3.14") == Decimal("3.14")

    def test_invalid_string_returns_none(self):
        assert _decimal("not_a_number") is None

    def test_decimal_passthrough(self):
        d = Decimal("7.77")
        assert _decimal(d) == d


class TestFloatHelper:
    def test_none_returns_none(self):
        assert _float(None) is None

    def test_int_converts(self):
        assert _float(5) == 5.0

    def test_decimal_converts(self):
        assert _float(Decimal("2.5")) == 2.5


class TestRoundHelper:
    def test_none_returns_none(self):
        assert _round(None) is None

    def test_rounds_to_two_places(self):
        assert _round(3.14159) == 3.14

    def test_custom_digits(self):
        assert _round(3.14159, 4) == 3.1416


class TestAvgHelper:
    def test_empty_returns_none(self):
        assert _avg([]) is None

    def test_all_none_returns_none(self):
        assert _avg([None, None]) is None

    def test_mixed_none_skipped(self):
        assert _avg([None, 4.0, None, 6.0]) == 5.0

    def test_single_value(self):
        assert _avg([3.0]) == 3.0

    def test_average_computed(self):
        assert _avg([1.0, 2.0, 3.0]) == 2.0


class TestPctHelper:
    def test_zero_denominator_returns_zero(self):
        assert _pct(5, 0) == 0.0

    def test_negative_denominator_returns_zero(self):
        assert _pct(5, -1) == 0.0

    def test_normal_ratio(self):
        assert _pct(1, 4) == 0.25

    def test_full_fill(self):
        assert _pct(10, 10) == 1.0


class TestAwareDatetime:
    def test_none_returns_now(self):
        before = datetime.now(UTC)
        result = _aware_datetime(None)
        after = datetime.now(UTC)
        assert before <= result <= after

    def test_naive_datetime_gets_utc(self):
        naive = datetime(2024, 1, 1, 12, 0, 0)
        result = _aware_datetime(naive)
        assert result.tzinfo is not None

    def test_aware_datetime_converted_to_utc(self):
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=5))
        aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=tz)
        result = _aware_datetime(aware)
        assert result.tzinfo == UTC


class TestMillisecondsBetween:
    def test_both_none_returns_none(self):
        assert milliseconds_between(None, None) is None

    def test_one_none_returns_none(self):
        t = datetime.now(UTC)
        assert milliseconds_between(t, None) is None
        assert milliseconds_between(None, t) is None

    def test_same_timestamps_returns_zero(self):
        t = datetime.now(UTC)
        assert milliseconds_between(t, t) == 0

    def test_1_second_apart(self):
        t1 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        t2 = datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC)
        assert milliseconds_between(t1, t2) == 1000

    def test_end_before_start_returns_zero(self):
        t1 = datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC)
        t2 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert milliseconds_between(t1, t2) == 0

    def test_naive_datetimes_handled(self):
        t1 = datetime(2024, 1, 1, 12, 0, 0)
        t2 = datetime(2024, 1, 1, 12, 0, 2)
        assert milliseconds_between(t1, t2) == 2000


# ── Order fake ────────────────────────────────────────────────────────────────

def _order(**kwargs):
    """Create a minimal fake Order object."""
    o = MagicMock()
    o.execution_environment = None
    o.is_dry_run = False
    o.status = "filled"
    o.side = "buy"
    o.order_type = "market"
    o.avg_fill_price = None
    o.expected_fill_price = None
    o.limit_price = None
    o.stop_price = None
    o.quantity = None
    o.filled_quantity = None
    o.slippage_pct = None
    o.slippage_value = None
    o.broker_latency_ms = None
    o.fill_latency_ms = None
    o.reconciliation_latency_ms = None
    o.submitted_at = None
    o.first_ack_at = None
    o.filled_at = None
    o.cancelled_at = None
    o.rejected_at = None
    o.updated_at = None
    o.execution_quality_score = None
    o.execution_quality_grade = None
    o.execution_quality_notes = {}
    o.signal = None
    o.error_message = None
    for k, v in kwargs.items():
        setattr(o, k, v)
    return o


# ── infer_execution_environment ───────────────────────────────────────────────

class TestInferExecutionEnvironment:
    def test_explicit_environment_returned(self):
        o = _order(execution_environment="paper")
        assert infer_execution_environment(o) == "paper"

    def test_dry_run_returns_dry_run(self):
        o = _order(execution_environment=None, is_dry_run=True)
        assert infer_execution_environment(o) == "dry_run"

    def test_live_order_returns_broker(self):
        o = _order(execution_environment=None, is_dry_run=False)
        assert infer_execution_environment(o) == "broker"


# ── infer_expected_fill_price ─────────────────────────────────────────────────

class TestInferExpectedFillPrice:
    def test_explicit_expected_price_used(self):
        o = _order(expected_fill_price=Decimal("150.00"))
        assert infer_expected_fill_price(o) == Decimal("150.00")

    def test_zero_expected_price_falls_through(self):
        signal = MagicMock()
        signal.entry_price = Decimal("120.00")
        o = _order(expected_fill_price=Decimal("0"), signal=signal)
        assert infer_expected_fill_price(o) == Decimal("120.00")

    def test_falls_back_to_limit_price(self):
        o = _order(expected_fill_price=None, signal=None, limit_price=Decimal("99.50"))
        assert infer_expected_fill_price(o) == Decimal("99.50")

    def test_falls_back_to_stop_price(self):
        o = _order(
            expected_fill_price=None, signal=None,
            limit_price=None, stop_price=Decimal("88.00")
        )
        assert infer_expected_fill_price(o) == Decimal("88.00")

    def test_returns_none_when_nothing_available(self):
        o = _order(expected_fill_price=None, signal=None, limit_price=None, stop_price=None)
        assert infer_expected_fill_price(o) is None


# ── grade_execution_quality ───────────────────────────────────────────────────

class TestGradeExecutionQuality:
    def test_none_returns_pending(self):
        assert grade_execution_quality(None) == "pending"

    def test_95_is_excellent(self):
        assert grade_execution_quality(95.0) == "excellent"

    def test_90_is_excellent(self):
        assert grade_execution_quality(90.0) == "excellent"

    def test_89_is_good(self):
        assert grade_execution_quality(89.0) == "good"

    def test_75_is_good(self):
        assert grade_execution_quality(75.0) == "good"

    def test_74_is_watch(self):
        assert grade_execution_quality(74.0) == "watch"

    def test_60_is_watch(self):
        assert grade_execution_quality(60.0) == "watch"

    def test_59_is_degraded(self):
        assert grade_execution_quality(59.0) == "degraded"

    def test_40_is_degraded(self):
        assert grade_execution_quality(40.0) == "degraded"

    def test_39_is_poor(self):
        assert grade_execution_quality(39.0) == "poor"

    def test_zero_is_poor(self):
        assert grade_execution_quality(0.0) == "poor"


# ── score_execution_quality ───────────────────────────────────────────────────

class TestScoreExecutionQuality:
    def _score(self, **kwargs):
        defaults = dict(
            status="filled",
            order_type="market",
            slippage_pct=None,
            broker_latency_ms=None,
            fill_latency_ms=None,
            reconciliation_latency_ms=None,
        )
        defaults.update(kwargs)
        return score_execution_quality(**defaults)

    def test_pending_status_returns_none(self):
        score, grade, notes = self._score(status="pending")
        assert score is None
        assert grade == "pending"
        assert notes.get("pending") is True

    def test_clean_fill_scores_100(self):
        score, grade, notes = self._score(status="filled")
        assert score == Decimal("100.00")
        assert grade == "excellent"
        assert notes["penalties"] == {}

    def test_error_status_heavy_penalty(self):
        score, grade, notes = self._score(status="error")
        assert score == Decimal("25.00")
        assert grade == "poor"

    def test_rejected_status_penalty(self):
        score, grade, notes = self._score(status="rejected")
        assert score == Decimal("30.00")

    def test_cancelled_status_penalty(self):
        score, grade, notes = self._score(status="cancelled")
        assert float(score) == 65.0

    def test_adverse_slippage_penalises_score(self):
        score_clean, _, _ = self._score(status="filled")
        score_slip, _, notes = self._score(
            status="filled", slippage_pct=Decimal("1.0")
        )
        assert score_slip < score_clean
        assert "slippage" in notes["penalties"]

    def test_slippage_penalty_capped_at_45(self):
        _, _, notes = self._score(
            status="filled", slippage_pct=Decimal("100.0")
        )
        assert notes["penalties"]["slippage"] == 45.0

    def test_negative_slippage_not_penalised(self):
        score, _, notes = self._score(
            status="filled", slippage_pct=Decimal("-0.5")
        )
        assert score == Decimal("100.00")
        assert "slippage" not in notes["penalties"]

    def test_slow_broker_ack_penalised(self):
        _, _, notes = self._score(status="filled", broker_latency_ms=2000)
        assert "first_ack_latency" in notes["penalties"]

    def test_fast_broker_ack_not_penalised(self):
        _, _, notes = self._score(status="filled", broker_latency_ms=500)
        assert "first_ack_latency" not in notes["penalties"]

    def test_slow_fill_market_order_penalised(self):
        _, _, notes = self._score(
            status="filled", order_type="market", fill_latency_ms=10_000
        )
        assert "fill_latency" in notes["penalties"]

    def test_slow_fill_limit_order_higher_threshold(self):
        # 10s fill is fine for a limit order (threshold = 60s)
        _, _, notes = self._score(
            status="filled", order_type="limit", fill_latency_ms=10_000
        )
        assert "fill_latency" not in notes["penalties"]

    def test_slow_reconciliation_penalised(self):
        _, _, notes = self._score(
            status="filled", reconciliation_latency_ms=120_000
        )
        assert "reconciliation_latency" in notes["penalties"]

    def test_score_never_below_zero(self):
        score, _, _ = self._score(
            status="error",
            slippage_pct=Decimal("100.0"),
            broker_latency_ms=100_000,
            fill_latency_ms=100_000,
            reconciliation_latency_ms=100_000,
        )
        assert score >= Decimal("0.00")

    def test_existing_notes_preserved(self):
        _, _, notes = self._score(
            status="filled",
            existing_notes={"my_key": "my_val"},
        )
        assert notes.get("my_key") == "my_val"

    def test_penalties_key_not_leaked_from_previous_notes(self):
        # If existing_notes already has "penalties", it should be replaced not merged
        _, _, notes = self._score(
            status="filled",
            existing_notes={"penalties": {"stale": 99.0}},
        )
        assert "stale" not in notes["penalties"]


# ── calculate_order_execution_quality ─────────────────────────────────────────

class TestCalculateOrderExecutionQuality:
    def test_filled_order_clean_returns_100(self):
        o = _order(
            status="filled",
            side="buy",
            order_type="market",
            expected_fill_price=Decimal("100.00"),
            avg_fill_price=Decimal("100.00"),
            filled_quantity=Decimal("10"),
        )
        metrics = calculate_order_execution_quality(o)
        assert metrics["execution_quality_score"] == Decimal("100.00")
        assert metrics["execution_quality_grade"] == "excellent"

    def test_buy_adverse_slippage_computed(self):
        # Buy fills above expected → adverse
        o = _order(
            status="filled",
            side="buy",
            order_type="market",
            expected_fill_price=Decimal("100.00"),
            avg_fill_price=Decimal("101.00"),
            filled_quantity=Decimal("10"),
        )
        metrics = calculate_order_execution_quality(o)
        assert metrics["slippage_pct"] is not None
        assert metrics["slippage_pct"] > 0

    def test_sell_adverse_slippage_computed(self):
        # Sell fills below expected → adverse
        o = _order(
            status="filled",
            side="sell",
            order_type="market",
            expected_fill_price=Decimal("100.00"),
            avg_fill_price=Decimal("99.00"),
            filled_quantity=Decimal("10"),
        )
        metrics = calculate_order_execution_quality(o)
        assert metrics["slippage_pct"] is not None
        assert metrics["slippage_pct"] > 0

    def test_latency_inferred_from_timestamps(self):
        t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        t1 = datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC)
        t2 = datetime(2024, 1, 1, 12, 0, 3, tzinfo=UTC)
        o = _order(
            status="filled",
            submitted_at=t0,
            first_ack_at=t1,
            filled_at=t2,
        )
        metrics = calculate_order_execution_quality(o)
        assert metrics["broker_latency_ms"] == 1000
        assert metrics["fill_latency_ms"] == 3000

    def test_stored_slippage_used_when_present(self):
        o = _order(
            status="filled",
            slippage_pct=Decimal("0.50"),
            slippage_value=Decimal("5.00"),
        )
        metrics = calculate_order_execution_quality(o)
        assert metrics["slippage_pct"] == Decimal("0.50")

    def test_environment_dry_run(self):
        o = _order(is_dry_run=True, status="filled")
        metrics = calculate_order_execution_quality(o)
        assert metrics["execution_environment"] == "dry_run"


# ── should_alert_abnormal_slippage ────────────────────────────────────────────

class TestShouldAlertAbnormalSlippage:
    def test_dry_run_never_alerts(self):
        o = _order(is_dry_run=True, status="filled", slippage_pct=Decimal("5.0"))
        assert not should_alert_abnormal_slippage(o)

    def test_non_filled_never_alerts(self):
        o = _order(is_dry_run=False, status="rejected", slippage_pct=Decimal("5.0"))
        assert not should_alert_abnormal_slippage(o)

    def test_already_alerted_skipped(self):
        o = _order(
            is_dry_run=False, status="filled",
            slippage_pct=Decimal("5.0"),
            execution_quality_notes={"slippage_alerted": True},
        )
        assert not should_alert_abnormal_slippage(o)

    def test_high_slippage_pct_alerts(self):
        o = _order(
            is_dry_run=False, status="filled",
            slippage_pct=ABNORMAL_SLIPPAGE_PCT,
            slippage_value=Decimal("1.00"),
            execution_quality_notes={},
        )
        assert should_alert_abnormal_slippage(o)

    def test_high_slippage_value_alerts(self):
        o = _order(
            is_dry_run=False, status="filled",
            slippage_pct=Decimal("0.10"),
            slippage_value=ABNORMAL_SLIPPAGE_VALUE,
            execution_quality_notes={},
        )
        assert should_alert_abnormal_slippage(o)

    def test_normal_slippage_no_alert(self):
        o = _order(
            is_dry_run=False, status="filled",
            slippage_pct=Decimal("0.10"),
            slippage_value=Decimal("1.00"),
            execution_quality_notes={},
        )
        assert not should_alert_abnormal_slippage(o)

    def test_zero_slippage_no_alert(self):
        o = _order(
            is_dry_run=False, status="filled",
            slippage_pct=Decimal("0"),
            execution_quality_notes={},
        )
        assert not should_alert_abnormal_slippage(o)


# ── mark_slippage_alerted ─────────────────────────────────────────────────────

class TestMarkSlippageAlerted:
    def test_sets_flag_on_notes(self):
        o = _order(execution_quality_notes={})
        mark_slippage_alerted(o)
        assert o.execution_quality_notes["slippage_alerted"] is True

    def test_preserves_existing_notes(self):
        o = _order(execution_quality_notes={"other_key": "val"})
        mark_slippage_alerted(o)
        assert o.execution_quality_notes["other_key"] == "val"
        assert o.execution_quality_notes["slippage_alerted"] is True

    def test_none_notes_handled(self):
        o = _order(execution_quality_notes=None)
        mark_slippage_alerted(o)
        assert o.execution_quality_notes["slippage_alerted"] is True
