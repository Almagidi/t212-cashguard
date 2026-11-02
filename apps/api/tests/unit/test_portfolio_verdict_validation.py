"""The final verdict follows the validated evidence; the in-sample label is shown beside it."""

from __future__ import annotations

import pytest

from app.api.v1.routes import backtest as route
from tests.unit.test_portfolio_backtest_route import _fake_result, _fake_wf


def _interpret(*, in_sample: float, validated: float | None, **wf_kwargs):
    walk_forward = (
        None if validated is None else _fake_wf(passes=True, sharpe=validated, **wf_kwargs)
    )
    return route._interpret_portfolio_results(_fake_result(sharpe=in_sample), walk_forward)


def test_both_verdicts_are_always_shown() -> None:
    interpretation = _interpret(in_sample=0.8, validated=0.8)
    assert interpretation["in_sample_verdict"] == "promising"
    assert interpretation["validated_verdict"] == "promising"
    assert interpretation["validation"]["in_sample_verdict"] == "promising"
    assert interpretation["validation"]["verdicts_agree"] is True
    assert interpretation["validation"]["validated_evidence"]["holdout"]["sharpe_ratio"] == 0.8
    assert interpretation["verdict"] == "promising"


@pytest.mark.parametrize(
    ("in_sample", "validated"),
    [
        (1.3, 0.8),  # in-sample strong, validated only promising
        (0.8, 1.3),  # validated strong, in-sample only promising
        (1.3, 0.1),  # in-sample strong, validated marginal
        (0.8, 0.4),  # in-sample promising, validated mixed
    ],
)
def test_disagreeing_verdicts_never_emit_promising_or_strong(
    in_sample: float, validated: float
) -> None:
    interpretation = _interpret(in_sample=in_sample, validated=validated)
    assert interpretation["verdict"] == "unvalidated"
    assert interpretation["validation"]["verdicts_agree"] is False
    assert "disagree" in interpretation["validation"]["refusal_reason"]
    assert interpretation["in_sample_verdict"] != interpretation["validated_verdict"]


def test_validated_positive_with_a_negative_in_sample_run_is_not_promoted() -> None:
    interpretation = route._interpret_portfolio_results(
        _fake_result(sharpe=0.8, total_return=-2.0), _fake_wf(passes=True, sharpe=1.3)
    )
    assert interpretation["in_sample_verdict"] == "losing"
    assert interpretation["validated_verdict"] == "strong"
    assert interpretation["verdict"] == "unvalidated"
    assert interpretation["validation"]["refused_verdict"] == "strong"


def test_validated_evidence_takes_the_weaker_of_oos_and_holdout() -> None:
    walk_forward = _fake_wf(passes=True, sharpe=1.3)
    walk_forward.holdout_summary["sharpe_ratio"] = 0.7  # holdout is only 'promising'
    interpretation = route._interpret_portfolio_results(_fake_result(sharpe=1.3), walk_forward)
    assert interpretation["validated_verdict"] == "promising"
    assert interpretation["verdict"] == "unvalidated"


def test_holdout_loss_overrides_a_strong_oos_series() -> None:
    walk_forward = _fake_wf(passes=True, sharpe=1.3)
    walk_forward.holdout_summary["total_return_pct"] = -1.0
    interpretation = route._interpret_portfolio_results(_fake_result(sharpe=1.3), walk_forward)
    assert interpretation["validated_verdict"] == "losing"
    assert interpretation["verdict"] == "unvalidated"


def test_missing_broad_index_caps_the_verdict_at_unvalidated() -> None:
    interpretation = _interpret(
        in_sample=0.8, validated=0.8, benchmarks=("universe_equal_weight_costed",)
    )
    assert interpretation["verdict"] == "unvalidated"
    assert "broad-index" in interpretation["validation"]["refusal_reason"]


def test_missing_validated_evidence_is_not_a_positive_verdict() -> None:
    walk_forward = _fake_wf(passes=True, sharpe=0.8)
    walk_forward.oos["sharpe_ratio"] = None
    interpretation = route._interpret_portfolio_results(_fake_result(sharpe=0.8), walk_forward)
    assert interpretation["validated_verdict"] is None
    assert interpretation["verdict"] == "unvalidated"


def test_non_positive_in_sample_verdicts_are_unchanged() -> None:
    interpretation = route._interpret_portfolio_results(
        _fake_result(sharpe=0.1), _fake_wf(passes=True, sharpe=0.1)
    )
    assert interpretation["verdict"] == "marginal"
    assert interpretation["validated_verdict"] == "marginal"
