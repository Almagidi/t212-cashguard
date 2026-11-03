"""Deterministic synthetic daily histories for portfolio backtest tests (no I/O)."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING

from app.market_data.exchange_calendar import calendar_for_venue
from app.strategies.indicators import Bar

if TYPE_CHECKING:
    from collections.abc import Sequence


@lru_cache(maxsize=8)
def session_times(length: int, start: date = date(2021, 1, 4)) -> tuple[datetime, ...]:
    sessions = calendar_for_venue("XNYS").expected_sessions(
        start, start + timedelta(days=length * 2 + 60)
    )
    if len(sessions) < length:
        raise AssertionError("fixture did not allocate enough XNYS sessions")
    return tuple(session.close_at.astimezone(UTC) for session in sessions[:length])


def synth_history(
    length: int,
    *,
    seed: int,
    drift: float = 0.0004,
    vol: float = 0.01,
    start_price: float = 100.0,
) -> tuple[list[Bar], list[datetime]]:
    rng = random.Random(seed)
    price = start_price
    bars: list[Bar] = []
    for _ in range(length):
        open_price = price
        close_price = max(1.0, open_price * (1.0 + rng.gauss(drift, vol)))
        high = max(open_price, close_price) * 1.002
        low = min(open_price, close_price) * 0.998
        bars.append(
            Bar(
                open=Decimal(f"{open_price:.4f}"),
                high=Decimal(f"{high:.4f}"),
                low=Decimal(f"{low:.4f}"),
                close=Decimal(f"{close_price:.4f}"),
                volume=Decimal("100000"),
            )
        )
        price = close_price
    return bars, list(session_times(length))


class ConstantWeightStrategy:
    """Equal weight on every symbol; ignores history (so warm-up is purely a delay)."""

    label = "Constant weight"
    description = "test"

    def __init__(self, *, min_history_bars: int = 0, rebalance_frequency: str = "monthly") -> None:
        self.min_history_bars = min_history_bars
        self.rebalance_frequency = rebalance_frequency

    def target_weights(self, history: dict[str, list[Bar]], *, as_of_index: int) -> dict:
        del as_of_index
        tickers: Sequence[str] = sorted(history)
        weight = Decimal("1") / Decimal(len(tickers))
        return dict.fromkeys(tickers, weight)
