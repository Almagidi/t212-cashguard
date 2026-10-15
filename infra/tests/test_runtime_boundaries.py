"""Production network contracts, rendered by Docker Compose using synthetic values.

Run: python3 -m unittest discover -s infra/tests -p 'test_runtime_boundaries.py' -v
No services are started by this suite. Runtime probes are a separate explicit command.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
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

    def test_missing_public_origin_does_not_block_restoring_application(self):
        result = render_compose(missing="PUBLIC_ORIGIN")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(result.stdout)["services"]["grafana"]["environment"][
                "GF_SERVER_ROOT_URL"
            ],
            "/grafana/",
        )

    def test_nginx_request_time_dns_and_auth_limits(self):
        source = (ROOT / "infra/nginx/nginx.conf").read_text()
        route = source.split("location /grafana/ {", 1)[1].split("}", 1)[0]
        for required in (
            "resolver 127.0.0.11 valid=10s ipv6=off;",
            "resolver_timeout 3s;",
            "set $grafana_upstream http://grafana:3000;",
            "proxy_pass $grafana_upstream;",
            "limit_req zone=grafana_login burst=3 nodelay;",
            "limit_req zone=grafana burst=60 nodelay;",
            "limit_req_status 429;",
        ):
            self.assertIn(required, route)
        self.assertIn("~^/grafana/login/?$ $binary_remote_addr;", source)
        self.assertIn("zone=grafana_login:1m rate=5r/m;", source)
        self.assertNotIn("server grafana:", source)

    def test_basic_auth_cannot_bypass_login_limit(self):
        self.assertEqual(
            self.services["grafana"]["environment"]["GF_AUTH_BASIC_ENABLED"], "false"
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
            if key in ("APP_MODE", "LIVE_TRADING_ENABLED", "PUBLIC_ORIGIN"):
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


class IdentityOutputContracts(unittest.TestCase):
    def test_fixture_docker_client_does_not_receive_password_in_argv(self):
        from runtime_probe import docker

        secret = SYNTHETIC["GRAFANA_PASSWORD"]
        with patch(
            "runtime_probe.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as runner:
            docker("run", "-e", "GF_SECURITY_ADMIN_PASSWORD=" + secret, "fixture-image")
        invocation = runner.call_args.args[0]
        self.assertFalse(
            any(secret in item for item in invocation),
            "Fixture credential in Docker client argv",
        )
        self.assertTrue(
            runner.call_args.kwargs["env"]["GF_SECURITY_ADMIN_PASSWORD"] == secret,
            "Literal fixture environment changed",
        )

    def test_complete_supported_cli_identity_output(self):
        # Exercise the exact source shell gate, not a copied parser. These are
        # CLI-output fixtures; API normalization is a separate runtime fact.
        source = (ROOT / "infra/grafana/start-grafana.sh").read_text()
        start = source.index("# Only the known single-admin identity")
        end = source.index("\nreset 1\nchanged\n", start)
        gate = "fail() { exit 1; };\n" + source[start:end]
        heading = "Please try to run the command again specifying a user-id (--user-id) from the list below:"
        fixtures = [
            ("\t Username: admin ID: 1\n", True),
            ("\t Username: admin ID: 1\nx ID: 1\n", False),
            (
                "\t Username: admin ID: 1\n\t Username: Please try to run the command again specifying a user-id ID: 2\n",
                False,
            ),
            ("\t Username: " + heading + "\nUsername: admin ID: 1\n", False),
            ("\t Username: admin ID: 2\n", False),
            ("\t Username: admin ID: 1\n\t Username: admin ID: 1\n", False),
            ("", False),
        ]
        with tempfile.TemporaryDirectory(prefix="cg-identity-output-") as temporary:
            private = Path(temporary)
            for rows, accepted in fixtures:
                with self.subTest(accepted=accepted, rows=rows):
                    result_file = private / "result"
                    result_file.write_text(
                        "Admin user cannot be found\n" + heading + "\n" + rows
                    )
                    result_file.chmod(0o600)
                    result = subprocess.run(
                        ["sh", "-c", gate],
                        env={
                            "PATH": os.environ["PATH"],
                            "LC_ALL": "C",
                            "private_dir": str(private),
                        },
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(result.returncode == 0, accepted)


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

    def test_container_removal_failure_does_not_skip_owned_volume_cleanup(self):
        from runtime_probe import cleanup_owned

        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            if "inspect" in args:
                metadata = {"Labels": {"cashguard.run": "test-run"}}
                if args[0] == "container":
                    metadata = {"Config": metadata}
                return subprocess.CompletedProcess(args, 0, json.dumps([metadata]), "")
            if args[:2] == ("rm", "-f"):
                return subprocess.CompletedProcess(args, 1, "", "injected failure")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("runtime_probe.docker", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                cleanup_owned(["container"], [], "test-run", ["volume"])
        self.assertIn(("volume", "rm", "volume"), calls)
        self.assertIn(
            ("volume", "ls", "-q", "--filter", "label=cashguard.run=test-run"), calls
        )


if __name__ == "__main__":
    unittest.main()
