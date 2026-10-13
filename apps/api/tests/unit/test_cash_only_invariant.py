"""Cash-only operation is an invariant of the application, not a setting that can be turned off."""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings

API_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _settings_stay_cash_only() -> Any:
    """If a guard under test ever fails, do not leave the shared settings switched off."""
    yield
    object.__setattr__(settings, "CASH_ONLY_MODE", True)


OFF_SPELLINGS = ["false", "False", "FALSE", "0", "no", "off", "n", "f"]
REFUSAL = "cash-only"


@pytest.mark.parametrize("name", ["CASH_ONLY_MODE", "cash_only_mode", "Cash_Only_Mode"])
@pytest.mark.parametrize("value", OFF_SPELLINGS)
def test_environment_variable_cannot_turn_cash_only_mode_off(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=REFUSAL):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", OFF_SPELLINGS)
def test_env_file_cannot_turn_cash_only_mode_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.delenv("CASH_ONLY_MODE", raising=False)
    env_file = tmp_path / "settings.env"
    env_file.write_text(f"CASH_ONLY_MODE={value}\n")

    with pytest.raises(ValidationError, match=REFUSAL):
        Settings(_env_file=str(env_file))


@pytest.mark.parametrize("value", [False, 0, "false", "off", None])
def test_constructor_argument_cannot_turn_cash_only_mode_off(value: object) -> None:
    with pytest.raises(ValidationError, match=REFUSAL):
        Settings(_env_file=None, CASH_ONLY_MODE=value)


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on"])
def test_cash_only_mode_may_be_stated_explicitly(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CASH_ONLY_MODE", value)

    assert Settings(_env_file=None).CASH_ONLY_MODE is True


@pytest.mark.parametrize("value", [False, 0, "false", None])
def test_cash_only_mode_cannot_be_reassigned_on_the_running_settings(value: object) -> None:
    with pytest.raises((ValidationError, ValueError, TypeError), match=REFUSAL):
        settings.CASH_ONLY_MODE = value  # type: ignore[assignment]

    assert settings.CASH_ONLY_MODE is True


def test_cash_only_mode_cannot_be_deleted_from_the_running_settings() -> None:
    with pytest.raises((AttributeError, ValueError, TypeError)):
        del settings.CASH_ONLY_MODE

    assert settings.CASH_ONLY_MODE is True


def test_shipped_settings_are_cash_only() -> None:
    assert settings.CASH_ONLY_MODE is True
    assert Settings.model_fields["CASH_ONLY_MODE"].default is True


@pytest.mark.parametrize(
    "module",
    ["app.main", "app.workers.celery_app", "app.workers.tasks", "app.core.config"],
)
def test_no_process_starts_with_cash_only_mode_off(module: str) -> None:
    """The API, the worker and the scheduler all import their settings before anything else."""
    environment = {**os.environ, "CASH_ONLY_MODE": "false"}

    completed = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode != 0
    assert REFUSAL in completed.stderr


def test_celery_command_line_cannot_load_the_application_with_cash_only_mode_off() -> None:
    """The worker and the scheduler both start by loading this application object."""
    environment = {**os.environ, "CASH_ONLY_MODE": "false"}

    completed = subprocess.run(
        [sys.executable, "-m", "celery", "-A", "app.workers.celery_app", "report"],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode != 0
    assert REFUSAL in completed.stderr + completed.stdout


# ── Short selling and CFD treatment cannot be switched on through a strategy ──

SHORT_REQUESTS = [True, 1, "true", "yes", "on", 1.0, {"enabled": True}, [1]]
NOT_SHORT_REQUESTS = [False, None, 0]


@pytest.mark.parametrize("value", SHORT_REQUESTS)
def test_parameter_asking_for_short_selling_is_recognised(value: object) -> None:
    from app.services.cash_only import short_selling_parameters

    assert short_selling_parameters({"allow_short": value}) == ["allow_short"]


@pytest.mark.parametrize("value", NOT_SHORT_REQUESTS)
def test_parameter_that_does_not_ask_for_short_selling_is_accepted(value: object) -> None:
    from app.services.cash_only import short_selling_parameters

    assert short_selling_parameters({"allow_short": value}) == []
    assert short_selling_parameters({}) == []
    assert short_selling_parameters(None) == []


@pytest.mark.parametrize("value", SHORT_REQUESTS)
def test_strategy_cannot_be_created_with_short_selling(value: object) -> None:
    from app.api.schemas import StrategyCreate

    with pytest.raises(ValidationError, match=REFUSAL):
        StrategyCreate(name="orb", type="orb", params={"allow_short": value})


@pytest.mark.parametrize("value", SHORT_REQUESTS)
def test_strategy_cannot_be_updated_to_short_selling(value: object) -> None:
    from app.api.schemas import StrategyUpdate

    with pytest.raises(ValidationError, match=REFUSAL):
        StrategyUpdate(params={"allow_short": value})


@pytest.mark.parametrize("value", NOT_SHORT_REQUESTS)
def test_strategy_without_short_selling_is_still_accepted(value: object) -> None:
    from app.api.schemas import StrategyCreate, StrategyUpdate

    created = StrategyCreate(name="orb", type="orb", params={"allow_short": value})
    updated = StrategyUpdate(params={"allow_short": value, "min_rvol": 1.5})

    assert created.params == {"allow_short": value}
    assert updated.params == {"allow_short": value, "min_rvol": 1.5}


def test_no_shipped_preset_asks_for_short_selling() -> None:
    from app.services.cash_only import short_selling_parameters
    from app.services.strategy_presets import PRESET_CONFIGS

    assert PRESET_CONFIGS
    for preset in PRESET_CONFIGS.values():
        assert short_selling_parameters(preset.strategy_params) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_risk_engine_refuses_a_cfd_order_before_any_other_check(side: str) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock, MagicMock

    from app.risk.engine import RiskEngine, RiskViolation

    engine = RiskEngine(MagicMock())
    engine._log = AsyncMock()  # type: ignore[method-assign]
    engine.check_kill_switch = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RiskViolation) as refused:
        await engine.run_all_checks(
            ticker="AAPL",
            side=side,
            quantity=Decimal("1"),
            estimated_price=Decimal("100"),
            available_cash=Decimal("100000"),
            account_value=Decimal("100000"),
            realized_pnl_today=Decimal("0"),
            current_open_positions=0,
            is_cfd=True,
        )

    assert refused.value.event_type == "cash_only_cfd_block"
    assert REFUSAL in refused.value.reason
    engine.check_kill_switch.assert_not_awaited()


# ── The CFD funding task is retired; its historical table is kept ────────────


def test_funding_task_is_neither_scheduled_nor_registered() -> None:
    from app.workers import tasks
    from app.workers.celery_app import celery_app

    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}

    assert not any("funding" in name for name in scheduled)
    assert not any("funding" in name for name in celery_app.tasks)
    assert not hasattr(tasks, "track_cfd_funding")


