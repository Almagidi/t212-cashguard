"""The scheduled reconciler reads an order only at the account that order belongs to."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import update
from structlog.testing import capture_logs

from app.db.models import BrokerConnection
from tests.integration.scheduled_reconciliation_cases import (
    T0,
    Brokers,
    add_connection,
    add_order,
    connection_is_active,
    filled,
    install_borrowed_session,
    run_task,
    scope_of,
    stored,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
def brokers(monkeypatch: pytest.MonkeyPatch, db: AsyncSession) -> Brokers:
    registry = Brokers()
    install_borrowed_session(monkeypatch, registry, db)
    return registry


async def test_each_account_is_asked_only_about_its_own_orders(
    db: AsyncSession, brokers: Brokers
) -> None:
    first = await add_connection(db, account_id="ACC-1", created_at=T0)
    second = await add_connection(db, account_id="ACC-2", created_at=T0 + timedelta(minutes=1))
    own_first = await add_order(db, account_scope=scope_of(first), broker_order_id="first-1")
    own_second = await add_order(db, account_scope=scope_of(second), broker_order_id="second-1")

    summary = await run_task()

    assert brokers.for_user(first.user_id).order_reads == ["first-1"]
    assert brokers.for_user(second.user_id).order_reads == ["second-1"]
    assert summary["reconciled"] == 2
    assert (await stored(db, own_first.id)).last_reconciled_at is not None
    assert (await stored(db, own_second.id)).last_reconciled_at is not None


async def test_an_order_from_another_account_is_never_read_and_is_reported(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    mine = await add_order(db, account_scope=scope_of(connection), broker_order_id="mine")
    foreign = await add_order(
        db, account_scope="trading212:demo:account:someone-else", broker_order_id="foreign"
    )

    summary = await run_task()

    assert brokers.all_order_reads == ["mine"]
    assert summary["out_of_scope"] == {"other_account": 1}
    assert (await stored(db, mine.id)).last_reconciled_at is not None
    untouched = await stored(db, foreign.id)
    assert untouched.last_reconciled_at is None
    assert untouched.status == "accepted"


async def test_a_foreign_order_the_broker_would_call_filled_stays_as_it_is(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    foreign = await add_order(
        db, account_scope="trading212:demo:account:someone-else", broker_order_id="foreign"
    )
    brokers.for_user(connection.user_id).responses["foreign"] = filled()

    await run_task()

    assert (await stored(db, foreign.id)).status == "accepted"
    assert brokers.all_order_reads == []


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("another environment", {"environment": "live"}),
        ("another venue", {"venue": "kraken"}),
        ("a dry run", {"is_dry_run": True}),
        ("already filled", {"status": "filled"}),
        ("already cancelled", {"status": "cancelled"}),
        ("already rejected", {"status": "rejected"}),
        ("already failed", {"status": "error"}),
    ],
)
async def test_orders_outside_this_reconcilers_remit_are_never_read(
    db: AsyncSession, brokers: Brokers, label: str, overrides: dict[str, object]
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    await add_order(db, account_scope=scope_of(connection), broker_order_id="in-scope")
    await add_order(db, account_scope=scope_of(connection), broker_order_id="outside", **overrides)

    await run_task()

    assert brokers.all_order_reads == ["in-scope"], label


async def test_an_order_with_no_recorded_environment_is_not_read_and_is_reported(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    await add_order(db, account_scope=scope_of(connection), broker_order_id="mine")
    await add_order(
        db, account_scope=scope_of(connection), broker_order_id="no-env", environment=None
    )

    summary = await run_task()

    assert brokers.all_order_reads == ["mine"]
    assert summary["out_of_scope"] == {"no_environment": 1}


async def test_an_order_with_no_recorded_environment_is_reported_even_when_nothing_else_waits(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    await add_order(
        db, account_scope=scope_of(connection), broker_order_id="no-env", environment=None
    )

    summary = await run_task()

    assert summary == {"reconciled": 0, "out_of_scope": {"no_environment": 1}}
    assert brokers.requests == [], "nothing to read, so no provider should be built"


async def test_a_disabled_connection_reads_nothing_and_its_orders_are_left_alone(
    db: AsyncSession, brokers: Brokers
) -> None:
    active = await add_connection(db, account_id="ACC-1")
    disabled = await add_connection(db, account_id="ACC-2", is_active=False)
    await add_order(db, account_scope=scope_of(active), broker_order_id="active-1")
    await add_order(db, account_scope=scope_of(disabled), broker_order_id="disabled-1")

    summary = await run_task()

    assert brokers.for_user(active.user_id).order_reads == ["active-1"]
    assert disabled.user_id not in brokers.by_user
    assert summary["out_of_scope"] == {"other_account": 1}


async def test_a_connection_for_another_environment_is_not_used(
    db: AsyncSession, brokers: Brokers
) -> None:
    demo = await add_connection(db, environment="demo", account_id="ACC-1")
    live = await add_connection(db, environment="live", account_id="ACC-1")
    await add_order(db, account_scope=scope_of(demo), broker_order_id="demo-1")
    await add_order(db, account_scope=scope_of(live), broker_order_id="live-1", environment="live")

    await run_task()

    assert brokers.for_user(demo.user_id).order_reads == ["demo-1"]
    assert live.user_id not in brokers.by_user


async def test_a_sole_connection_also_reads_orders_recorded_without_an_account(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    await add_order(db, account_scope=scope_of(connection), broker_order_id="scoped")
    await add_order(db, account_scope=None, broker_order_id="unscoped")

    summary = await run_task()

    assert sorted(brokers.all_order_reads) == ["scoped", "unscoped"]
    assert "out_of_scope" not in summary


async def test_an_unknown_submission_without_an_account_is_not_matched_in_an_identified_account(
    db: AsyncSession, brokers: Brokers
) -> None:
    """History is matched loosely (ticker, quantity, side, time). An account that knows who
    it is cannot show an order with no recorded account was placed there."""
    connection = await add_connection(db, account_id="ACC-1")
    unknown = await add_order(db, account_scope=None, status="submission_unknown")

    summary = await run_task()

    broker = brokers.for_user(connection.user_id)
    assert broker.history_reads == []
    assert broker.order_reads == []
    assert summary["out_of_scope"] == {"no_account_identity": 1}
    after = await stored(db, unknown.id)
    assert after.status == "submission_unknown"
    assert after.last_reconciled_at is None


async def test_an_unknown_submission_without_an_account_is_checked_when_nothing_is_identified(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id=None)
    await add_order(db, account_scope=None, status="submission_unknown")

    summary = await run_task()

    assert brokers.for_user(connection.user_id).history_reads == ["AAPL_US_EQ"]
    assert "out_of_scope" not in summary


async def test_when_one_of_two_connections_goes_away_the_other_does_not_inherit_its_unknowns(
    db: AsyncSession, brokers: Brokers
) -> None:
    staying = await add_connection(db, account_id="ACC-1", created_at=T0)
    leaving = await add_connection(db, account_id="ACC-2", created_at=T0 + timedelta(minutes=1))
    await add_order(db, account_scope=scope_of(leaving), broker_order_id="leavers-order")
    await add_order(db, account_scope=None, status="submission_unknown")
    await add_order(db, account_scope=None, broker_order_id="unscoped-by-id")

    first = await run_task()
    assert first["out_of_scope"] == {"no_account_identity": 2}

    await db.execute(
        update(BrokerConnection).where(BrokerConnection.id == leaving.id).values(is_active=False)
    )
    await db.commit()
    second = await run_task()

    remaining = brokers.for_user(staying.user_id)
    assert remaining.history_reads == [], "the remaining account matched an unattributed order"
    assert remaining.order_reads == ["unscoped-by-id"], "only the exact-id lookup is allowed"
    assert second["out_of_scope"] == {"other_account": 1, "no_account_identity": 1}


async def test_with_two_connections_an_order_without_an_account_is_read_by_neither(
    db: AsyncSession, brokers: Brokers
) -> None:
    first = await add_connection(db, account_id="ACC-1")
    await add_connection(db, account_id="ACC-2")
    await add_order(db, account_scope=scope_of(first), broker_order_id="scoped")
    unscoped = await add_order(db, account_scope=None, broker_order_id="unscoped")

    summary = await run_task()

    assert brokers.all_order_reads == ["scoped"]
    assert summary["out_of_scope"] == {"no_account_identity": 1}
    assert (await stored(db, unscoped.id)).last_reconciled_at is None


async def test_a_connection_without_an_account_id_cannot_read_another_accounts_orders(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id=None)
    await add_order(db, account_scope=None, broker_order_id="unscoped")
    await add_order(
        db, account_scope="trading212:demo:account:known-account", broker_order_id="scoped"
    )

    summary = await run_task()

    assert brokers.for_user(connection.user_id).order_reads == ["unscoped"]
    assert summary["out_of_scope"] == {"other_account": 1}


async def test_among_several_connections_one_without_an_account_id_reads_nothing(
    db: AsyncSession, brokers: Brokers
) -> None:
    identified = await add_connection(db, account_id="ACC-1")
    anonymous = await add_connection(db, account_id=None)
    await add_order(db, account_scope=scope_of(identified), broker_order_id="scoped")
    await add_order(db, account_scope=None, broker_order_id="unscoped")

    summary = await run_task()

    assert brokers.all_order_reads == ["scoped"]
    assert brokers.for_user(anonymous.user_id).order_reads == []
    assert summary["out_of_scope"] == {"no_account_identity": 1}
    assert summary["connections_without_account_identity"] == 1


async def test_a_connection_for_another_broker_is_not_used(
    db: AsyncSession, brokers: Brokers
) -> None:
    other = await add_connection(db, account_id="ACC-1", broker="kraken")
    await add_order(db, account_scope=None, broker_order_id="unscoped")

    summary = await run_task()

    assert other.user_id not in brokers.by_user
    assert brokers.all_order_reads == []
    assert summary == {"reconciled": 0, "skipped": "no_connection"}


async def test_no_credentials_are_decrypted_when_there_is_nothing_to_reconcile(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_connection(db, account_id="ACC-1")

    def no_decrypt(_value: str) -> str:
        raise AssertionError("an idle run must not decrypt broker credentials")

    monkeypatch.setattr("app.core.security.decrypt_field", no_decrypt)

    assert await run_task() == {"reconciled": 0}
    assert brokers.requests == []


async def test_one_unreadable_connection_does_not_stop_the_other_account(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.security import CredentialDecryptionError

    broken = await add_connection(db, account_id="ACC-1", created_at=T0)
    healthy = await add_connection(db, account_id="ACC-2", created_at=T0 + timedelta(minutes=1))
    await add_order(db, account_scope=scope_of(broken), broker_order_id="broken-1")
    await add_order(db, account_scope=scope_of(healthy), broker_order_id="healthy-1")

    def decrypt(value: str) -> str:
        if str(broken.user_id) in value:
            raise CredentialDecryptionError("unreadable")
        return f"plain-{value}"

    monkeypatch.setattr("app.core.security.decrypt_field", decrypt)

    summary = await run_task()

    assert brokers.all_order_reads == ["healthy-1"]
    assert summary["reconciled"] == 1
    assert summary["connections_unusable"] == 1
    assert await connection_is_active(db, broken) is False
    assert await connection_is_active(db, healthy) is True


async def test_a_legacy_submitted_order_without_a_broker_id_stays_visible_and_unsent(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    orphan = await add_order(
        db,
        account_scope=scope_of(connection),
        status="submitted",
        created_at=T0 - timedelta(days=1),
    )
    assert orphan.broker_order_id is None

    with capture_logs() as logs:
        summary = await run_task()

    reported = [entry for entry in logs if entry["event"] == "execution.orphaned_submitted_order"]
    assert [entry["order_id"] for entry in reported] == [str(orphan.id)]
    assert summary["orphaned_submitted"] == 1
    assert brokers.all_order_reads == []
    assert brokers.all_write_calls == []
    assert (await stored(db, orphan.id)).status == "submitted"


async def test_reconciliation_never_calls_a_broker_write(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    await add_order(db, account_scope=scope, broker_order_id="working")
    await add_order(db, account_scope=scope, status="submission_unknown")
    await add_order(db, account_scope=scope, status="submitted")
    brokers.for_user(connection.user_id).responses["working"] = filled()

    await run_task()

    assert brokers.all_write_calls == []
    assert len(brokers.all_order_reads) == 1
