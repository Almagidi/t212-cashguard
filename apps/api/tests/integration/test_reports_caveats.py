"""
Integration tests: report/attribution coverage-caveat disclosure.

These prove the read-only disclosure work from
docs/architecture/backtest-execution-quality-parity-investigation.md (PR1):
performance and portfolio-attribution responses must say plainly whether
slippage, fees, rejected/cancelled orders, and reconciliation delay are
included — without changing any existing numeric calculation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from app.db.models import Strategy, Trade

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
class TestPerformanceReportCaveats:
    async def test_empty_report_includes_coverage_caveats(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        resp = await client.get("/v1/reports/performance", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert "coverage_caveats" in data
        caveats = data["coverage_caveats"]
        assert isinstance(caveats, list)
        assert len(caveats) > 0
        assert all(isinstance(c, str) for c in caveats)

        joined = " ".join(caveats).lower()
        assert "slippage" in joined
        assert "fee" in joined
        assert "rejected" in joined or "cancelled" in joined
        assert "reconcil" in joined

        # Existing zero-trade numeric behaviour must be unchanged.
        assert data["total_trades"] == 0
        assert data["total_pnl"] == 0.0

    async def test_report_with_trades_keeps_numeric_fields_unchanged(
        self, client: AsyncClient, auth_headers: dict, db: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        db.add_all(
            [
                Trade(
                    id=uuid.uuid4(),
                    ticker="AAPL",
                    side="buy",
                    quantity=Decimal("10"),
                    open_price=Decimal("100"),
                    close_price=Decimal("110"),
                    realized_pnl=Decimal("100.00"),
                    opened_at=now - timedelta(hours=2),
                    closed_at=now - timedelta(hours=1),
                    is_dry_run=False,
                ),
                Trade(
                    id=uuid.uuid4(),
                    ticker="MSFT",
                    side="buy",
                    quantity=Decimal("5"),
                    open_price=Decimal("200"),
                    close_price=Decimal("190"),
                    realized_pnl=Decimal("-50.00"),
                    opened_at=now - timedelta(hours=2),
                    closed_at=now - timedelta(hours=1),
                    is_dry_run=False,
                ),
            ]
        )
        await db.commit()

        resp = await client.get("/v1/reports/performance", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Same numeric outcome as before this change — disclosure-only.
        assert data["total_trades"] == 2
        assert data["winning_trades"] == 1
        assert data["losing_trades"] == 1
        assert data["total_pnl"] == 50.0

        assert len(data["coverage_caveats"]) > 0


@pytest.mark.asyncio
class TestPortfolioAttributionCaveats:
    async def test_empty_strategy_summary_includes_coverage_caveats(
        self, client: AsyncClient, auth_headers: dict, db: AsyncSession
    ) -> None:
        strategy = Strategy(
            id=uuid.uuid4(),
            name="Caveat Coverage Strategy",
            type="buy_hold_core",
            is_enabled=True,
            is_live=False,
            params={},
            allowed_tickers=["SPY"],
            session_start="09:30",
            session_end="16:00",
            eod_flatten=False,
        )
        db.add(strategy)
        await db.commit()

        resp = await client.get("/v1/strategies/portfolio-attribution", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 1
        row = rows[0]

        assert "coverage_caveats" in row
        caveats = row["coverage_caveats"]
        assert isinstance(caveats, list)
        assert len(caveats) > 0

        joined = " ".join(caveats).lower()
        assert "slippage" in joined
        assert "fee" in joined
        assert "rejected" in joined or "cancelled" in joined
        assert "reconcil" in joined

        # No fills exist — zeroed numeric behaviour must be unchanged.
        assert row["order_count"] == 0
        assert row["total_pnl"] == 0.0
