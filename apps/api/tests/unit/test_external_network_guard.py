"""The test suite must fail closed on any attempt to reach a host outside this machine.

No test here can reach a network, whether or not the session-wide guard is installed:
the ``sealed`` fixture installs the guard itself and replaces every real resolver,
connect and send function behind it with a sentinel that fails the test if reached.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

from tests import network_guard

BROKER_HOSTS = ("live.trading212.com", "demo.trading212.com", "api.kraken.com")
# TEST-NET-1 (RFC 5737): reserved for documentation, never routed.
UNROUTABLE_ADDRESS = "192.0.2.1"
REAL_FUNCTION_NAMES = (
    "_REAL_GETADDRINFO",
    "_REAL_GETHOSTBYNAME",
    "_REAL_GETHOSTBYNAME_EX",
    "_REAL_GETHOSTBYADDR",
    "_REAL_GETNAMEINFO",
    "_REAL_CONNECT",
    "_REAL_CONNECT_EX",
    "_REAL_SENDTO",
    "_REAL_SENDMSG",
)


API_ROOT = Path(__file__).resolve().parents[2]
# Child-session probes. Each first proves the guard is installed and then puts a failing
# sentinel behind it, so a probe cannot reach a network even if the wiring under test is broken.
PROBE_PREAMBLE = """
import contextlib, socket
from tests import network_guard

assert network_guard.is_installed(), "conftest did not install the guard"

def _sentinel(*_args, **_kwargs):
    raise AssertionError("reached a real network function")

for _name in [name for name in dir(network_guard) if name.startswith("_REAL_")]:
    setattr(network_guard, _name, _sentinel)

def _swallowed_lookup():
    with contextlib.suppress(OSError):
        socket.getaddrinfo("demo.trading212.com", 443)
"""
CLEAN_PROBE = (
    PROBE_PREAMBLE
    + """
def test_clean():
    socket.getaddrinfo  # no lookup made
"""
)
SWALLOWING_TEST_PROBE = (
    PROBE_PREAMBLE
    + """
def test_code_under_test_swallows_the_refusal():
    _swallowed_lookup()
"""
)
OUTSIDE_TEST_PROBE = (
    PROBE_PREAMBLE
    + """
_swallowed_lookup()  # at import time, outside any test

def test_clean():
    pass
