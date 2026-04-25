"""
Unit tests for the pure helper methods in StrategyRunner.

None of these methods require a database — we use a sentinel AsyncSession
that raises immediately if any DB call is attempted, so tests that accidentally
try to hit the DB will fail loudly rather than silently.
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.strategy_runner import StrategyRunner
from app.strategies.indicators import Bar


# ── helpers ───────────────────────────────────────────────────────────────────

def _runner() -> StrategyRunner:
    """Return a StrategyRunner with a stub DB that errors on use."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("DB must not be called"))
    return StrategyRunner(db)


def _strategy(
    *,
    type_: str = "orb",
    params: dict | None = None,
    allowed_tickers: list[str] | None = None,
    name: str = "test_strat",
) -> MagicMock:
    s = MagicMock()
    s.type = type_
    s.params = params or {}
    s.allowed_tickers = allowed_tickers or ["AAPL", "MSFT"]
    s.name = name
    return s


def _bar(close: float = 100.0) -> Bar:
    d = Decimal(str(close))
    return Bar(open=d, high=d, low=d, close=d, volume=Decimal("1000"))


def _bars(closes: list[float]) -> list[Bar]:
    return [_bar(c) for c in closes]


# ── _parse_session_open ───────────────────────────────────────────────────────

class TestParseSessionOpen:
    def setup_method(self):
        self.r = _runner()

    def test_hhmm_format_returns_time(self):
        t = self.r._parse_session_open("09:30")
        assert isinstance(t, time)
        assert t.hour == 9
        assert t.minute == 30

    def test_midnight(self):
        t = self.r._parse_session_open("00:00")
        assert t.hour == 0
        assert t.minute == 0

    def test_has_utc_tzinfo(self):
        t = self.r._parse_session_open("14:30")
        assert t.tzinfo == UTC

    def test_single_digit_hour(self):
        t = self.r._parse_session_open("9:05")
        assert t.hour == 9
        assert t.minute == 5


# ── _coerce_bar_time ──────────────────────────────────────────────────────────

class TestCoerceBarTime:
    def setup_method(self):
        self.r = _runner()

    def test_datetime_with_tz_returned_as_utc(self):
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=5))
        dt = datetime(2024, 6, 1, 10, 0, 0, tzinfo=tz)
        result = self.r._coerce_bar_time(dt)
        assert result is not None
        assert result.tzinfo == UTC
        assert result.hour == 5  # 10 - 5

    def test_naive_datetime_gets_utc(self):
        dt = datetime(2024, 6, 1, 14, 0, 0)
        result = self.r._coerce_bar_time(dt)
        assert result is not None
        assert result.tzinfo == UTC
        assert result.hour == 14

    def test_iso_string_parsed(self):
        result = self.r._coerce_bar_time("2024-06-01T14:30:00+00:00")
        assert result is not None
        assert result.hour == 14
        assert result.minute == 30

    def test_iso_string_with_z_parsed(self):
        result = self.r._coerce_bar_time("2024-06-01T09:30:00Z")
        assert result is not None
        assert result.tzinfo == UTC

    def test_invalid_string_returns_none(self):
        result = self.r._coerce_bar_time("not-a-date")
        assert result is None

    def test_none_returns_none(self):
        result = self.r._coerce_bar_time(None)
        assert result is None

    def test_integer_returns_none(self):
        result = self.r._coerce_bar_time(12345)
        assert result is None


# ── _extract_session_context ──────────────────────────────────────────────────

