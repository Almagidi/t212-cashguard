"""Fail-closed network guard for the test session.

Tests must never reach a host outside this machine: not a broker, not a notifier, not a
market-data vendor. Every DNS lookup, connection or datagram to anything but loopback is
refused and recorded, and a test that made one fails at teardown even when the code under
test swallowed the refusal. Attempts made outside any test fail the whole session.

The guard works inside this Python process. It does not cover child processes or code
that bypasses the ``socket`` module, so it complements, and does not replace, isolation
at the operating-system level.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
import threading
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GETHOSTBYNAME = socket.gethostbyname
_REAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex
_REAL_GETHOSTBYADDR = socket.gethostbyaddr
_REAL_GETNAMEINFO = socket.getnameinfo
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_SENDTO = socket.socket.sendto
_REAL_SENDMSG = socket.socket.sendmsg
_LOCAL_NAMES = frozenset({"", "localhost"})
_INTERNET_FAMILIES = (socket.AF_INET, socket.AF_INET6)
_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY")


class ExternalNetworkBlocked(OSError):
    """Raised in place of a lookup, connection or datagram that would leave this machine."""


class ExternalNetworkAttempts:
    """Refused attempts: those of the running test, and those made outside any test."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[tuple[str, str]] = []
        self._unattributed: list[tuple[str, str]] = []

    def record(self, kind: str, host: str) -> None:
        with self._lock:
            self._pending.append((kind, host))

    def pending(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._pending)

    def unattributed(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._unattributed)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()

    def begin_test(self) -> None:
        """Anything still pending was attempted outside a test; keep it for the session end."""
        with self._lock:
            self._unattributed.extend(self._pending)
            self._pending.clear()

    @contextlib.contextmanager
    def expected(self) -> Iterator[list[tuple[str, str]]]:
        """Collect the attempts made inside the block instead of failing the test for them."""
        with self._lock:
            already_pending = len(self._pending)
        collected: list[tuple[str, str]] = []
        try:
            yield collected
        finally:
            with self._lock:
                collected.extend(self._pending[already_pending:])
                del self._pending[already_pending:]

    def fail_if_any(self) -> None:
        with self._lock:
            attempts = list(self._pending)
            self._pending.clear()
        if attempts:
            described = ", ".join(f"{kind} {host}" for kind, host in attempts)
            pytest.fail(f"test attempted external network access: {described}", pytrace=False)


ATTEMPTS = ExternalNetworkAttempts()


def _host_text(host: Any) -> str:
    if host is None:
        return ""
    if isinstance(host, bytes):
        return host.decode("ascii", errors="replace")
    return str(host)


def is_local_host(host: Any) -> bool:
    """True only for loopback addresses and the name ``localhost``."""
    text = _host_text(host).strip().lower()
    if text in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _refuse(kind: str, host: Any) -> ExternalNetworkBlocked:
    name = _host_text(host)
    ATTEMPTS.record(kind, name)
    return ExternalNetworkBlocked(f"test attempted external network access: {kind} {name}")


def _is_external_address(sock: Any, address: Any) -> bool:
    """True for an internet-family ``(host, port, ...)`` address that is not loopback."""
    if not isinstance(address, tuple) or not address:
        return False
    if getattr(sock, "family", socket.AF_INET) not in _INTERNET_FAMILIES:
        return False
    return not is_local_host(address[0])


def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if not is_local_host(host):
        raise _refuse("dns", host)
    return _REAL_GETADDRINFO(host, *args, **kwargs)


def guarded_gethostbyname(hostname: Any) -> Any:
    if not is_local_host(hostname):
        raise _refuse("dns", hostname)
    return _REAL_GETHOSTBYNAME(hostname)


def guarded_gethostbyname_ex(hostname: Any) -> Any:
    if not is_local_host(hostname):
        raise _refuse("dns", hostname)
    return _REAL_GETHOSTBYNAME_EX(hostname)


def guarded_gethostbyaddr(ip_address: Any) -> Any:
    if not is_local_host(ip_address):
        raise _refuse("dns", ip_address)
    return _REAL_GETHOSTBYADDR(ip_address)


def guarded_getnameinfo(sockaddr: Any, flags: int) -> Any:
    host = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else sockaddr
    if not is_local_host(host):
        raise _refuse("dns", host)
    return _REAL_GETNAMEINFO(sockaddr, flags)


def guarded_connect(sock: Any, address: Any) -> Any:
    if _is_external_address(sock, address):
        raise _refuse("connect", address[0])
    return _REAL_CONNECT(sock, address)


def guarded_connect_ex(sock: Any, address: Any) -> Any:
    if _is_external_address(sock, address):
        raise _refuse("connect_ex", address[0])
    return _REAL_CONNECT_EX(sock, address)


def guarded_sendto(sock: Any, data: Any, *args: Any) -> Any:
    # sendto(data, address) or sendto(data, flags, address)
    address = args[-1] if args else None
    if _is_external_address(sock, address):
        raise _refuse("sendto", address[0])
    return _REAL_SENDTO(sock, data, *args)


def guarded_sendmsg(sock: Any, buffers: Any, *args: Any) -> Any:
    # sendmsg(buffers[, ancdata[, flags[, address]]])
    address = args[2] if len(args) >= 3 else None
    if _is_external_address(sock, address):
        raise _refuse("sendmsg", address[0])
    return _REAL_SENDMSG(sock, buffers, *args)


_SOCKET_MODULE_GUARDS = {
    "getaddrinfo": guarded_getaddrinfo,
    "gethostbyname": guarded_gethostbyname,
    "gethostbyname_ex": guarded_gethostbyname_ex,
    "gethostbyaddr": guarded_gethostbyaddr,
    "getnameinfo": guarded_getnameinfo,
}
_SOCKET_METHOD_GUARDS = {
    "connect": guarded_connect,
    "connect_ex": guarded_connect_ex,
    "sendto": guarded_sendto,
    "sendmsg": guarded_sendmsg,
}


def install(monkeypatch: pytest.MonkeyPatch | None = None) -> None:
    """Route this process's lookups, connections and datagrams through the guard.

    Proxy settings are neutralised as well: a loopback proxy would otherwise carry an
    external request out of the process without the guard seeing its destination.
    With ``monkeypatch`` the change lasts for one test; without it, for the session.
    """
    for name, guard in _SOCKET_MODULE_GUARDS.items():
        if monkeypatch is None:
            setattr(socket, name, guard)
        else:
            monkeypatch.setattr(socket, name, guard)
    for name, guard in _SOCKET_METHOD_GUARDS.items():
        if monkeypatch is None:
            setattr(socket.socket, name, guard)
        else:
            monkeypatch.setattr(socket.socket, name, guard)
    for variable in _PROXY_VARIABLES:
        for spelling in (variable, variable.lower()):
            if monkeypatch is None:
                os.environ.pop(spelling, None)
            else:
                monkeypatch.delenv(spelling, raising=False)
    for spelling in ("NO_PROXY", "no_proxy"):
        if monkeypatch is None:
            os.environ[spelling] = "*"
        else:
            monkeypatch.setenv(spelling, "*")


def is_installed() -> bool:
    return all(
        getattr(socket, name) is guard for name, guard in _SOCKET_MODULE_GUARDS.items()
    ) and all(
        getattr(socket.socket, name) is guard for name, guard in _SOCKET_METHOD_GUARDS.items()
    )


def session_exit_status(exit_status: int) -> int:
    """A session in which something outside any test tried to leave the machine fails."""
    if exit_status == 0 and (ATTEMPTS.unattributed() or ATTEMPTS.pending()):
        return 1
    return exit_status