"""
)


def _run_child_session(tmp_path: Path, probe_source: str) -> subprocess.CompletedProcess[str]:
    """Run one probe file in a child pytest session that loads this suite's conftest."""
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    (tmp_path / "test_probe.py").write_text(probe_source)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.conftest",
            "--rootdir",
            str(tmp_path),
            "-c",
            str(tmp_path / "pytest.ini"),
            str(tmp_path / "test_probe.py"),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(API_ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_conftest_wiring_leaves_a_clean_session_green(tmp_path: Path) -> None:
    result = _run_child_session(tmp_path, CLEAN_PROBE)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_conftest_wiring_fails_a_test_whose_code_swallowed_a_refusal(tmp_path: Path) -> None:
    result = _run_child_session(tmp_path, SWALLOWING_TEST_PROBE)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "1 passed, 1 error" in result.stdout
    assert "test attempted external network access: dns demo.trading212.com" in result.stdout


def test_conftest_wiring_fails_the_session_for_an_attempt_outside_any_test(
    tmp_path: Path,
) -> None:
    result = _run_child_session(tmp_path, OUTSIDE_TEST_PROBE)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "external network access attempted outside any test: dns demo.trading212.com" in (
        result.stdout
    )


def _reached_the_network(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError("a call got through to a real network function")


@pytest.fixture
def sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the guard for this test and put a failing sentinel behind it."""
    network_guard.install(monkeypatch)
    for name in REAL_FUNCTION_NAMES:
        monkeypatch.setattr(network_guard, name, _reached_the_network)


@pytest.fixture
def session_guard_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a session where conftest never installed the guard, without real functions."""
    for name in (
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
    ):
        monkeypatch.setattr(socket, name, _reached_the_network)
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        monkeypatch.setattr(socket.socket, name, _reached_the_network)


def test_guard_is_installed_for_the_whole_test_session() -> None:
    assert network_guard.is_installed()
    assert socket.getaddrinfo is network_guard.guarded_getaddrinfo
    assert socket.socket.connect is network_guard.guarded_connect


def test_sealed_tests_stay_offline_even_when_the_session_guard_is_missing(
    session_guard_missing: None,
    sealed: None,
    external_network: network_guard.ExternalNetworkAttempts,
) -> None:
    assert network_guard.is_installed()
    with (
        external_network.expected() as attempts,
        pytest.raises(network_guard.ExternalNetworkBlocked),
    ):
        socket.getaddrinfo("demo.trading212.com", 443)

    assert attempts == [("dns", "demo.trading212.com")]


@pytest.mark.parametrize("host", BROKER_HOSTS)
@pytest.mark.parametrize(
    "lookup",
    [
        lambda host: socket.getaddrinfo(host, 443),
        lambda host: socket.gethostbyname(host),
        lambda host: socket.gethostbyname_ex(host),
        lambda host: socket.gethostbyaddr(host),
        lambda host: socket.getnameinfo((host, 443), 0),
    ],
    ids=["getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"],
)
def test_resolving_a_broker_host_is_blocked_and_recorded(
    sealed: None, external_network: network_guard.ExternalNetworkAttempts, host: str, lookup: Any
) -> None:
    with (
        external_network.expected() as attempts,
        pytest.raises(network_guard.ExternalNetworkBlocked, match=host),
    ):
        lookup(host)

    assert attempts == [("dns", host)]


@pytest.mark.parametrize("host", BROKER_HOSTS[:2])
def test_http_request_to_a_broker_endpoint_is_blocked_before_any_lookup(
    sealed: None, external_network: network_guard.ExternalNetworkAttempts, host: str
) -> None:
    with external_network.expected() as attempts, pytest.raises(httpx.ConnectError):
        httpx.get(f"https://{host}/api/v0/equity/account/cash", timeout=2, trust_env=False)

    assert attempts == [("dns", host)]


def test_async_http_request_to_a_broker_endpoint_is_blocked(
    sealed: None, external_network: network_guard.ExternalNetworkAttempts
) -> None:
    async def request() -> None:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            await client.get("https://demo.trading212.com/api/v0/equity/account/cash")

    with external_network.expected() as attempts, pytest.raises(httpx.ConnectError):
        asyncio.run(request())

    assert attempts == [("dns", "demo.trading212.com")]


def test_http_request_does_not_leave_through_a_configured_proxy(
    monkeypatch: pytest.MonkeyPatch, external_network: network_guard.ExternalNetworkAttempts
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    network_guard.install(monkeypatch)
    for name in REAL_FUNCTION_NAMES:
        monkeypatch.setattr(network_guard, name, _reached_the_network)

    assert "HTTPS_PROXY" not in os.environ
    assert "https_proxy" not in os.environ
    assert os.environ["NO_PROXY"] == "*"
    with external_network.expected() as attempts, pytest.raises(httpx.ConnectError):
        httpx.get("https://live.trading212.com/api/v0/equity/account/cash", timeout=2)

    assert attempts == [("dns", "live.trading212.com")]


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_connecting_to_a_non_loopback_address_is_blocked_and_recorded(
    sealed: None, external_network: network_guard.ExternalNetworkAttempts, method: str
) -> None:
    with (
        external_network.expected() as attempts,
        socket.socket() as sock,
        pytest.raises(network_guard.ExternalNetworkBlocked, match=UNROUTABLE_ADDRESS),
    ):
        getattr(sock, method)((UNROUTABLE_ADDRESS, 443))

    assert attempts == [(method, UNROUTABLE_ADDRESS)]


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("sendto", (b"x", (UNROUTABLE_ADDRESS, 53))),
        ("sendto", (b"x", 0, (UNROUTABLE_ADDRESS, 53))),
        ("sendmsg", ([b"x"], [], 0, (UNROUTABLE_ADDRESS, 53))),
    ],
    ids=["sendto", "sendto-with-flags", "sendmsg"],
)
def test_datagram_to_a_non_loopback_address_is_blocked_and_recorded(
    sealed: None,
    external_network: network_guard.ExternalNetworkAttempts,
    method: str,
    arguments: tuple[Any, ...],
) -> None:
    with (
        external_network.expected() as attempts,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        pytest.raises(network_guard.ExternalNetworkBlocked, match=UNROUTABLE_ADDRESS),
    ):
        getattr(sock, method)(*arguments)

    assert attempts == [(method, UNROUTABLE_ADDRESS)]


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "::1", "127.0.0.53", "", None, b"localhost"],
)
def test_loopback_targets_are_allowed(host: Any) -> None:
    assert network_guard.is_local_host(host)


