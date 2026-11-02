"""Survivorship is disclosed on every result and beside every positive or gate-passing verdict."""

from __future__ import annotations

import pytest

from app.api.v1.routes import backtest as route
from tests.unit.test_portfolio_backtest_route import _fake_result, _fake_wf


def _has_survivorship(warnings: list[str]) -> bool:
    return any("survivorship" in w.lower() and "point-in-time" in w.lower() for w in warnings)


def test_disclosure_names_the_three_causes() -> None:
    text = route.PORTFOLIO_DISCLOSURES["survivorship"].lower()
    assert "hand-picked" in text
    assert "point-in-time" in text
    assert "adjusted" in text


@pytest.mark.parametrize("sharpe", [0.8, 1.3])
def test_positive_verdict_carries_the_survivorship_warning(sharpe: float) -> None:
    interpretation = route._interpret_portfolio_results(
        _fake_result(sharpe=sharpe), _fake_wf(passes=True, sharpe=sharpe)
    )
    assert interpretation["verdict"] in {"promising", "strong"}
    assert _has_survivorship(interpretation["warnings"])


def test_refused_positive_verdict_still_carries_it() -> None:
    interpretation = route._interpret_portfolio_results(_fake_result(sharpe=0.8))
    assert interpretation["verdict"] == "unvalidated"
    assert _has_survivorship(interpretation["warnings"])


def test_passed_gate_with_a_non_positive_label_still_carries_it() -> None:
    interpretation = route._interpret_portfolio_results(
        _fake_result(sharpe=0.1), _fake_wf(passes=True, sharpe=0.1)
    )
    assert interpretation["verdict"] not in {"promising", "strong"}
    assert _has_survivorship(interpretation["warnings"])


def test_losing_run_without_a_gate_need_not_repeat_it() -> None:
    interpretation = route._interpret_portfolio_results(_fake_result(sharpe=0.8, total_return=-3.0))
    assert interpretation["verdict"] == "losing"
    assert not _has_survivorship(interpretation["warnings"])
