"""Actual optional SDK integration, offline and with synthetic data only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sentry_sdk

API_ROOT = Path(__file__).resolve().parents[2]
PROBE = API_ROOT / "tests" / "sentry_privacy_probe.py"


@pytest.mark.parametrize("enabled", [False, True, None])
def test_optional_sentry_wire_payloads_are_confidential(
    tmp_path: Path, enabled: bool | None
) -> None:
    # The parent runtime selects the SDK; a comparison overlay never changes source pins.
    sdk_root = Path(sentry_sdk.__file__).resolve().parent.parent
    directory = tmp_path / ("enabled" if enabled else "disabled")
    env = {
        "PATH": str(Path(sys.executable).parent) + os.pathsep + "/usr/bin:/bin",
        "PYTHONPATH": str(sdk_root) + os.pathsep + str(API_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CASHGUARD_ENV_FILE": "",
        "APP_MODE": "mock",
        "LIVE_TRADING_ENABLED": "false",
        "CASH_ONLY_MODE": "true",
        "MARKET_DATA_PROVIDER": "mock",
        "DEMO_RECONCILIATION_SCHEDULER_ENABLED": "false",
        "DEMO_RECONCILIATION_WORKER_ENABLED": "false",
        "T212_DEMO_ORDER_ENABLED": "false",
        "DATABASE_URL": "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:1/synthetic",
        "REDIS_URL": "redis://127.0.0.1:1/15",
        "CELERY_BROKER_URL": "memory://",
        "CELERY_RESULT_BACKEND": "cache+memory://",
        "SECRET_KEY": "SYNTHETIC_ONLY_" + "a" * 48,
        "MASTER_KEY": "SYNTHETIC_ONLY_" + "b" * 48,
        "ADMIN_EMAIL": "synthetic@example.invalid",
        "ADMIN_PASSWORD": "SYNTHETIC_ONLY_NOT_REAL",
        "SENTRY_DSN": "https://synthetic-public-key@sentry.invalid/1" if enabled else "",
        "SENTRY_TRACES_SAMPLE_RATE": "1.0",
        # Even an operator requesting profiles cannot bypass the qualified envelope policy.
        "SENTRY_PROFILES_SAMPLE_RATE": "1.0",
        "LOG_LEVEL": "WARNING",
        "LANG": "C.UTF-8",
    }
    command = [sys.executable, str(PROBE), str(directory)]
    if enabled is None:
        command.append("--tripwire-selftest")
    result = subprocess.run(
        command,
        cwd=API_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((directory / "report.json").read_text())
    if enabled is None:
        assert report["status"] == "PASS_TRIPWIRE_SELFTEST"
        assert report["checks"]["blocked_before_any_sdk_or_app_import"]
        assert len(report["network_attempts"]) == 1
        return
    assert report["status"] == "PASS_HARNESS_EXECUTION"
    assert report["network_attempts"] == []
    envelopes = json.loads((directory / "envelopes.json").read_text())
    if not enabled:
        assert report["init_call_count"] == 0
        assert envelopes == []
        return
    assert report["init_call_count"] == 1
    assert report["checks"]["production_transport_boundary_present"]
    items = [item for envelope in envelopes for item in envelope["items"]]
    assert [item["headers"]["type"] for item in items].count("event") == 5
    assert [item["headers"]["type"] for item in items].count("transaction") == 6
    assert len(items) == 11  # no profiles, sessions, logs or other bypass channels
    for envelope in envelopes:
        assert "SYNTHETIC_CG325_" not in json.dumps(envelope)
    for wire in directory.glob("envelope-*.bin"):
        assert b"SYNTHETIC_CG325_" not in wire.read_bytes()
    errors = [item["json"] for item in items if item["headers"]["type"] == "event"]
    assert any(item.get("exception") for item in errors)
    assert any(
        frame["filename"].startswith("app/")
        for item in errors
        for value in item.get("exception", {}).get("values", [])
        for frame in value.get("stacktrace", {}).get("frames", [])
    )
    assert all(
        item["json"].get("contexts", {}).get("trace", {}).get("trace_id")
        for item in items
        if item["headers"]["type"] == "transaction"
    )
