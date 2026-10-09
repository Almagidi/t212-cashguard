"""
Property-based tests using Hypothesis.
These tests find edge cases that hand-crafted tests miss by
generating thousands of random inputs and checking invariants.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from hypothesis import assume, example, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from app.broker.trading212 import make_sell_quantity
from app.market_data.exchange_calendar import calendar_for_venue
from app.market_data.mock_provider import MockMarketDataProvider


@given(as_of_date=st.dates(min_value=date(2026, 1, 1), max_value=date(2026, 12, 31)))
@example(as_of_date=date(2026, 3, 8))
@example(as_of_date=date(2026, 3, 9))
@example(as_of_date=date(2026, 7, 3))
@example(as_of_date=date(2026, 11, 1))
@example(as_of_date=date(2026, 11, 2))
@example(as_of_date=date(2026, 11, 27))
@h_settings(max_examples=60)
def test_orb_breakout_profile_is_valid_xnys_history_without_future_bars(
    as_of_date: date,
) -> None:
    as_of = datetime.combine(as_of_date, time(18), UTC)
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA",
        interval_minutes=5,
        bars=100,
        as_of=as_of,
    )
    calendar = calendar_for_venue("XNYS")
    timestamps = [datetime.fromisoformat(str(row["timestamp"])) for row in rows]
    local_date = as_of.astimezone(ZoneInfo(calendar.exchange_timezone)).date()
    sessions = calendar.expected_sessions(local_date - timedelta(days=14), local_date)
    expected_session = calendar.session_for_timestamp(as_of) or sessions[-1]
    previous_session = calendar.previous_session(expected_session)
    current_timestamps = timestamps[1:]

    assert rows
    assert timestamps == sorted(set(timestamps))
    assert all(timestamp + timedelta(minutes=5) <= as_of for timestamp in timestamps)
    assert all(calendar.session_for_timestamp(timestamp) is not None for timestamp in timestamps)
    assert timestamps[0] == calendar.session_close(previous_session) - timedelta(minutes=5)
    assert calendar.is_terminal_bar(previous_session, timestamps[0], interval_minutes=5)
    assert current_timestamps
    assert current_timestamps[0] == calendar.session_open(expected_session)
    assert all(
        calendar.session_for_timestamp(timestamp) == expected_session
        for timestamp in current_timestamps
    )
    assert all(
        Decimal(str(row["low"])) > 0
        and Decimal(str(row["low"]))
        <= min(Decimal(str(row["open"])), Decimal(str(row["close"])))
        <= max(Decimal(str(row["open"])), Decimal(str(row["close"])))
        <= Decimal(str(row["high"]))
        and Decimal(str(row["volume"])) > 0
        for row in rows
    )


# ── Cash guard invariants ─────────────────────────────────────────────────────


def cash_guard_check(quantity: Decimal, price: Decimal, available: Decimal) -> bool:
    """True = order allowed, False = blocked. Side is always buy here."""
    if quantity <= 0:
        return True  # Sell — always allowed
    cost = quantity * price
    return cost <= available


@given(
    quantity=st.decimals(
        min_value="0.01", max_value="10000", allow_nan=False, allow_infinity=False
    ),
    price=st.decimals(min_value="0.01", max_value="100000", allow_nan=False, allow_infinity=False),
    available=st.decimals(
        min_value="0", max_value="1000000", allow_nan=False, allow_infinity=False
    ),
)
@h_settings(max_examples=500)
def test_cash_guard_never_overspends(quantity, price, available):
    """
    INVARIANT: if cash_guard_check returns True, cost <= available.
    No matter what inputs we generate, the guard must never allow spending
    more than available.
    """
    allowed = cash_guard_check(quantity, price, available)
    cost = quantity * price
    if allowed:
        # If allowed, cost must not exceed available (with tiny fp tolerance)
        assert cost <= available + Decimal("0.000001"), (
            f"VIOLATION: allowed order with cost={cost} > available={available}"
        )


@given(
    quantity=st.decimals(
        min_value="-10000", max_value="-0.01", allow_nan=False, allow_infinity=False
    ),
    price=st.decimals(min_value="0.01", max_value="100000", allow_nan=False, allow_infinity=False),
    available=st.decimals(min_value="0", max_value="0", allow_nan=False, allow_infinity=False),
)
@h_settings(max_examples=200)
def test_sell_never_blocked_by_cash_guard(quantity, price, available):
    """
    INVARIANT: sell orders (negative quantity) are NEVER blocked by cash guard.
    Cash is needed to BUY. To SELL you already hold the position.
    """
    allowed = cash_guard_check(quantity, price, available)
    assert allowed, f"VIOLATION: sell blocked by cash guard (qty={quantity}, available={available})"


@given(
    q=st.decimals(
        min_value="-100000", max_value="100000", allow_nan=False, allow_infinity=False
    ).filter(lambda x: x != 0),
)
@h_settings(max_examples=500)
def test_sell_quantity_always_negative(q):
    """
    INVARIANT: make_sell_quantity always returns a negative number.
    T212 requires negative quantity for all sell orders.
    Any input, any sign — output must be negative.
    """
    result = make_sell_quantity(q)
    assert result < 0, f"VIOLATION: make_sell_quantity({q}) = {result} is not negative"
    expected = q.copy_abs().copy_negate()
    assert result == expected, f"VIOLATION: {result} != {expected}"


@given(
    q=st.decimals(min_value="0.01", max_value="100000", allow_nan=False, allow_infinity=False),
)
@h_settings(max_examples=200)
def test_sell_quantity_magnitude_preserved(q):
    """
    INVARIANT: magnitude is preserved — only sign changes.
    """
    result = make_sell_quantity(q)
    magnitude = result.copy_abs()
    assert magnitude == q, f"VIOLATION: abs(make_sell_quantity({q})) = {magnitude} != {q}"


# ── ORB quantity sizing invariants ────────────────────────────────────────────


def calc_orb_quantity(
    entry: Decimal,
    stop: Decimal,
    account_value: Decimal,
    available_cash: Decimal,
    risk_pct: Decimal = Decimal("1.0"),
) -> Decimal:
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return Decimal("0")
    max_risk_dollars = account_value * risk_pct / 100
    qty_by_risk = max_risk_dollars / risk_per_share
    qty_by_cash = available_cash / entry if entry > 0 else Decimal("0")
    return min(qty_by_risk, qty_by_cash)


@given(
    entry=st.decimals(min_value="1", max_value="10000", allow_nan=False, allow_infinity=False),
    stop=st.decimals(min_value="0.5", max_value="9999", allow_nan=False, allow_infinity=False),
    account_value=st.decimals(
        min_value="100", max_value="1000000", allow_nan=False, allow_infinity=False
    ),
    available_cash=st.decimals(
        min_value="0", max_value="1000000", allow_nan=False, allow_infinity=False
    ),
    risk_pct=st.decimals(min_value="0.1", max_value="5.0", allow_nan=False, allow_infinity=False),
)
@h_settings(max_examples=500)
def test_orb_quantity_never_exceeds_cash(entry, stop, account_value, available_cash, risk_pct):
    """
    INVARIANT: calculated position size never requires more cash than available.
    This is a safety property — position sizing must respect cash constraints.
    """
    assume(entry > stop)  # Long trade: entry above stop
    assume(available_cash <= account_value)

    qty = calc_orb_quantity(entry, stop, account_value, available_cash, risk_pct)
    cost = qty * entry

    assert cost <= available_cash + Decimal("0.000001"), (
        f"VIOLATION: calculated qty={qty} costs {cost} > available_cash={available_cash}"
    )


@given(
    entry=st.decimals(min_value="1", max_value="10000", allow_nan=False, allow_infinity=False),
    stop=st.decimals(min_value="0.5", max_value="9999", allow_nan=False, allow_infinity=False),
    account_value=st.decimals(
        min_value="100", max_value="1000000", allow_nan=False, allow_infinity=False
    ),
    available_cash=st.decimals(
        min_value="1", max_value="1000000", allow_nan=False, allow_infinity=False
    ),
    risk_pct=st.decimals(min_value="0.1", max_value="5.0", allow_nan=False, allow_infinity=False),
)
@h_settings(max_examples=500)
def test_orb_quantity_non_negative(entry, stop, account_value, available_cash, risk_pct):
    """INVARIANT: quantity is always >= 0."""
    assume(entry > stop)
    qty = calc_orb_quantity(entry, stop, account_value, available_cash, risk_pct)
    assert qty >= 0, f"VIOLATION: negative quantity {qty}"


# ── Dedup key invariants ──────────────────────────────────────────────────────


def make_client_key(signal_id: str | None, ticker: str, side: str) -> str:
    raw = f"{signal_id or 'manual'}:{ticker}:{side}:"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


@given(
    signal_id=st.one_of(st.none(), st.text(min_size=1, max_size=50)),
    ticker=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("Lu",))),
    side=st.sampled_from(["buy", "sell"]),
)
@h_settings(max_examples=300)
def test_dedup_key_deterministic(signal_id, ticker, side):
    """INVARIANT: same inputs always produce same key."""
    k1 = make_client_key(signal_id, ticker, side)
    k2 = make_client_key(signal_id, ticker, side)
    assert k1 == k2, f"VIOLATION: key is not deterministic for ({signal_id}, {ticker}, {side})"


@given(
    ticker=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("Lu",))),
)
@h_settings(max_examples=200)
def test_dedup_key_buy_sell_differ(ticker):
    """INVARIANT: buy and sell keys must differ for same ticker."""
    assume(len(ticker) > 0)
    buy_key = make_client_key("sig-1", ticker, "buy")
    sell_key = make_client_key("sig-1", ticker, "sell")
    assert buy_key != sell_key, f"VIOLATION: buy and sell produce same key for {ticker}"
