"""Account routes — summary, cash guard status."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_broker, get_current_user
from app.api.schemas import AccountSummaryOut, CashGuardStatus
from app.api.v1.routes._broker_errors import broker_http_exception
from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.core.config import settings

router = APIRouter(prefix="/account", tags=["account"])

_ACCOUNT_SUMMARY_CACHE_TTL_SECONDS = 20.0
_account_summary_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_account_summary_cache_lock = asyncio.Lock()


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, dict):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_number(mapping: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in mapping:
            return _to_float(mapping[key], default)
    return default


def _normalise_account_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Normalise Trading 212/mock account summary shapes.

    Trading 212 may return cash as a nested object. In that shape, top-level
    ``total`` is account total value, not cash total. Therefore nested cash
    values must be read from ``summary["cash"]`` first, otherwise cash can be
    incorrectly inflated to total account value.
    """
    cash_raw = summary.get("cash")
    invested = _first_number(summary, "invested")
    result = _first_number(summary, "result")

    if isinstance(cash_raw, dict):
        available_to_trade = _first_number(
            cash_raw,
            "availableToTrade",
            "free",
            "available",
        )
        reserved = _first_number(
            cash_raw, "blockedForPendingOrders", "blocked", "reserved"
        ) + _first_number(cash_raw, "inPies")

        # Important: do not read top-level summary["total"] here. That is the
        # account total value, not cash total, for Trading 212 nested cash data.
        cash = _first_number(
            cash_raw,
            "total",
            "cashTotal",
            "totalCash",
            default=available_to_trade + reserved,
        )
        free = _first_number(
            cash_raw,
            "free",
            "availableToTrade",
            "available",
            default=available_to_trade,
        )
    else:
        cash = _to_float(cash_raw)
        free = _first_number(summary, "free", "availableToTrade", default=cash)

    total = _first_number(summary, "total", "totalValue", default=cash + invested + result)
    currency = summary.get("currency") or summary.get("currencyCode") or "USD"

    return {
        "total_value": total,
        "cash": cash,
        "free_funds": free,
        "invested": invested,
        "result": result,
        "currency": str(currency),
    }


def _account_summary_cache_key(broker: Any) -> tuple[str, str, str]:
    return (
        settings.APP_MODE,
        str(getattr(broker, "environment", "unknown")),
        str(getattr(broker, "base_url", "unknown")),
    )


async def _cached_account_summary(broker: Any) -> dict[str, Any]:
    cache_key = _account_summary_cache_key(broker)
    now = time.monotonic()
    cached = _account_summary_cache.get(cache_key)
    if cached is not None:
        expires_at, summary = cached
        if expires_at > now:
            return dict(summary)

    async with _account_summary_cache_lock:
        now = time.monotonic()
        cached = _account_summary_cache.get(cache_key)
        if cached is not None:
            expires_at, summary = cached
            if expires_at > now:
                return dict(summary)

        async with broker as b:
            summary = await b.get_account_summary()

        cached_summary = dict(summary)
        _account_summary_cache[cache_key] = (
            time.monotonic() + _ACCOUNT_SUMMARY_CACHE_TTL_SECONDS,
            cached_summary,
        )
        return dict(cached_summary)


@router.get("/summary", response_model=AccountSummaryOut)
async def account_summary(
    _: object = Depends(get_current_user),
    broker: Any = Depends(get_broker),
) -> AccountSummaryOut:
    try:
        summary = await _cached_account_summary(broker)
    except (T212RateLimitError, T212AuthError, T212APIError) as exc:
        raise broker_http_exception(exc) from exc

    normalised = _normalise_account_summary(summary)

    return AccountSummaryOut(
        total_value=normalised["total_value"],
        cash=normalised["cash"],
        free_funds=normalised["free_funds"],
        invested=normalised["invested"],
        result=normalised["result"],
        currency=normalised["currency"],
        synced_at=datetime.now(UTC),
        mode=settings.APP_MODE,
    )


@router.get("/cash-guard", response_model=CashGuardStatus)
async def cash_guard_status(
    _: object = Depends(get_current_user),
    broker: Any = Depends(get_broker),
) -> CashGuardStatus:
    try:
        summary = await _cached_account_summary(broker)
    except (T212RateLimitError, T212AuthError, T212APIError) as exc:
        raise broker_http_exception(exc) from exc

    normalised = _normalise_account_summary(summary)
    free = normalised["free_funds"]
    cash = normalised["cash"]
    return CashGuardStatus(
        available_to_trade=free,
        reserved=max(0.0, cash - free),
        total_cash=cash,
        cash_only_mode=True,  # Hardcoded — never False
        currency=normalised["currency"],
    )
