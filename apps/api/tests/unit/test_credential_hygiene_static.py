from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[4]
LAUNCHER_ROOT = REPO_ROOT / "launcher"

DEMO_CREDENTIAL_READERS = (
    REPO_ROOT / "Makefile",
    REPO_ROOT / "apps/api/app/api/deps.py",
    REPO_ROOT / "apps/api/app/api/v1/routes/broker.py",
    REPO_ROOT / "apps/api/app/services/demo_reconciliation_scheduler.py",
    REPO_ROOT / "apps/api/scripts/t212_demo_controlled_multi_order.py",
    REPO_ROOT / "apps/api/scripts/t212_demo_controlled_order.py",
    REPO_ROOT / "apps/api/scripts/t212_demo_multi_order_reconciliation_smoke.py",
    REPO_ROOT / "apps/api/scripts/t212_demo_readonly_smoke.py",
    REPO_ROOT / "apps/api/scripts/t212_demo_reconcile_order.py",
    REPO_ROOT / "apps/api/scripts/t212_demo_reconciliation_worker.py",
)

EXPECTED_CREDENTIAL_READERS = {
    "Makefile",
    "apps/api/app/api/deps.py",
    "apps/api/app/api/v1/routes/broker.py",
    "apps/api/app/api/v1/routes/operator.py",
    "apps/api/app/core/config.py",
    "apps/api/app/services/demo_reconciliation_scheduler.py",
    "apps/api/app/services/safety_policy.py",
    "apps/api/app/services/startup_validation.py",
    "apps/api/scripts/real_worker_paper_smoke.py",
    "apps/api/scripts/t212_demo_controlled_multi_order.py",
    "apps/api/scripts/t212_demo_controlled_order.py",
    "apps/api/scripts/t212_demo_multi_order_reconciliation_smoke.py",
    "apps/api/scripts/t212_demo_readonly_smoke.py",
    "apps/api/scripts/t212_demo_reconcile_order.py",
    "apps/api/scripts/t212_demo_reconciliation_worker.py",
    "launcher/2. Start CashGuard.command",
    "launcher/5. Check Status.command",
}


def test_demo_credential_readers_use_demo_specific_names_only() -> None:
    forbidden_names = {
        "T212_API_KEY",
        "T212_API_SECRET",
        "T212_LIVE_API_KEY",
        "T212_LIVE_API_SECRET",
    }

    violations: list[str] = []
    for path in DEMO_CREDENTIAL_READERS:
        source = path.read_text()
        if "generic-fallback" in source:
            violations.append(f"{path.relative_to(REPO_ROOT)} retains generic fallback logic")
        for name in forbidden_names:
            if re.search(rf"(?<![A-Z0-9_]){name}(?![A-Z0-9_])", source):
                violations.append(f"{path.relative_to(REPO_ROOT)} reads {name}")

    assert violations == []


def test_runtime_settings_have_no_generic_trading212_credentials() -> None:
    assert "T212_API_KEY" not in Settings.model_fields
    assert "T212_API_SECRET" not in Settings.model_fields
    assert "T212_DEMO_API_KEY" in Settings.model_fields
    assert "T212_LIVE_API_KEY" in Settings.model_fields


def test_trading212_environment_credential_reader_inventory_is_complete() -> None:
    roots = (
        REPO_ROOT / "apps/api/app",
        REPO_ROOT / "apps/api/scripts",
        REPO_ROOT / "launcher",
    )
    candidates = [REPO_ROOT / "Makefile"]
    for root in roots:
        candidates.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".command", ".sh"}
        )

    credential_name = re.compile(
        r"(?<![A-Z0-9_])T212_(?:(?:DEMO|LIVE)_API_(?:KEY|SECRET)|API_(?:KEY|SECRET))"
        r"(?![A-Z0-9_])"
    )
    actual = {
        str(path.relative_to(REPO_ROOT))
        for path in candidates
        if credential_name.search(path.read_text())
    }

    assert actual == EXPECTED_CREDENTIAL_READERS


def test_launchers_cannot_collect_persist_or_activate_live_credentials() -> None:
    violations: list[str] = []
    forbidden = (
        "T212_LIVE_API_KEY",
        "T212_LIVE_API_SECRET",
        "APP_MODE=live",
        "NEXT_PUBLIC_APP_MODE=live",
        "LIVE_TRADING_ENABLED=true",
    )

    for path in sorted(LAUNCHER_ROOT.glob("*.command")):
        source = path.read_text()
        for token in forbidden:
            if token in source:
                violations.append(f"{path.name} contains {token}")

    assert violations == []


