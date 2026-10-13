"""The operator's demo reconciliation never asks one account about another account's order."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import update

from app.db.models import Order
from app.services.demo_order_reconciliation import DemoOrderReconciler
from app.services.demo_reconciliation_worker import DemoReconciliationWorker
from app.services.safety_policy import SafetyPolicyViolation
from tests.integration import test_demo_reconciliation_worker as worker_cases

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

MINE = "trading212:demo:account:mine"
THEIRS = "trading212:demo:account:theirs"

_demo_safety = worker_cases._demo_safety


class ScopedBroker(worker_cases.WorkerBroker):
    def __init__(self, account_scope: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.account_scope = account_scope


async def _orders(db: AsyncSession) -> dict[str, Order]:
    made = {
        "mine": worker_cases._demo_order(broker_order_id="1001", broker_account_scope=MINE),
        "theirs": worker_cases._demo_order(broker_order_id="1002", broker_account_scope=THEIRS),
        "unscoped": worker_cases._demo_order(broker_order_id="1003", broker_account_scope=None),
    }
    db.add_all(made.values())
    await db.flush()
    return made


async def test_the_worker_selects_its_own_accounts_orders_and_those_without_an_account(
    db: AsyncSession,
) -> None:
    made = await _orders(db)
    worker = DemoReconciliationWorker(db, ScopedBroker(MINE))

    selected = {order.id for order in await worker.select_reconciliation_candidates()}

    assert selected == {made["mine"].id, made["unscoped"].id}


async def test_the_worker_never_consults_history_for_another_accounts_order(
    db: AsyncSession,
) -> None:
    made = await _orders(db)
    broker = ScopedBroker(MINE)

    summary = await DemoReconciliationWorker(db, broker).run_once()

    assert summary.candidates_found == 2
    assert len(broker.history_calls) == 2
    assert str(made["theirs"].id) not in {item["order_id"] for item in summary.order_results}
    assert made["theirs"].last_reconciled_at is None


async def test_an_adapter_with_no_account_identity_selects_as_it_did_before(
    db: AsyncSession,
) -> None:
    made = await _orders(db)
    worker = DemoReconciliationWorker(db, ScopedBroker(None))

    selected = {order.id for order in await worker.select_reconciliation_candidates()}

    assert selected == {order.id for order in made.values()}


async def test_reconciling_one_order_is_refused_when_it_belongs_to_another_account(
    db: AsyncSession,
) -> None:
    made = await _orders(db)
    broker = ScopedBroker(MINE)

    with pytest.raises(SafetyPolicyViolation) as refusal:
        await DemoOrderReconciler(db, broker).reconcile_order(made["theirs"])

    assert refusal.value.decision_code == "demo_reconciliation_account_scope_block"
    assert broker.history_calls == []
    assert made["theirs"].status == "accepted"
    assert made["theirs"].last_reconciled_at is None


@pytest.mark.parametrize(
    ("broker_scope", "order_name"),
    [(MINE, "mine"), (MINE, "unscoped"), (None, "theirs"), (None, "unscoped")],
)
async def test_reconciling_one_order_is_allowed_unless_a_mismatch_is_proven(
    db: AsyncSession, broker_scope: str | None, order_name: str
) -> None:
    made = await _orders(db)
    broker = ScopedBroker(broker_scope)

    result = await DemoOrderReconciler(db, broker).reconcile_order(made[order_name])

    assert result.outcome == "missing"
    assert len(broker.history_calls) == 1


async def test_the_worker_reads_each_order_again_before_it_asks_the_broker(
    db: AsyncSession,
) -> None:
    first = worker_cases._demo_order(broker_order_id="2001")
    second = worker_cases._demo_order(broker_order_id="2002")
    db.add_all([first, second])
    await db.flush()

    class SettlesTheSecondOrderMeanwhile(ScopedBroker):
        async def get_historical_orders(self, **kwargs: Any) -> Any:
            # Another writer cancels the second order while the first is being checked.
            await db.execute(
                update(Order)
                .where(Order.id == second.id)
                .values(status="cancelled")
                .execution_options(synchronize_session=False)
            )
            return await super().get_historical_orders(**kwargs)

    broker = SettlesTheSecondOrderMeanwhile(None)

    summary = await DemoReconciliationWorker(db, broker).run_once()

    assert len(broker.history_calls) == 1, "the broker was asked about an order already settled"
    assert summary.attempted == 1
    assert summary.skipped == 1
    assert second.status == "cancelled"
