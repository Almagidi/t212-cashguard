"""Production network contracts, rendered by Docker Compose using synthetic values.

Run: python3 -m unittest discover -s infra/tests -p 'test_runtime_boundaries.py' -v
No services are started by this suite. Runtime probes are a separate explicit command.
"""

import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = {
    "POSTGRES_PASSWORD": "synthetic-pg-runtime-only",
    "REDIS_PASSWORD": "synthetic-redis-runtime-only",
    "SECRET_KEY": "synthetic-session-key-runtime-only",
    "MASTER_KEY": "synthetic-master-key-runtime-only",
    "PUBLIC_ORIGIN": "https://nginx",
    "GRAFANA_PASSWORD": "synthetic-grafana-'\"\\${SMTP_USER}$(printf nope)",
    "APP_MODE": "mock",
    "LIVE_TRADING_ENABLED": "false",
}


def render_compose(missing=None):
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT")
        if key in os.environ
    }
    env.update(SYNTHETIC)
    if missing:
        env.pop(missing)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            str(ROOT / "docker-compose.prod.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if missing:
        return result
    if result.returncode:
        raise RuntimeError("Synthetic Compose rendering failed (raw config withheld)")
    return json.loads(result.stdout)


def runtime_environment(service):
    """Compose serializes dollar signs doubled for safe re-parsing.

    Docker CLI receives the resulting runtime values rather than Compose JSON's
    escaped representation. A separate actual Compose-create control verifies this.
    """
    return {
        k: v.replace("$$", "$") if isinstance(v, str) else v
        for k, v in service["environment"].items()
    }


class RuntimeBoundaryContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = render_compose()
        cls.services = cls.config["services"]

    def networks(self, service):
        return set(self.services[service]["networks"])

    def test_nginx_can_resolve_every_configured_upstream(self):
        # Every intended upstream needs a network edge, including request-time DNS.
        for service in ("api", "web", "grafana"):
            with self.subTest(service=service):
                self.assertTrue(
                    self.networks("nginx") & self.networks(service), service
                )

    def test_ingress_does_not_join_datastore_network(self):
        for service in (
            "postgres",
            "redis",
            "prometheus",
            "alertmanager",
            "postgres-exporter",
            "redis-exporter",
        ):
            with self.subTest(service=service):
                self.assertFalse(
                    self.networks("nginx") & self.networks(service), service
                )

    def test_grafana_ingress_network_is_private_and_has_only_two_members(self):
        shared = self.networks("nginx") & self.networks("grafana")
        self.assertEqual(len(shared), 1)
        network = shared.pop()
        self.assertIs(self.config["networks"][network].get("internal", False), True)
        members = {name for name in self.services if network in self.networks(name)}
        self.assertEqual(members, {"nginx", "grafana"})
        self.assertNotIn("external", self.networks("grafana"))

    def test_nginx_startup_is_independent_of_monitoring(self):
        self.assertNotIn("grafana", self.services["nginx"]["depends_on"])

    def test_grafana_data_network_contains_only_grafana_and_prometheus(self):
        shared = self.networks("grafana") & self.networks("prometheus")
        self.assertEqual(len(shared), 1)
        network = shared.pop()
        self.assertIs(self.config["networks"][network].get("internal", False), True)
        self.assertEqual(
            {n for n in self.services if network in self.networks(n)},
            {"grafana", "prometheus"},
        )
        for service in (
            "postgres",
            "redis",
            "api",
            "web",
            "alertmanager",
            "postgres-exporter",
            "redis-exporter",
        ):
            self.assertFalse(self.networks("grafana") & self.networks(service), service)

    def test_grafana_preflight_is_in_normal_recipe(self):
        self.assertEqual(self.services["grafana"].get("tmpfs"), ["/tmp:mode=1777"])
        self.assertEqual(
            self.services["grafana"].get("entrypoint"),
            ["/bin/sh", "/etc/grafana/start-grafana.sh"],
        )
        self.assertEqual(
            self.services["grafana"]["environment"]["GF_SERVER_ROOT_URL"],
            "https://nginx/grafana/",
        )

    def test_only_nginx_publishes_host_ports(self):
        for name, service in self.services.items():
            with self.subTest(service=name):
                self.assertEqual(bool(service.get("ports")), name == "nginx")
        self.assertEqual(
            {p["target"] for p in self.services["nginx"]["ports"]}, {80, 443}
        )

    def test_grafana_requires_authentication(self):
        env = self.services["grafana"]["environment"]
        self.assertEqual(env["GF_AUTH_ANONYMOUS_ENABLED"], "false")
        self.assertEqual(env["GF_SECURITY_COOKIE_SECURE"], "true")
        self.assertEqual(
            env["GF_SECURITY_ADMIN_PASSWORD"],
            SYNTHETIC["GRAFANA_PASSWORD"].replace("$", "$$"),
        )

    def test_monitoring_edges_remain_available(self):
        for source, target in (
            ("grafana", "prometheus"),
            ("prometheus", "api"),
            ("prometheus", "alertmanager"),
            ("prometheus", "postgres-exporter"),
            ("prometheus", "redis-exporter"),
            ("postgres-exporter", "postgres"),
            ("redis-exporter", "redis"),
        ):
            with self.subTest(source=source, target=target):
                self.assertTrue(self.networks(source) & self.networks(target))

    def test_missing_required_secrets_refuse_rendering_without_echoing_values(self):
        for key in SYNTHETIC:
            if key in ("APP_MODE", "LIVE_TRADING_ENABLED"):
                continue
            with self.subTest(key=key):
                result = render_compose(missing=key)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)
                for sentinel in SYNTHETIC.values():
                    if sentinel in ("mock", "false"):
                        continue
                    self.assertNotIn(sentinel, result.stderr)
                self.assertEqual(result.stdout, "")


class CleanupFailureContracts(unittest.TestCase):
    def exercise_failure(self, mode):
        from runtime_probe import cleanup_owned

        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("container", "inspect"):
                label = "someone-else" if mode == "unowned" else "test-run"
                return subprocess.CompletedProcess(
                    args,
                    0,
                    json.dumps([{"Config": {"Labels": {"cashguard.run": label}}}]),
                    "",
                )
            if args[:2] == ("rm", "-f") and args[-1] == "second":
                if mode == "timeout":
                    raise subprocess.TimeoutExpired(args, 1)
                return subprocess.CompletedProcess(
                    args, 1, "", "simulated removal failure"
                )
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("runtime_probe.docker", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                cleanup_owned(["first", "second"], [], "test-run")
        if mode == "unowned":
            self.assertFalse(any(c[:2] == ("rm", "-f") for c in calls))
        else:
            self.assertIn(
                ("rm", "-f", "first"),
                calls,
                "One failure prevented later resource cleanup",
            )

    def test_cleanup_failure_is_reported_and_other_resources_are_attempted(self):
        self.exercise_failure("failed")

    def test_cleanup_timeout_is_reported_and_other_resources_are_attempted(self):
        self.exercise_failure("timeout")

    def test_cleanup_refuses_resources_without_this_run_label(self):
        self.exercise_failure("unowned")


if __name__ == "__main__":
    unittest.main()
