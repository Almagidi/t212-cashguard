"""The inventory of the retired funding table reports what is there and changes nothing."""

from __future__ import annotations

import importlib.util
import os
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import CFDFundingCost

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inventory_cfd_funding_costs.py"
WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|copy|merge|call|do)\b", re.I
)


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("inventory_cfd_funding_costs", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(ticker: str, moment: datetime, charge: str, *, rate: str = "5.5") -> CFDFundingCost:
    return CFDFundingCost(
        id=uuid.uuid4(),
        ticker=ticker,
        quantity=Decimal("10"),
        price_at_close=Decimal("100"),
        notional=Decimal("1000"),
        annual_rate_pct=Decimal(rate),
        daily_charge=Decimal(charge),
        currency="USD",
        recorded_at=moment,
    )


def test_every_inventory_statement_is_a_single_select() -> None:
    queries = _script().QUERIES

    assert set(queries) == {
        "totals",
        "by_currency",
        "by_annual_rate",
        "by_ticker",
        "by_day",
        "repeated_ticker_days",
    }
    for statement in queries.values():
        assert statement.startswith("SELECT ")
        assert ";" not in statement
        assert WRITE_WORDS.search(statement) is None
        assert statement.count("cfd_funding_costs") == 1


@pytest.mark.asyncio
async def test_inventory_reports_rows_and_repeated_days_without_changing_anything(
    db: AsyncSession,
) -> None:
    first_run = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    second_run = datetime(2026, 9, 1, 12, 5, tzinfo=UTC)
    next_day = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    db.add_all(
        [
            _row("AAPL_US_EQ", first_run, "0.15"),
            _row("AAPL_US_EQ", second_run, "0.15"),
            _row("MSFT_US_EQ", next_day, "0.20", rate="4.0"),
        ]
    )
    await db.flush()
    before = (await db.execute(select(CFDFundingCost.id, CFDFundingCost.daily_charge))).all()

    report = await _script().inventory(await db.connection())

    (totals,) = report["totals"]
    assert totals["row_count"] == 3
    assert Decimal(str(totals["total_charge"])) == Decimal("0.50")
    assert totals["rows_without_strategy"] == 3
    assert [(row["ticker"], row["row_count"]) for row in report["by_ticker"]] == [
        ("AAPL_US_EQ", 2),
        ("MSFT_US_EQ", 1),
    ]
    assert [row["row_count"] for row in report["by_day"]] == [2, 1]
    assert [(row["ticker"], row["row_count"]) for row in report["repeated_ticker_days"]] == [
        ("AAPL_US_EQ", 2)
    ]
    assert sorted(Decimal(str(row["annual_rate_pct"])) for row in report["by_annual_rate"]) == [
        Decimal("4.0"),
        Decimal("5.5"),
    ]
    assert "None was reported by a broker" in report["provenance"]
    after = (await db.execute(select(CFDFundingCost.id, CFDFundingCost.daily_charge))).all()
    assert sorted(after) == sorted(before)
    assert (await db.execute(select(func.count()).select_from(CFDFundingCost))).scalar_one() == 3


@pytest.mark.asyncio
async def test_inventory_of_an_empty_table_reports_zero_rows(db: AsyncSession) -> None:
    report = await _script().inventory(await db.connection())

    (totals,) = report["totals"]
    assert totals["row_count"] == 0
    assert Decimal(str(totals["total_charge"])) == 0
    assert report["by_ticker"] == []
    assert report["repeated_ticker_days"] == []


@pytest.mark.asyncio
@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required: PostgreSQL enforces read-only transactions",
)
async def test_script_transaction_refuses_any_write_on_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script's own transaction is read-only, whatever the statements in it try."""
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    module = _script()

    async def attempt_a_write(connection: Any) -> dict[str, Any]:
        await connection.execute(
            text(
                "INSERT INTO cfd_funding_costs "
                "(id, ticker, quantity, price_at_close, notional, annual_rate_pct, "
                "daily_charge, currency) "
                "VALUES (:id, 'X', 1, 1, 1, 1, 1, 'USD')"
            ),
            {"id": uuid.uuid4()},
        )
        return {}

    try:
        before = (await module.run_inventory(engine))["totals"][0]["row_count"]
        monkeypatch.setattr(module, "inventory", attempt_a_write)
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await module.run_inventory(engine)
        monkeypatch.undo()
        after = (await _script().run_inventory(engine))["totals"][0]["row_count"]
    finally:
        await engine.dispose()

    assert after == before
