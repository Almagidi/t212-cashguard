"""Exercise the actual quickstart with synthetic commands, never a real .env/stack."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

# Absolute Python interpreter avoids depending on the stubbed python3 command.
COMMAND_STUB = r"""
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
log = Path(os.environ["TEST_COMMAND_LOG"])
entry = {"command": name, "args": args}
status = 0
if name == "docker":
    if args == ["--version"]:
        print("Docker version synthetic, build synthetic")
    elif args[:2] == ["compose", "up"]:
        status = int(os.environ["TEST_START_STATUS"])
    elif "pg_isready" in args:
        previous = [json.loads(line) for line in log.read_text().splitlines()]
        attempt = 1 + sum("pg_isready" in item["args"] for item in previous)
        tcp = any(args[i:i + 2] == ["-h", "127.0.0.1"] for i in range(len(args)))
        ready_after = int(os.environ["TEST_TCP_READY_AFTER"])
        ready = tcp and ready_after > 0 and attempt >= ready_after
        # The temporary initialization server already accepts socket probes.
        socket_ready = not tcp and os.environ["TEST_SOCKET_READY"] == "1"
        status = 0 if ready or socket_ready else int(os.environ["TEST_PROBE_STATUS"])
        entry.update(attempt=attempt, tcp=tcp, tcp_ready=ready)
    else:
        raise SystemExit("Unexpected Docker operation in synthetic quickstart")
elif name == "node":
    print("v24.0.0")
elif name == "python3":
    print("3.12")
elif name == "pip":
    print("Synthetic dependencies available")
elif name not in {"alembic", "python", "npm", "sleep"}:
    raise SystemExit("Unexpected synthetic command")
entry["status"] = status
with log.open("a") as stream:
    stream.write(json.dumps(entry) + "\n")
raise SystemExit(status)
"""


class QuickstartReadinessTests(unittest.TestCase):
    def run_quickstart(
        self, ready_after=1, probe_status=2, start_status=0, socket_ready=True
    ):
        with tempfile.TemporaryDirectory(prefix="cg-quickstart-readiness-") as folder:
            project = Path(folder)
            script = project / "infra/scripts/quickstart.sh"
            script.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "infra/scripts/quickstart.sh", script)
            (project / "apps/api").mkdir(parents=True)
            (project / "apps/web").mkdir(parents=True)
            (project / ".env").write_text("APP_MODE=mock\n")
            commands = project / "commands"
            commands.mkdir()
            for command in (
                "docker",
                "node",
                "python3",
                "pip",
                "alembic",
                "python",
                "npm",
                "sleep",
            ):
                stub = commands / command
                stub.write_text("#!" + sys.executable + "\n" + COMMAND_STUB)
                stub.chmod(0o700)
            log = project / "commands.jsonl"
            log.touch()
            env = {
                "PATH": str(commands) + os.pathsep + os.defpath,
                "HOME": str(project),
                "TEST_COMMAND_LOG": str(log),
                "TEST_TCP_READY_AFTER": str(ready_after),
                "TEST_PROBE_STATUS": str(probe_status),
                "TEST_START_STATUS": str(start_status),
                "TEST_SOCKET_READY": "1" if socket_ready else "0",
            }
            result = subprocess.run(
                ["/bin/bash", str(script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=40,
            )
            events = [json.loads(line) for line in log.read_text().splitlines()]
            return result, events

    def assert_stopped_before_setup(self, result, events):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("PostgreSQL ready", result.stdout)
        self.assertFalse(
            any(
                item["command"] in {"pip", "alembic", "python", "npm"}
                for item in events
            ),
            "Unready infrastructure must not reach dependency install, migrations or seed",
        )

    def test_temporary_socket_server_does_not_allow_setup_before_tcp_ready(self):
        result, events = self.run_quickstart(ready_after=3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        probes = [item for item in events if "pg_isready" in item["args"]]
        self.assertEqual(
            len(probes), 3, "The socket-only server was accepted too early"
        )
        self.assertTrue(probes[-1]["tcp_ready"])
        ready_index = events.index(probes[-1])
        for index, item in enumerate(events):
            if item["command"] in {"pip", "alembic", "python", "npm"}:
                self.assertGreater(index, ready_index)
        self.assertIn("Setup complete!", result.stdout)

    def test_last_allowed_tcp_attempt_can_succeed(self):
        result, events = self.run_quickstart(ready_after=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        probes = [item for item in events if "pg_isready" in item["args"]]
        self.assertEqual(len(probes), 30)
        self.assertTrue(probes[-1]["tcp_ready"])
        self.assertIn("PostgreSQL ready", result.stdout)

    def test_refusal_and_probe_errors_stop_setup_after_bounded_attempts(self):
        for status in (1, 2, 3, 125):
            with self.subTest(status=status):
                result, events = self.run_quickstart(
                    ready_after=0, probe_status=status, socket_ready=False
                )
                self.assert_stopped_before_setup(result, events)
                probes = [item for item in events if "pg_isready" in item["args"]]
                self.assertEqual(len(probes), 30)
                self.assertIn("PostgreSQL", result.stdout + result.stderr)

    def test_compose_start_failure_stops_without_readiness_or_setup(self):
        result, events = self.run_quickstart(start_status=125)
        self.assert_stopped_before_setup(result, events)
        self.assertFalse(any("pg_isready" in item["args"] for item in events))


if __name__ == "__main__":
    unittest.main()
