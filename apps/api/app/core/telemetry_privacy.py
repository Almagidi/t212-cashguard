"""Keep only structured diagnostic metadata in optional outbound telemetry.

Free-form strings and unknown fields are withheld, including data at nested
locations. Code locations are rebuilt from application source, not copied
from potentially sensitive absolute paths, snippets or local variables.
"""

from __future__ import annotations

import ast
import builtins
import math
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

_APP_ROOT = Path(__file__).resolve().parents[1]
_LEVELS = frozenset({"debug", "info", "warning", "error", "fatal", "critical"})
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_ENVIRONMENTS = frozenset({"mock", "paper", "demo", "live"})
_OPERATIONS = frozenset(
    {
        "http.server",
        "http.client",
        "db",
        "db.sql.query",
        "db.sql.transaction",
        "queue.task.celery",
        "queue.process",
        "celery.task",
        "function",
        "middleware.starlette",
        "middleware.asgi",
        "rpc.server",
        "rpc.client",
    }
)
_STATUSES = frozenset(
    {
        "ok",
        "cancelled",
        "unknown",
        "unknown_error",
        "invalid_argument",
        "deadline_exceeded",
        "not_found",
        "already_exists",
        "permission_denied",
        "resource_exhausted",
        "failed_precondition",
        "aborted",
        "out_of_range",
        "unimplemented",
        "internal_error",
        "unavailable",
        "data_loss",
        "unauthenticated",
    }
)
_CATEGORIES = frozenset(
    {
        "query",
        "sqlalchemy",
        "httpx",
        "httplib",
        "http",
        "celery",
        "redis",
        "subprocess",
        "console",
        "log",
        "sentry",
    }
)
_MECHANISMS = frozenset(
    {
        "generic",
        "celery",
        "logging",
        "starlette",
        "fastapi",
        "asgi",
        "asyncio",
        "excepthook",
        "threading",
    }
)
_EXCEPTION_TYPES = frozenset(
    name
    for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
) | frozenset(
    {
        "ValidationError",
        "RequestValidationError",
        "HTTPException",
        "IntegrityError",
        "OperationalError",
        "DataError",
        "DBAPIError",
        "CeleryError",
        "Retry",
        "Reject",
    }
)


def _dictionary(value: object) -> dict[str, Any] | None:
    return value if type(value) is dict else None


def _items(value: object, limit: int) -> list[Any]:
    return value[:limit] if type(value) is list else []


def _known(value: object, allowed: frozenset[str]) -> str | None:
    if type(value) is str and value in allowed:
        return value
    return None


def _identifier(value: object, length: int) -> str | None:
    if type(value) is str and re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        return value
    return None


