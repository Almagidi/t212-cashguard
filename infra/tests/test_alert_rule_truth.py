"""Collector exposition and real, source-pinned promtool evaluation. Synthetic only."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "infra/prometheus/alerts.yml"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, capture_output=True, text=True, check=False, timeout=90
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result


def fixture_tests() -> dict:
    positions = "cashguard_open_positions_count"
    counter = 'cashguard_kill_switch_activations_total{actor="infra_fixture"}'
    position_alert = {
        "exp_labels": {"severity": "warning", "team": "trading"},
        "exp_annotations": {
            "summary": "Reported open-position count is high",
            "description": "The scraped process reports 9 open positions, above the threshold of 8. Verify current positions in the operator view.",
        },
    }
    event_alert = {
        "exp_labels": {
            "severity": "critical",
            "team": "trading",
            "actor": "infra_fixture",
        },
        "exp_annotations": {
            "summary": "Recent kill-switch activation observed",
            "description": "The scraped activation counter increased in the last five minutes. This historical event does not establish current trading state. Actor: infra_fixture. Check the operator view.",
        },
    }

    def case(name: str, series: str, values: str, alert: str, times: list) -> dict:
        return {
            "name": name,
            "interval": "1m",
            "input_series": [{"series": series, "values": values}],
            "alert_rule_test": [
                {"eval_time": when, "alertname": alert, "exp_alerts": expected}
                for when, expected in times
            ],
        }

    return {
        "rule_files": ["/qualification/alerts.yml"],
        "evaluation_interval": "15s",
        "tests": [
            case(
                "positions pending firing recovery",
                positions,
                "9 9 9 8 0",
                "HighOpenPositions",
                [("1m", []), ("2m", [position_alert]), ("3m", []), ("4m", [])],
            ),
            case(
                "positions threshold and zero",
                positions,
                "8 8 8 0 0",
                "HighOpenPositions",
                [("2m", []), ("4m", [])],
            ),
            case(
                "stale position sample is not healthy proof",
                positions,
                "9 stale _ _",
                "HighOpenPositions",
                [("2m", [])],
            ),
            case(
                "activation increment expires",
                counter,
                "0 1 1 1 1 1 1 1",
                "KillSwitchActivated",
                [("1m", [event_alert]), ("7m", [])],
            ),
            case(
                "static lifetime event is not recent",
                counter,
                "5 5 5 5 5 5 5 5",
                "KillSwitchActivated",
                [("2m", []), ("7m", [])],
            ),
            case(
                "counter reset alone is not activation",
                counter,
                "5 5 0 0 0",
                "KillSwitchActivated",
                [("2m", []), ("3m", [])],
            ),
            case(
                "increment after reset",
                counter,
                "5 5 0 1 1",
                "KillSwitchActivated",
                [("3m", [event_alert])],
            ),
            case(
                "first sample cannot establish increment",
                counter,
                "1 1 1 1",
                "KillSwitchActivated",
                [("2m", [])],
            ),
            case(
                "zero traffic", counter, "0 0 0 0", "KillSwitchActivated", [("2m", [])]
            ),
            {
                "name": "absent telemetry is not a current-state assertion",
                "interval": "1m",
                "input_series": [],
                "alert_rule_test": [
                    {"eval_time": "7m", "alertname": name, "exp_alerts": []}
                    for name in ["HighOpenPositions", "KillSwitchActivated"]
                ],
            },
        ],
    }


class AlertRuleTruth(unittest.TestCase):
    def test_actual_collector_exposition(self) -> None:
        """Call only the metrics module in this process, never financial services."""
        from prometheus_client.parser import text_string_to_metric_families

        path = ROOT / "apps/api/app/api/metrics.py"
        spec = importlib.util.spec_from_file_location("infra_collector_fixture", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.PROMETHEUS_AVAILABLE)
        module.update_account_metrics(0, 0, 9)
        module.record_kill_switch("infra_fixture")
        module.record_kill_switch_deactivated()
        body = asyncio.run(module.metrics()).body.decode()
        samples = {
            sample.name: sample.value
            for family in text_string_to_metric_families(body)
            for sample in family.samples
            if sample.name.startswith("cashguard_")
        }
        self.assertEqual(samples["cashguard_open_positions_count"], 9)
        self.assertNotIn("cashguard_account_open_positions", samples)
        self.assertEqual(samples["cashguard_kill_switch_activations_total"], 1)
        self.assertEqual(samples["cashguard_kill_switch_active"], 0)
        print(
            json.dumps(
                {
                    "collector_source_sha256": hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest(),
                    "scope": "manual module-only synthetic helper calls; no financial runtime emission proof",
                }
            )
        )

    def test_real_promtool(self) -> None:
        compose = (ROOT / "docker-compose.prod.yml").read_text()
        match = re.search(r"(?m)^  prometheus:\n    image: ([^\s]+)", compose)
        self.assertIsNotNone(match)
        assert match is not None
        image_tag = match.group(1)
        metadata = json.loads(run("docker", "image", "inspect", image_tag).stdout)[0]
        image_id = metadata["Id"]
        name = "cg-b-rule-" + uuid.uuid4().hex[:12]
        label = "cashguard.run=" + name
        with tempfile.TemporaryDirectory(prefix=name) as temporary:
            fixture = Path(temporary) / "tests.json"
            fixture.write_text(json.dumps(fixture_tests()))
            fixture.chmod(0o644)
            command = [
                "docker",
                "run",
                "--name",
                name,
                "--network",
                "none",
                "--label",
                label,
                "--entrypoint",
                "/bin/promtool",
                "--tmpfs",
                "/prometheus:uid=65534,gid=65534",
                "-v",
                f"{RULES}:/qualification/alerts.yml:ro",
                "-v",
                f"{fixture}:/qualification/tests.json:ro",
                image_id,
            ]
            try:
                created = run(*command, "check", "rules", "/qualification/alerts.yml")
                inspection = json.loads(run("docker", "inspect", name).stdout)[0]
                self.assertEqual(inspection["Image"], image_id)
                self.assertEqual(inspection["Config"]["Labels"]["cashguard.run"], name)
                self.assertFalse(inspection["HostConfig"]["PortBindings"])
                self.assertTrue(
                    all(mount["Type"] != "volume" for mount in inspection["Mounts"])
                )
                run("docker", "rm", name)
                evaluated = run(*command, "test", "rules", "/qualification/tests.json")
                print(created.stdout + evaluated.stdout)
                print(
                    json.dumps(
                        {
                            "image": image_tag,
                            "image_id": image_id,
                            "digests": metadata["RepoDigests"],
                            "architecture": metadata["Architecture"],
                            "rule_sha256": hashlib.sha256(
                                RULES.read_bytes()
                            ).hexdigest(),
                            "evaluation_assertions": sum(
                                len(case["alert_rule_test"])
                                for case in fixture_tests()["tests"]
                            ),
                        }
                    )
                )
            finally:
                found = run(
                    "docker", "ps", "-aq", "--filter", "name=^/" + name + "$"
                ).stdout.strip()
                if found:
                    actual = json.loads(run("docker", "inspect", name).stdout)[0]
                    if actual["Config"]["Labels"].get("cashguard.run") != name:
                        raise RuntimeError(
                            "Refusing cleanup of foreign rule-test container"
                        )
                    run("docker", "rm", "-f", name)
                self.assertEqual(
                    run(
                        "docker", "ps", "-aq", "--filter", "label=" + label
                    ).stdout.strip(),
                    "",
                )


if __name__ == "__main__":
    unittest.main()