def test_no_application_code_reads_or_writes_the_funding_table() -> None:
    app_root = API_ROOT / "app"
    offenders = [
        str(path.relative_to(app_root))
        for path in sorted(app_root.rglob("*.py"))
        if "migrations" not in path.parts
        and path.relative_to(app_root).as_posix() != "db/models/__init__.py"
        and ("CFDFundingCost" in path.read_text() or "cfd_funding" in path.read_text())
    ]

    assert offenders == []


def test_historical_funding_table_is_still_part_of_the_schema() -> None:
    from app.db.models import Base, CFDFundingCost

    assert CFDFundingCost.__tablename__ == "cfd_funding_costs"
    assert "cfd_funding_costs" in Base.metadata.tables
    assert (API_ROOT / "app/db/migrations/versions/0004_cfd_funding_cost.py").is_file()


# ── A sell must be covered by a position the broker has clearly reported ─────


@pytest.mark.parametrize(
    ("positions", "expected"),
    [
        ([{"ticker": "AAPL_US_EQ", "quantity": 5}], "5"),
        ([{"ticker": "aapl_us_eq ", "quantity": "5", "maxSell": "2.5"}], "2.5"),
        ([{"ticker": "AAPL_US_EQ", "quantity": 2, "maxSell": 10}], "2"),
        ([{"ticker": "AAPL_US_EQ", "quantity": 1}, {"ticker": "AAPL_US_EQ", "quantity": 2}], "3"),
        ([{"ticker": "MSFT_US_EQ", "quantity": 5}], "0"),
        ([], "0"),
        (None, "0"),
        ({"ticker": "AAPL_US_EQ", "quantity": 5}, "0"),
        (["AAPL_US_EQ"], "0"),
        ([{"ticker": "AAPL_US_EQ"}], "0"),
        ([{"ticker": "AAPL_US_EQ", "quantity": -5}], "0"),
        ([{"ticker": "AAPL_US_EQ", "quantity": 0}], "0"),
        ([{"ticker": "AAPL_US_EQ", "quantity": "many"}], "0"),
        ([{"ticker": "AAPL_US_EQ", "quantity": float("nan")}], "0"),
        ([{"ticker": "AAPL_US_EQ", "quantity": float("inf")}], "0"),
        ([{"ticker": "AAPL_US_EQ", "quantity": 5, "maxSell": 0}], "0"),
        ([{"ticker": "AAPL_US_EQ", "quantity": 5, "maxSell": "none"}], "0"),
        ([{"ticker": "AAPL_US_EQ", "quantity": 5}, {"ticker": "AAPL_US_EQ", "quantity": -1}], "0"),
    ],
)
def test_sellable_quantity_counts_only_what_the_broker_clearly_reports(
    positions: object, expected: str
) -> None:
    from app.services.cash_only import sellable_quantity

    assert sellable_quantity(positions, "AAPL_US_EQ") == Decimal(expected)
