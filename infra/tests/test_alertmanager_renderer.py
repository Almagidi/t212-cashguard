"""Synthetic config checks with the actual Alertmanager image, no network.

Requires pre-pulled prom/alertmanager:v0.27.0 and Docker. Never sends alerts.
"""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "prom/alertmanager:v0.27.0"


class AlertmanagerRendererTests(unittest.TestCase):
    def render(self, password="synthetic-password", setup="", template=None):
        name = "cg-b-render-" + uuid.uuid4().hex[:12]
        env = {
            "SMTP_HOST": "smtp.example.invalid",
            "SMTP_PORT": "587",
            "SMTP_USER": "synthetic-user",
            "SMTP_PASSWORD": password,
            "ALERT_EMAIL_FROM": "alerts@example.invalid",
            "ALERT_EMAIL_TO": "operator@example.invalid",
            "ALERTMANAGER_CHECK_CONFIG": "1",
        }
        command = [
            "docker",
            "run",
            "--name",
            name,
            "--network",
            "none",
            "--pull",
            "never",
            "--tmpfs",
            "/alertmanager",
            "--entrypoint",
            "sh",
        ]
        for key, value in env.items():
            command.extend(["-e", key + "=" + value])
        for path in ("render-alertmanager.sh", "alertmanager.yml"):
            command.extend(
                [
                    "-v",
                    str(
                        template
                        if path == "alertmanager.yml" and template
                        else ROOT / "infra/prometheus" / path
                    )
                    + ":/etc/alertmanager/"
                    + path
                    + ":ro",
                ]
            )
        # Inspect only synthetic output, which remains inside this owned container.
        script = 'sh /etc/alertmanager/render-alertmanager.sh; rc=$?; for f in $(find /tmp -name alertmanager.yml); do stat -c "MODE:%a" "$f"; cat "$f"; done; exit "$rc"'
        try:
            result = subprocess.run(
                command + [IMAGE, "-c", setup + script],
                capture_output=True,
                text=True,
                timeout=30,
            )
            inspected = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            mounts = json.loads(inspected.stdout)[0]["Mounts"]
            self.assertFalse(
                any(mount["Type"] == "volume" for mount in mounts),
                "Renderer fixture allocated an anonymous volume",
            )
            return result
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=15
            )

    def test_ordinary_config_validates_with_private_rendered_file(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, "Synthetic amtool check failed")
        self.assertIn("MODE:600", result.stdout)
        self.assertNotIn("MODE:644", result.stdout)

    def test_existing_tmp_config_symlink_is_not_followed(self):
        setup = (
            "printf synthetic-existing-file > /tmp/keep-config; "
            "ln -s /tmp/keep-config /tmp/alertmanager.yml; "
        )
        result = self.render(setup=setup)
        self.assertEqual(result.returncode, 0)
        self.assertIn("synthetic-existing-file", result.stdout)
        self.assertIn("MODE:600", result.stdout)

    def test_password_punctuation_and_template_text_are_literal(self):
        for value in (
            "synthetic'quote",
            "synthetic|pipe",
            "synthetic&slash/\\",
            "${ALERT_EMAIL_TO}",
        ):
            with self.subTest(value_kind=value[:9]):
                result = self.render(value)
                self.assertEqual(
                    result.returncode, 0, "Synthetic credential broke rendering"
                )
                # YAML single quotes represent an embedded quote by doubling it.
                self.assertIn(
                    "smtp_auth_password: '" + value.replace("'", "''") + "'",
                    result.stdout,
                )

    def test_invalid_template_is_rejected_by_actual_amtool(self):
        with tempfile.TemporaryDirectory(prefix="cg-b-invalid-template-") as directory:
            template = Path(directory) / "invalid.yml"
            template.write_text("global: [synthetic-unterminated\n")
            result = self.render(template=template)
            self.assertEqual(result.returncode, 1)
            self.assertIn("FAILED", result.stdout + result.stderr)
            self.assertIn("yaml:", result.stdout + result.stderr)

    def test_multiline_input_is_rejected_without_echoing_it(self):
        for separator in ("\n", "\r", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=ascii(separator)):
                value = "synthetic-line-one" + separator + "synthetic-line-two"
                result = self.render(value)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "Alertmanager renderer refuses multiline SMTP/address settings",
                    result.stderr,
                )
                self.assertNotIn("synthetic-line-one", result.stdout + result.stderr)
                self.assertNotIn("synthetic-line-two", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
