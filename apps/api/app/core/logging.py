"""
Structured logging — compatible with uvicorn and stdlib logging.
"""
from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar

import structlog

from app.core.config import settings

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def get_trace_id() -> str:
    return _trace_id.get() or str(uuid.uuid4())[:8]


def set_trace_id(trace_id: str) -> None:
    _trace_id.set(trace_id)


def _add_trace_id(logger: object, method: str, event_dict: dict) -> dict:
    event_dict["trace_id"] = get_trace_id()
    return event_dict


def _add_app_context(logger: object, method: str, event_dict: dict) -> dict:
    event_dict["app_mode"] = settings.APP_MODE
    return event_dict


def configure_logging() -> None:
    """
    Configure structlog to work alongside uvicorn's stdlib logging.
    Uses stdlib integration (not PrintLoggerFactory) to avoid the
    'PrintLogger has no attribute name' error.
    """
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_trace_id,
        _add_app_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.DEBUG:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    # Use stdlib integration — this is what makes it compatible with uvicorn
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Keep uvicorn loggers working normally
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
