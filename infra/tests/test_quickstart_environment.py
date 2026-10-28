"""Actual quickstart environment creation in synthetic, disposable projects."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
STUB = r"""
import os
from pathlib import Path
import sys
name=Path(sys.argv[0]).name
if name=='docker':
    if sys.argv[1:]==['--version']:print('Docker version synthetic')
elif name=='node':print('v24.21.0')
elif name=='openssl':
    p=Path(os.environ['HOME'])/'openssl-count'
    count=int(p.read_text())+1 if p.exists() else 1
    p.write_text(str(count))
    print(('a' if count==1 else 'b')*64)
elif name not in {'pip','python','npm','alembic','sleep'}:raise SystemExit('Unexpected command')
"""


class QuickstartEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="cashguard-env-test-")
        self.addCleanup(self.folder.cleanup)
        self.project = Path(self.folder.name)
        scripts = self.project / "infra/scripts"
        scripts.mkdir(parents=True)
        self.script = scripts / "quickstart.sh"
        shutil.copyfile(ROOT / "infra/scripts/quickstart.sh", self.script)
        helper = ROOT / "infra/scripts/create_env.py"
        if helper.exists():
            shutil.copyfile(helper, scripts / helper.name)
        shutil.copyfile(ROOT / ".env.example", self.project / ".env.example")
        for path in ("apps/api", "apps/web", "commands"):
            (self.project / path).mkdir(parents=True)
        commands = self.project / "commands"
        for name in (
            "docker",
            "node",
            "openssl",
            "pip",
            "python",
            "npm",
            "alembic",
            "sleep",
        ):
            p = commands / name
            p.write_text("#!" + sys.executable + "\n" + STUB)
            p.chmod(0o700)
        (commands / "python3").symlink_to(sys.executable)
        self.env = {
            "PATH": str(commands) + os.pathsep + os.defpath,
            "HOME": str(self.project),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run_script(self):
        return subprocess.run(
            ["/bin/bash", str(self.script)],
            env=self.env,
            cwd=self.project,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_new_environment_has_distinct_256_bit_keys_without_disclosing_them(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = dict(
            line.split("=", 1)
            for line in (self.project / ".env").read_text().splitlines()
            if line.startswith(("SECRET_KEY=", "MASTER_KEY="))
        )
        self.assertTrue(values["SECRET_KEY"] != values["MASTER_KEY"], "Keys collided")
        for value in values.values():
            self.assertTrue(
                len(value) == 64 and all(c in "0123456789abcdef" for c in value)
            )
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_new_environment_is_private_and_has_no_secret_backup(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.project / ".env").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.project / ".env.bak").exists())
        self.assertFalse(list(self.project.glob(".cashguard-env-*")))

    def test_existing_keys_and_all_other_bytes_are_preserved(self):
        data = b"APP_MODE=mock\nSECRET_KEY=synthetic-existing-$()/&\nMASTER_KEY=synthetic-master\n# retained\n"
        p = self.project / ".env"
        p.write_bytes(data)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(p.read_bytes() == data)
        self.assertFalse((self.project / "openssl-count").exists())

    def test_repeat_run_never_rotates_created_keys(self):
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        p = self.project / ".env"
        digest = hashlib.sha256(p.read_bytes()).digest()
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(hashlib.sha256(p.read_bytes()).digest(), digest)

    def test_existing_partial_configuration_is_not_silently_repaired(self):
        p = self.project / ".env"
        data = b"APP_MODE=mock\nSECRET_KEY=synthetic-existing\nMASTER_KEY=change-me-generate-with-openssl-rand-hex-32\n"
        p.write_bytes(data)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(p.read_bytes() == data)

    def test_existing_symlink_is_rejected_without_touching_its_target(self):
        target = self.project / "synthetic-existing-config"
        data = b"APP_MODE=mock\nSECRET_KEY=synthetic-preserve\n"
        target.write_bytes(data)
        (self.project / ".env").symlink_to(target)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(target.read_bytes() == data)
        self.assertTrue((self.project / ".env").is_symlink())


if __name__ == "__main__":
    unittest.main()
