"""
Venue isolation test suite — 12 tests covering kill-switch gating,
degraded mode, auto-trading gate, order venue tagging, and seed correctness.

T1 -T6, T12: mock VenueConfigRepository; never hit the DB.
T7 -T9:     use the in-memory SQLite `db` fixture.
T10-T11:    structural / static tests.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api.schemas import _KRAKEN_STRATEGY_TYPES, StrategyCreate, StrategyPresetCreate
from app.execution.engine import ExecutionEngine
from app.services.strategy_runner import StrategyRunner

# ── shared helpers ────────────────────────────────────────────────────────────


def _venue_cfg(
    *,
    kill_switch_active: bool = False,
    degraded_mode_active: bool = False,
    auto_trading_enabled: bool = True,
) -> MagicMock:
    cfg = MagicMock()
    cfg.kill_switch_active = kill_switch_active
    cfg.degraded_mode_active = degraded_mode_active
    cfg.auto_trading_enabled = auto_trading_enabled
    return cfg


def _strategy(
    *,
    type_: str = "kraken_trend_follow",
    is_live: bool = False,
    venue: str = "kraken",
) -> MagicMock:
    s = MagicMock()
    s.type = type_
    s.params = {}
    s.name = "test_strategy"
    s.is_live = is_live
    s.venue = venue
    s.allowed_tickers = ["BTC/USD"]
    return s


def _runner() -> StrategyRunner:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("DB must not be called in venue gate tests"))
    return StrategyRunner(db)


async def _call_run_strategy(runner: StrategyRunner, strategy: MagicMock) -> tuple[int, int, int]:
    return await runner._run_strategy(
        strategy=strategy,
        broker=MagicMock(),
        cash=Decimal("10000"),
        total=Decimal("10000"),
        n_open=0,
        pos_map={},
        all_positions=[],
        intelligence={},
        allocator=MagicMock(),
        allocation_state=MagicMock(),
    )


# ── T1 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kraken_kill_switch_blocks_kraken_strategy():
    """T1: venue_cfg.kill_switch_active=True for 'kraken' → _run_strategy returns (0,0,0)."""
    runner = _runner()
    strategy = _strategy(type_="kraken_trend_follow", is_live=False, venue="kraken")

    with patch("app.services.strategy_runner.VenueConfigRepository") as MockRepo:
        MockRepo.return_value.get_by_venue = AsyncMock(
            return_value=_venue_cfg(kill_switch_active=True)
        )
        result = await _call_run_strategy(runner, strategy)

    assert result == (0, 0, 0)
    MockRepo.return_value.get_by_venue.assert_called_once_with("kraken")


# ── T2 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kraken_kill_switch_does_not_block_t212_strategy():
    """T2: Kraken kill switch active, but T212 strategy runs — venue gate queries 't212' only."""
    runner = _runner()
    strategy = _strategy(type_="orb", is_live=False, venue="t212")
    runner._get_tickers = MagicMock(return_value=[])

    with patch("app.services.strategy_runner.VenueConfigRepository") as MockRepo:
        MockRepo.return_value.get_by_venue = AsyncMock(
            return_value=_venue_cfg(kill_switch_active=False)
        )
        result = await _call_run_strategy(runner, strategy)

    assert result == (0, 0, 0)
    # Gate queried "t212" (not "kraken"), and returned non-blocking config.
    MockRepo.return_value.get_by_venue.assert_called_once_with("t212")
    # _get_tickers was reached — the venue gate did NOT early-return.
    runner._get_tickers.assert_called_once()


# ── T3 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t212_kill_switch_does_not_block_kraken_strategy():
    """T3: T212 kill switch active, but Kraken strategy runs — venue gate queries 'kraken' only."""
    runner = _runner()
    strategy = _strategy(type_="kraken_trend_follow", is_live=False, venue="kraken")
    runner._get_tickers = MagicMock(return_value=[])

    with patch("app.services.strategy_runner.VenueConfigRepository") as MockRepo:
        MockRepo.return_value.get_by_venue = AsyncMock(
            return_value=_venue_cfg(kill_switch_active=False)
        )
        result = await _call_run_strategy(runner, strategy)

    assert result == (0, 0, 0)
    MockRepo.return_value.get_by_venue.assert_called_once_with("kraken")
    runner._get_tickers.assert_called_once()


# ── T4 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_degraded_mode_blocks_target_venue_only():
    """T4: degraded_mode_active=True blocks only the targeted venue; other venue unaffected."""
    # Kraken is degraded — blocks.
    runner_k = _runner()
    with patch("app.services.strategy_runner.VenueConfigRepository") as MockRepo:
        MockRepo.return_value.get_by_venue = AsyncMock(
            return_value=_venue_cfg(degraded_mode_active=True)
        )
        result_k = await _call_run_strategy(
            runner_k, _strategy(type_="kraken_trend_follow", is_live=False, venue="kraken")
        )
    assert result_k == (0, 0, 0)
    MockRepo.return_value.get_by_venue.assert_called_once_with("kraken")

    # T212 is not degraded — passes venue gate and reaches _get_tickers.
    runner_t = _runner()
    runner_t._get_tickers = MagicMock(return_value=[])
    with patch("app.services.strategy_runner.VenueConfigRepository") as MockRepo2:
        MockRepo2.return_value.get_by_venue = AsyncMock(
            return_value=_venue_cfg(degraded_mode_active=False)
        )
        await _call_run_strategy(runner_t, _strategy(type_="orb", is_live=False, venue="t212"))
    MockRepo2.return_value.get_by_venue.assert_called_once_with("t212")
    runner_t._get_tickers.assert_called_once()


# ── T5 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_trading_off_blocks_live_strategy_only():
    """T5: auto_trading_enabled=False blocks is_live=True but not dry-run strategies."""
    # Live strategy — blocked.
    runner_live = _runner()
    with patch("app.services.strategy_runner.VenueConfigRepository") as MockRepo:
        MockRepo.return_value.get_by_venue = AsyncMock(
            return_value=_venue_cfg(auto_trading_enabled=False)
        )
        result_live = await _call_run_strategy(
            runner_live, _strategy(type_="kraken_trend_follow", is_live=True, venue="kraken")
        )
    assert result_live == (0, 0, 0)

    # Dry-run strategy — not blocked by auto_trading_enabled=False.
    runner_dry = _runner()
    runner_dry._get_tickers = MagicMock(return_value=[])
    with patch("app.services.strategy_runner.VenueConfigRepository") as MockRepo2:
        MockRepo2.return_value.get_by_venue = AsyncMock(
            return_value=_venue_cfg(auto_trading_enabled=False)
        )
        await _call_run_strategy(
            runner_dry, _strategy(type_="kraken_trend_follow", is_live=False, venue="kraken")
        )
    runner_dry._get_tickers.assert_called_once()


# ── T6 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_venue_config_blocks_safely():
    """T6: None from get_by_venue → strategy is blocked (fail-closed); _get_tickers never reached."""
    runner = _runner()
    runner._get_tickers = MagicMock(return_value=[])

    with patch("app.services.strategy_runner.VenueConfigRepository") as MockRepo:
        MockRepo.return_value.get_by_venue = AsyncMock(return_value=None)
        result = await _call_run_strategy(
            runner, _strategy(type_="kraken_trend_follow", is_live=False, venue="kraken")
        )

    assert result == (0, 0, 0)
    runner._get_tickers.assert_not_called()


# ── T7 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_venue_set_from_strategy_venue_t212(db):
    """T7: create_order_intent(venue='t212') → order.venue == 't212'."""
    engine = ExecutionEngine(db, MagicMock())
    order = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        venue="t212",
    )
    assert order.venue == "t212"


# ── T8 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_venue_set_from_strategy_venue_kraken(db):
    """T8: create_order_intent(venue='kraken') → order.venue == 'kraken'."""
    engine = ExecutionEngine(db, MagicMock())
    order = await engine.create_order_intent(
        ticker="BTC/USD",
        side="buy",
        order_type="market",
        quantity=Decimal("0.01"),
        venue="kraken",
    )
    assert order.venue == "kraken"


# ── T9 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_venue_default_is_t212(db):
    """T9: create_order_intent() without venue= defaults to 't212' (backward-compat)."""
    engine = ExecutionEngine(db, MagicMock())
    order = await engine.create_order_intent(
        ticker="MSFT",
        side="buy",
        order_type="market",
        quantity=Decimal("2"),
    )
    assert order.venue == "t212"


# ── T10 ───────────────────────────────────────────────────────────────────────


def test_seed_orb_strategy_has_explicit_t212_venue():
    """T10: seed.py Strategy() call for 'ORB Demo Strategy' has explicit venue='t212'."""
    import ast

    seed_path = pathlib.Path(__file__).parents[2] / "app" / "db" / "seed.py"
    source = seed_path.read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "Strategy"):
            continue
        name_kws = [k for k in node.keywords if k.arg == "name"]
        if not name_kws:
            continue
        name_val = name_kws[0].value
        if not (isinstance(name_val, ast.Constant) and "ORB" in str(name_val.value)):
            continue
        # Found the ORB Demo Strategy() constructor — assert explicit venue="t212".
        venue_kws = [k for k in node.keywords if k.arg == "venue"]
        assert venue_kws, "ORB Demo Strategy() constructor must have explicit venue= keyword"
        assert isinstance(venue_kws[0].value, ast.Constant)
        assert venue_kws[0].value.value == "t212"
        return

    pytest.fail("Did not find the ORB Demo Strategy() constructor call in seed.py")


# ── T11 ───────────────────────────────────────────────────────────────────────


def test_kraken_strategy_venue_attribute_propagated_to_engine():
    """T11: _make_engine for each kraken type returns engine with VENUE == 'kraken'."""
    runner = StrategyRunner(MagicMock())

    for type_ in ("kraken_breakout_retest", "kraken_trend_follow"):
        strategy = MagicMock()
        strategy.type = type_
        strategy.params = {}
        engine = runner._make_engine(strategy)
        assert engine is not None, f"_make_engine returned None for type={type_!r}"
        assert (
            engine.VENUE == "kraken"
        ), f"engine for {type_!r} must have VENUE='kraken', got {engine.VENUE!r}"


# ── T12 ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_venue_gating_uses_engine_venue_not_strategy_db_field():
    """T12: gating key comes from engine.VENUE, not strategy.venue.

    strategy.venue is set to 't212' but strategy.type is 'kraken_trend_follow'
    (engine.VENUE = 'kraken'). The gate must query for 'kraken'.
    """
    runner = _runner()
    strategy = MagicMock()
    strategy.type = "kraken_trend_follow"
    strategy.params = {}
    strategy.name = "mismatch_test"
    strategy.is_live = False
    strategy.venue = "t212"  # intentionally wrong DB value

    with patch("app.services.strategy_runner.VenueConfigRepository") as MockRepo:
        MockRepo.return_value.get_by_venue = AsyncMock(
            return_value=_venue_cfg(kill_switch_active=True)
        )
        result = await _call_run_strategy(runner, strategy)

    # Kill switch fired for 'kraken' (gating used engine.VENUE, not strategy.venue)
    assert result == (0, 0, 0)
    MockRepo.return_value.get_by_venue.assert_called_once_with("kraken")


# ── Venue hardening — schema validation tests (H1-H5) ────────────────────────


def test_invalid_venue_value_rejected():
    """H1: StrategyCreate rejects any venue value outside {'t212', 'kraken'}."""
    with pytest.raises(ValidationError) as exc_info:
        StrategyCreate(name="bad", type="orb", venue="foo")
    errors = exc_info.value.errors()
    assert any(
        e["loc"] == ("venue",) for e in errors
    ), f"Expected a venue field error, got: {errors}"


def test_kraken_type_with_t212_venue_rejected():
    """H2: Kraken strategy types must not be created with venue='t212'."""
    for kraken_type in ("kraken_breakout_retest", "kraken_trend_follow"):
        with pytest.raises(ValidationError) as exc_info:
            StrategyCreate(name="bad", type=kraken_type, venue="t212")
        errors = exc_info.value.errors()
        assert any(
            "venue" in str(e).lower() or "kraken" in str(e).lower() for e in errors
        ), f"Expected venue/type mismatch error for {kraken_type!r}, got: {errors}"


def test_t212_type_with_kraken_venue_rejected():
    """H3: T212 strategy types must not be created with venue='kraken'."""
    for t212_type in (
        "orb",
        "opening_fade",
        "vwap_reclaim",
        "closing_momentum",
        "intraday_periodicity",
    ):
        with pytest.raises(ValidationError) as exc_info:
            StrategyCreate(name="bad", type=t212_type, venue="kraken")
        errors = exc_info.value.errors()
        assert any(
            "venue" in str(e).lower() or "kraken" in str(e).lower() for e in errors
        ), f"Expected venue/type mismatch error for {t212_type!r}, got: {errors}"


def test_valid_kraken_type_kraken_venue_accepted():
    """H4: Valid Kraken type + venue='kraken' passes StrategyCreate validation."""
    for kraken_type in ("kraken_breakout_retest", "kraken_trend_follow"):
        sc = StrategyCreate(name="ok", type=kraken_type, venue="kraken")
        assert sc.venue == "kraken"
        assert sc.type == kraken_type


def test_valid_t212_type_t212_venue_accepted():
    """H5: Valid T212 type + venue='t212' passes StrategyCreate validation."""
    for t212_type in (
        "orb",
        "opening_fade",
        "vwap_reclaim",
        "closing_momentum",
        "intraday_periodicity",
    ):
        sc = StrategyCreate(name="ok", type=t212_type, venue="t212")
        assert sc.venue == "t212"
        assert sc.type == t212_type


# ── Preset-path structural tests (P1-P3) ─────────────────────────────────────


def test_preset_create_schema_has_no_venue_field():
    """P1: StrategyPresetCreate has no venue field — callers cannot inject a venue value.

    This proves the preset API path rejects any attempt to supply an invalid venue
    value: there is no venue parameter to supply in the first place.
    """
    sc = StrategyPresetCreate()
    assert not hasattr(sc, "venue"), (
        "StrategyPresetCreate must not expose a venue field — "
        "venue injection through the preset path must be impossible"
    )
    fields = set(StrategyPresetCreate.model_fields.keys())
    assert "venue" not in fields


def test_preset_key_types_are_all_t212_venue():
    """P2: Every StrategyPresetKey value is a T212 type — none are Kraken types.

    Proves: the preset path cannot produce a Kraken-type strategy row,
    so the constraint 'Kraken types → venue=kraken' can never be violated
    through the preset creation route.
    """
    from app.services.strategy_presets import PRESET_CONFIGS

    for key in PRESET_CONFIGS:
        assert key not in _KRAKEN_STRATEGY_TYPES, (
            f"Preset key {key!r} is a Kraken strategy type but appears in PRESET_CONFIGS. "
            "Kraken presets are not supported; remove it or add explicit venue='kraken' handling."
        )


def test_preset_route_sets_explicit_t212_venue():
    """P3: The create_strategy_from_preset route sets venue='t212' explicitly on Strategy(...).

    Guards against a future ORM-default change silently breaking preset venue correctness.
    """
    import ast

    route_path = (
        pathlib.Path(__file__).parents[2] / "app" / "api" / "v1" / "routes" / "strategies.py"
    )
    source = route_path.read_text()
    tree = ast.parse(source)

    found_preset_strategy_call = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "Strategy"):
            continue
        # Identify the preset Strategy() call by presence of type=preset.key
        type_kws = [k for k in node.keywords if k.arg == "type"]
        if not type_kws:
            continue
        type_val = type_kws[0].value
        # preset route uses: type=preset.key (an Attribute node)
        if not isinstance(type_val, ast.Attribute):
            continue
        if type_val.attr != "key":
            continue
        # This is the preset Strategy() call — check for explicit venue="t212"
        found_preset_strategy_call = True
        venue_kws = [k for k in node.keywords if k.arg == "venue"]
        assert (
            venue_kws
        ), "create_strategy_from_preset Strategy() constructor must set venue='t212' explicitly"
        assert isinstance(venue_kws[0].value, ast.Constant)
        assert (
            venue_kws[0].value.value == "t212"
        ), f"Expected venue='t212', got {venue_kws[0].value.value!r}"
        break

    assert (
        found_preset_strategy_call
    ), "Did not find the preset Strategy() constructor call in strategies.py"
