from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.feed_health import (
    _summarize_status,
    get_feed_health_snapshot,
    is_symbol_trade_safe,
    record_feed_health,
    reset_feed_health,
)


def test_unknown_symbol_is_trade_safe():
    reset_feed_health()

    assert is_symbol_trade_safe("AAPL") is True


def test_feed_health_keeps_only_most_recent_symbols():
    reset_feed_health()

    for index in range(51):
        record_feed_health(
            provider="test",
            ticker=f"T{index}",
            status="ok",
            detail="fresh",
            used_source="primary",
            primary_timestamp=datetime.now(UTC) + timedelta(seconds=index),
        )

    tickers = {symbol["ticker"] for symbol in get_feed_health_snapshot()["symbols"]}

    assert len(tickers) == 50
    assert "T0" not in tickers
    assert "T50" in tickers


def test_summarize_status_precedence_and_empty_state():
    assert _summarize_status([]) == "unknown"
    assert _summarize_status([{"status": "error"}, {"status": "ok"}]) == "error"
    assert _summarize_status([{"status": "stale"}, {"status": "ok"}]) == "stale"
    assert _summarize_status([{"status": "fallback"}]) == "fallback"
    assert _summarize_status([{"status": "ok"}]) == "ok"
    assert _summarize_status([{"status": "mystery"}]) == "unknown"