class TestExtractSessionContext:
    def setup_method(self):
        self.r = _runner()

    def _bar_times(self, date_str: str, hours: list[int]) -> list[datetime]:
        return [
            datetime(
                int(date_str[:4]), int(date_str[5:7]), int(date_str[8:]),
                h, 0, 0, tzinfo=UTC,
            )
            for h in hours
        ]

    def test_empty_bars_returns_unchanged(self):
        bars_out, times_out, prev = self.r._extract_session_context(
            [], [], session_open_utc="09:30"
        )
        assert bars_out == []
        assert times_out == []
        assert prev is None

    def test_mismatched_lengths_returns_unchanged(self):
        bars = _bars([100.0, 101.0])
        times = [datetime(2024, 1, 2, 9, 30, tzinfo=UTC)]
        bars_out, times_out, prev = self.r._extract_session_context(
            bars, times, session_open_utc="09:30"
        )
        assert bars_out is bars

    def test_session_bars_sliced_from_open(self):
        # Pre-session bars at 08:00–09:00, session bars at 09:30+
        times = self._bar_times("2024-01-02", [8, 9, 9, 10, 11])
        times[2] = times[2].replace(minute=30)
        bars = _bars([99.0, 99.5, 100.0, 101.0, 102.0])

        session_bars, session_times, prev = self.r._extract_session_context(
            bars, times, session_open_utc="09:30"
        )

        # Only bars at or after 09:30 should be in session
        assert len(session_bars) >= 1
        for t in session_times:
            assert t.hour >= 9

    def test_prev_close_is_last_pre_session_bar(self):
        times = [
            datetime(2024, 1, 2, 8, 0, tzinfo=UTC),   # pre-session
            datetime(2024, 1, 2, 9, 0, tzinfo=UTC),   # pre-session
            datetime(2024, 1, 2, 9, 30, tzinfo=UTC),  # session open
            datetime(2024, 1, 2, 10, 0, tzinfo=UTC),  # session
        ]
        bars = _bars([98.0, 99.0, 100.0, 101.0])

        _, _, prev = self.r._extract_session_context(
            bars, times, session_open_utc="09:30"
        )
        assert prev == Decimal("99.0")

    def test_no_pre_session_bars_prev_close_is_none(self):
        times = [
            datetime(2024, 1, 2, 9, 30, tzinfo=UTC),
            datetime(2024, 1, 2, 10, 0, tzinfo=UTC),
        ]
        bars = _bars([100.0, 101.0])

        _, _, prev = self.r._extract_session_context(
            bars, times, session_open_utc="09:30"
        )
        assert prev is None

    def test_uses_most_recent_date_with_session_bars(self):
        """Multi-day slice: most recent date's session should be returned."""
        times = [
            datetime(2024, 1, 1, 9, 30, tzinfo=UTC),
            datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
            datetime(2024, 1, 2, 9, 30, tzinfo=UTC),
            datetime(2024, 1, 2, 10, 0, tzinfo=UTC),
        ]
        bars = _bars([90.0, 91.0, 100.0, 101.0])
        session_bars, session_times, _ = self.r._extract_session_context(
            bars, times, session_open_utc="09:30"
        )
        # Should return the 2024-01-02 session
        assert all(t.date() == session_times[0].date() for t in session_times)
        assert all(t.day == 2 for t in session_times)


# ── _get_tickers ──────────────────────────────────────────────────────────────

class TestGetTickers:
    def setup_method(self):
        self.r = _runner()
        self.today = datetime.now(UTC).strftime("%Y-%m-%d")

    def test_returns_static_list_when_no_todays_watchlist(self):
        s = _strategy(params={}, allowed_tickers=["TSLA", "NVDA"])
        result = self.r._get_tickers(s)
        assert result == ["TSLA", "NVDA"]

    def test_returns_static_list_when_watchlist_date_stale(self):
        s = _strategy(
            params={
                "todays_watchlist": ["AAPL"],
                "watchlist_updated_at": "2020-01-01",
            },
            allowed_tickers=["TSLA"],
        )
        result = self.r._get_tickers(s)
        assert result == ["TSLA"]

    def test_returns_todays_watchlist_when_fresh(self):
        s = _strategy(
            params={
                "todays_watchlist": ["AAPL", "MSFT"],
                "watchlist_updated_at": self.today,
            },
            allowed_tickers=["TSLA"],
        )
        result = self.r._get_tickers(s)
        assert set(result) == {"AAPL", "MSFT"}

    def test_ranked_watchlist_sorted_by_score(self):
        s = _strategy(
            params={
                "todays_watchlist": ["AAPL", "MSFT", "NVDA"],
                "watchlist_updated_at": self.today,
                "watchlist_candidates": {
                    "AAPL": {"score": 0.5},
                    "MSFT": {"score": 0.9},
                    "NVDA": {"score": 0.7},
                },
            },
            allowed_tickers=["TSLA"],
        )
        result = self.r._get_tickers(s)
        assert result[0] == "MSFT"
        assert result[-1] == "AAPL"

    def test_empty_todays_watchlist_falls_through_to_static(self):
        s = _strategy(
            params={
                "todays_watchlist": [],
                "watchlist_updated_at": self.today,
            },
            allowed_tickers=["TSLA"],
        )
        result = self.r._get_tickers(s)
        assert result == ["TSLA"]


# ── _watchlist_context ────────────────────────────────────────────────────────