@pytest.mark.parametrize(
    "host",
    [
        *BROKER_HOSTS,
        "api.telegram.org",
        "localhost.",
        "localhost.example.com",
        "127.0.0.1.example.com",
        "127.1",
        "2130706433",
        "10.0.0.5",
        "192.168.1.10",
        "169.254.169.254",
        "0.0.0.0",
        "::ffff:8.8.8.8",
        "host.docker.internal",
        b"demo.trading212.com",
    ],
)
def test_everything_else_is_external(host: Any) -> None:
    assert not network_guard.is_local_host(host)


def test_loopback_calls_still_reach_the_real_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    reached: list[str] = []
    network_guard.install(monkeypatch)
    for name in REAL_FUNCTION_NAMES:
        monkeypatch.setattr(
            network_guard, name, lambda *_a, _name=name, **_k: reached.append(_name) or []
        )

    socket.getaddrinfo("localhost", 5432)
    socket.gethostbyname("localhost")
    with socket.socket() as sock:
        sock.connect(("127.0.0.1", 5432))
        sock.connect_ex(("127.0.0.1", 5432))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.sendto(b"x", ("127.0.0.1", 9))

    assert reached == [
        "_REAL_GETADDRINFO",
        "_REAL_GETHOSTBYNAME",
        "_REAL_CONNECT",
        "_REAL_CONNECT_EX",
        "_REAL_SENDTO",
    ]


@pytest.mark.parametrize(
    ("family", "address"),
    [(socket.AF_UNIX, "/tmp/example.sock"), (socket.AF_UNIX, ("not-an-ip", 1))],
    ids=["unix-path", "non-internet-tuple"],
)
def test_non_internet_addresses_are_passed_through(
    monkeypatch: pytest.MonkeyPatch, family: int, address: Any
) -> None:
    reached: list[Any] = []
    monkeypatch.setattr(
        network_guard, "_REAL_CONNECT", lambda _sock, target: reached.append(target)
    )
    sock = type("FakeSocket", (), {"family": family})()

    network_guard.guarded_connect(sock, address)

    assert reached == [address]


def test_an_attempt_swallowed_by_the_code_under_test_still_fails_the_test(
    sealed: None, external_network: network_guard.ExternalNetworkAttempts
) -> None:
    # What application code that tolerates network errors would do.
    with contextlib.suppress(OSError):
        socket.getaddrinfo("live.trading212.com", 443)

    assert external_network.pending() == [("dns", "live.trading212.com")]
    with pytest.raises(pytest.fail.Exception, match=r"live\.trading212\.com"):
        external_network.fail_if_any()
    assert external_network.pending() == []


def test_expected_attempts_do_not_fail_the_test(
    sealed: None, external_network: network_guard.ExternalNetworkAttempts
) -> None:
    with external_network.expected(), pytest.raises(network_guard.ExternalNetworkBlocked):
        socket.getaddrinfo("demo.trading212.com", 443)

    assert external_network.pending() == []
    external_network.fail_if_any()


def test_attempts_made_outside_a_test_are_kept_and_fail_the_session() -> None:
    attempts = network_guard.ExternalNetworkAttempts()
    attempts.record("dns", "demo.trading212.com")

    attempts.begin_test()

    assert attempts.pending() == []
    assert attempts.unattributed() == [("dns", "demo.trading212.com")]


@pytest.mark.parametrize(
    ("unattributed", "exit_status", "expected"),
    [(False, 0, 0), (True, 0, 1), (True, 2, 2), (False, 1, 1)],
)
def test_session_exit_status_reflects_attempts_outside_tests(
    monkeypatch: pytest.MonkeyPatch, unattributed: bool, exit_status: int, expected: int
) -> None:
    attempts = network_guard.ExternalNetworkAttempts()
    if unattributed:
        attempts.record("connect", UNROUTABLE_ADDRESS)
        attempts.begin_test()
    monkeypatch.setattr(network_guard, "ATTEMPTS", attempts)

    assert network_guard.session_exit_status(exit_status) == expected