def _timestamp(value: object) -> str | float | int | None:
    if type(value) is int and 0 <= value <= 100_000_000_000:
        return value
    if type(value) is float and math.isfinite(value) and 0 <= value <= 100_000_000_000:
        return value
    if type(value) is str and len(value) <= 40 and re.fullmatch(r"[0-9T:.\-+Z ]+", value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value
    return None


@dataclass(frozen=True)
class _Source:
    filename: str
    functions: dict[str, tuple[tuple[int, int], ...]]
    classes: frozenset[str]


@lru_cache(maxsize=128)
def _source(module: str) -> _Source | None:
    parts = module.split(".")
    if len(module) > 160 or parts[0] != "app" or not all(p.isidentifier() for p in parts):
        return None
    stem = _APP_ROOT.joinpath(*parts[1:])
    candidates = (
        [stem.with_suffix(".py"), stem / "__init__.py"]
        if len(parts) > 1
        else [
            _APP_ROOT / "__init__.py",
        ]
    )
    for candidate in candidates:
        try:
            path = candidate.resolve(strict=True)
            if not path.is_relative_to(_APP_ROOT):
                continue
            text = path.read_text()
            tree = ast.parse(text)
            functions: dict[str, list[tuple[int, int]]] = {
                "<module>": [(1, len(text.splitlines()))],
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                    functions.setdefault(node.name, []).append((start, node.end_lineno or start))
            return _Source(
                "app/" + path.relative_to(_APP_ROOT).as_posix(),
                {name: tuple(ranges) for name, ranges in functions.items()},
                frozenset(n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)),
            )
        except (OSError, UnicodeError, SyntaxError, ValueError):
            continue
    return None


def _frame(value: object) -> dict[str, Any] | None:
    frame = _dictionary(value)
    if frame is None:
        return None
    module, function, line = frame.get("module"), frame.get("function"), frame.get("lineno")
    if type(module) is not str or type(function) is not str or type(line) is not int:
        return None
    source = _source(module)
    if source is None or not any(
        start <= line <= end for start, end in source.functions.get(function, ())
    ):
        return None
    return {
        "module": module,
        "function": function,
        "filename": source.filename,
        "lineno": line,
        "in_app": True,
    }


def _stack(value: object) -> dict[str, Any] | None:
    stack = _dictionary(value)
    if stack is None:
        return None
    frames = [clean for raw in _items(stack.get("frames"), 100) if (clean := _frame(raw))]
    return {"frames": frames} if frames else None


def _exception(value: object) -> dict[str, Any] | None:
    exception = _dictionary(value)
    if exception is None:
        return None
    values = []
    for raw in _items(exception.get("values"), 10):
        item = _dictionary(raw)
        if item is None:
            continue
        name = _known(item.get("type"), _EXCEPTION_TYPES)
        module = item.get("module")
        if name is None and type(module) is str:
            source = _source(module)
            name = _known(item.get("type"), source.classes) if source else None
        clean: dict[str, Any] = {"type": name or "Error", "value": "[Filtered]"}
        stack = _stack(item.get("stacktrace"))
        if stack:
            clean["stacktrace"] = stack
        mechanism = _dictionary(item.get("mechanism"))
        if mechanism:
            safe: dict[str, Any] = {}
            kind = _known(mechanism.get("type"), _MECHANISMS)
            if kind:
                safe["type"] = kind
            for key in ("handled", "is_exception_group", "synthetic"):
                if type(mechanism.get(key)) is bool:
                    safe[key] = mechanism[key]
            for key in ("exception_id", "parent_id"):
                number = mechanism.get(key)
                if type(number) is int and 0 <= number <= 1000:
                    safe[key] = number
            if safe:
                clean["mechanism"] = safe
        values.append(clean)
    return {"values": values} if values else None


def _trace(value: object) -> dict[str, Any]:
    trace = _dictionary(value) or {}
    result: dict[str, Any] = {}
    for key, length in (("trace_id", 32), ("span_id", 16), ("parent_span_id", 16)):
        identity = _identifier(trace.get(key), length)
        if identity:
            result[key] = identity
    for key, allowed in (("op", _OPERATIONS), ("status", _STATUSES)):
        known = _known(trace.get(key), allowed)
        if known:
            result[key] = known
    for key in ("start_timestamp", "timestamp"):
        stamp = _timestamp(trace.get(key))
        if stamp is not None:
            result[key] = stamp
    return result


def _breadcrumbs(value: object) -> dict[str, Any] | None:
    breadcrumbs = _dictionary(value)
    if breadcrumbs is None:
        return None
    values = []
    for raw in _items(breadcrumbs.get("values"), 100):
        item = _dictionary(raw)
        if item is None:
            continue
        clean: dict[str, Any] = {"category": _known(item.get("category"), _CATEGORIES) or "other"}
        level = _known(item.get("level"), _LEVELS)
        if level:
            clean["level"] = level
        stamp = _timestamp(item.get("timestamp"))
        if stamp is not None:
            clean["timestamp"] = stamp
        if "message" in item:
            clean["message"] = "[Filtered]"
        values.append(clean)
    return {"values": values} if values else None


def _http_status(value: object) -> str | None:
    if type(value) is int and 100 <= value <= 599:
        return str(value)
    if type(value) is str and len(value) == 3 and value.isascii() and value.isdigit():
        number = int(value)
        return str(number) if 100 <= number <= 599 else None
    return None


def sanitize_event(event: object) -> dict[str, Any] | None:
    """Return a fresh metadata-only event, or drop on unknown/malformed input."""
    try:
        raw = _dictionary(event)
        if raw is None or raw.get("type") not in (None, "transaction"):
            return None
        transaction = raw.get("type") == "transaction"
        exception = _exception(raw.get("exception"))
        if not transaction and exception is None and "message" not in raw and "logentry" not in raw:
            return None
        clean: dict[str, Any] = {"platform": "python"}
        identity = _identifier(raw.get("event_id"), 32)
        if identity:
            clean["event_id"] = identity
        for key in ("timestamp", "start_timestamp"):
            stamp = _timestamp(raw.get(key))
            if stamp is not None:
                clean[key] = stamp
        for key, allowed in (("level", _LEVELS), ("environment", _ENVIRONMENTS)):
            known = _known(raw.get(key), allowed)
            if known:
                clean[key] = known
        release = raw.get("release")
        if type(release) is str and re.fullmatch(r"cashguard@\d+\.\d+\.\d+", release):
            clean["release"] = release
        if exception:
            clean["exception"] = exception
        if "message" in raw or "logentry" in raw:
            clean["message"] = "[Filtered]"
        stack = _stack(raw.get("stacktrace"))
        if stack:
            clean["stacktrace"] = stack
        contexts = _dictionary(raw.get("contexts")) or {}
        trace = _trace(contexts.get("trace"))
        if trace:
            clean["contexts"] = {"trace": trace}
        trace_data = _dictionary((_dictionary(contexts.get("trace")) or {}).get("data")) or {}
        tags = _dictionary(raw.get("tags")) or {}
        for container in (trace_data, tags):
            for key in ("http.status_code", "http.response.status_code"):
                status = _http_status(container.get(key))
                if status:
                    clean["tags"] = {"http.status_code": status}
        request = _dictionary(raw.get("request")) or {}
        method = _known(request.get("method"), _METHODS)
        if method:
            clean["request"] = {"method": method}
        breadcrumbs = _breadcrumbs(raw.get("breadcrumbs"))
        if breadcrumbs:
            clean["breadcrumbs"] = breadcrumbs
        if transaction:
            clean["type"] = "transaction"
            clean["transaction"] = trace.get("op", "transaction")
            clean["spans"] = [
                span for item in _items(raw.get("spans"), 500) if (span := _trace(item))
            ]
        elif "transaction" in raw:
            clean["transaction"] = trace.get("op", "application")
        return clean
    except Exception:
        # A privacy failure must not return the original event or leak it via logs.
        return None


def sanitize_envelope_headers(value: object) -> dict[str, Any]:
    """Drop dynamic sampling baggage and every unrecognized envelope header."""
    raw = _dictionary(value) or {}
    clean: dict[str, Any] = {}
    identity = _identifier(raw.get("event_id"), 32)
    if identity:
        clean["event_id"] = identity
    stamp = _timestamp(raw.get("sent_at"))
    if stamp is not None:
        clean["sent_at"] = stamp
    return clean
