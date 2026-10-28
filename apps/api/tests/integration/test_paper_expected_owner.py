"""Expected owner is an equality precondition, never an authority selector."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models import Order, PaperOperationReceipt, User
from app.execution.paper_operations import OperationResult, PaperOperationService


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "different"])
async def test_missing_or_spoofed_expected_owner_is_rejected_before_any_command(
    client, auth_headers, db, monkeypatch, header
):
    submit = AsyncMock(return_value=OperationResult(503, {"detail": "synthetic service boundary"}))
    monkeypatch.setattr(PaperOperationService, "submit", submit)
    headers = dict(auth_headers)
    if header:
        headers["X-Paper-Expected-Owner"] = str(uuid4())
    result = await client.post(
        "/v1/orders/paper",
        headers=headers,
        json={
            "operation_id": str(uuid4()),
            "ticker": "OWNER",
            "side": "buy",
            "quantity": "1",
        },
    )
    assert result.status_code == 403
    assert "recorded" not in result.json()
    submit.assert_not_awaited()
    assert not (await db.scalars(select(Order))).all()
    assert not (await db.scalars(select(PaperOperationReceipt))).all()


@pytest.mark.asyncio
async def test_header_cannot_override_the_authenticated_principal(
    client, auth_headers, db, monkeypatch
):
    owner = await db.scalar(select(User))
    submit = AsyncMock(return_value=OperationResult(503, {"detail": "synthetic service boundary"}))
    monkeypatch.setattr(PaperOperationService, "submit", submit)
    response = await client.post(
        "/v1/orders/paper",
        headers={
            **auth_headers,
            "X-Paper-Expected-Owner": str(owner.id),
        },
        json={"operation_id": str(uuid4()), "ticker": "OWNER", "side": "buy", "quantity": "1"},
    )
    assert response.status_code == 503
    # It reaches the authenticated owner's service, never a body/header-selected owner.
    assert submit.await_args.kwargs["user"].id == owner.id


@pytest.mark.asyncio
async def test_lookup_expected_owner_mismatch_cannot_disclose_or_complete_a_same_uuid_receipt(
    client, auth_headers, db, monkeypatch
):
    lookup = AsyncMock()
    monkeypatch.setattr(PaperOperationService, "lookup", lookup)
    response = await client.get(
        f"/v1/orders/paper/operations/{uuid4()}",
        headers={
            **auth_headers,
            "X-Paper-Expected-Owner": str(uuid4()),
        },
    )
    assert response.status_code == 403
    assert set(response.json()) == {"detail"}
    lookup.assert_not_awaited()
    assert not (await db.scalars(select(Order))).all()
    assert not (await db.scalars(select(PaperOperationReceipt))).all()
