"""Helpers for translating broker adapter failures into API responses."""
from __future__ import annotations

import math

from fastapi import HTTPException

from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError


def broker_http_exception(exc: Exception) -> HTTPException:
    """Return a user-facing HTTP error for known broker adapter exceptions."""
    if isinstance(exc, T212RateLimitError):
        retry_after = max(1, int(math.ceil(exc.retry_after)))
        return HTTPException(
            status_code=429,
            detail={
                "code": "broker_rate_limited",
                "message": (
                    "Trading 212 is rate limiting account data requests. "
                    "Wait a moment before refreshing broker-backed dashboard data."
                ),
            },
            headers={"Retry-After": str(retry_after)},
        )

    if isinstance(exc, T212AuthError):
        return HTTPException(
            status_code=502,
            detail={
                "code": "broker_auth_rejected",
                "message": (
                    "Trading 212 rejected the configured broker credentials. "
                    "Reconnect the broker account before using broker-backed dashboard data."
                ),
            },
        )

    if isinstance(exc, T212APIError):
        return HTTPException(
            status_code=502,
            detail={
                "code": "broker_unavailable",
                "message": (
                    "Trading 212 returned an error while loading broker-backed dashboard data. "
                    "Try again later or use the mock manual QA path for operator testing."
                ),
            },
        )

    raise exc
