"""In-memory feed-health registry for primary/validator market data checks."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal

FeedStatus = Literal["ok", "degraded", "stale", "fallback", "error", "unknown"]

_MAX_SYMBOLS = 50
_state: dict[str, Any] = {
    "status": "unknown",
    "provider": "unknown",
    "checked_at": None,
    "detail": "No market-data validation has run yet.",
    "symbols": {},
}


def reset_feed_health() -> None:
    _state["status"] = "unknown"
    _state["provider"] = "unknown"
    _state["checked_at"] = None
    _state["detail"] = "No market-data validation has run yet."
    _state["symbols"] = {}


def record_feed_health(
    *,
    provider: str,
    ticker: str,
    status: FeedStatus,
    detail: str,
    used_source: str,
    validator_source: str | None = None,
    fallback_used: bool = False,
    primary_timestamp: datetime | None = None,
    validator_timestamp: datetime | None = None,
    divergence_pct: float | None = None,
) -> None:
    checked_at = datetime.now(UTC)
    symbols: dict[str, Any] = _state["symbols"]
    symbols[ticker.upper()] = {
        "ticker": ticker.upper(),
        "status": status,
        "detail": detail,
        "used_source": used_source,
        "validator_source": validator_source,
        "fallback_used": fallback_used,
        "primary_timestamp": primary_timestamp,
        "validator_timestamp": validator_timestamp,
        "divergence_pct": divergence_pct,
        "checked_at": checked_at,
    }

    if len(symbols) > _MAX_SYMBOLS:
        oldest = min(
            symbols.items(),
            key=lambda item: item[1].get("checked_at") or datetime.min.replace(tzinfo=UTC),
        )[0]
        symbols.pop(oldest, None)

    _state["provider"] = provider
    _state["checked_at"] = checked_at
    _state["detail"] = detail
    _state["status"] = _summarize_status(symbols.values())


def get_feed_health_snapshot() -> dict[str, Any]:
    symbols = sorted(
        deepcopy(list(_state["symbols"].values())),
        key=lambda item: item.get("checked_at") or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return {
        "status": _state["status"],
        "provider": _state["provider"],
        "checked_at": _state["checked_at"],
        "detail": _state["detail"],
        "symbols": symbols,
    }


def is_symbol_trade_safe(ticker: str) -> bool:
    symbol = _state["symbols"].get(ticker.upper())
    if not symbol:
        return True
    return symbol["status"] in {"ok", "fallback"}


def _summarize_status(symbols: Any) -> FeedStatus:
    statuses = {symbol.get("status", "unknown") for symbol in symbols}
    if not statuses:
        return "unknown"
    if "error" in statuses:
        return "error"
    if "stale" in statuses:
        return "stale"
    if "degraded" in statuses:
        return "degraded"
    if "fallback" in statuses:
        return "fallback"
    if statuses == {"ok"}:
        return "ok"
    return "unknown"