def test_launchers_that_start_api_enforce_non_live_boundary() -> None:
    start = (LAUNCHER_ROOT / "2. Start CashGuard.command").read_text()
    maintenance = (LAUNCHER_ROOT / "9. Run Migrations and Test.command").read_text()

    live_refusal = start.index("Live trading is prohibited; refusing to start")
    api_start = start.index("-m uvicorn")
    assert live_refusal < api_start
    assert 'APP_MODE_VALUE" = "live"' in start
    assert "LIVE_TRADING_VALUE" in start

    mock_boundary = maintenance.index("export APP_MODE=mock")
    maintenance_api_start = maintenance.index("-m uvicorn")
    assert mock_boundary < maintenance_api_start
    assert "export MARKET_DATA_PROVIDER=mock" in maintenance
    assert "export LIVE_TRADING_ENABLED=false" in maintenance


def test_supported_launchers_do_not_write_broker_credentials_to_env() -> None:
    credential_names = (
        "T212_API_KEY",
        "T212_API_SECRET",
        "T212_DEMO_API_KEY",
        "T212_DEMO_API_SECRET",
        "T212_LIVE_API_KEY",
        "T212_LIVE_API_SECRET",
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "POLYGON_API_KEY",
    )
    mutating_launchers = (
        LAUNCHER_ROOT / "1. Setup (Run First).command",
        LAUNCHER_ROOT / "4. Update API Keys.command",
        LAUNCHER_ROOT / "6. Enable Live Trading (Read First).command",
    )

    violations = [
        f"{path.name} contains {name}"
        for path in mutating_launchers
        for name in credential_names
        if name in path.read_text()
    ]

    assert violations == []


def test_shell_secret_prompts_disable_terminal_echo() -> None:
    insecure_prompts: list[str] = []
    secret_prompt = re.compile(r"^\s*read\b.*(?:secret|password|api key)", re.IGNORECASE)

    for path in sorted(LAUNCHER_ROOT.glob("*.command")):
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if secret_prompt.search(line) and not re.search(r"\bread\s+-[^\n]*s", line):
                insecure_prompts.append(f"{path.name}:{line_number}")

    assert insecure_prompts == []