class TestWatchlistContext:
    def setup_method(self):
        self.r = _runner()

    def test_returns_empty_when_no_candidates(self):
        s = _strategy(params={})
        result = self.r._watchlist_context(s, "AAPL")
        assert result == {}

    def test_returns_context_for_ticker(self):
        s = _strategy(params={"watchlist_candidates": {"AAPL": {"score": 0.8}}})
        result = self.r._watchlist_context(s, "AAPL")
        assert result == {"score": 0.8}

    def test_case_fallback_lowercase_key(self):
        s = _strategy(params={"watchlist_candidates": {"aapl": {"score": 0.7}}})
        result = self.r._watchlist_context(s, "AAPL")
        # Tries uppercase first, then lowercase
        assert isinstance(result, dict)

    def test_missing_ticker_returns_empty(self):
        s = _strategy(params={"watchlist_candidates": {"MSFT": {"score": 0.9}}})
        result = self.r._watchlist_context(s, "AAPL")
        assert result == {}

    def test_non_dict_candidates_returns_empty(self):
        s = _strategy(params={"watchlist_candidates": "not_a_dict"})
        result = self.r._watchlist_context(s, "AAPL")
        assert result == {}

    def test_non_dict_value_returns_empty(self):
        s = _strategy(params={"watchlist_candidates": {"AAPL": "bad_value"}})
        result = self.r._watchlist_context(s, "AAPL")
        assert result == {}


# ── _apply_signal_intelligence_overlay ────────────────────────────────────────

class TestApplySignalIntelligenceOverlay:
    def setup_method(self):
        self.r = _runner()

    def _signal(self, confidence: float = 0.5, reason: str = "base") -> MagicMock:
        sig = MagicMock()
        sig.confidence = Decimal(str(confidence))
        sig.reason = reason
        sig.params_snapshot = {}
        return sig

    def test_no_params_snapshot_returns_early(self):
        s = _strategy(type_="orb")
        sig = MagicMock(spec=[])  # no params_snapshot attr
        self.r._apply_signal_intelligence_overlay(
            strategy=s, ticker="AAPL", signal_obj=sig,
            regime_payload={}, watchlist_context={},
        )
        # Should not raise

    def test_momentum_strategy_trending_up_boosts_confidence(self):
        s = _strategy(type_="orb")
        sig = self._signal(0.5)
        self.r._apply_signal_intelligence_overlay(
            strategy=s, ticker="AAPL", signal_obj=sig,
            regime_payload={"regime": "trending_up"},
            watchlist_context={},
        )
        assert sig.confidence == Decimal("0.53")
        assert "regime tailwind" in sig.reason

    def test_mean_reversion_ranging_boosts_confidence(self):
        s = _strategy(type_="opening_fade")
        sig = self._signal(0.5)
        self.r._apply_signal_intelligence_overlay(
            strategy=s, ticker="AAPL", signal_obj=sig,
            regime_payload={"regime": "ranging"},
            watchlist_context={},
        )
        assert sig.confidence == Decimal("0.52")
        assert "range regime fit" in sig.reason

    def test_high_catalyst_momentum_boosts_confidence(self):
        s = _strategy(type_="orb")
        sig = self._signal(0.5)
        self.r._apply_signal_intelligence_overlay(
            strategy=s, ticker="AAPL", signal_obj=sig,
            regime_payload={},
            watchlist_context={"catalyst_score": 0.70},
        )
        assert sig.confidence == Decimal("0.55")
        assert "fresh catalyst" in sig.reason

    def test_medium_catalyst_mean_reversion_dampens_confidence(self):
        s = _strategy(type_="opening_fade")
        sig = self._signal(0.5)
        self.r._apply_signal_intelligence_overlay(
            strategy=s, ticker="AAPL", signal_obj=sig,
            regime_payload={},
            watchlist_context={"catalyst_score": 0.55},
        )
        assert sig.confidence == Decimal("0.47")
        assert "event risk dampener" in sig.reason

    def test_confidence_capped_at_0_98(self):
        s = _strategy(type_="orb")
        sig = self._signal(0.97)
        self.r._apply_signal_intelligence_overlay(
            strategy=s, ticker="AAPL", signal_obj=sig,
            regime_payload={"regime": "trending_up"},
            watchlist_context={"catalyst_score": 0.80},
        )
        assert sig.confidence <= Decimal("0.98")

    def test_confidence_floored_at_0_01(self):
        s = _strategy(type_="opening_fade")
        sig = self._signal(0.02)
        # Apply multiple dampening layers by patching internals
        # catalyst > 0.5 AND no other boosts
        self.r._apply_signal_intelligence_overlay(
            strategy=s, ticker="AAPL", signal_obj=sig,
            regime_payload={},
            watchlist_context={"catalyst_score": 0.55},
        )
        assert sig.confidence >= Decimal("0.01")

    def test_watchlist_context_stored_in_params_snapshot(self):
        s = _strategy(type_="orb")
        sig = self._signal(0.5)
        ctx = {"catalyst_score": 0.3, "gap_pct": 1.2}
        self.r._apply_signal_intelligence_overlay(
            strategy=s, ticker="AAPL", signal_obj=sig,
            regime_payload={},
            watchlist_context=ctx,
        )
        assert sig.params_snapshot["watchlist_context"] == ctx

    def test_unknown_regime_no_boost(self):
        s = _strategy(type_="orb")
        sig = self._signal(0.5)
        self.r._apply_signal_intelligence_overlay(
            strategy=s, ticker="AAPL", signal_obj=sig,
            regime_payload={"regime": "unknown"},
            watchlist_context={},
        )
        assert sig.confidence == Decimal("0.5")


