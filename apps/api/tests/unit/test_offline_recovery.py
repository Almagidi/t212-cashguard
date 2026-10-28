"""Target selection and fail-closed reporting never use ambient credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.operations import recovery
from app.services.recovery_controls import RecoveryRefusal

URL = "postgresql+asyncpg://synthetic:synthetic-password@127.0.0.1:5432/synthetic"


def load(path):
    return recovery.load_target(
        path, host="127.0.0.1", port=5432, database="synthetic", role="synthetic"
    )


def test_explicit_file_ignores_inherited_database_and_key(tmp_path, monkeypatch):
    config = tmp_path / "synthetic.env"
    config.write_text("DATABASE_URL=" + URL + "\nMASTER_KEY=" + "x" * 40 + "\n")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://wrong:wrong@wrong.invalid:9999/wrong")
    monkeypatch.setenv("MASTER_KEY", "wrong inherited key")
    target = load(config)
    assert target.url.database == "synthetic"
    assert target.url.host == "127.0.0.1"
    assert target.master_key == "x" * 40


@pytest.mark.parametrize(
    "content",
    [
        "",
        "DATABASE_URL\n",
        "DATABASE_URL=${OTHER}\n",
        "DATABASE_URL='unterminated\n",
        "DATABASE_URL=" + URL + "\nDATABASE_URL=" + URL + "\n",
        "DATABASE_URL=" + URL + "\ndatabase_url=" + URL + "\n",
        "DATABASE_URL=" + URL.replace("5432", "5433") + "\n",
        "DATABASE_URL=" + URL.replace("/synthetic", "/wrong") + "\n",
        "DATABASE_URL=" + URL.replace("synthetic-password", "") + "\n",
        "DATABASE_URL=" + URL + "?options=-csearch_path%3Dwrong\n",
        "DATABASE_URL=sqlite+aiosqlite:///:memory:\n",
    ],
)
def test_ambiguous_unresolved_or_wrong_target_is_refused_without_disclosing_config(
    tmp_path, content
):
    config = tmp_path / "synthetic.env"
    config.write_text(content)
    with pytest.raises(RecoveryRefusal) as result:
        load(config)
    assert "synthetic-password" not in str(result.value)


def test_missing_key_does_not_prevent_loading_target_for_block(tmp_path):
    config = tmp_path / "synthetic.env"
    config.write_text("DATABASE_URL=" + URL + "\n")
    assert load(config).master_key is None


def test_import_and_help_never_import_runtime_settings_or_application(tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "CASHGUARD_ENV_FILE": str(tmp_path / "MUST_NOT_BE_READ"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.operations.recovery; "
                "assert 'app.main' not in sys.modules; "
                "assert 'app.core.config' not in sys.modules; "
                "assert 'app.core.security' not in sys.modules; "
                "assert 'app.db.session' not in sys.modules"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_uncertain_commit_reports_only_unconfirmed_and_never_retries(tmp_path, monkeypatch, capsys):
    config = tmp_path / "synthetic.env"
    config.write_text("DATABASE_URL=" + URL + "\n")
    operation_id = uuid.uuid4()
    calls = []

    async def interrupted(target, command, operation):
        calls.append((target.url.database, command, operation))
        raise OSError("synthetic-password transport loss during COMMIT")

    monkeypatch.setattr(recovery, "run", interrupted)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recovery",
            "block",
            "--env-file",
            str(config),
            "--expect-host",
            "127.0.0.1",
            "--expect-port",
            "5432",
            "--expect-database",
            "synthetic",
            "--expect-role",
            "synthetic",
            "--operation-id",
            str(operation_id),
        ],
    )
    assert recovery.main() == 3
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "UNCONFIRMED"
    assert "synthetic-password" not in output
    assert calls == [("synthetic", "block", operation_id)]
