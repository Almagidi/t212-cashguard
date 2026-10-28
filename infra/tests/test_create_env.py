"""Failure/race controls for first-time configuration publication."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[1] / "scripts/create_env.py"
SPEC = importlib.util.spec_from_file_location("cashguard_create_env", HELPER)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CreateEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="cashguard-publish-test-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.template = self.root / "template"
        self.target = self.root / ".env"
        self.content = (
            "SECRET_KEY="
            + MODULE.PLACEHOLDER
            + "\r\nMASTER_KEY="
            + MODULE.PLACEHOLDER
            + "\r\n# "
            + MODULE.PLACEHOLDER
            + "\r\n"
        )
        self.template.write_bytes(self.content.encode())

    def test_only_named_assignments_change_and_crlf_is_preserved(self):
        with patch.object(
            MODULE.secrets, "token_hex", side_effect=["a" * 64, "b" * 64]
        ) as random:
            self.assertTrue(MODULE.create_environment(self.template, self.target))
        self.assertEqual(
            random.call_args_list, [unittest.mock.call(32), unittest.mock.call(32)]
        )
        self.assertTrue(
            self.target.read_bytes()
            == (
                "SECRET_KEY="
                + "a" * 64
                + "\r\nMASTER_KEY="
                + "b" * 64
                + "\r\n# "
                + MODULE.PLACEHOLDER
                + "\r\n"
            ).encode()
        )

    def test_duplicate_random_results_fail_before_creating_any_configuration(self):
        with patch.object(MODULE.secrets, "token_hex", return_value="a" * 64):
            with self.assertRaises(ValueError):
                MODULE.create_environment(self.template, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob(".cashguard-env-*")))

    def test_missing_or_duplicate_template_fields_never_publish(self):
        for content in (
            "APP_MODE=mock\n",
            self.content + "SECRET_KEY=" + MODULE.PLACEHOLDER,
        ):
            with self.subTest(content=content):
                self.template.write_bytes(content.encode())
                with self.assertRaises(ValueError):
                    MODULE.create_environment(self.template, self.target)
                self.assertFalse(self.target.exists())

    def test_configuration_created_by_another_process_is_preserved(self):
        data = b"SECRET_KEY=synthetic-race-winner\n"

        def concurrent_create(_source, destination):
            Path(destination).write_bytes(data)
            raise FileExistsError

        with patch.object(MODULE.os, "link", side_effect=concurrent_create):
            self.assertFalse(MODULE.create_environment(self.template, self.target))
        self.assertTrue(self.target.read_bytes() == data)
        self.assertFalse(list(self.root.glob(".cashguard-env-*")))

    def test_publication_failure_leaves_no_partial_environment_or_secret_copy(self):
        with patch.object(
            MODULE.os, "link", side_effect=OSError("synthetic unavailable link")
        ):
            with self.assertRaises(OSError):
                MODULE.create_environment(self.template, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob(".cashguard-env-*")))


if __name__ == "__main__":
    unittest.main()
