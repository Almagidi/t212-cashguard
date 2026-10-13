"""Run a copied diagnostic script with synthetic .env and command stubs only."""

import os
from pathlib import Path
import shutil
import shlex
import sys
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DiagnosticDotenvTests(unittest.TestCase):
    def run_diagnostic(self, dotenv, with_venv=True, real_config=False, extra_env=None):
        with tempfile.TemporaryDirectory(prefix="cg-b-diagnostic-") as folder:
            project = Path(folder)
            (project / "launcher").mkdir()
            shutil.copy(ROOT / "launcher/diagnose.sh", project / "launcher/diagnose.sh")
            marker = project / "executed-env"
            if dotenv is not None:
                (project / ".env").write_text(
                    dotenv.replace("MARKER_PATH", str(marker))
                )
            commands = project / "commands"
            commands.mkdir()
            for command in ("pg_isready", "lsof", "curl"):
                stub = commands / command
                stub.write_text("#!/bin/sh\nexit 1\n")
                stub.chmod(0o700)
            if with_venv:
                venv = project / "venv/bin"
                venv.mkdir(parents=True)
                python = venv / "python"
                python.write_text(
                    '#!/bin/sh\nif [ "$1" = "--version" ]; then\n echo "Python synthetic"\nelse\n echo "exists: True"\n echo "app_mode: mock"\nfi\n'
                )
                if real_config:
                    config = project / "apps/api/app/core"
                    config.mkdir(parents=True)
                    shutil.copy(
                        ROOT / "apps/api/app/core/config.py", config / "config.py"
                    )
                    (project / "docker-compose.yml").touch()
                    python.write_text(
                        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n'
                    )
                python.chmod(0o700)
                pip = venv / "pip"
                pip.write_text('#!/bin/sh\necho "Version: synthetic"\n')
                pip.chmod(0o700)
            env = {
                "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                "HOME": str(project),
            }
            env.update(extra_env or {})
            result = subprocess.run(
                ["bash", str(project / "launcher/diagnose.sh")],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result, marker.exists()

    def test_dotenv_command_substitution_is_not_executed(self):
        result, executed = self.run_diagnostic(
            "SECRET_KEY=synthetic-diagnostic-secret\nADMIN_EMAIL=admin@example.invalid\n"
            'ADMIN_PASSWORD=synthetic-password\nAPP_NAME="$(touch MARKER_PATH)"\n'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(executed, "Diagnostics executed shell syntax from dotenv data")
        self.assertIn("Config / Settings", result.stdout)
        self.assertIn("exists: True", result.stdout)
        self.assertIn("Skipping auth test", result.stdout)
        self.assertNotIn("synthetic-password", result.stdout + result.stderr)
        self.assertNotIn("synthetic-diagnostic-secret", result.stdout + result.stderr)

    def test_plain_dotenv_preserves_configuration_check(self):
        result, executed = self.run_diagnostic(
            "SECRET_KEY=synthetic-secret\nADMIN_EMAIL=admin@example.invalid\n"
            "ADMIN_PASSWORD=synthetic-password\nAPP_MODE=mock\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(executed)
        self.assertIn(".env loaded by pydantic-settings", result.stdout)
        self.assertIn("Diagnostics complete.", result.stdout)

    def test_real_pydantic_loads_dotenv_without_shell_and_preserves_env_precedence(
        self,
    ):
        dotenv = (
            "SECRET_KEY=synthetic-diagnostic-secret-key-xxxxxxxx\n"
            "MASTER_KEY=synthetic-diagnostic-master-key-xxxxxxxx\n"
            'ADMIN_EMAIL=admin@example.invalid\nADMIN_PASSWORD="synthetic=pass#word"\n'
            'APP_MODE=mock\nAPP_NAME="$(touch MARKER_PATH)"\n'
        )
        for inherited, expected in (({}, "mock"), ({"APP_MODE": "paper"}, "paper")):
            with self.subTest(expected=expected):
                result, executed = self.run_diagnostic(
                    dotenv, real_config=True, extra_env=inherited
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(executed)
                self.assertIn("exists: True", result.stdout)
                self.assertIn("app_mode: " + expected, result.stdout)
                self.assertIn("admin_email: admin@example.invalid", result.stdout)
                self.assertNotIn("synthetic=pass#word", result.stdout + result.stderr)

    def test_missing_dotenv_and_missing_venv_are_reported(self):
        result, executed = self.run_diagnostic(None, with_venv=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(executed)
        self.assertIn(".env file NOT FOUND", result.stdout)
        self.assertIn("Python venv not found", result.stdout)
        self.assertIn("Skipping auth test", result.stdout)


if __name__ == "__main__":
    unittest.main()
