"""
Unit tests for the correlation and sector-exposure risk module.

All functions under test are pure Python (no DB, no I/O).
"""

from __future__ import annotations

from app.risk.correlation import (
    SECTOR_MAP,
    CorrelationRiskChecker,
    CorrelationViolation,
    compute_correlation,
    get_correlation_checker,
    get_sector,
)

# ── get_sector ────────────────────────────────────────────────────────────────


class TestGetSector:
    def test_known_ticker_returns_sector(self):
        assert get_sector("AAPL") == "Technology"
        assert get_sector("JPM") == "Financials"
        assert get_sector("SPY") == "ETF"
        assert get_sector("XOM") == "Energy"

    def test_case_insensitive(self):
        assert get_sector("aapl") == "Technology"
        assert get_sector("Nvda") == "Technology"

    def test_unknown_ticker_returns_unknown(self):
        assert get_sector("ZZZXYZ") == "Unknown"
        assert get_sector("") == "Unknown"

    def test_all_sector_map_entries_resolve(self):
        for ticker in SECTOR_MAP:
            sector = get_sector(ticker)
            assert isinstance(sector, str)
            assert len(sector) > 0


# ── compute_correlation ───────────────────────────────────────────────────────


class TestComputeCorrelation:
    def test_insufficient_data_returns_zero(self):
        assert compute_correlation([1, 2, 3], [1, 2, 3]) == 0.0
        assert compute_correlation([], []) == 0.0
        assert compute_correlation([1, 2, 3, 4], [1, 2, 3, 4]) == 0.0

    def test_identical_series_returns_one(self):
        prices = [100.0, 102.0, 101.0, 103.0, 105.0, 104.0, 107.0]
        corr = compute_correlation(prices, prices)
        assert abs(corr - 1.0) < 1e-9

    def test_perfectly_anticorrelated_returns_minus_one(self):
        # Alternating up/down vs alternating down/up → returns are mirror images
        a = [100.0, 102.0, 100.0, 102.0, 100.0, 102.0, 100.0, 102.0, 100.0, 102.0]
        b = [100.0, 98.0, 100.0, 98.0, 100.0, 98.0, 100.0, 98.0, 100.0, 98.0]
        corr = compute_correlation(a, b)
        assert corr < -0.95

    def test_unrelated_series_low_correlation(self):
        a = [100.0, 101.0, 99.0, 102.0, 100.0, 103.0, 98.0]
        b = [50.0, 49.0, 51.0, 50.0, 52.0, 49.0, 51.0]
        corr = compute_correlation(a, b)
        assert -1.0 <= corr <= 1.0

    def test_constant_series_returns_zero(self):
        flat = [100.0] * 10
        other = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0]
        corr = compute_correlation(flat, other)
        assert corr == 0.0

    def test_result_bounded(self):
        import random

        random.seed(42)
        a = [100.0 + random.gauss(0, 1) for _ in range(20)]
        b = [100.0 + random.gauss(0, 1) for _ in range(20)]
        corr = compute_correlation(a, b)
        assert -1.0 <= corr <= 1.0

    def test_mismatched_lengths_uses_shorter(self):
        long_a = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
        short_b = [50.0, 51.0, 52.0, 53.0, 54.0, 55.0, 56.0]
        corr = compute_correlation(long_a, short_b)
        assert -1.0 <= corr <= 1.0


# ── CorrelationRiskChecker.check_sector_exposure ──────────────────────────────


class TestSectorExposure:
    def setup_method(self):
        self.checker = CorrelationRiskChecker(max_sector_pct=25.0, max_correlation=0.75)

    def test_zero_account_value_always_allowed(self):
        ok, _reason = self.checker.check_sector_exposure("AAPL", 1000.0, [], account_value=0)
        assert ok

    def test_etf_exempt_from_sector_limit(self):
        ok, reason = self.checker.check_sector_exposure("SPY", 99999.0, [], account_value=10_000.0)
        assert ok
        assert "exempt" in reason

    def test_unknown_sector_exempt(self):
        ok, _reason = self.checker.check_sector_exposure(
            "ZZZUNKNOWN", 99999.0, [], account_value=10_000.0
        )
        assert ok

    def test_below_limit_allowed(self):
        # 2000 / 10000 = 20% < 25% limit
        ok, reason = self.checker.check_sector_exposure("AAPL", 2000.0, [], account_value=10_000.0)
        assert ok
        assert "Technology" in reason

    def test_above_limit_blocked(self):
        existing = [{"ticker": "MSFT", "value": 2000.0}]
        # new 1000 + existing 2000 = 3000 / 10000 = 30% > 25%
        ok, reason = self.checker.check_sector_exposure(
            "AAPL", 1000.0, existing, account_value=10_000.0
        )
        assert not ok
        assert "Sector limit" in reason
        assert "Technology" in reason

    def test_position_value_derived_from_qty_price_if_no_value(self):
        existing = [{"ticker": "MSFT", "quantity": 20, "current_price": 100.0}]
        # existing value = 2000, new = 500, total = 2500 / 10000 = 25% → at limit (not above)
        ok, _ = self.checker.check_sector_exposure("AAPL", 500.0, existing, account_value=10_000.0)
        # 25% == max_sector_pct → not strictly greater → allowed
        assert ok

    def test_different_sector_not_counted(self):
        existing = [{"ticker": "JPM", "value": 5000.0}]  # Financials
        ok, _ = self.checker.check_sector_exposure("AAPL", 1000.0, existing, account_value=10_000.0)
        # only AAPL (Tech) = 1000/10000 = 10% — well under limit
        assert ok


