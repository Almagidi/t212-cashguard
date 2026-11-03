from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.feed_health import (
    _summarize_status,
    get_feed_health_snapshot,
    is_symbol_trade_safe,
    record_feed_health,
    reset_feed_health,
)


def test_unknown_symbol_is_not_trade_safe_for_entries_but_exits_are_unaffected():
    reset_feed_health()

    assert is_symbol_trade_safe("AAPL") is False
    assert is_symbol_trade_safe("AAPL", purpose="entry") is False
    assert is_symbol_trade_safe("AAPL", purpose="exit") is True


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


def _record(ticker: str, status: str, **kwargs):
    record_feed_health(
        provider="test",
        ticker=ticker,
        status=status,
        detail="x",
        used_source="primary",
        **kwargs,
    )


def test_ok_symbol_is_trade_safe_for_entries_and_exits():
    reset_feed_health()
    _record("AAPL", "ok")

    assert is_symbol_trade_safe("AAPL") is True
    assert is_symbol_trade_safe("AAPL", purpose="exit") is True


def test_delayed_fallback_blocks_entries_but_not_exits():
    reset_feed_health()
    _record("AAPL", "fallback", fallback_used=True, is_delayed=True)

    assert is_symbol_trade_safe("AAPL") is False
    assert is_symbol_trade_safe("AAPL", purpose="exit") is True


def test_fallback_with_unknown_delay_fails_closed_for_entries():
    reset_feed_health()
    _record("AAPL", "fallback", fallback_used=True)  # is_delayed not stated

    assert is_symbol_trade_safe("AAPL") is False


def test_realtime_fallback_is_trade_safe_for_entries():
    reset_feed_health()
    _record("AAPL", "fallback", fallback_used=True, is_delayed=False)

    assert is_symbol_trade_safe("AAPL") is True


def test_degraded_stale_error_block_entries_and_exits_keep_legacy_behaviour():
    reset_feed_health()
    for index, status in enumerate(("degraded", "stale", "error")):
        ticker = f"T{index}"
        _record(ticker, status)
        assert is_symbol_trade_safe(ticker) is False
        assert is_symbol_trade_safe(ticker, purpose="exit") is False
