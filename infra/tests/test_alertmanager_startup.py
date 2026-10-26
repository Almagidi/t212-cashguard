"""Real source-derived Compose startup regression; synthetic internal-only Docker.

Run explicitly: python3 -m unittest discover -s infra/tests -p test_alertmanager_startup.py -v
Pre-pull prom/alertmanager:v0.27.0. No host ports, .env, SMTP or broker traffic.
"""

from pathlib import Path
import tempfile
import unittest

from alertmanager_probe import AlertmanagerProbe
from runtime_probe import docker
from test_runtime_boundaries import render_compose
from test_runtime_boundaries import ROOT


class AlertmanagerStartupTests(unittest.TestCase):
    def fixture(self, **kwargs):
        probe = AlertmanagerProbe(**kwargs)
        self.addCleanup(probe.close)
        probe.start()
        return probe

    def test_normal_compose_starts_private_pid1_and_stops_cleanly(self):
        p = self.fixture()
        p.wait_ready()
        p.assert_private_pid()
        # The source healthcheck must measure HTTP readiness, not mere PID existence.
        health = p.service["healthcheck"]
        self.assertEqual(
            health["test"],
            ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:9093/-/ready"],
        )
        docker("stop", "--time", "5", p.name)
        state = p.inspect()["State"]
        self.assertEqual(state["ExitCode"], 0)
        self.assertIn("Received SIGTERM", p.logs())
        self.assertFalse(p.ready())
        p.event("sigterm_clean_exit", exit=state["ExitCode"])
        docker("start", p.name)
        p.wait_ready()
        p.assert_private_pid()

    def test_invalid_template_and_missing_receiver_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="cg-b-n41-invalid-") as directory:
            for index, content in enumerate(
                (
                    "global: [unterminated\n",
                    "route:\n  receiver: absent\nreceivers: []\n",
                )
            ):
                template = Path(directory) / (str(index) + ".yml")
                template.write_text(content)
                p = self.fixture(template=template)
                p.wait_failed()
                self.assertIn("refuses invalid or unreadable configuration", p.logs())

    def test_malformed_smtp_is_rejected_without_secret_echo(self):
        p = self.fixture(
            environment={
                "SMTP_PASSWORD": "synthetic-private-first\nsynthetic-private-last"
            }
        )
        p.wait_failed()
        self.assertIn("refuses multiline", p.logs())
        self.assertNotIn("synthetic-private-first", p.logs())

    def test_unreadable_rendered_file_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="cg-b-n41-rendered-") as directory:
            renderer = Path(directory) / "renderer.sh"
            source = (ROOT / "infra/prometheus/render-alertmanager.sh").read_text()
            marker = "# Refuse configuration errors"
            self.assertIn(marker, source)
            renderer.write_text(
                source.replace(marker, 'chmod 000 "$rendered_config"\n' + marker)
            )
            p = self.fixture(renderer=renderer)
            p.wait_failed()
            self.assertIn("invalid or unreadable configuration", p.logs())

    def test_readonly_tmp_and_unwritable_storage_fail_closed(self):
        for destination in ("/tmp", "/alertmanager"):
            with self.subTest(destination=destination):

                def readonly(service):
                    service.setdefault("tmpfs", [])
                    service["tmpfs"] = [
                        v
                        for v in service["tmpfs"]
                        if not v.startswith(destination + ":")
                    ]
                    service["tmpfs"].append(destination + ":ro,mode=1777")
                    if destination == "/alertmanager":
                        service["volumes"] = [
                            v for v in service["volumes"] if v["target"] != destination
                        ]

                p = self.fixture(mutate=readonly)
                p.wait_failed()

    def test_missing_renderer_and_unreadable_template_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="cg-b-n41-prereq-") as directory:
            template = Path(directory) / "unreadable.yml"
            template.write_text("global: {}\n")
            template.chmod(0)
            try:
                for unreadable in (False, True):

                    def missing(service):
                        service["volumes"] = [
                            v
                            for v in service["volumes"]
                            if v["target"] != "/etc/alertmanager/render-alertmanager.sh"
                        ]

                    if unreadable:
                        # Docker Desktop itself refuses an unreadable host bind;
                        # on native Linux it reaches the renderer and exits.
                        p = AlertmanagerProbe(template=template)
                        self.addCleanup(p.close)
                        try:
                            p.start()
                        except RuntimeError as error:
                            self.assertIn("permission denied", str(error))
                            self.assertFalse(p.inspect()["State"]["Running"])
                            p.event("unreadable_host_bind_refused")
                        else:
                            p.wait_failed()
                    else:
                        p = self.fixture(mutate=missing)
                        p.wait_failed()
            finally:
                template.chmod(0o600)

    def test_application_ingress_does_not_depend_on_alertmanager(self):
        services = render_compose()["services"]
        for name in ("nginx", "api", "web"):
            self.assertNotIn("alertmanager", services[name].get("depends_on", {}))


if __name__ == "__main__":
    unittest.main()