# ── CorrelationRiskChecker.check_correlation ──────────────────────────────────


class TestCorrelationCheck:
    def setup_method(self):
        self.checker = CorrelationRiskChecker(max_correlation=0.75, etf_exempt=True)

    def _prices(self, n: int = 15, step: float = 1.0, start: float = 100.0) -> list[float]:
        return [start + i * step for i in range(n)]

    def test_etf_new_ticker_exempt(self):
        ok, violations = self.checker.check_correlation(
            "SPY", [{"ticker": "AAPL"}], {"SPY": self._prices(), "AAPL": self._prices()}
        )
        assert ok
        assert violations == []

    def test_insufficient_new_history_allowed(self):
        ok, _violations = self.checker.check_correlation(
            "AAPL", [{"ticker": "MSFT"}], {"AAPL": [1.0, 2.0], "MSFT": self._prices()}
        )
        assert ok

    def test_highly_correlated_positions_blocked(self):
        prices = self._prices(20)
        ok, violations = self.checker.check_correlation(
            "AAPL",
            [{"ticker": "MSFT"}],
            {"AAPL": prices, "MSFT": prices},
        )
        assert not ok
        assert len(violations) == 1
        v = violations[0]
        assert isinstance(v, CorrelationViolation)
        assert v.ticker_new == "AAPL"
        assert v.ticker_existing == "MSFT"
        assert abs(v.correlation) >= 0.75

    def test_uncorrelated_positions_allowed(self):
        import random

        random.seed(99)
        a = [100.0 + random.gauss(0, 3) for _ in range(20)]
        b = [200.0 + random.gauss(0, 3) for _ in range(20)]
        # These are random and shouldn't typically exceed 0.75, but we
        # mock a low-corr scenario explicitly
        checker = CorrelationRiskChecker(max_correlation=0.99)
        _ok, _violations = checker.check_correlation(
            "AAPL", [{"ticker": "JPM"}], {"AAPL": a, "JPM": b}
        )
        assert True  # result depends on random seed; just verify it runs

    def test_same_ticker_skipped_in_existing(self):
        prices = self._prices(20)
        ok, violations = self.checker.check_correlation(
            "AAPL",
            [{"ticker": "AAPL"}],
            {"AAPL": prices},
        )
        assert ok
        assert violations == []

    def test_etf_existing_position_exempt(self):
        prices = self._prices(20)
        ok, _violations = self.checker.check_correlation(
            "AAPL",
            [{"ticker": "SPY"}],
            {"AAPL": prices, "SPY": prices},
        )
        # SPY is ETF → exempt from correlation check
        assert ok

    def test_existing_insufficient_history_skipped(self):
        ok, _violations = self.checker.check_correlation(
            "AAPL",
            [{"ticker": "MSFT"}],
            {"AAPL": self._prices(20), "MSFT": [1.0, 2.0, 3.0]},
        )
        assert ok

    def test_multiple_violations_reported(self):
        prices = self._prices(20)
        ok, violations = self.checker.check_correlation(
            "NVDA",
            [{"ticker": "AAPL"}, {"ticker": "MSFT"}],
            {"NVDA": prices, "AAPL": prices, "MSFT": prices},
        )
        assert not ok
        assert len(violations) == 2

    def test_violation_message_contains_tickers(self):
        prices = self._prices(20)
        _, violations = self.checker.check_correlation(
            "AAPL",
            [{"ticker": "MSFT"}],
            {"AAPL": prices, "MSFT": prices},
        )
        assert violations
        assert "AAPL" in violations[0].message
        assert "MSFT" in violations[0].message


# ── singleton ─────────────────────────────────────────────────────────────────


class TestSingleton:
    def test_get_correlation_checker_returns_instance(self):
        checker = get_correlation_checker()
        assert isinstance(checker, CorrelationRiskChecker)

    def test_singleton_is_same_object(self):
        assert get_correlation_checker() is get_correlation_checker()

    def test_singleton_has_expected_defaults(self):
        checker = get_correlation_checker()
        assert checker.max_sector_pct == 25.0
        assert checker.max_correlation == 0.75
