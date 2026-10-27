"""Actual safety exposition, dashboard consumers and source-pinned Promtool.

Only synthetic in-memory state is supplied to the handler. PostgreSQL reader
proofs live in apps/api/tests/integration/test_metrics_safety_state.py.
Pre-pull the source Prometheus image; rule evaluation has no network or ports.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "infra/prometheus/alerts.yml"
sys.path.insert(0, str(ROOT / "apps/api"))

SYNTHETIC = {
    "CASHGUARD_ENV_FILE": "",
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "SECRET_KEY": "monitoring-contract-synthetic-session-key",
    "MASTER_KEY": "monitoring-contract-synthetic-master-key",
    "APP_MODE": "mock",
    "MARKET_DATA_PROVIDER": "mock",
    "LIVE_TRADING_ENABLED": "false",
}
UNSUPPORTED = {
    "cashguard_account_cash_available",
    "cashguard_account_total_value",
    "cashguard_account_open_positions",
    "cashguard_open_positions_count",
    "cashguard_kill_switch_activations_total",
}
KNOWN_LABELS = {
    (control, scope)
    for scope in ("global", "t212", "kraken")
    for control in ("kill_switch", "auto_trading", "degraded_mode")
    if scope != "global" or control != "degraded_mode"
}
SAFETY_METRICS = {
    "cashguard_kill_switch_active",
    "cashguard_auto_trading_enabled",
    "cashguard_venue_kill_switch_active",
    "cashguard_venue_auto_trading_enabled",
    "cashguard_venue_degraded_mode_active",
    "cashguard_kill_switch_last_activation_timestamp_seconds",
    "cashguard_safety_state_read_ok",
    "cashguard_safety_state_known",
}
SUMMARIES = {
    "KillSwitchActive": "Saved global kill switch is active",
    "VenueKillSwitchActive": "Saved venue kill switch is active",
    "KillSwitchActivated": "Recent recorded global kill-switch activation",
    "SafetyStateReadUnavailable": "Saved safety state is unavailable",
    "SafetyControlUnknown": "Saved safety control is unknown",
    "AutoTradingDisabled": "Auto trading has been disabled for >5 minutes",
    "APIDown": "CashGuard API is unreachable",
}
DESCRIPTIONS = {
    "KillSwitchActive": "The saved global kill switch is active. Check the operator view; this does not confirm positions are flat or cancellations complete.",
    "VenueKillSwitchActive": "The saved {venue} kill switch is active. This is venue state, not the global switch or proof that positions are flat.",
    "KillSwitchActivated": "The most recent retained global activation occurred within five minutes. This is historical evidence, not current halt state or an activation count. Resolution does not mean trading resumed.",
    "SafetyStateReadUnavailable": "The API scrape succeeded but the saved safety-state read failed or its readiness marker is missing. Inspect database access and read limits. Unknown does not mean safe or enabled.",
    "SafetyControlUnknown": "Saved {control} for {scope} is unavailable. A venue may be intentionally unconfigured; check the deployment runbook. Do not enable a venue or change defaults to clear this warning.",
    "AutoTradingDisabled": "Auto trading has been off for more than 5 minutes. This may be intentional or a configuration issue.",
    "APIDown": "Prometheus cannot scrape the CashGuard API /metrics endpoint. The service may be down.",
}


def expected(alert: str, instance: str = "api-a", **labels: str) -> dict:
    ops = alert in {"SafetyStateReadUnavailable", "SafetyControlUnknown", "APIDown"}
    warning = alert in {
        "SafetyStateReadUnavailable",
        "SafetyControlUnknown",
        "AutoTradingDisabled",
    }
    return {
        "exp_labels": {
            "job": "cashguard_api",
            "instance": instance,
            "severity": "warning" if warning else "critical",
            "team": "ops" if ops else "trading",
            **labels,
        },
        "exp_annotations": {
            "summary": SUMMARIES[alert],
            "description": DESCRIPTIONS[alert].format(**labels),
        },
    }


def series(name: str, values: str, instance: str = "api-a", **labels: str) -> dict:
    all_labels = {"job": "cashguard_api", "instance": instance, **labels}
    label_text = ",".join(
        f'{key}="{value}"' for key, value in sorted(all_labels.items())
    )
    return {"series": name + "{" + label_text + "}", "values": values}


def fixture_tests() -> dict:
    cases = []

    def case(name: str, inputs: list, checks: list, interval: str = "1m") -> None:
        cases.append(
            {
                "name": name,
                "interval": interval,
                "input_series": inputs,
                "alert_rule_test": [
                    {"eval_time": at, "alertname": alert, "exp_alerts": alerts}
                    for at, alert, alerts in checks
                ],
            }
        )

    def up(values="1+0x12", **labels):
        return series("up", values, **labels)

    def read(values="1+0x12", **labels):
        return series("cashguard_safety_state_read_ok", values, **labels)

    def stamp(values, **labels):
        return series(
            "cashguard_kill_switch_last_activation_timestamp_seconds", values, **labels
        )

    def known(values, **labels):
        return series("cashguard_safety_state_known", values, **labels)

    case(
        "saved global state changes independently of history",
        [
            up(),
            read(),
            series("cashguard_kill_switch_active", "1 1 0 0"),
        ],
        [
            ("0m", "KillSwitchActive", [expected("KillSwitchActive")]),
            ("1m", "KillSwitchActive", [expected("KillSwitchActive")]),
            ("2m", "KillSwitchActive", []),
            ("3m", "KillSwitchActivated", []),
        ],
    )
    case(
        "venue state retains venue and is not global",
        [
            up(),
            read(),
            series("cashguard_kill_switch_active", "0+0x3"),
            series("cashguard_venue_kill_switch_active", "1 1 0 0", venue="t212"),
            series("cashguard_venue_kill_switch_active", "0 0 1 1", venue="kraken"),
        ],
        [
            (
                "0m",
                "VenueKillSwitchActive",
                [expected("VenueKillSwitchActive", venue="t212")],
            ),
            (
                "2m",
                "VenueKillSwitchActive",
                [expected("VenueKillSwitchActive", venue="kraken")],
            ),
            ("2m", "KillSwitchActive", []),
        ],
    )
    case(
        "failed read is not a current halt assertion",
        [
            up(),
            read("0 0 1 1"),
            series("cashguard_kill_switch_active", "1+0x3"),
        ],
        [
            ("0m", "SafetyStateReadUnavailable", []),
            (
                "1m",
                "SafetyStateReadUnavailable",
                [expected("SafetyStateReadUnavailable")],
            ),
            ("1m", "KillSwitchActive", []),
            ("2m", "SafetyStateReadUnavailable", []),
        ],
    )
    case(
        "absent readiness marker is unavailable",
        [up()],
        [
            ("0m", "SafetyStateReadUnavailable", []),
            (
                "1m",
                "SafetyStateReadUnavailable",
                [expected("SafetyStateReadUnavailable")],
            ),
        ],
    )
    case(
        "one healthy replica cannot hide failed replica",
        [
            up(),
            read(),
            up(instance="api-b"),
            read("0+0x12", instance="api-b"),
        ],
        [
            (
                "1m",
                "SafetyStateReadUnavailable",
                [expected("SafetyStateReadUnavailable", "api-b")],
            )
        ],
    )
    case(
        "one healthy replica cannot hide legacy replica",
        [
            up(),
            read(),
            up(instance="api-b"),
        ],
        [
            (
                "1m",
                "SafetyStateReadUnavailable",
                [expected("SafetyStateReadUnavailable", "api-b")],
            )
        ],
    )
    case(
        "partial unknown retains control and scope",
        [
            up(),
            read(),
            known("0 0 1 1", control="kill_switch", scope="global"),
        ],
        [
            ("0m", "SafetyControlUnknown", []),
            (
                "1m",
                "SafetyControlUnknown",
                [
                    expected(
                        "SafetyControlUnknown", control="kill_switch", scope="global"
                    )
                ],
            ),
            ("2m", "SafetyControlUnknown", []),
            ("1m", "SafetyStateReadUnavailable", []),
        ],
    )
    for policy in ("missing required venue", "intentionally unused venue"):
        case(
            policy + " stays unknown without enabling it",
            [
                up(),
                read(),
                *[
                    known("0+0x3", control=c, scope="kraken")
                    for c in ("kill_switch", "auto_trading", "degraded_mode")
                ],
            ],
            [
                (
                    "1m",
                    "SafetyControlUnknown",
                    [
                        expected("SafetyControlUnknown", control=c, scope="kraken")
                        for c in ("kill_switch", "auto_trading", "degraded_mode")
                    ],
                )
            ],
        )
    case(
        "whole read failure does not duplicate every unknown marker",
        [
            up(),
            read("0+0x3"),
            *[known("0+0x3", control=c, scope=s) for c, s in sorted(KNOWN_LABELS)],
        ],
        [
            ("1m", "SafetyControlUnknown", []),
            (
                "1m",
                "SafetyStateReadUnavailable",
                [expected("SafetyStateReadUnavailable")],
            ),
        ],
    )
    case(
        "configured target down uses availability alert",
        [
            up("0+0x3"),
            read("1+0x3"),
            known("0+0x3", control="kill_switch", scope="global"),
        ],
        [
            ("1m", "APIDown", [expected("APIDown")]),
            ("1m", "SafetyStateReadUnavailable", []),
            ("1m", "SafetyControlUnknown", []),
        ],
    )
    case(
        "known default auto off retains five minute warning",
        [
            up(),
            read(),
            known("1+0x12", control="auto_trading", scope="global"),
            series("cashguard_auto_trading_enabled", "0+0x12"),
        ],
        [
            ("4m", "AutoTradingDisabled", []),
            ("5m", "AutoTradingDisabled", [expected("AutoTradingDisabled")]),
        ],
    )
    case(
        "missing or enabled auto is not known disabled",
        [
            up(),
            read(),
            known("0+0x12", control="auto_trading", scope="global"),
            series("cashguard_auto_trading_enabled", "0+0x12"),
        ],
        [("6m", "AutoTradingDisabled", [])],
    )
    case(
        "known auto on does not fire from a zero bool sample",
        [
            up(),
            read(),
            known("1+0x12", control="auto_trading", scope="global"),
            series("cashguard_auto_trading_enabled", "1+0x12"),
        ],
        [("6m", "AutoTradingDisabled", [])],
    )
    case(
        "recent event expires from stored event time",
        [up(), read(), stamp("0+0x12")],
        [
            ("0m", "KillSwitchActivated", [expected("KillSwitchActivated")]),
            ("4m59s", "KillSwitchActivated", [expected("KillSwitchActivated")]),
            ("5m", "KillSwitchActivated", []),
            ("6m", "KillSwitchActivated", []),
            ("1m", "KillSwitchActive", []),
        ],
    )
    case(
        "future event is not recent evidence yet",
        [up(), read(), stamp("600+0x12")],
        [("0m", "KillSwitchActivated", []), ("1m", "KillSwitchActivated", [])],
    )
    case(
        "old event first observed after long delay stays quiet",
        [
            up(),
            read(),
            stamp("_ _ _ _ _ _ _ _ _ _ 0 0 0"),
        ],
        [("10m", "KillSwitchActivated", []), ("12m", "KillSwitchActivated", [])],
    )
    case(
        "old history reappears after stale without becoming new",
        [
            up(),
            read(),
            stamp("0 stale _ _ _ _ _ _ _ _ 0 0 0"),
        ],
        [
            ("0m", "KillSwitchActivated", [expected("KillSwitchActivated")]),
            ("1m", "KillSwitchActivated", []),
            ("10m", "KillSwitchActivated", []),
        ],
    )
    case(
        "recent history reappearance cannot restart its age",
        [
            up(),
            read(),
            stamp("0 stale _ 0 0 0 0"),
        ],
        [
            ("1m", "KillSwitchActivated", []),
            ("3m", "KillSwitchActivated", [expected("KillSwitchActivated")]),
            ("5m", "KillSwitchActivated", []),
        ],
    )
    case(
        "missing sample differs from explicit stale marker",
        [
            up(),
            read(),
            stamp("0 _ _ _ _ _ _"),
        ],
        [
            ("1m", "KillSwitchActivated", [expected("KillSwitchActivated")]),
            ("5m", "KillSwitchActivated", []),
        ],
    )
    case(
        "event replicas retain independent instance labels",
        [
            up(),
            read(),
            stamp("0+0x3"),
            up(instance="api-b"),
            read(instance="api-b"),
            stamp("0+0x3", instance="api-b"),
        ],
        [
            (
                "1m",
                "KillSwitchActivated",
                [
                    expected("KillSwitchActivated"),
                    expected("KillSwitchActivated", "api-b"),
                ],
            )
        ],
    )
    case(
        "history absent during unknown read is not resumed proof",
        [
            up(),
            read("1 0 0 1"),
            stamp("0 stale _ 0"),
        ],
        [
            ("1m", "KillSwitchActivated", []),
            (
                "2m",
                "SafetyStateReadUnavailable",
                [expected("SafetyStateReadUnavailable")],
            ),
            ("3m", "SafetyStateReadUnavailable", []),
            ("3m", "KillSwitchActivated", [expected("KillSwitchActivated")]),
        ],
    )
    return {
        "rule_files": ["/qualification/alerts.yml"],
        "evaluation_interval": "15s",
        "tests": cases,
    }


async def scrape(case: str) -> dict:
    # Never read a developer dotenv or use inherited app/broker credentials.
    with patch.dict(os.environ, SYNTHETIC, clear=True):
        from tests import network_guard

        network_guard.install()
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from prometheus_client.parser import text_string_to_metric_families
        from app.api import metrics, metrics_state

        states = {
            "known": metrics_state.SafetyState(
                metrics_state.Controls(True, False),
                {
                    "t212": metrics_state.Controls(False, False, False),
                    "kraken": metrics_state.Controls(True, False, True),
                },
                datetime.fromtimestamp(100, UTC),
            ),
            "failed": None,
            "missing": metrics_state.SafetyState(metrics_state.Controls()),
            "partial": metrics_state.SafetyState(
                metrics_state.Controls(False, False),
                {"t212": metrics_state.Controls(False, False, False)},
            ),
        }
        app = FastAPI()
        app.include_router(metrics.router)
        factory = object()
        app.dependency_overrides[metrics.get_metrics_session_factory] = lambda: factory
        with patch.object(
            metrics_state,
            "scrape_safety_state",
            new=AsyncMock(return_value=states[case]),
        ) as reader:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://fixture"
            ) as client:
                response = await client.get("/metrics")
            reader.assert_awaited_once_with(factory)
        assert response.status_code == 200
        return {
            (sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for family in text_string_to_metric_families(response.text)
            for sample in family.samples
            if sample.name.startswith("cashguard_")
        }


class AlertRuleTruth(unittest.TestCase):
    def test_actual_handler_uses_dependency_and_authoritative_state(self):
        samples = asyncio.run(scrape("known"))
        self.assertEqual(samples[("cashguard_kill_switch_active", ())], 1)
        self.assertEqual(samples[("cashguard_auto_trading_enabled", ())], 0)
        self.assertEqual(
            samples[("cashguard_kill_switch_last_activation_timestamp_seconds", ())],
            100,
        )
        self.assertEqual(
            samples[("cashguard_venue_kill_switch_active", (("venue", "kraken"),))], 1
        )
        known = {
            tuple(dict(labels)[k] for k in ("control", "scope")): value
            for (name, labels), value in samples.items()
            if name == "cashguard_safety_state_known"
        }
        self.assertEqual(set(known), KNOWN_LABELS)
        self.assertEqual(set(known.values()), {1})
        self.assertFalse({name for name, _ in samples} & UNSUPPORTED)

    def test_unknown_read_and_missing_rows_never_become_zero_values(self):
        for case, read_ok in (("failed", 0), ("missing", 1)):
            with self.subTest(case=case):
                samples = asyncio.run(scrape(case))
                self.assertEqual(
                    samples[("cashguard_safety_state_read_ok", ())], read_ok
                )
                known = {
                    tuple(dict(labels)[k] for k in ("control", "scope")): value
                    for (name, labels), value in samples.items()
                    if name == "cashguard_safety_state_known"
                }
                self.assertEqual(set(known), KNOWN_LABELS)
                self.assertEqual(set(known.values()), {0})
                self.assertFalse(
                    {name for name, _ in samples}
                    & (
                        SAFETY_METRICS
                        - {
                            "cashguard_safety_state_read_ok",
                            "cashguard_safety_state_known",
                        }
                    )
                )
                self.assertFalse({name for name, _ in samples} & UNSUPPORTED)

    def test_missing_venue_stays_unknown_without_changing_other_scopes(self):
        samples = asyncio.run(scrape("partial"))
        for control in ("kill_switch", "auto_trading", "degraded_mode"):
            for scope, expected_value in (("kraken", 0), ("t212", 1)):
                labels = tuple(sorted({"control": control, "scope": scope}.items()))
                self.assertEqual(
                    samples[("cashguard_safety_state_known", labels)], expected_value
                )
        self.assertFalse(
            any(dict(labels).get("venue") == "kraken" for _, labels in samples)
        )

    def test_active_rules_do_not_claim_unproduced_account_or_counter_data(self):
        rules = [
            r
            for group in yaml.safe_load(RULES.read_text())["groups"]
            for r in group["rules"]
        ]
        names = {r["alert"] for r in rules}
        self.assertTrue(set(SUMMARIES) <= names)
        self.assertFalse({"LowAvailableCash", "HighOpenPositions"} & names)
        for rule in rules:
            self.assertFalse(
                set(re.findall(r"cashguard_[a-z_]+", rule["expr"])) & UNSUPPORTED
            )
            if rule["alert"] in SUMMARIES:
                self.assertNotIn("bool", rule["expr"].split())

    def test_dashboard_is_instant_unknown_or_explicitly_unavailable(self):
        panels = {
            p["id"]: p
            for p in json.loads(
                (ROOT / "infra/grafana/dashboards/cashguard.json").read_text()
            )["panels"]
        }
        for panel_id in (2, 3, 4):
            self.assertEqual(panels[panel_id]["type"], "text")
            self.assertFalse(panels[panel_id].get("targets"))
            self.assertIn("unavailable", panels[panel_id]["options"]["content"].lower())
        for panel_id in (1, 5):
            self.assertEqual(
                panels[panel_id]["fieldConfig"]["defaults"]["noValue"], "UNKNOWN"
            )
            self.assertEqual(
                panels[panel_id]["options"]["reduceOptions"]["calcs"], ["last"]
            )
        for panel_id in (1, 5, 9, 13, 14):
            for target in panels[panel_id]["targets"]:
                self.assertIs(target["instant"], True)
                self.assertIs(target["range"], False)
                self.assertNotIn("or vector(0)", target["expr"])
        self.assertIn(
            "cashguard_safety_state_read_ok", panels[13]["targets"][0]["expr"]
        )
        self.assertIn("cashguard_safety_state_known", panels[14]["targets"][0]["expr"])
        self.assertIn(
            "cashguard_kill_switch_last_activation_timestamp_seconds",
            panels[9]["targets"][0]["expr"],
        )
        self.assertIn("* 1000", panels[9]["targets"][0]["expr"])
        for panel in panels.values():
            for target in panel.get("targets", []):
                self.assertFalse(
                    set(re.findall(r"cashguard_[a-z_]+", target["expr"])) & UNSUPPORTED
                )

    def test_real_promtool(self):
        compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
        image = compose["services"]["prometheus"]["image"]
        self.assertEqual(image, "prom/prometheus:v3.1.0")

        def docker(*args, check=True):
            result = subprocess.run(
                ["docker", *args], capture_output=True, text=True, timeout=90
            )
            if check and result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            return result

        metadata = json.loads(docker("image", "inspect", image).stdout)[0]
        image_id = metadata["Id"]
        run = "cg-monitor-contract-" + uuid.uuid4().hex[:12]
        label = "cashguard.run=" + run
        with tempfile.TemporaryDirectory(prefix=run) as folder:
            path = Path(folder)
            # Public source/synthetic copies avoid host checkout600 versus image65534.
            rule_copy = path / "alerts.yml"
            rule_copy.write_bytes(RULES.read_bytes())
            fixture = path / "tests.json"
            fixture.write_text(json.dumps(fixture_tests()))
            for file in (rule_copy, fixture):
                file.chmod(0o444)
            command = [
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--label",
                label,
                "--entrypoint",
                "/bin/promtool",
                "--tmpfs",
                "/prometheus:uid=65534,gid=65534",
                "-v",
                f"{rule_copy}:/qualification/alerts.yml:ro",
                "-v",
                f"{fixture}:/qualification/tests.json:ro",
                image_id,
            ]
            version = docker(*command, "--version").stdout
            self.assertIn("version 3.1.0", version)
            docker(*command, "check", "rules", "/qualification/alerts.yml")
            evaluated = docker(*command, "test", "rules", "/qualification/tests.json")
            print(evaluated.stdout)
            print(
                json.dumps(
                    {
                        "image": image,
                        "image_id": image_id,
                        "digests": metadata["RepoDigests"],
                        "version": version.strip(),
                        "rule_sha256": hashlib.sha256(RULES.read_bytes()).hexdigest(),
                        "evaluation_assertions": sum(
                            len(c["alert_rule_test"]) for c in fixture_tests()["tests"]
                        ),
                    }
                )
            )
            self.assertEqual(
                docker("ps", "-aq", "--filter", "label=" + label).stdout.strip(), ""
            )


if __name__ == "__main__":
    unittest.main()
