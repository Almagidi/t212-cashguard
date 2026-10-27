"""Create a private local configuration without replacing any existing path."""

import os
from pathlib import Path
import re
import secrets
import tempfile

PLACEHOLDER = "change-me-generate-with-openssl-rand-hex-32"


def existing_environment(target: Path) -> bool:
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("Configuration must be a regular file")
    return target.exists()


def create_environment(template: Path, target: Path) -> bool:
    if existing_environment(target):
        return False
    content = template.read_bytes().decode("utf-8")
    values = {name: secrets.token_hex(32) for name in ("SECRET_KEY", "MASTER_KEY")}
    if values["SECRET_KEY"] == values["MASTER_KEY"]:
        raise ValueError("Random source returned duplicate keys")
    for name, value in values.items():
        content, count = re.subn(
            rf"^{name}={re.escape(PLACEHOLDER)}(?=\r?$)",
            name + "=" + value,
            content,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise ValueError("Template must contain one placeholder for each key")
    # The private temporary file is complete before publication. A hard link
    # fails if another process creates .env; neither files nor symlinks are replaced.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=target.parent,
        prefix=".cashguard-env-",
        delete=True,
    ) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        try:
            os.link(stream.name, target)
        except FileExistsError:
            if not existing_environment(target):
                raise
            return False
    return True


if __name__ == "__main__":
    try:
        created = create_environment(Path(".env.example"), Path(".env"))
    except (OSError, ValueError):
        raise SystemExit(
            "Configuration creation failed; existing configuration was not replaced."
        ) from None
    print(
        "Created private .env with independent keys"
        if created
        else "Existing .env preserved"
    )
