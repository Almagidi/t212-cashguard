"""The kill switch and the auto-trading switch are obeyed as they are now, not as they were
when a long-running session first read them.

The settings row is changed behind the session's back with plain SQL, which is what a
change committed by another process looks like to a session that has already loaded it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from app.db.models import AppSettings
from app.risk.engine import RiskEngine, RiskViolation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _trading_allowed(db: AsyncSession) -> AppSettings:
    row = AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False)
    db.add(row)
    await db.flush()
    return row


async def _change_behind_the_session(db: AsyncSession, assignment: str) -> None:
    await db.execute(text(f"UPDATE app_settings SET {assignment} WHERE id = 1"))


async def _checks(risk: RiskEngine) -> None:
    await risk.run_all_checks(
        ticker="AAPL",
        side="buy",
        quantity=Decimal("1"),
        estimated_price=Decimal("10"),
        available_cash=Decimal("1000"),
        account_value=Decimal("10000"),
        realized_pnl_today=Decimal("0"),
        current_open_positions=0,
    )


@pytest.mark.asyncio
async def test_a_kill_switch_set_after_the_session_read_its_settings_stops_the_next_check(
    db: AsyncSession,
) -> None:
    loaded = await _trading_allowed(db)
    risk = RiskEngine(db)
    await risk.check_kill_switch()

    await _change_behind_the_session(db, "kill_switch_active = 1")

    assert loaded.kill_switch_active is False, "the session already knows; nothing is proved"
    with pytest.raises(RiskViolation) as blocked:
        await risk.check_kill_switch()
    assert blocked.value.event_type == "kill_switch_block"


@pytest.mark.asyncio
async def test_the_full_set_of_checks_obeys_a_kill_switch_set_mid_run(db: AsyncSession) -> None:
    # A run keeps hold of the settings it read at its start, as the strategy runner does;
    # that is what keeps the session's copy alive and stale.
    held_by_the_run = await _trading_allowed(db)
    risk = RiskEngine(db)
    await _checks(risk)

    await _change_behind_the_session(db, "kill_switch_active = 1")

    assert held_by_the_run.kill_switch_active is False
    with pytest.raises(RiskViolation) as blocked:
        await _checks(risk)
    assert blocked.value.event_type == "kill_switch_block"


@pytest.mark.asyncio
async def test_the_checks_an_entry_goes_through_obey_a_pause_set_mid_run(db: AsyncSession) -> None:
    """The strategy runner calls the full set of checks for each entry without asking for
    the auto-trading check to be skipped; this is that call."""
    held_by_the_run = await _trading_allowed(db)
    risk = RiskEngine(db)
    await _checks(risk)

    await _change_behind_the_session(db, "auto_trading_enabled = 0")

    assert held_by_the_run.auto_trading_enabled is True
    with pytest.raises(RiskViolation) as blocked:
        await _checks(risk)
    assert blocked.value.event_type == "auto_trading_disabled"


@pytest.mark.asyncio
async def test_auto_trading_switched_off_after_the_session_read_its_settings_stops_the_next_check(
    db: AsyncSession,
) -> None:
    loaded = await _trading_allowed(db)
    risk = RiskEngine(db)
    await risk.check_auto_trading_enabled()

    await _change_behind_the_session(db, "auto_trading_enabled = 0")

    assert loaded.auto_trading_enabled is True, "the session already knows; nothing is proved"
    with pytest.raises(RiskViolation) as blocked:
        await risk.check_auto_trading_enabled()
    assert blocked.value.event_type == "auto_trading_disabled"


@pytest.mark.asyncio
async def test_a_kill_switch_this_session_has_set_but_not_saved_still_stops_the_check(
    db: AsyncSession,
) -> None:
    """The saved row still says "not halted"; the session's own activation must not be lost."""
    loaded = await _trading_allowed(db)
    risk = RiskEngine(db)
    loaded.kill_switch_active = True

    # Application sessions do not save pending changes before a query; nor may this one.
    with db.no_autoflush:
        assert await risk._saved_setting(AppSettings.kill_switch_active) is False
        with pytest.raises(RiskViolation):
            await risk.check_kill_switch()


@pytest.mark.asyncio
async def test_auto_trading_this_session_has_switched_off_but_not_saved_still_stops_the_check(
    db: AsyncSession,
) -> None:
    loaded = await _trading_allowed(db)
    risk = RiskEngine(db)
    loaded.auto_trading_enabled = False

    # Application sessions do not save pending changes before a query; nor may this one.
    with db.no_autoflush:
        assert await risk._saved_setting(AppSettings.auto_trading_enabled) is True
        with pytest.raises(RiskViolation):
            await risk.check_auto_trading_enabled()


@pytest.mark.asyncio
@pytest.mark.parametrize("check", ["check_kill_switch", "check_auto_trading_enabled"])
async def test_a_switch_whose_saved_value_cannot_be_found_stops_the_check(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    """A missing row or an empty column is not permission to trade."""
    await _trading_allowed(db)
    risk = RiskEngine(db)

    async def nothing_saved(_column: object) -> None:
        return None

    monkeypatch.setattr(risk, "_saved_setting", nothing_saved)

    with pytest.raises(RiskViolation):
        await getattr(risk, check)()


@pytest.mark.asyncio
async def test_with_both_switches_permitting_trading_the_checks_pass(db: AsyncSession) -> None:
    await _trading_allowed(db)
    risk = RiskEngine(db)

    await risk.check_kill_switch()
    await risk.check_auto_trading_enabled()
    await _checks(risk)


@pytest.mark.asyncio
async def test_a_kill_switch_released_elsewhere_is_not_trusted_over_this_sessions_own_copy(
    db: AsyncSession,
) -> None:
    """Fail closed: either source saying "halted" is enough."""
    row = AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=True)
    db.add(row)
    await db.flush()
    risk = RiskEngine(db)

    await _change_behind_the_session(db, "kill_switch_active = 0")

    with pytest.raises(RiskViolation):
        await risk.check_kill_switch()
