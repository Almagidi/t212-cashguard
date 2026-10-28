"""Synthetic denomination metadata must not silently change any example amount."""

from unittest.mock import AsyncMock, patch

from app.api.v1.routes import account, ws
from app.broker.mock_adapter import MockBrokerAdapter


async def test_mock_summary_explicitly_declares_the_metadata_currency():
    broker = MockBrokerAdapter()
    summary = await broker.get_account_summary()
    metadata = await broker.get_account_metadata()

    assert summary.get("currencyCode") == metadata["currencyCode"] == "USD"
    rest = await account.account_summary(None, broker)
    cash = await account.cash_guard_status(None, broker)
    assert rest.currency == cash.currency == metadata["currencyCode"]


async def test_mock_stream_uses_the_same_unit_without_reconciling_example_values():
    controls = {
        "auto_trading_enabled": False,
        "kill_switch_active": True,
        "max_daily_loss_pct": 3.25,
    }
    with (
        patch("time.time", return_value=0),
        patch.object(ws.MarketRegimeService, "evaluate", AsyncMock(return_value={})),
    ):
        stream = await ws._mock_payload(controls)

    assert stream["account"]["currency"] == "USD"
    assert stream["data_source"] == "mock_market_animation"
    assert stream["system"] == controls
    assert stream["account"] == {
        "total_value": 10989.8,
        "free_cash": 8234.8,
        "invested": 2755.0,
        "currency": "USD",
        "unrealized_pnl": 142.5,
    }
    assert [
        (p["ticker"], p["quantity"], p["market_value"], p["unrealized_pnl"])
        for p in stream["positions"]
    ] == [
        ("AAPL", 5.0, 906.0, 13.5),
        ("MSFT", 3.0, 1265.4, 20.4),
        ("NVDA", 2.0, 1789.0, 29.0),
    ]


async def test_currency_metadata_does_not_mutate_mock_nominal_balances_or_orders():
    broker = MockBrokerAdapter()
    before_positions = {ticker: dict(position) for ticker, position in broker._positions.items()}
    summary = await broker.get_account_summary()
    await broker.get_account_metadata()

    assert {
        key: summary[key] for key in ("cash", "free", "invested", "result", "total", "pieCash")
    } == {
        "cash": 10000.0,
        "free": 10000.0,
        "invested": 3665.0,
        "result": 95.0,
        "total": 13760.0,
        "pieCash": 0.0,
    }
    assert broker._positions == before_positions
    assert broker._orders == {}
