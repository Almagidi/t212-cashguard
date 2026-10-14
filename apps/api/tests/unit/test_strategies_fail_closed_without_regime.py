"""No strategy may take a trade when the Choppiness Index cannot be computed.

Each case runs a scenario twice: once with the regime gate answering with a value that
allows the trade, to prove the scenario really reaches and passes the gate, and once with
the gate answering "unavailable", which must stop it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

import app.strategies.closing_momentum as closing_momentum_module
import app.strategies.intraday_periodicity as intraday_periodicity_module
import app.strategies.kraken_momentum as kraken_momentum_module
import app.strategies.kraken_trend_follow as kraken_trend_follow_module
import app.strategies.opening_fade as opening_fade_module
import app.strategies.orb_production as orb_module
import app.strategies.vwap_reclaim as vwap_reclaim_module
from app.strategies import indicators
from app.strategies.indicators import Bar, market_regime
from tests.unit import test_intraday_strategy_pack as intraday_cases
from tests.unit import test_kraken_strategy_coverage as kraken_cases
from tests.unit import test_opening_fade as fade_cases
from tests.unit import test_orb_production as orb_cases
from tests.unit import test_vwap_reclaim as vwap_cases


def _bar(open_: object, high: object, low: object, close: object, volume: int = 30_000) -> Bar:
    return Bar(*(Decimal(str(value)) for value in (open_, high, low, close, volume)))


def _rising(count: int) -> list[Bar]:
    return [_bar(100 + i, 102 + i, 99 + i, 101 + i) for i in range(count)]


def _falling(count: int) -> list[Bar]:
    return [_bar(200 - i, 201 - i, 198 - i, 199 - i) for i in range(count)]


def _flat(count: int) -> list[Bar]:
    return [_bar(100, 102, 98, 100) for _ in range(count)]


# ── market_regime: one implementation, explicit "unknown" ────────────────────


@pytest.mark.parametrize("count", [0, 5, 14])
def test_regime_is_unknown_before_the_index_has_enough_bars(count: int) -> None:
    assert market_regime(_rising(count)) == "unknown"


def test_regime_is_unknown_when_the_window_has_no_range() -> None:
    assert market_regime([_bar(100, 100, 100, 100) for _ in range(40)]) == "unknown"


def test_regime_is_available_from_fifteen_bars() -> None:
    # The trend averages need 21 bars, so a short clean trend is "neutral", not "unknown".
    assert market_regime(_rising(15)) == "neutral"
    assert market_regime(_rising(40)) == "trending_up"
    assert market_regime(_falling(40)) == "trending_down"


def test_flat_back_and_forth_market_is_choppy() -> None:
    assert market_regime(_flat(40)) == "choppy"


def test_regime_uses_the_shared_choppiness_index(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def recording(bars: list[Bar], period: int = 14) -> Decimal | None:
        calls.append((len(bars), period))
        return Decimal("61.81")

    monkeypatch.setattr(indicators, "choppiness_index", recording)

    assert market_regime(_rising(40)) == "choppy"
    assert calls == [(40, 14)]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("61.80", "trending_up"), ("61.81", "choppy"), ("0.00", "trending_up"), ("287.74", "choppy")],
)
def test_choppy_threshold_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setattr(indicators, "choppiness_index", lambda _bars, _period=14: Decimal(value))

    assert market_regime(_rising(40)) == expected


# ── ORB ──────────────────────────────────────────────────────────────────────


def _orb() -> orb_module.OpeningRangeBreakoutStrategy:
    return orb_module.OpeningRangeBreakoutStrategy(params=orb_cases.RELAXED)


@pytest.mark.parametrize(
    ("check", "bars", "reason"),
    [
        ("_check_filters", _rising(25), "Market regime unavailable — skip"),
        ("_check_filters_short", _falling(25), "Market regime unavailable — skip shorts"),
    ],
    ids=["long", "short"],
)
def test_orb_filters_refuse_an_unavailable_regime(
    monkeypatch: pytest.MonkeyPatch, check: str, bars: list[Bar], reason: str
) -> None:
    strategy = _orb()
    monkeypatch.setattr(orb_module, "market_regime", lambda _bars: "neutral")
    assert getattr(strategy, check)(bars, orb_cases.VALID_TIME, None)[0] is True

    monkeypatch.setattr(orb_module, "market_regime", lambda _bars: "unknown")

    assert getattr(strategy, check)(bars, orb_cases.VALID_TIME, None) == (False, reason)


@pytest.mark.parametrize("check", ["_check_filters", "_check_filters_short"])
def test_orb_filters_refuse_a_session_too_short_for_the_index(check: str) -> None:
    # Real indicator, no patching: 14 bars cannot give 14 true ranges.
    ok, reason = getattr(_orb(), check)(_rising(14), orb_cases.VALID_TIME, None)

    assert ok is False
    assert "regime unavailable" in reason


# ── Strategies that read market_regime ───────────────────────────────────────


def _closing_momentum_signal() -> Any:
    return closing_momentum_module.ClosingMomentumStrategy().generate_signal(
        ticker="AAPL",
        bars=intraday_cases.build_rising_session(
            start_price=Decimal("100.20"), bar_count=74, step=Decimal("0.08")
        ),
        account_value=Decimal("25000"),
        available_cash=Decimal("12000"),
        current_time_utc="20:35",
        prev_close=Decimal("99.70"),
    )


def _intraday_periodicity_signal() -> Any:
    session_start = datetime(2026, 4, 10, 14, 30, tzinfo=UTC)
    current_bars = intraday_cases.build_rising_session(
        start_price=Decimal("108.50"),
        bar_count=66,
        step=Decimal("0.08"),
        heavy_last_volume=Decimal("180000"),
    )
    current_times = [session_start + timedelta(minutes=5 * i) for i in range(len(current_bars))]
    history_bars, history_times = intraday_cases.build_periodicity_history(
        start_day=datetime(2026, 4, 3, tzinfo=UTC), sessions=5, slot_index=10
    )
    return intraday_periodicity_module.IntradayPeriodicityStrategy().generate_signal(
        ticker="MSFT",
        bars=current_bars,
        account_value=Decimal("30000"),
        available_cash=Decimal("15000"),
        current_time_utc="19:55",
        prev_close=Decimal("108.10"),
        bar_times=current_times,
        history_bars=history_bars + current_bars,
        history_bar_times=history_times + current_times,
    )


def _vwap_reclaim_signal() -> Any:
    strategy = vwap_reclaim_module.VWAPReclaimStrategy(params=vwap_cases.RELAXED_PARAMS)
    return strategy.generate_signal(
        "TSLA",
        vwap_cases._build_reclaim_bars(volume=80_000),
        Decimal("100000"),
        Decimal("50000"),
        vwap_cases.VALID_TIME,
    )


@pytest.mark.parametrize(
    ("module", "produce", "allowing"),
    [
        (closing_momentum_module, _closing_momentum_signal, "trending_up"),
        (intraday_periodicity_module, _intraday_periodicity_signal, "trending_up"),
        (vwap_reclaim_module, _vwap_reclaim_signal, "neutral"),
    ],
    ids=["closing_momentum", "intraday_periodicity", "vwap_reclaim"],
)
def test_regime_reading_strategy_takes_no_trade_when_the_regime_is_unknown(
    monkeypatch: pytest.MonkeyPatch, module: Any, produce: Any, allowing: str
) -> None:
    monkeypatch.setattr(module, "market_regime", lambda _bars: allowing)
    assert produce() is not None

    monkeypatch.setattr(module, "market_regime", lambda _bars: "unknown")

    assert produce() is None


# ── Strategies that read the index directly ──────────────────────────────────


def test_kraken_momentum_takes_no_trade_when_the_index_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kraken_cases._patch_momentum_success(monkeypatch)
    strategy = kraken_momentum_module.KrakenMomentumStrategy()
    arguments = (
        "BTC/USD",
        kraken_cases._bars(25, price=125),
        kraken_cases.ACCOUNT,
        kraken_cases.CASH,
        kraken_cases.NOW,
    )
    assert strategy.generate_signal(*arguments) is not None

    monkeypatch.setattr(kraken_momentum_module, "choppiness_index", lambda _bars, _period: None)

    assert strategy.generate_signal(*arguments) is None


def test_kraken_trend_follow_takes_no_trade_when_the_index_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kraken_cases._patch_trend_success(monkeypatch)
    strategy = kraken_trend_follow_module.KrakenHTFBreakoutStrategy()
    arguments = (
        "BTC/USD",
        kraken_cases._bars(55, price=150),
        kraken_cases.ACCOUNT,
        kraken_cases.CASH,
        kraken_cases.NOW,
    )
    assert strategy.generate_signal(*arguments) is not None

    monkeypatch.setattr(kraken_trend_follow_module, "choppiness_index", lambda _bars, _period: None)

    assert strategy.generate_signal(*arguments) is None


def test_opening_fade_stops_at_the_gate_when_the_index_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fade_cases.TestGenerateSignalGapDown()
    strategy = case._svc()
    gate_calls: list[int] = []
    past_the_gate: list[str] = []
    answer: list[Decimal | None] = [Decimal("80")]

    def gate(bars: list[Bar], period: int = 14) -> Decimal | None:
        gate_calls.append(len(bars))
        return answer[0]

    original_confirm = opening_fade_module.OpeningFadeStrategy._count_confirm_bars

    def recording_confirm(self: Any, *args: Any, **kwargs: Any) -> Any:
        past_the_gate.append(kwargs.get("direction", ""))
        return original_confirm(self, *args, **kwargs)

    monkeypatch.setattr(opening_fade_module, "choppiness_index", gate)
    monkeypatch.setattr(
        opening_fade_module.OpeningFadeStrategy, "_count_confirm_bars", recording_confirm
    )

    def run() -> Any:
        return strategy.generate_signal(
            "AAPL",
            case._gap_down_bars(),
            fade_cases.ACCOUNT,
            fade_cases.CASH,
            fade_cases.VALID_TIME,
            prev_close=Decimal("110"),
            session_open=Decimal("105"),
        )

    run()
    assert gate_calls == [20]
    assert past_the_gate == ["up"]

    answer[0] = None
    result = run()

    assert result is None
    assert len(gate_calls) == 2
    assert past_the_gate == ["up"]
