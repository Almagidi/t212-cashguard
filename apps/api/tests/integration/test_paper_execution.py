"""Paper execution API safety tests."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.broker.kraken import KrakenAdapter
from app.broker.trading212 import Trading212Adapter
from app.db.models import AppSettings, AuditLog, Order, PositionSnapshot

if TYPE_CHECKING:
    from httpx import AsyncClient


def _paper_payload(**overrides):
    payload = {
        "ticker": "PAPERXYZ",
        "side": "buy",
        "quantity": "2",
        "estimated_price": "25.50",
        "source": "test_signal",
        "strategy": "paper-test",
        "venue": "paper",
        "paper_only": True,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_paper_order_endpoint_requires_auth(client: AsyncClient):
    response = await client.post("/v1/orders/paper", json=_paper_payload())

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_paper_order_creates_local_order_audits_and_position(
    client: AsyncClient,
    auth_headers: dict,
    db,
):
    response = await client.post(
        "/v1/orders/paper",
        headers=auth_headers,
        json=_paper_payload(),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["ticker"] == "PAPERXYZ"
    assert body["status"] == "filled"
    assert body["is_dry_run"] is True
    assert body["execution_environment"] == "paper_mock"
    assert body["broker_order_id"] is None

    order = (
        await db.execute(select(Order).where(Order.id == uuid.UUID(body["id"])))
    ).scalar_one()
    assert order.broker_request["no_broker_order_sent"] is True
    assert order.broker_response["status"] == "PAPER_FILLED"

    audits = (
        await db.execute(
            select(AuditLog).where(AuditLog.entity_id == str(order.id))
        )
    ).scalars().all()
    assert {audit.action for audit in audits} >= {
        "paper_order_created",
        "paper_fill_simulated",
    }
    all_audits = (await db.execute(select(AuditLog))).scalars().all()
    assert {audit.action for audit in all_audits} >= {
        "paper_signal_accepted",
        "paper_risk_check_result",
        "paper_order_created",
        "paper_fill_simulated",
        "paper_position_updated",
    }
    paper_audits = [audit for audit in all_audits if audit.action.startswith("paper_")]
    assert all(audit.payload["paper_only"] is True for audit in paper_audits)

    position = (
        await db.execute(
            select(PositionSnapshot).where(PositionSnapshot.ticker == "PAPERXYZ")
        )
    ).scalar_one()
    assert position.quantity == Decimal("2")
    assert position.avg_price == Decimal("25.5")
    assert position.raw["paper_only"] is True


@pytest.mark.asyncio
async def test_paper_order_does_not_call_live_broker_order_methods(
    client: AsyncClient,
    auth_headers: dict,
    monkeypatch,
):
    patched = [
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    ]
    (
        t212_market,
        t212_limit,
        t212_stop,
        t212_stop_limit,
        kraken_market,
        kraken_limit,
        kraken_stop,
        kraken_stop_limit,
    ) = patched
    monkeypatch.setattr(Trading212Adapter, "place_market_order", t212_market)
    monkeypatch.setattr(Trading212Adapter, "place_limit_order", t212_limit)
    monkeypatch.setattr(Trading212Adapter, "place_stop_order", t212_stop)
    monkeypatch.setattr(Trading212Adapter, "place_stop_limit_order", t212_stop_limit)
    monkeypatch.setattr(KrakenAdapter, "place_market_order", kraken_market)
    monkeypatch.setattr(KrakenAdapter, "place_limit_order", kraken_limit)
    monkeypatch.setattr(KrakenAdapter, "place_stop_order", kraken_stop)
    monkeypatch.setattr(KrakenAdapter, "place_stop_limit_order", kraken_stop_limit)

    response = await client.post(
        "/v1/orders/paper",
        headers=auth_headers,
        json=_paper_payload(ticker="NOBROKER"),
    )

    assert response.status_code == 201, response.text
    for method in patched:
        method.assert_not_called()


@pytest.mark.asyncio
async def test_paper_order_rejects_liveish_payloads_and_unsupported_venue(
    client: AsyncClient,
    auth_headers: dict,
):
    for payload in (
        _paper_payload(paper_only=False),
        _paper_payload(live=True),
        _paper_payload(venue="kraken"),
    ):
        response = await client.post(
            "/v1/orders/paper",
            headers=auth_headers,
            json=payload,
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_paper_order_kill_switch_blocks_and_audits(
    client: AsyncClient,
    auth_headers: dict,
    db,
):
    settings = (
        await db.execute(select(AppSettings).where(AppSettings.id == 1))
    ).scalar_one()
    settings.kill_switch_active = True
    await db.flush()

    response = await client.post(
        "/v1/orders/paper",
        headers=auth_headers,
        json=_paper_payload(ticker="BLOCKED"),
    )

    assert response.status_code == 422
    assert "Kill switch is active" in response.json()["detail"]
    assert (await db.execute(select(Order))).scalars().all() == []
    audits = (await db.execute(select(AuditLog))).scalars().all()
    actions = [audit.action for audit in audits]
    assert "paper_signal_rejected" in actions
    assert "paper_risk_check_result" in actions
    risk_audit = next(
        audit for audit in audits if audit.action == "paper_risk_check_result"
    )
    assert risk_audit.payload["result"] == "blocked"
    assert risk_audit.payload["decision_code"] == "kill_switch_block"


@pytest.mark.asyncio
async def test_paper_position_is_visible_in_positions_endpoint(
    client: AsyncClient,
    auth_headers: dict,
):
    create_response = await client.post(
        "/v1/orders/paper",
        headers=auth_headers,
        json=_paper_payload(ticker="LOCALPAPER"),
    )
    assert create_response.status_code == 201, create_response.text

    positions_response = await client.get("/v1/positions", headers=auth_headers)

    assert positions_response.status_code == 200
    tickers = {position["ticker"] for position in positions_response.json()}
    assert "LOCALPAPER" in tickers
