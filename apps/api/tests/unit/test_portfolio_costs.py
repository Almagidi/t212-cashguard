from __future__ import annotations

from decimal import Decimal

import pytest

from app.backtest.portfolio_costs import TransactionCostModel
from app.backtest.portfolio_engine import PortfolioBacktester
from app.execution.paper_policy import evaluate_paper_fill
from tests.unit.portfolio_synth import ConstantWeightStrategy, synth_history

BPS = Decimal("10000")


def _single_buy(cost_model: TransactionCostModel | None, ticker: str = "AAA"):
    history = synth_history(5, seed=11, vol=0.0)
    return PortfolioBacktester(
        strategy=ConstantWeightStrategy(),
        universe=[ticker],
        initial_capital=Decimal("10000"),
        start_date=history[1][0].date(),
        end_date=history[1][-1].date(),
        cost_model=cost_model,
    ).run({ticker: history})


def _cost_bps(trade) -> Decimal:
    return trade.cost / trade.notional * BPS


def test_single_usd_buy_on_gbp_account_pays_fx_fee_plus_half_spread_by_default() -> None:
    result = _single_buy(None)  # default model: GBP account, unknown currency => foreign
    trade = result.trades[0]

    assert trade.side == "buy"
    assert trade.fx_fee_cost > 0
    # >= 15 bps FX + half of the default 10 bps spread (5 bps); slippage and commission add more.
    assert _cost_bps(trade) >= Decimal("15") + Decimal("5")
    assert trade.fx_fee_cost / trade.notional * BPS == pytest.approx(
        Decimal("15"), rel=Decimal("0.005")
    )
    assert result.total_fx_fee_cost == trade.fx_fee_cost
    assert result.cost_model["label"] == "standard"
    assert result.cost_model["unknown_currency_policy"] == "treated_as_foreign_fx_fee_applied"


def test_explicit_usd_currency_matches_unknown_currency() -> None:
    unknown = _single_buy(TransactionCostModel()).trades[0]
    usd = _single_buy(TransactionCostModel(instrument_currencies={"AAA": "usd"})).trades[0]
    assert usd.fx_fee_cost == unknown.fx_fee_cost > 0


def test_gbp_listed_instrument_has_no_fx_fee_and_matches_paper_policy() -> None:
    result = _single_buy(TransactionCostModel(instrument_currencies={"AAA": "GBP"}))
    trade = result.trades[0]
    expected = evaluate_paper_fill(
        side="buy", quantity=trade.shares, quote_price=trade.quote_price, profile="standard"
    )

    assert trade.fx_fee_cost == 0
    assert trade.price == expected.fill_price
    assert trade.fee_cost == expected.fee_amount


def test_default_instrument_currency_can_mark_whole_universe_as_account_currency() -> None:
    model = TransactionCostModel(default_instrument_currency="GBP")
    assert not model.is_foreign("ANYTHING")
    assert TransactionCostModel().is_foreign("ANYTHING")


def test_stress_multiplier_scales_every_cost_component() -> None:
    base = TransactionCostModel()
    quote = Decimal("100")
    quantity = Decimal("50")
    one = base.fill(side="buy", quantity=quantity, quote_price=quote, ticker="X")
    for multiple in (2, 3):
        scaled = TransactionCostModel(stress_multiplier=Decimal(multiple)).fill(
            side="buy", quantity=quantity, quote_price=quote, ticker="X"
        )
        assert scaled.fx_fee == pytest.approx(one.fx_fee * multiple, rel=Decimal("0.005"))
        assert (scaled.price - quote) == pytest.approx(
            (one.price - quote) * multiple, rel=Decimal("0.005")
        )
        assert scaled.fee == pytest.approx(one.fee * multiple, rel=Decimal("0.005"))


def test_stress_multiplier_raises_total_cost_in_a_run_and_is_reported() -> None:
    costs = {
        mult: _single_buy(TransactionCostModel(stress_multiplier=Decimal(mult)))
        for mult in (1, 2, 3)
    }
    totals = [costs[m].total_execution_cost for m in (1, 2, 3)]
    assert totals[0] < totals[1] < totals[2]
    assert totals[2] / totals[0] == pytest.approx(Decimal(3), rel=Decimal("0.02"))
    assert costs[3].cost_model["stress_multiplier"] == 3.0


def test_benchmark_is_costed_with_the_same_model() -> None:
    history = synth_history(5, seed=11, vol=0.0, drift=0.0)
    flat = {"AAA": history}
    kwargs = {
        "strategy": ConstantWeightStrategy(),
        "universe": ["AAA"],
        "initial_capital": Decimal("10000"),
        "start_date": history[1][0].date(),
        "end_date": history[1][-1].date(),
    }
    costed = PortfolioBacktester(**kwargs).run(flat)
    free = PortfolioBacktester(**kwargs, cost_model=TransactionCostModel.explicit_zero()).run(flat)

    assert free.benchmark_return_pct == Decimal("0.00")
    assert costed.benchmark_return_pct < Decimal("-0.20")  # >= 27 bps entry cost on a flat series


def test_zero_cost_only_when_explicit_and_it_is_labelled() -> None:
    with pytest.raises(ValueError, match="explicitly"):
        TransactionCostModel(
            spread_bps=Decimal(0),
            slippage_bps=Decimal(0),
            commission_bps=Decimal(0),
            fx_fee_rate=Decimal(0),
        )
    with pytest.raises(ValueError, match="zero_cost=True requires"):
        TransactionCostModel(zero_cost=True)

    result = _single_buy(TransactionCostModel.explicit_zero())
    assert result.cost_model["label"] == "zero_cost_explicit"
    assert result.cost_model["zero_cost"] is True
    assert result.total_execution_cost == 0


def test_negative_and_invalid_inputs_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        TransactionCostModel(fx_fee_rate=Decimal("-0.001"))
    with pytest.raises(ValueError, match="positive"):
        TransactionCostModel(stress_multiplier=Decimal(0))
    with pytest.raises(ValueError, match="either"):
        PortfolioBacktester(
            strategy=ConstantWeightStrategy(),
            universe=["AAA"],
            initial_capital=Decimal("1"),
            start_date=synth_history(2, seed=1)[1][0].date(),
            end_date=synth_history(2, seed=1)[1][1].date(),
            transaction_cost_bps=Decimal(5),
            cost_model=TransactionCostModel(),
        )


def test_per_ticker_spread_override_changes_fill_price() -> None:
    wide = TransactionCostModel(spread_bps_by_ticker={"wide": Decimal(100)})
    narrow = wide.fill(side="buy", quantity=Decimal(1), quote_price=Decimal(100), ticker="NARROW")
    broad = wide.fill(side="buy", quantity=Decimal(1), quote_price=Decimal(100), ticker="WIDE")
    assert broad.price - Decimal(100) == Decimal("0.55")  # 50 bps half spread + 5 bps slippage
    assert narrow.price - Decimal(100) == Decimal("0.10")