SECRET_VARIABLE_NAME = re.compile(
    r"^(?:[A-Z0-9_]*(?:PASSWORD|SECRET|API_KEY|TOKEN)|SECRET_KEY|MASTER_KEY)$"
)
SHELL_ASSIGNMENT = re.compile(r"^\s*(?:local\s+|export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
SHELL_EXPANSION = re.compile(r"\$\{?[#!]?([A-Za-z_][A-Za-z0-9_]*)")
SECRET_ENV_FILE_LOOKUP = re.compile(r"\^([A-Z0-9_]+)=")
RENDER_COMMAND = re.compile(r"^\s*(?:echo|printf|ok|fail|warn|info)\b")
PYTHON_SECRET_PRINT = re.compile(r"print\(.*settings\.(?:SECRET_KEY|MASTER_KEY|ADMIN_PASSWORD)\b")


def _launcher_scripts() -> list[Path]:
    return sorted([*LAUNCHER_ROOT.glob("*.command"), *LAUNCHER_ROOT.glob("*.sh")])


def _is_secret_name(name: str, secret_names: set[str]) -> bool:
    return name in secret_names or bool(SECRET_VARIABLE_NAME.match(name))


def _reads_secret(expression: str, secret_names: set[str]) -> bool:
    return any(_is_secret_name(name, secret_names) for name in SHELL_EXPANSION.findall(expression))


def _assigned_from_secret(value: str, secret_names: set[str]) -> bool:
    looked_up = SECRET_ENV_FILE_LOOKUP.findall(value)
    if looked_up:
        return any(_is_secret_name(name, secret_names) for name in looked_up)
    if "$(" in value or "`" in value:
        # Output of another command (for example an HTTP response), not the secret itself.
        return False
    return _reads_secret(value, secret_names)


def _secret_derived_names(lines: list[str]) -> set[str]:
    """Variables assigned directly from a secret variable or from its line in an env file."""
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for line in lines:
            assignment = SHELL_ASSIGNMENT.match(line)
            if not assignment or assignment.group(1) in names:
                continue
            if _assigned_from_secret(assignment.group(2), names):
                names.add(assignment.group(1))
                changed = True
    return names


def test_launchers_never_render_secret_values() -> None:
    violations: list[str] = []

    for path in _launcher_scripts():
        lines = path.read_text().splitlines()
        secret_names = _secret_derived_names(lines)
        for line_number, line in enumerate(lines, start=1):
            renders_shell_secret = RENDER_COMMAND.match(line) and _reads_secret(line, secret_names)
            if renders_shell_secret or PYTHON_SECRET_PRINT.search(line):
                violations.append(f"{path.name}:{line_number}")

    assert violations == []


def test_secret_alias_tracking_follows_assignments() -> None:
    lines = [
        'PW="${ADMIN_PASSWORD:-}"',
        'COPY="$PW"',
        'FROM_FILE=$(grep "^SECRET_KEY=" "$ENV_FILE" | cut -d= -f2)',
        'EMAIL="${ADMIN_EMAIL:-admin@localhost}"',
        "PASS=0",
        'RESPONSE=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/risk/profile")',
    ]

    assert _secret_derived_names(lines) == {"PW", "COPY", "FROM_FILE"}
    assert _reads_secret('echo "first four: ${COPY:0:4}"', {"PW", "COPY", "FROM_FILE"})
    assert not _reads_secret('echo "$EMAIL passed $PASS checks"', {"PW", "COPY", "FROM_FILE"})


def test_launchers_never_put_a_login_password_on_a_command_line() -> None:
    inline_login_body = re.compile(r"""(?:-d|--data(?:-raw|-binary)?)\s+["'].*password""")
    violations = [
        f"{path.name}:{line_number}"
        for path in _launcher_scripts()
        for line_number, line in enumerate(path.read_text().splitlines(), start=1)
        if inline_login_body.search(line)
    ]

    assert violations == []


LOGIN_BODY_SCRIPTS = (
    "8. Verify Login.command",
    "9. Run Migrations and Test.command",
    "diagnose.sh",
)


@pytest.mark.parametrize("script_name", LOGIN_BODY_SCRIPTS)
def test_launcher_login_body_encodes_any_password_as_json(script_name: str) -> None:
    script = (LAUNCHER_ROOT / script_name).read_text()
    helper = re.search(r"^\s*login_body\(\) \{\n.*?\n\s*\}\n", script, re.MULTILINE | re.DOTALL)
    assert helper is not None
    awkward_password = "pa\"ss\\wo$rd `id` ; #' é"

    result = subprocess.run(
        [
            "bash",
            "-c",
            helper.group(0) + '\nlogin_body "$1" "$2"',
            "bash",
            "admin@localhost",
            awkward_password,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {"email": "admin@localhost", "password": awkward_password}


def test_default_configuration_and_quickstart_are_mock_only() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text()
    readme = (REPO_ROOT / "README.md").read_text()
    setup = (LAUNCHER_ROOT / "1. Setup (Run First).command").read_text()

    assert re.search(r"^APP_MODE=mock$", env_example, re.MULTILINE)
    assert re.search(r"^MARKET_DATA_PROVIDER=mock$", env_example, re.MULTILINE)
    assert "MARKET_DATA_PROVIDER=mock" in readme
    assert "MARKET_DATA_PROVIDER=mock" in setup
    assert "MARKET_DATA_PROVIDER=${" not in setup


def test_secret_file_guard_is_shared_by_precommit_and_ci() -> None:
    guard = REPO_ROOT / "scripts/check_secret_files.sh"
    precommit = (REPO_ROOT / ".pre-commit-config.yaml").read_text()
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()

    assert guard.is_file()
    assert "scripts/check_secret_files.sh --staged" in precommit
    assert "scripts/check_secret_files.sh" in ci
    assert ".env.*" in gitignore
    assert "!.env.example" in gitignore


def test_secret_file_guard_rejects_production_variant(
    tmp_path: Path,
) -> None:
    guard = REPO_ROOT / "scripts/check_secret_files.sh"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env.example").write_text("APP_MODE=mock\n")
    (tmp_path / ".env.production").write_text("SECRET=value\n")
    subprocess.run(["git", "add", ".env.example"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "add", "-f", ".env.production"],
        cwd=tmp_path,
        check=True,
    )

    result = subprocess.run(
        ["bash", str(guard), "--staged"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert ".env.production" in result.stderr


def test_gitleaks_scans_command_files() -> None:
    config = REPO_ROOT / ".gitleaks.toml"
    if not config.exists():
        return

    source = config.read_text().lower()
    assert ".command" not in source or "allowlist" not in source
