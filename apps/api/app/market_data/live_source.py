"""Resolve authoritative live provider configuration without network activity.

This boundary establishes the selected vendor, not feed entitlement or quote
freshness. Those still require observed vendor data; a configured name alone is
never a claim that a request succeeded.
"""

from app.core.config import settings


class LiveMarketSourceError(RuntimeError):
    """Live provider configuration cannot establish a real source."""


def resolve_live_source() -> str:
    """Strict resolution shared by startup, construction and dispatch.

    Re-read settings each time so removing credentials or changing configuration
    cannot silently make a running live caller fall back to synthetic data.
    No credential value is returned, recorded or included in errors.
    """
    configured = settings.MARKET_DATA_PROVIDER.strip().lower()
    key = bool(settings.ALPACA_API_KEY.strip())
    secret = bool(settings.ALPACA_API_SECRET.strip())
    polygon = bool(settings.POLYGON_API_KEY.strip())
    if configured == "auto":
        if key != secret:
            raise LiveMarketSourceError("Live primary market-data credentials are incomplete.")
        if key and secret:
            return "validated" if polygon else "alpaca"
        if polygon:
            return "polygon"
    elif configured == "alpaca" and key and secret:
        return "alpaca"
    elif configured == "polygon" and polygon:
        return "polygon"
    elif configured in {"validated", "alpaca_primary_polygon_validator"}:
        if key and secret and polygon:
            return "validated"
    raise LiveMarketSourceError(
        "Live market-data source is mock, unknown, or missing its required credentials."
    )


def require_live_source() -> str | None:
    """Keep isolated mock/paper behavior; actual live callers must resolve."""
    return resolve_live_source() if settings.APP_MODE == "live" else None
