# mypy: disable-error-code="no-untyped-def"
"""Protective exits: halts block entries, never the exit of an already-held long position."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Alert, AppSettings, AuditLog, Order, RiskProfile, Signal, Strategy
from app.execution.engine import ExecutionEngine
from app.services import position_monitor
from app.services.position_monitor import PositionMonitor
from app.services.safety_policy import (
    ProtectiveExitAuthorization,
    SafetyPolicyViolation,
    require_order_submission_allowed,
    verify_protective_exit,
)
from app.strategies.indicators import Bar


class _Broker:
    environment = None
    account_scope = None

    def __init__(self, positions: list[dict[str, Any]], total: int = 10_000) -> None:
        self.positions = positions
        self.total = total

    async def __aenter__(self) -> _Broker:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_positions(self) -> list[dict[str, Any]]:
        return self.positions

    async def get_account_summary(self) -> dict[str, Any]:
        return {"total": self.total}


class _StopHit:
    def __init__(self, _params: Any = None) -> None:
        pass

    def check_exit_conditions(self, _ticker, _state, price, _bars):
        return SimpleNamespace(
            signal_type="stop",
            suggested_quantity=Decimal("-5"),
            stop_price=price,
            take_profit_price=price,
            confidence=Decimal("1"),
            reason="stop hit",
        )


@pytest.fixture(autouse=True)
def _mock_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(position_monitor, "OpeningRangeBreakoutStrategy", _StopHit)
    monkeypatch.setattr(position_monitor, "alert_kill_switch_activated", AsyncMock())


async def _settings(db, *, kill: bool = False, auto: bool = True) -> None:
    db.add(
        AppSettings(
            id=1,
            auto_trading_enabled=auto,
            kill_switch_active=kill,
            live_trading_unlocked=False,
        )
    )
    await db.commit()


async def _held_long_with_entry_signal(db) -> None:
    strategy = Strategy(
        id=uuid.uuid4(),
        name="ORB",
        type="orb",
        params={},
        venue="t212",
        session_end="16:00",
        eod_flatten=False,
        is_enabled=True,
        is_live=True,
    )
    db.add(strategy)
    await db.flush()
    db.add(
        Signal(
            id=uuid.uuid4(),
            strategy_id=strategy.id,
            ticker="AAPL",
            side="buy",
            signal_type="entry",
            status="executed",
            stop_price=Decimal("95"),
            take_profit_price=Decimal("110"),
        )
    )
    await db.commit()


def _monitor(db, broker: _Broker) -> PositionMonitor:
    monitor = PositionMonitor(db)
    bars = [
        Bar(
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000"),
        )
        for _ in range(20)
    ]
    monitor._get_broker = AsyncMock(return_value=broker)  # type: ignore[method-assign]
    monitor._get_market_data = AsyncMock(return_value=(bars, Decimal("94")))  # type: ignore[method-assign]
    return monitor


def _position(quantity: str = "5", ppl: int = 0) -> dict[str, Any]:
    return {
        "ticker": "AAPL",
        "quantity": quantity,
        "maxSell": quantity,
        "averagePrice": "100",
        "ppl": ppl,
    }


async def _sells(db) -> list[Order]:
    rows = await db.execute(select(Order).where(Order.side == "sell"))
    return list(rows.scalars().all())


async def test_kill_switch_on_stop_loss_sell_is_still_submitted(db) -> None:
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)

    result = await _monitor(db, _Broker([_position()])).run()

    assert result["entries_blocked"] == "kill_switch_active"
    assert result["exits_submitted"] == 1
    assert result["stops_hit"] == 1
    sells = await _sells(db)
    assert len(sells) == 1
    assert sells[0].status == "filled"
    assert sells[0].quantity == Decimal("5")


async def test_auto_trading_paused_exits_still_run(db) -> None:
    await _settings(db, auto=False)
    await _held_long_with_entry_signal(db)

    result = await _monitor(db, _Broker([_position()])).run()

    assert result["entries_blocked"] == "auto_trading_disabled"
    assert result["exits_submitted"] == 1
    assert [o.status for o in await _sells(db)] == ["filled"]


async def test_daily_loss_breach_activates_kill_switch_and_exits_still_run(db) -> None:
    await _settings(db)
    await _held_long_with_entry_signal(db)
    db.add(
        RiskProfile(
            id=uuid.uuid4(),
            name="Default",
            max_risk_per_trade_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("3.0"),
            max_open_positions=5,
            max_position_size_pct=Decimal("10.0"),
            max_trades_per_day=20,
            stop_after_consecutive_losses=3,
            symbol_cooldown_seconds=300,
            force_flat_eod=True,
            is_default=True,
        )
    )
    await db.commit()

    result = await _monitor(db, _Broker([_position(ppl=-4000)])).run()

    assert result["halted"] == "daily_loss_breach"
    assert result["exits_submitted"] == 1
    app_settings = (await db.execute(select(AppSettings))).scalar_one()
    await db.refresh(app_settings)
    assert app_settings.kill_switch_active is True
    assert [o.status for o in await _sells(db)] == ["filled"]


async def test_buy_is_still_refused_under_kill_switch(db) -> None:
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([_position()]))
    buy = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("1"), is_dry_run=True
    )

    with pytest.raises(SafetyPolicyViolation) as plain:
        await engine.submit_order(buy)
    assert plain.value.decision_code == "kill_switch_block"

    other = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("2"), is_dry_run=True
    )
    with pytest.raises(SafetyPolicyViolation) as protective:
        await engine.submit_protective_exit(other)
    assert protective.value.decision_code == "protective_exit_refused"


async def test_plain_sell_is_still_refused_under_kill_switch(db) -> None:
    """Only the verified protective-exit path is exempt, not sells in general."""
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([_position()]))
    sell = await engine.create_order_intent(
        ticker="AAPL", side="sell", order_type="market", quantity=Decimal("1"), is_dry_run=True
    )

    with pytest.raises(SafetyPolicyViolation) as plain:
        await engine.submit_order(sell)

    assert plain.value.decision_code == "kill_switch_block"


@pytest.mark.parametrize(
    ("positions", "quantity"),
    [
        ([_position("5")], "6"),  # exceeds the held quantity
        ([], "1"),  # nothing held: would open a short
        ([{"ticker": "MSFT", "quantity": "9", "maxSell": "9"}], "1"),  # other ticker only
        ([{"ticker": "AAPL", "quantity": "-3", "maxSell": "3"}], "1"),  # a short is not held
        ([{"ticker": "AAPL", "quantity": "5", "maxSell": "2"}], "3"),  # beyond maxSell
    ],
)
async def test_protective_exit_beyond_held_long_or_opening_short_is_refused(
    db, positions, quantity
) -> None:
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker(positions))
    sell = await engine.create_order_intent(
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal(quantity),
        is_dry_run=True,
    )

    with pytest.raises(SafetyPolicyViolation) as refused:
        await engine.submit_protective_exit(sell)

    assert refused.value.decision_code == "protective_exit_refused"
    assert sell.status == "pending_intent"


async def test_sells_already_in_flight_count_against_the_held_quantity(db) -> None:
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([_position("5")]))
    db.add(
        Order(
            id=uuid.uuid4(),
            client_order_key="in-flight-sell",
            ticker="AAPL",
            side="sell",
            order_type="market",
            quantity=Decimal("4"),
            filled_quantity=Decimal("0"),
            status="submitted",
            is_dry_run=True,
        )
    )
    await db.flush()
    sell = await engine.create_order_intent(
        ticker="AAPL", side="sell", order_type="market", quantity=Decimal("2"), is_dry_run=True
    )

    with pytest.raises(SafetyPolicyViolation):
        await engine.submit_protective_exit(sell)  # 4 in flight + 2 > 5 held


async def test_policy_rechecks_the_authorization_against_the_order(db) -> None:
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([]))
    buy = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("1"), is_dry_run=True
    )
    forged = ProtectiveExitAuthorization(ticker="AAPL", held_quantity=Decimal("100"))
    with pytest.raises(SafetyPolicyViolation) as exc:
        await require_order_submission_allowed(
            db, order=buy, broker_environment=None, protective_exit=forged
        )
    assert exc.value.decision_code == "protective_exit_refused"

    sell = await engine.create_order_intent(
        ticker="AAPL", side="sell", order_type="market", quantity=Decimal("2"), is_dry_run=True
    )
    for wrong in (
        ProtectiveExitAuthorization(ticker="MSFT", held_quantity=Decimal("100")),
        ProtectiveExitAuthorization(ticker="AAPL", held_quantity=Decimal("1")),
        ProtectiveExitAuthorization(ticker="AAPL", held_quantity=Decimal("100")),
    ):
        with pytest.raises(SafetyPolicyViolation):
            await require_order_submission_allowed(
                db, order=sell, broker_environment=None, protective_exit=wrong
            )


async def test_duplicate_broker_position_rows_fail_closed_instead_of_summing(db) -> None:
    """Two rows for one ticker could be one holding listed twice: never add them up."""
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([_position("5"), _position("5")]))
    sell = await engine.create_order_intent(
        ticker="AAPL", side="sell", order_type="market", quantity=Decimal("10"), is_dry_run=True
    )

    with pytest.raises(SafetyPolicyViolation) as refused:
        await engine.submit_protective_exit(sell)

    assert refused.value.decision_code == "protective_exit_refused"
    assert "duplicate position rows" in refused.value.reason
    assert sell.status == "pending_intent"


async def test_duplicate_rows_refuse_even_a_small_sell(db) -> None:
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([_position("5"), _position("2")]))
    sell = await engine.create_order_intent(
        ticker="AAPL", side="sell", order_type="market", quantity=Decimal("1"), is_dry_run=True
    )

    with pytest.raises(SafetyPolicyViolation):
        await engine.submit_protective_exit(sell)


async def test_max_sell_already_net_of_pending_sells_is_treated_conservatively(db) -> None:
    """maxSell may already exclude a pending sell; the engine subtracts it again and refuses.

    Documented fail-closed behaviour: a refused stop is retried and alerted, a short is
    not recoverable. 10 held, broker maxSell 6 (4 pending at the broker), our own record of
    that 4 in flight: a sell of 6 is refused although it would in fact be safe.
    """
    await _settings(db, kill=True)
    position = {**_position("10"), "maxSell": "6"}
    engine = ExecutionEngine(db, _Broker([position]))
    db.add(
        Order(
            id=uuid.uuid4(),
            client_order_key="pending-at-broker",
            ticker="AAPL",
            side="sell",
            order_type="market",
            quantity=Decimal("4"),
            filled_quantity=Decimal("0"),
            status="submitted",
            is_dry_run=True,
        )
    )
    await db.flush()
    sell = await engine.create_order_intent(
        ticker="AAPL", side="sell", order_type="market", quantity=Decimal("6"), is_dry_run=True
    )

    with pytest.raises(SafetyPolicyViolation):
        await engine.submit_protective_exit(sell)

    smaller = await engine.create_order_intent(
        ticker="AAPL", side="sell", order_type="market", quantity=Decimal("2"), is_dry_run=True
    )
    assert (await engine.submit_protective_exit(smaller)).status == "filled"


# -- Concurrency: exits for one ticker are serialized ------------------------------------


class _DemoBroker:
    """A demo-environment broker that holds 10 AAPL and leaves each sell working."""

    environment = "demo"
    account_scope = None

    def __init__(self, held: str = "10", *, delay: float = 0.02) -> None:
        self.held = held
        self.delay = delay
        self.sent: list[Decimal] = []
        self.inside = 0
        self.max_inside = 0

    async def __aenter__(self) -> _DemoBroker:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_account_summary(self) -> dict[str, Any]:
        return {"total": 10_000}

    async def get_positions(self) -> list[dict[str, Any]]:
        self.inside += 1
        self.max_inside = max(self.max_inside, self.inside)
        await asyncio.sleep(self.delay)  # a real position read is network I/O
        self.inside -= 1
        return [_position(self.held)]

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        self.sent.append(abs(Decimal(str(quantity))))
        return {"id": f"B-{len(self.sent)}", "status": "WORKING", "filledQuantity": 0}


async def _demo_sell(engine: ExecutionEngine, quantity: str, identity: str) -> Order:
    return await engine.create_order_intent(
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal(quantity),
        is_dry_run=False,
        stable_operation_identity=identity,
    )


async def test_two_concurrent_exits_for_one_ticker_cannot_both_sell_the_held_quantity(
    db, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    broker = _DemoBroker("10")
    engine = ExecutionEngine(db, broker)
    first = await _demo_sell(engine, "10", "test:concurrent:a")
    second = await _demo_sell(engine, "9", "test:concurrent:b")

    results = await asyncio.gather(
        engine.submit_protective_exit(first),
        engine.submit_protective_exit(second),
        return_exceptions=True,
    )

    refused = [r for r in results if isinstance(r, SafetyPolicyViolation)]
    assert len(refused) == 1, results
    assert len(broker.sent) == 1  # never 10 + 9 against 10 held
    assert broker.max_inside == 1  # the position read and the dispatch commit never overlap


async def test_monitor_does_not_issue_a_second_exit_while_a_sell_is_active(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    db.add(
        Order(
            id=uuid.uuid4(),
            client_order_key="earlier-exit",
            ticker="AAPL",
            side="sell",
            order_type="market",
            quantity=Decimal("5"),
            filled_quantity=Decimal("0"),
            status="submitted",
            is_dry_run=False,
        )
    )
    await db.commit()
    broker = _DemoBroker("10")  # room for another 5: only the active-sell rule stops it

    result = await _monitor(db, broker).run()

    assert result["exits_submitted"] == 0
    assert result["errors"] == []
    assert broker.sent == []
    assert len(await _sells(db)) == 1


# -- Authorization cannot be forged or replayed --------------------------------------------


async def test_forged_authorization_with_huge_held_quantity_is_refused(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    broker = _DemoBroker("1")
    engine = ExecutionEngine(db, broker)
    sell = await _demo_sell(engine, "500", "test:forged")
    forged = ProtectiveExitAuthorization(ticker="AAPL", held_quantity=Decimal("1000000"))

    with pytest.raises(SafetyPolicyViolation) as refused:
        await engine.submit_order(sell, protective_exit=forged)

    assert refused.value.decision_code == "protective_exit_refused"
    assert broker.sent == []
    assert sell.status == "pending_intent"


async def test_engine_minted_authorization_is_bound_to_order_quantity_and_time(
    db, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _DemoBroker("10"))
    sell = await _demo_sell(engine, "4", "test:bound:a")
    other = await _demo_sell(engine, "4", "test:bound:b")
    minted = await engine._verified_protective_exit(sell)
    from app.services.safety_policy import PROTECTIVE_EXIT_AUTHORIZATION_TTL

    verify_protective_exit(sell, minted)  # the real thing verifies

    with pytest.raises(SafetyPolicyViolation):  # another order of the same size and ticker
        verify_protective_exit(other, minted)
    with pytest.raises(SafetyPolicyViolation):  # tampered copy of a real authorization
        verify_protective_exit(sell, dataclasses.replace(minted, held_quantity=Decimal("999")))
    sell.quantity = Decimal("5")
    with pytest.raises(SafetyPolicyViolation):  # order changed after the verification
        verify_protective_exit(sell, minted)
    sell.quantity = Decimal("4")
    later = minted.issued_at + PROTECTIVE_EXIT_AUTHORIZATION_TTL + timedelta(seconds=1)
    with pytest.raises(SafetyPolicyViolation) as expired:  # stale
        verify_protective_exit(sell, minted, now=later)
    assert "expired" in expired.value.reason


# -- Exit idempotency: one identity per exit, a new attempt only after a failed one -----------


class _ScriptedBroker(_DemoBroker):
    """Answers each sell with the next scripted broker status and reflects fills in the position."""

    def __init__(self, held: str, script: list[str]) -> None:
        super().__init__(held, delay=0)
        self.script = list(script)

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        qty = abs(Decimal(str(quantity)))
        self.sent.append(qty)
        status = self.script.pop(0)
        filled = qty if status == "FILLED" else Decimal("0")
        if status == "FILLED":
            self.held = str(Decimal(self.held) - qty)
        return {
            "id": f"B-{len(self.sent)}",
            "status": status,
            "filledQuantity": float(filled),
            "filledPrice": 94.0,
        }


def _exit_strategy(signal_type: str, quantity: str):
    class _Exit:
        def __init__(self, _params: Any = None) -> None:
            pass

        def check_exit_conditions(self, _ticker, _state, price, _bars):
            return SimpleNamespace(
                signal_type=signal_type,
                suggested_quantity=-Decimal(quantity),
                stop_price=price,
                take_profit_price=price,
                confidence=Decimal("1"),
                reason=signal_type,
            )

    return _Exit


async def _run_exit(db, monkeypatch, broker, signal_type: str, quantity: str) -> dict[str, Any]:
    monkeypatch.setattr(
        position_monitor, "OpeningRangeBreakoutStrategy", _exit_strategy(signal_type, quantity)
    )
    return await _monitor(db, broker).run()


async def test_partial_exit_then_stop_create_two_orders(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    broker = _ScriptedBroker("10", ["FILLED", "FILLED"])

    first = await _run_exit(db, monkeypatch, broker, "partial_exit", "4")
    second = await _run_exit(db, monkeypatch, broker, "stop", "6")

    assert first["exits_submitted"] == 1
    assert second["exits_submitted"] == 1
    assert broker.sent == [Decimal("4"), Decimal("6")]
    sells = await _sells(db)
    assert len(sells) == 2
    assert len({o.client_order_key for o in sells}) == 2
    assert {o.status for o in sells} == {"filled"}


async def test_rejected_exit_is_retried_with_a_new_attempt(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    broker = _ScriptedBroker("10", ["REJECTED", "FILLED"])

    first = await _run_exit(db, monkeypatch, broker, "stop", "10")
    assert first["exits_submitted"] == 1  # the order was recorded (as rejected)
    assert [o.status for o in await _sells(db)] == ["rejected"]

    second = await _run_exit(db, monkeypatch, broker, "stop", "10")

    assert second["exits_submitted"] == 1
    assert broker.sent == [Decimal("10"), Decimal("10")]
    statuses = sorted(o.status for o in await _sells(db))
    assert statuses == ["filled", "rejected"]


async def test_identical_exit_still_in_flight_is_deduplicated(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    broker = _ScriptedBroker("10", ["WORKING", "FILLED"])

    await _run_exit(db, monkeypatch, broker, "stop", "10")
    await _run_exit(db, monkeypatch, broker, "stop", "10")

    assert broker.sent == [Decimal("10")]
    sells = await _sells(db)
    assert [o.status for o in sells] == ["accepted"]
    # And at the engine: the same exit asked for again is the same identity (attempt 0).
    engine = ExecutionEngine(db, broker)
    entry = (await db.execute(select(Signal).where(Signal.side == "buy"))).scalar_one()
    identity = await engine.next_exit_identity(
        ticker="AAPL", entry_signal_id=entry.id, exit_type="stop"
    )
    assert identity == f"exit:{entry.id}:stop:0"


# -- A refused or blocked exit raises one alert per (ticker, reason) per window ------------


async def _audit_rows(db, action: str) -> list[AuditLog]:
    rows = await db.execute(select(AuditLog).where(AuditLog.action == action))
    return list(rows.scalars().all())


async def _alerts(db, alert_type: str) -> list[Alert]:
    rows = await db.execute(select(Alert).where(Alert.alert_type == alert_type))
    return list(rows.scalars().all())


async def test_repeated_refusal_for_one_ticker_and_reason_is_audited_and_alerted_once(
    db,
) -> None:
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([_position("5"), _position("5")]))  # duplicate rows

    for index in range(3):
        sell = await engine.create_order_intent(
            ticker="AAPL",
            side="sell",
            order_type="market",
            quantity=Decimal("1"),
            is_dry_run=True,
            stable_operation_identity=f"test:spam:{index}",
        )
        with pytest.raises(SafetyPolicyViolation):
            await engine.submit_protective_exit(sell)

    assert len(await _audit_rows(db, "protective_exit_refused")) == 1
    assert len(await _alerts(db, "protective_exit_refused")) == 1


async def test_a_different_reason_or_ticker_is_reported_separately(db) -> None:
    await _settings(db, kill=True)
    duplicate = ExecutionEngine(db, _Broker([_position("5"), _position("5")]))
    nothing_held = ExecutionEngine(db, _Broker([]))
    for engine, ticker, identity in (
        (duplicate, "AAPL", "test:sep:1"),
        (nothing_held, "AAPL", "test:sep:2"),
        (nothing_held, "MSFT", "test:sep:3"),
    ):
        sell = await engine.create_order_intent(
            ticker=ticker,
            side="sell",
            order_type="market",
            quantity=Decimal("1"),
            is_dry_run=True,
            stable_operation_identity=identity,
        )
        with pytest.raises(SafetyPolicyViolation):
            await engine.submit_protective_exit(sell)

    assert len(await _alerts(db, "protective_exit_refused")) == 3


async def test_refusal_is_reported_again_after_the_window(db) -> None:
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([]))

    async def refuse(identity: str) -> None:
        sell = await engine.create_order_intent(
            ticker="AAPL",
            side="sell",
            order_type="market",
            quantity=Decimal("1"),
            is_dry_run=True,
            stable_operation_identity=identity,
        )
        with pytest.raises(SafetyPolicyViolation):
            await engine.submit_protective_exit(sell)

    await refuse("test:window:1")
    for row in await _audit_rows(db, "protective_exit_refused"):
        row.occurred_at = datetime.now(UTC) - timedelta(minutes=16)
    await db.flush()
    await refuse("test:window:2")

    assert len(await _alerts(db, "protective_exit_refused")) == 2


async def test_submission_unknown_sell_blocks_later_exits_with_one_alert(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    db.add(
        Order(
            id=uuid.uuid4(),
            client_order_key="unknown-outcome",
            ticker="AAPL",
            side="sell",
            order_type="market",
            quantity=Decimal("5"),
            filled_quantity=Decimal("0"),
            status="submission_unknown",
            is_dry_run=False,
        )
    )
    await db.commit()
    broker = _DemoBroker("10")

    summaries = [await _monitor(db, broker).run() for _ in range(3)]

    assert broker.sent == []
    assert all(s["exits_unconfirmed"] == 1 for s in summaries)
    assert len(await _sells(db)) == 1
    alerts = await _alerts(db, "protective_exit_blocked")
    assert len(alerts) == 1
    assert "unknown" in alerts[0].message.lower()


async def test_exit_that_failed_every_attempt_is_not_retried_and_is_alerted_once(
    db, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    entry = (await db.execute(select(Signal).where(Signal.side == "buy"))).scalar_one()
    engine = ExecutionEngine(db, _DemoBroker("10"))
    for attempt in range(ExecutionEngine.MAX_EXIT_ATTEMPTS):
        await engine.create_order_intent(
            ticker="AAPL",
            side="sell",
            order_type="market",
            quantity=Decimal("5"),
            is_dry_run=False,
            stable_operation_identity=f"exit:{entry.id}:stop:{attempt}",
        )
    for order in await _sells(db):
        order.status = "rejected"
    await db.commit()
    broker = _DemoBroker("10")

    for _ in range(3):
        await _run_exit(db, monkeypatch, broker, "stop", "5")

    assert broker.sent == []
    assert len(await _sells(db)) == ExecutionEngine.MAX_EXIT_ATTEMPTS
    assert len(await _alerts(db, "protective_exit_blocked")) == 1


# -- In-flight sells are scoped to this environment and account, and stale ones escalate ----


class _ScopedBroker(_DemoBroker):
    account_scope = "acct-A"


def _leftover_sell(**fields: Any) -> Order:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "client_order_key": f"leftover-{uuid.uuid4()}",
        "ticker": "AAPL",
        "side": "sell",
        "order_type": "market",
        "quantity": Decimal("5"),
        "filled_quantity": Decimal("0"),
        "status": "submission_unknown",
        "is_dry_run": False,
    }
    values.update(fields)
    return Order(**values)


@pytest.mark.parametrize(
    "fields",
    [
        {"execution_environment": "live"},  # another environment (this run is demo)
        {"execution_environment": "demo", "broker_account_scope": "acct-B"},  # another account
    ],
)
async def test_sell_from_another_environment_or_account_does_not_block_a_stop(
    db, monkeypatch, fields
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    db.add(_leftover_sell(**fields))
    await db.commit()
    broker = _ScopedBroker("10")

    result = await _monitor(db, broker).run()

    assert result["exits_submitted"] == 1
    assert broker.sent == [Decimal("5")]


async def test_engine_does_not_count_other_scope_sells_against_the_held_quantity(
    db, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    db.add(_leftover_sell(status="submitted", quantity=Decimal("4"), execution_environment="live"))
    db.add(
        _leftover_sell(
            status="submitted",
            quantity=Decimal("4"),
            execution_environment="demo",
            broker_account_scope="acct-B",
        )
    )
    await db.commit()
    broker = _ScopedBroker("5")
    sell = await _demo_sell(ExecutionEngine(db, broker), "5", "test:scope:a")

    assert (
        await ExecutionEngine(db, broker).submit_protective_exit(sell)
    ).status != "pending_intent"
    assert broker.sent == [Decimal("5")]


async def test_same_scope_active_sell_still_blocks_and_counts(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    db.add(
        _leftover_sell(
            status="submitted", execution_environment="demo", broker_account_scope="acct-A"
        )
    )
    await db.commit()
    broker = _ScopedBroker("10")

    result = await _monitor(db, broker).run()

    assert result["exits_submitted"] == 0
    assert broker.sent == []


@pytest.mark.parametrize("status", ["submission_unknown", "submitted", "accepted"])
async def test_stuck_sell_escalates_once_and_stops_blocking_the_monitor(
    db, monkeypatch, status
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "PROTECTIVE_SELL_STALE_AFTER_SECONDS", 600)
    reconcile = AsyncMock()
    monkeypatch.setattr(ExecutionEngine, "reconcile_unknown_submission", reconcile)
    monkeypatch.setattr(ExecutionEngine, "reconcile_order", reconcile)
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    db.add(_leftover_sell(status=status, created_at=datetime.now(UTC) - timedelta(minutes=11)))
    await db.commit()
    broker = _ScriptedBroker("10", ["WORKING", "WORKING", "WORKING"])

    first = await _run_exit(db, monkeypatch, broker, "stop", "5")
    await _run_exit(db, monkeypatch, broker, "stop", "5")

    assert first["exits_submitted"] == 1
    assert broker.sent[0] == Decimal("5")  # protected despite the stuck sell
    stale_alerts = await _alerts(db, "protective_sell_stale")
    assert len(stale_alerts) == 1
    assert stale_alerts[0].severity == "critical"
    assert reconcile.await_count == 1  # once per window, not per cycle


async def test_stuck_sell_never_allows_selling_more_than_the_held_quantity(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    old = datetime.now(UTC) - timedelta(minutes=30)
    db.add(_leftover_sell(status="submitted", quantity=Decimal("3"), created_at=old))
    # A fresh sell is truly active and still counts against the broker-verified 10 held.
    db.add(_leftover_sell(status="accepted", quantity=Decimal("8"), execution_environment="demo"))
    await db.commit()
    broker = _DemoBroker("10")

    result = await _monitor(db, broker).run()

    assert result["exits_submitted"] == 0  # the fresh sell still blocks the monitor
    assert broker.sent == []
    engine = ExecutionEngine(db, broker)
    sell = await _demo_sell(engine, "5", "test:stale:oversell")
    with pytest.raises(SafetyPolicyViolation):
        await engine.submit_protective_exit(sell)  # 8 active + 5 > 10; the stale 3 is ignored


# -- A stale pending intent is re-sized to the verified quantity ---------------------------


async def test_stale_pending_exit_intent_is_resized_to_the_shrunken_position(
    db, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    await _held_long_with_entry_signal(db)
    entry = (await db.execute(select(Signal).where(Signal.side == "buy"))).scalar_one()
    stale = await ExecutionEngine(db, _DemoBroker("10")).create_order_intent(
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal("10"),  # the position has since shrunk to 5
        is_dry_run=False,
        stable_operation_identity=f"exit:{entry.id}:stop:0",
    )
    await db.commit()
    broker = _DemoBroker("5")

    result = await _run_exit(db, monkeypatch, broker, "stop", "5")

    assert result["exits_submitted"] == 1
    assert result["errors"] == []
    assert broker.sent == [Decimal("5")]
    sells = await _sells(db)
    assert [o.id for o in sells] == [stale.id]  # same identity, no second order
    assert sells[0].quantity == Decimal("5")
    assert sells[0].status != "pending_intent"
    assert await _audit_rows(db, "protective_exit_refused") == []


async def test_only_a_pending_sell_intent_can_be_resized(db) -> None:
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([_position("5")]))
    sent = await engine.create_order_intent(
        ticker="AAPL", side="sell", order_type="market", quantity=Decimal("5"), is_dry_run=True
    )
    await engine.submit_protective_exit(sent)
    with pytest.raises(ValueError):
        await engine.resize_pending_exit(sent, Decimal("2"))


# -- The exit lock is released on every path; alerts run outside it ------------------------


async def _refusable_real_sell(db, monkeypatch, identity: str):
    """A non-dry-run (lock-taking) sell the broker position cannot cover."""
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    broker = _DemoBroker("0")  # nothing held (the position row quantity is "0")
    engine = ExecutionEngine(db, broker)
    return engine, broker, await _demo_sell(engine, "5", identity)


async def test_refusal_ends_the_transaction_before_alerting(db, monkeypatch) -> None:
    engine, _broker, sell = await _refusable_real_sell(db, monkeypatch, "test:lock:refuse")
    events: list[str] = []
    real_commit = db.commit

    async def commit() -> None:
        events.append("commit")
        await real_commit()

    async def alert(*_args: Any, **_kwargs: Any) -> None:
        events.append("alert")

    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr("app.execution.engine.send_protective_exit_blocked_alert", alert)

    with pytest.raises(SafetyPolicyViolation):
        await engine.submit_protective_exit(sell)

    assert events == ["commit", "alert"]  # lock released first, alert (network I/O) after
    assert len(await _audit_rows(db, "protective_exit_refused")) == 1


async def test_alert_failure_never_masks_the_refusal(db, monkeypatch) -> None:
    engine, _broker, sell = await _refusable_real_sell(db, monkeypatch, "test:lock:alertfail")

    async def broken_alert(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("telegram is down")

    monkeypatch.setattr("app.execution.engine.send_protective_exit_blocked_alert", broken_alert)

    with pytest.raises(SafetyPolicyViolation) as refused:
        await engine.submit_protective_exit(sell)

    assert refused.value.decision_code == "protective_exit_refused"


async def test_unexpected_failure_while_submitting_still_releases_the_lock(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    broker = _DemoBroker("10")
    engine = ExecutionEngine(db, broker)
    sell = await _demo_sell(engine, "5", "test:lock:boom")
    commits: list[int] = []
    real_commit = db.commit

    async def commit() -> None:
        commits.append(1)
        await real_commit()

    async def boom(*_args: Any, **_kwargs: Any) -> Order:
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(engine, "submit_order", boom)

    with pytest.raises(RuntimeError):
        await engine.submit_protective_exit(sell)

    assert commits  # the transaction (and so the advisory lock) did not stay open


async def test_lock_is_refused_when_the_dialect_cannot_be_determined(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    await _settings(db, kill=True)
    broker = _DemoBroker("10")
    engine = ExecutionEngine(db, broker)
    sell = await _demo_sell(engine, "5", "test:lock:nodialect")

    def no_bind(*_args: Any, **_kwargs: Any):
        raise RuntimeError("no bind")

    monkeypatch.setattr(db, "get_bind", no_bind)

    with pytest.raises(SafetyPolicyViolation) as refused:
        await engine.submit_protective_exit(sell)

    assert refused.value.decision_code == "protective_exit_refused"
    assert broker.sent == []  # nothing is sent unserialized


async def test_dry_run_exit_does_not_need_the_database_lock(db, monkeypatch) -> None:
    await _settings(db, kill=True)
    engine = ExecutionEngine(db, _Broker([_position("5")]))
    sell = await engine.create_order_intent(
        ticker="AAPL", side="sell", order_type="market", quantity=Decimal("5"), is_dry_run=True
    )

    def no_bind(*_args: Any, **_kwargs: Any):
        raise RuntimeError("no bind")

    monkeypatch.setattr(db, "get_bind", no_bind)

    assert (await engine.submit_protective_exit(sell)).status == "filled"
