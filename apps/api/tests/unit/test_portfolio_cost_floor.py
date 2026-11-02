"""Round-trip cost floor for gated verdicts, consistent bps units, and the legacy flat-fee label."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.api.v1.routes import backtest as route
from app.backtest.gate_floors import MIN_ROUND_TRIP_COST_BPS, cost_gate_refusal
from app.backtest.portfolio_costs import TransactionCostModel
from app.backtest.portfolio_engine import PortfolioBacktester
from tests.unit.portfolio_synth import ConstantWeightStrategy, synth_history
from tests.unit.test_portfolio_backtest_route import _fake_result, _fake_wf


def test_costs_are_compared_in_one_unit() -> None:
    model = TransactionCostModel(
        spread_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        commission_bps=Decimal("0"),
        fx_fee_rate=Decimal("0.0015"),
    )
    # 0.0015 of notional is 15 bps, not 0.0015 bps.
    assert model.total_per_side_bps == Decimal("15")
    assert model.fx_fee_bps == Decimal("15")


def test_per_ticker_spreads_count_as_a_cost_when_every_default_is_zero() -> None:
    # Old behaviour: "all costs are zero" was raised although a per-ticker spread was set.
    model = TransactionCostModel(
        spread_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        commission_bps=Decimal("0"),
        fx_fee_rate=Decimal("0"),
        spread_bps_by_ticker={"AAA": Decimal("8")},
    )
    assert model.round_trip_cost_bps("AAA") == Decimal("8")  # half-spread 4 per side
    assert model.round_trip_cost_bps("BBB") == Decimal("0")


def test_round_trip_cost_includes_every_component_and_the_stress_multiplier() -> None:
    foreign = TransactionCostModel()  # 10/2 + 5 + 2 + 15 = 27 bps per side
    assert foreign.round_trip_cost_bps("AAA") == Decimal("54")
    gbp = TransactionCostModel(instrument_currencies={"AAA": "GBP"})
    assert gbp.round_trip_cost_bps("AAA") == Decimal("24")
    stressed = TransactionCostModel(stress_multiplier=Decimal("2"))
    assert stressed.round_trip_cost_bps("AAA") == Decimal("108")


def test_describe_reports_the_cheapest_round_trip_across_the_universe() -> None:
    model = TransactionCostModel(instrument_currencies={"AAA": "GBP"})
    description = model.describe(tickers=["AAA", "BBB"])
    assert description["min_round_trip_cost_bps"] == 24.0
    assert description["label"] == "standard"


def test_near_zero_cost_models_are_not_eligible_for_gated_verdicts() -> None:
    cheap = TransactionCostModel(
        spread_bps=Decimal("1"),
        slippage_bps=Decimal("1"),
        commission_bps=Decimal("0.5"),
        fx_fee_rate=Decimal("0"),
    )
    refusal = cost_gate_refusal(cheap.describe(tickers=["AAA"]))
    assert refusal is not None
    assert f"{MIN_ROUND_TRIP_COST_BPS:g} bps" in refusal
    assert cost_gate_refusal(TransactionCostModel().describe(tickers=["AAA"])) is None


def test_cost_gate_refuses_zero_cost_and_unverifiable_descriptions() -> None:
    assert "zero-cost" in (
        cost_gate_refusal(TransactionCostModel.explicit_zero().describe(tickers=["AAA"])) or ""
    )
    assert cost_gate_refusal({"label": "standard", "zero_cost": False}) is not None


def test_legacy_flat_bps_path_is_labelled_and_never_gated() -> None:
    history = synth_history(40, seed=5, vol=0.0)
    result = PortfolioBacktester(
        strategy=ConstantWeightStrategy(),
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=history[1][0].date(),
        end_date=history[1][-1].date(),
        transaction_cost_bps=Decimal("50"),  # large enough to clear any numeric floor
    ).run({"AAA": history})

    assert result.cost_model["label"] == "legacy_flat_bps"
    assert result.cost_model["gated"] is False
    refusal = cost_gate_refusal(result.cost_model)
    assert refusal is not None
    assert "legacy" in refusal


def test_engine_description_covers_the_whole_universe() -> None:
    history = synth_history(40, seed=5, vol=0.0)
    result = PortfolioBacktester(
        strategy=ConstantWeightStrategy(),
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=history[1][0].date(),
        end_date=history[1][-1].date(),
    ).run({"AAA": history})
    assert result.cost_model["min_round_trip_cost_bps"] == 54.0


@pytest.mark.parametrize("label", ["promising", "strong"])
def test_route_refuses_positive_verdicts_on_near_zero_costs(label: str) -> None:
    sharpe = {"promising": 0.8, "strong": 1.3}[label]
    result = _fake_result(sharpe=sharpe)
    result.cost_model = {"label": "standard", "zero_cost": False, "min_round_trip_cost_bps": 2.0}
    interpretation = route._interpret_portfolio_results(
        result, _fake_wf(passes=True, sharpe=sharpe)
    )
    assert interpretation["verdict"] == "unvalidated"
    assert "round-trip" in interpretation["validation"]["refusal_reason"]


def test_route_refuses_legacy_cost_labels_even_when_expensive() -> None:
    result = _fake_result(sharpe=0.8)
    result.cost_model = {
        "label": "legacy_flat_bps",
        "zero_cost": False,
        "min_round_trip_cost_bps": 500.0,
    }
    interpretation = route._interpret_portfolio_results(result, _fake_wf(passes=True, sharpe=0.8))
    assert interpretation["verdict"] == "unvalidated"
    assert "legacy" in interpretation["validation"]["refusal_reason"]
