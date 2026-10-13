"""Production network contracts, rendered by Docker Compose using synthetic values.

Run: python3 -m unittest discover -s infra/tests -p 'test_runtime_boundaries.py' -v
No services are started by this suite. Runtime probes are a separate explicit command.
"""

import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = {
    "POSTGRES_PASSWORD": "synthetic-pg-runtime-only",
    "REDIS_PASSWORD": "synthetic-redis-runtime-only",
    "SECRET_KEY": "synthetic-session-key-runtime-only",
    "MASTER_KEY": "synthetic-master-key-runtime-only",
    "GRAFANA_PASSWORD": "synthetic-grafana-runtime-only",
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


class RuntimeBoundaryContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = render_compose()
        cls.services = cls.config["services"]

    def networks(self, service):
        return set(self.services[service]["networks"])

    def test_nginx_can_resolve_every_configured_upstream(self):
        # Grafana is resolved at nginx config load, not only when a user visits it.
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

    def test_grafana_is_started_before_nginx(self):
        self.assertIn("grafana", self.services["nginx"]["depends_on"])

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
        self.assertEqual(
            env["GF_SECURITY_ADMIN_PASSWORD"], SYNTHETIC["GRAFANA_PASSWORD"]
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


if __name__ == "__main__":
    unittest.main()
