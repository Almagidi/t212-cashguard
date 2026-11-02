"""Entry-only feed gating in the risk engine and strategy runner."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import AppSettings
from app.risk.engine import RiskEngine, RiskViolation
from app.services.feed_health import record_feed_health, reset_feed_health
from app.services.signal_allocator import SignalAllocator
from app.services.strategy_runner import StrategyRunner
from app.strategies.indicators import Bar

_REGIME = {"regime": "trending_up", "suppressed_strategies": []}


def _record(status: str, **kwargs) -> None:
    record_feed_health(
        provider="test", ticker="AAPL", status=status, detail="x",
        used_source="polygon", **kwargs,
    )


@pytest.mark.asyncio
async def test_risk_engine_blocks_entry_on_delayed_fallback(db):
    db.add(AppSettings(id=1))
    await db.commit()
    reset_feed_health()
    _record("fallback", fallback_used=True, is_delayed=True)

    with pytest.raises(RiskViolation) as exc:
        await RiskEngine(db).check_market_conditions(
            ticker="AAPL", strategy_type="orb", market_regime=_REGIME
        )
    assert exc.value.event_type == "feed_symbol_block"


@pytest.mark.asyncio
async def test_risk_engine_allows_entry_on_realtime_ok_feed(db):
    db.add(AppSettings(id=1))
    await db.commit()
    reset_feed_health()
    _record("ok", is_delayed=False)

    await RiskEngine(db).check_market_conditions(
        ticker="AAPL", strategy_type="orb", market_regime=_REGIME
    )




def _runner() -> StrategyRunner:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("DB must not be called"))
    return StrategyRunner(db)


def _context(runner: StrategyRunner):
    d = Decimal("100")
    bars = [Bar(open=d, high=d, low=d, close=d, volume=Decimal("1000")) for _ in range(4)]
    times = [datetime(2024, 1, 2, 14, 30 + i, tzinfo=UTC) for i in range(4)]

    async def fetch(ticker, **_kwargs):
        runner._entry_blocks[ticker] = "feed_unhealthy"
        return bars, times, bars, times, Decimal("99"), "14:33"

    return fetch


def _process_kwargs(runner: StrategyRunner, *, pos_map: dict):
    strategy = MagicMock()
    strategy.type = "orb"
    strategy.params = {"session_open_utc": "14:30"}
    strategy.allowed_tickers = ["AAPL"]
    strategy.name = "s"
    engine = MagicMock()
    engine.params = strategy.params
    engine.required_bars = 4
    engine.history_days = 5
    engine.max_history_bars = 180
    engine.DATA_PROVIDER_TYPE = "equity"
    risk = MagicMock()
    risk.check_market_conditions = AsyncMock()
    allocator = SignalAllocator()
    return {
        "ticker": "AAPL", "strategy": strategy, "engine": engine, "risk": risk,
        "broker": MagicMock(), "cash": Decimal("10000"), "total": Decimal("10000"),
        "n_open": 0, "pos_map": pos_map, "all_positions": [],
        "intelligence": {"regime": {"regime": "trending_up"}},
        "allocator": allocator, "allocation_state": allocator.new_state(),
    }


@pytest.mark.asyncio
async def test_runner_feed_block_stops_new_entries():
    runner = _runner()
    runner._fetch_market_context = _context(runner)
    kwargs = _process_kwargs(runner, pos_map={})

    result = await runner._process_ticker(**kwargs)

    assert result == (0, 0, 0)
    kwargs["engine"].generate_signal.assert_not_called()
    kwargs["risk"].check_market_conditions.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_feed_block_never_blocks_protective_exit_check():
    runner = _runner()
    runner._fetch_market_context = _context(runner)
    runner._check_exit = AsyncMock(return_value=5)
    kwargs = _process_kwargs(
        runner, pos_map={"AAPL": {"quantity": 5, "averagePrice": 100}}
    )

    result = await runner._process_ticker(**kwargs)

    runner._check_exit.assert_awaited_once()
    assert result == (1, 5, 0)


# ── unknown symbol rule in the risk engine ──────────────────────────────────

@pytest.mark.asyncio
async def test_risk_engine_blocks_unknown_symbol_with_real_provider(db, monkeypatch):
    from app.core.config import settings

    db.add(AppSettings(id=1))
    await db.commit()
    reset_feed_health()
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "validated")

    with pytest.raises(RiskViolation) as exc:
        await RiskEngine(db).check_market_conditions(
            ticker="AAPL", strategy_type="orb", market_regime=_REGIME
        )
    assert exc.value.event_type == "feed_symbol_block"


@pytest.mark.asyncio
async def test_risk_engine_blocks_unknown_symbol_in_live_mode_even_if_provider_says_mock(db, monkeypatch):
    from app.core.config import settings

    db.add(AppSettings(id=1))
    await db.commit()
    reset_feed_health()
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")

    with pytest.raises(RiskViolation) as exc:
        await RiskEngine(db).check_market_conditions(
            ticker="AAPL", strategy_type="orb", market_regime=_REGIME
        )
    assert exc.value.event_type == "feed_symbol_block"


@pytest.mark.asyncio
async def test_risk_engine_allows_unknown_symbol_in_mock_mode(db, monkeypatch):
    from app.core.config import settings

    db.add(AppSettings(id=1))
    await db.commit()
    reset_feed_health()
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")

    await RiskEngine(db).check_market_conditions(
        ticker="AAPL", strategy_type="orb", market_regime=_REGIME
    )


# ── runner: quote / health-check failure blocks ENTRY only ──────────────────

class _FakeMd:
    records_feed_health = True  # behaves like the validated provider

    def __init__(self, *, quote=object(), quote_error=None, safe=True, safe_error=None):
        self._quote, self._quote_error = quote, quote_error
        self._safe, self._safe_error = safe, safe_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def get_bars(self, *_a, **_k):
        return []

    async def get_quote(self, _ticker):
        if self._quote_error:
            raise self._quote_error
        return self._quote

    def is_trade_safe(self, _ticker, *, purpose="entry"):
        if self._safe_error:
            raise self._safe_error
        return self._safe


async def _fetch_with(monkeypatch, md):
    runner = _runner()
    monkeypatch.setattr("app.market_data.get_live_provider", lambda: md)
    await runner._fetch_market_context(
        "AAPL", session_open_utc="14:30", history_days=5, max_bars=10
    )
    return runner._entry_blocks.get("AAPL")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("md", "reason"),
    [
        (_FakeMd(quote_error=RuntimeError("boom")), "quote_unavailable"),
        (_FakeMd(quote=None), "quote_unavailable"),
        (_FakeMd(safe_error=RuntimeError("boom")), "feed_health_check_failed"),
        (_FakeMd(safe=False), "feed_unhealthy"),
        (_FakeMd(), None),
    ],
)
async def test_runner_records_entry_block_reason(monkeypatch, md, reason):
    assert await _fetch_with(monkeypatch, md) == reason


@pytest.mark.asyncio
async def test_quote_failure_blocks_entry_but_not_protective_exit():
    runner = _runner()

    base = _context(runner)

    async def fetch2(ticker, **kw):
        res = await base(ticker, **kw)
        runner._entry_blocks[ticker] = "quote_unavailable"
        return res

    runner._fetch_market_context = fetch2
    runner._check_exit = AsyncMock(return_value=5)
    exit_kwargs = _process_kwargs(runner, pos_map={"AAPL": {"quantity": 5, "averagePrice": 100}})
    assert await runner._process_ticker(**exit_kwargs) == (1, 5, 0)

    entry_kwargs = _process_kwargs(runner, pos_map={})
    assert await runner._process_ticker(**entry_kwargs) == (0, 0, 0)
    entry_kwargs["engine"].generate_signal.assert_not_called()


# ── unknown-symbol rule applies only when the provider records feed health ───

def _cfg(monkeypatch, *, mode, provider, alpaca=False, polygon=False):
    from app.core.config import settings

    monkeypatch.setattr(settings, "APP_MODE", mode)
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", provider)
    monkeypatch.setattr(settings, "ALPACA_API_KEY", "k" if alpaca else "")
    monkeypatch.setattr(settings, "ALPACA_API_SECRET", "s" if alpaca else "")
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "p" if polygon else "")


async def _check(db):
    db.add(AppSettings(id=1))
    await db.commit()
    reset_feed_health()
    await RiskEngine(db).check_market_conditions(
        ticker="AAPL", strategy_type="orb", market_regime=_REGIME
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "alpaca", "polygon"),
    [
        ("alpaca", True, False),
        ("polygon", False, True),
        ("auto", True, False),   # auto with a single vendor key
        ("auto", False, True),
        ("auto", False, False),  # auto with no keys falls back to mock
    ],
)
async def test_unknown_symbol_allowed_when_provider_records_no_health(
    db, monkeypatch, provider, alpaca, polygon
):
    _cfg(monkeypatch, mode="paper", provider=provider, alpaca=alpaca, polygon=polygon)
    await _check(db)  # must not raise


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["validated", "auto"])
async def test_unknown_symbol_blocked_when_validated_provider_has_no_record(
    db, monkeypatch, provider
):
    _cfg(monkeypatch, mode="paper", provider=provider, alpaca=True, polygon=True)
    with pytest.raises(RiskViolation) as exc:
        await _check(db)
    assert exc.value.event_type == "feed_symbol_block"


@pytest.mark.asyncio
async def test_validated_provider_with_record_passes(db, monkeypatch):
    _cfg(monkeypatch, mode="paper", provider="validated", alpaca=True, polygon=True)
    db.add(AppSettings(id=1))
    await db.commit()
    reset_feed_health()
    _record("ok", is_delayed=False)
    await RiskEngine(db).check_market_conditions(
        ticker="AAPL", strategy_type="orb", market_regime=_REGIME
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["mock", "alpaca", "auto"])
async def test_live_mode_is_never_exempt(db, monkeypatch, provider):
    # mock, a single vendor, or unresolved config in live: no record -> blocked.
    _cfg(monkeypatch, mode="live", provider=provider, alpaca=provider != "mock")
    with pytest.raises(RiskViolation) as exc:
        await _check(db)
    assert exc.value.event_type == "feed_symbol_block"


@pytest.mark.asyncio
async def test_runner_and_risk_engine_agree_for_raw_provider(monkeypatch):
    from app.market_data import provider_records_feed_health

    class _Raw(_FakeMd):
        records_feed_health = False

    _cfg(monkeypatch, mode="paper", provider="alpaca", alpaca=True)
    assert provider_records_feed_health() is False  # risk-engine view
    # runner view: raw provider is not asked is_trade_safe (even though it unsafe)
    assert await _fetch_with(monkeypatch, _Raw(safe=False)) is None


@pytest.mark.asyncio
async def test_runner_blocks_entries_in_live_when_provider_cannot_record_health(monkeypatch):
    class _Raw(_FakeMd):
        records_feed_health = False

    _cfg(monkeypatch, mode="live", provider="alpaca", alpaca=True)
    assert await _fetch_with(monkeypatch, _Raw()) == "feed_health_unavailable"


@pytest.mark.asyncio
async def test_risk_engine_unknown_symbol_rule_skips_kraken_but_not_equity(db, monkeypatch):
    from app.core.config import settings

    db.add(AppSettings(id=1))
    await db.commit()
    reset_feed_health()
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "validated")

    # Kraken tickers never get feed-health records: must not be blocked.
    await RiskEngine(db).check_market_conditions(
        ticker="XBTUSD", strategy_type="kraken_trend_follow", market_regime=_REGIME,
        data_provider_type="kraken",
    )
    # Equity (e.g. Trading 212) ticker behaviour is unchanged.
    with pytest.raises(RiskViolation) as exc:
        await RiskEngine(db).check_market_conditions(
            ticker="AAPL", strategy_type="orb", market_regime=_REGIME,
        )
    assert exc.value.event_type == "feed_symbol_block"
