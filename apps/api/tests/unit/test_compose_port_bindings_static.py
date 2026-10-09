from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DATASTORE_CONTAINER_PORTS = {"5432", "6379"}
SERVICE_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
PORT_ENTRY = re.compile(r"""^\s+-\s*["']?([^"'\s#]+)["']?\s*(?:#.*)?$""")


def _published_ports(compose_file: Path) -> list[tuple[str, str]]:
    """(service, port mapping) for every entry of a service-level `ports:` list."""
    published: list[tuple[str, str]] = []
    service = ""
    in_ports = False
    for line in compose_file.read_text().splitlines():
        service_key = SERVICE_KEY.match(line)
        if service_key:
            service, in_ports = service_key.group(1), False
        elif re.match(r"^    ports:\s*$", line):
            in_ports = True
        elif in_ports and (entry := PORT_ENTRY.match(line)):
            published.append((service, entry.group(1)))
        elif line.strip() and not line.strip().startswith("#"):
            in_ports = False
    return published


def _container_port(mapping: str) -> str:
    return mapping.rsplit(":", 1)[-1].split("/", 1)[0]


def test_parser_reads_every_published_port_of_the_local_stack() -> None:
    published = _published_ports(REPO_ROOT / "docker-compose.yml")

    assert {service for service, _ in published} == {"postgres", "redis", "api", "web"}
    assert {_container_port(mapping) for _, mapping in published} == {
        "5432",
        "6379",
        "8000",
        "3000",
    }


def test_local_stack_publishes_datastores_on_loopback_only() -> None:
    datastore_mappings = [
        (service, mapping)
        for service, mapping in _published_ports(REPO_ROOT / "docker-compose.yml")
        if _container_port(mapping) in DATASTORE_CONTAINER_PORTS
    ]

    assert len(datastore_mappings) == 2
    assert [entry for entry in datastore_mappings if not entry[1].startswith("127.0.0.1:")] == []


def test_production_stack_does_not_publish_datastores() -> None:
    published = _published_ports(REPO_ROOT / "docker-compose.prod.yml")

    assert published != []
    assert [
        entry for entry in published if _container_port(entry[1]) in DATASTORE_CONTAINER_PORTS
    ] == []