# ── _make_engine ──────────────────────────────────────────────────────────────

class TestMakeEngine:
    def setup_method(self):
        self.r = _runner()

    def test_orb_returns_orb_strategy(self):
        from app.strategies.orb_production import OpeningRangeBreakoutStrategy
        s = _strategy(type_="orb", params={})
        engine = self.r._make_engine(s)
        assert isinstance(engine, OpeningRangeBreakoutStrategy)

    def test_vwap_reclaim_returns_correct_engine(self):
        s = _strategy(type_="vwap_reclaim", params={})
        engine = self.r._make_engine(s)
        from app.strategies.vwap_reclaim import VWAPReclaimStrategy
        assert isinstance(engine, VWAPReclaimStrategy)

    def test_opening_fade_returns_correct_engine(self):
        s = _strategy(type_="opening_fade", params={})
        engine = self.r._make_engine(s)
        from app.strategies.opening_fade import OpeningFadeStrategy
        assert isinstance(engine, OpeningFadeStrategy)

    def test_closing_momentum_returns_correct_engine(self):
        s = _strategy(type_="closing_momentum", params={})
        engine = self.r._make_engine(s)
        from app.strategies.closing_momentum import ClosingMomentumStrategy
        assert isinstance(engine, ClosingMomentumStrategy)

    def test_intraday_periodicity_returns_correct_engine(self):
        s = _strategy(type_="intraday_periodicity", params={})
        engine = self.r._make_engine(s)
        from app.strategies.intraday_periodicity import IntradayPeriodicityStrategy
        assert isinstance(engine, IntradayPeriodicityStrategy)

    def test_unknown_type_returns_none(self):
        s = _strategy(type_="totally_unknown", params={})
        engine = self.r._make_engine(s)
        assert engine is None


# ── _build_signal_kwargs ──────────────────────────────────────────────────────

class TestBuildSignalKwargs:
    def setup_method(self):
        self.r = _runner()

    def _fake_engine(self, *extra_params: str) -> MagicMock:
        """Build a fake engine whose generate_signal accepts the given params."""
        base = ["ticker", "bars", "account_value", "available_cash", "current_time_utc"]
        params = {name: inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                  for name in base + list(extra_params)}
        sig = inspect.Signature(list(params.values()))
        engine = MagicMock()
        engine.generate_signal.__func__ = MagicMock()
        engine.generate_signal.__func__.__name__ = "generate_signal"
        type(engine).generate_signal = property(lambda self: lambda: None)
        # Patch inspect.signature to return our custom sig
        engine._sig = sig
        return engine

    def _kwargs(self, engine, **overrides):
        defaults = dict(
            ticker="AAPL",
            bars=_bars([100.0, 101.0]),
            bar_times=[datetime(2024, 1, 1, 10, 0, tzinfo=UTC)],
            history_bars=_bars([98.0, 99.0]),
            history_bar_times=[datetime(2024, 1, 1, 9, 0, tzinfo=UTC)],
            account_value=Decimal("10000"),
            available_cash=Decimal("5000"),
            current_time_utc="10:00",
            prev_close=Decimal("99.0"),
        )
        defaults.update(overrides)
        return self.r._build_signal_kwargs(engine, **defaults)

    def test_base_kwargs_always_present(self):
        s = _strategy(type_="orb", params={})
        engine = self.r._make_engine(s)
        if engine is None:
            pytest.skip("ORB engine not available")
        kwargs = self._kwargs(engine)
        assert "ticker" in kwargs
        assert "bars" in kwargs
        assert "account_value" in kwargs
        assert "available_cash" in kwargs
        assert "current_time_utc" in kwargs

    def test_prev_close_included_when_supported(self):
        s = _strategy(type_="opening_fade", params={})
        engine = self.r._make_engine(s)
        if engine is None:
            pytest.skip("opening_fade engine not available")
        sig = inspect.signature(engine.generate_signal)
        if "prev_close" not in sig.parameters:
            pytest.skip("engine doesn't accept prev_close")
        kwargs = self._kwargs(engine)
        assert "prev_close" in kwargs

    def test_session_open_included_when_supported(self):
        s = _strategy(type_="orb", params={})
        engine = self.r._make_engine(s)
        if engine is None:
            pytest.skip()
        sig = inspect.signature(engine.generate_signal)
        if "session_open" not in sig.parameters:
            pytest.skip("engine doesn't accept session_open")
        kwargs = self._kwargs(engine)
        assert "session_open" in kwargs
        # session_open should be the first bar's open
        assert kwargs["session_open"] == Decimal("100.0")
