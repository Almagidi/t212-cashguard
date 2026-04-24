from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.strategies.closing_momentum import ClosingMomentumStrategy
from app.strategies.indicators import Bar
from app.strategies.intraday_periodicity import IntradayPeriodicityStrategy


def make_bar(price: Decimal, *, volume: Decimal) -> Bar:
    return Bar(
        open=price - Decimal("0.04"),
        high=price + Decimal("0.10"),
        low=price - Decimal("0.08"),
        close=price,
        volume=volume,
    )


def build_rising_session(
    *,
    start_price: Decimal,
    bar_count: int,
    step: Decimal,
    heavy_last_volume: Decimal = Decimal("220000"),
) -> list[Bar]:
    bars: list[Bar] = []
    for index in range(bar_count):
        price = start_price + (step * Decimal(str(index)))
        volume = heavy_last_volume if index == bar_count - 1 else Decimal("100000")
        bars.append(make_bar(price, volume=volume))
    return bars


def build_periodicity_history(
    *,
    start_day: datetime,
    sessions: int,
    slot_index: int,
) -> tuple[list[Bar], list[datetime]]:
    bars: list[Bar] = []
    times: list[datetime] = []
    slot_minutes = 30
    bars_per_slot = slot_minutes // 5
    slots_per_day = 13

    for day_offset in range(sessions):
        session_date = start_day + timedelta(days=day_offset)
        session_start = session_date.replace(hour=14, minute=30, second=0, microsecond=0)
        base_price = Decimal("100") + Decimal(str(day_offset))
        for slot in range(slots_per_day):
            for bar_in_slot in range(bars_per_slot):
                bar_index = slot * bars_per_slot + bar_in_slot
                drift = Decimal("0.05") * Decimal(str(bar_index))
                if slot == slot_index:
                    drift += Decimal("0.12") * Decimal(str(bar_in_slot + 1))
                price = base_price + drift
                timestamp = session_start + timedelta(minutes=5 * bar_index)
                volume = Decimal("125000") if slot == slot_index and bar_in_slot == bars_per_slot - 1 else Decimal("90000")
                bars.append(make_bar(price, volume=volume))
                times.append(timestamp)
    return bars, times


def test_closing_momentum_generates_signal_in_final_half_hour():
    strategy = ClosingMomentumStrategy()
    session_bars = build_rising_session(
        start_price=Decimal("100.20"),
        bar_count=74,
        step=Decimal("0.08"),
    )

    signal = strategy.generate_signal(
        ticker="AAPL",
        bars=session_bars,
        account_value=Decimal("25000"),
        available_cash=Decimal("12000"),
        current_time_utc="20:35",
        prev_close=Decimal("99.70"),
    )

    assert signal is not None
    assert signal.side == "buy"
    assert signal.entry_price > signal.stop_price
    assert signal.take_profit_price > signal.entry_price
    assert signal.suggested_quantity > 0
    assert signal.params_snapshot["opening_return_pct"] > 0


def test_intraday_periodicity_requires_same_slot_history_and_live_confirmation():
    strategy = IntradayPeriodicityStrategy()
    current_session_start = datetime(2026, 4, 10, 14, 30, tzinfo=UTC)
    current_bars = build_rising_session(
        start_price=Decimal("108.50"),
        bar_count=66,
        step=Decimal("0.08"),
        heavy_last_volume=Decimal("180000"),
    )
    current_times = [
        current_session_start + timedelta(minutes=5 * index)
        for index in range(len(current_bars))
    ]
    history_bars, history_times = build_periodicity_history(
        start_day=datetime(2026, 4, 3, tzinfo=UTC),
        sessions=5,
        slot_index=10,
    )

    signal = strategy.generate_signal(
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

    assert signal is not None
    assert signal.side == "buy"
    assert signal.params_snapshot["history_sessions"] >= 4
    assert signal.params_snapshot["positive_ratio"] >= 0.6
    assert signal.take_profit_price > signal.entry_price > signal.stop_price
