"""Synthetic configuration/provenance checks; no vendor requests are performed."""

from __future__ import annotations

import secrets

import pytest

from app.core.config import settings
from app.market_data import get_live_provider
from app.market_data.live_source import LiveMarketSourceError, resolve_live_source
from app.market_data.mock_provider import MockMarketDataProvider


def configure(monkeypatch, provider, alpaca=False, polygon=False):
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", provider)
    monkeypatch.setattr(settings, "ALPACA_API_KEY", secrets.token_urlsafe(24) if alpaca else "")
    monkeypatch.setattr(settings, "ALPACA_API_SECRET", secrets.token_urlsafe(24) if alpaca else "")
    monkeypatch.setattr(settings, "POLYGON_API_KEY", secrets.token_urlsafe(24) if polygon else "")


@pytest.mark.parametrize(
    "provider,alpaca,polygon,expected",
    [
        ("auto", True, False, "alpaca"),
        ("auto", False, True, "polygon"),
        ("auto", True, True, "validated"),
        ("alpaca", True, False, "alpaca"),
        ("polygon", False, True, "polygon"),
        ("validated", True, True, "validated"),
    ],
)
def test_strict_live_resolution(monkeypatch, provider, alpaca, polygon, expected):
    configure(monkeypatch, provider, alpaca, polygon)
    assert resolve_live_source() == expected
    assert not isinstance(get_live_provider(), MockMarketDataProvider)


@pytest.mark.parametrize("provider", ["mock", "auto", "unknown", "alpaca", "polygon", "validated"])
def test_missing_or_mock_configuration_refuses(monkeypatch, provider):
    configure(monkeypatch, provider)
    with pytest.raises(LiveMarketSourceError):
        get_live_provider()


def test_incomplete_primary_cannot_silently_fall_back(monkeypatch):
    configure(monkeypatch, "auto", polygon=True)
    monkeypatch.setattr(settings, "ALPACA_API_KEY", secrets.token_urlsafe(24))
    with pytest.raises(LiveMarketSourceError):
        get_live_provider()


@pytest.mark.parametrize(
    "method", ["get_quote", "get_ohlcv", "is_market_open", "validate_staleness"]
)
def test_preexisting_mock_cannot_survive_runtime_switch_to_live(monkeypatch, method):
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    provider = MockMarketDataProvider()
    quote = provider.get_quote("FIXTURE")
    configure(monkeypatch, "alpaca", alpaca=True)
    with pytest.raises(LiveMarketSourceError):
        getattr(provider, method)(quote if method == "validate_staleness" else "FIXTURE")


def test_legacy_factory_cannot_fall_back_to_mock_in_live(monkeypatch):
    from app.market_data.polygon_provider import MarketDataProviderFactory

    configure(monkeypatch, "auto")
    with pytest.raises(LiveMarketSourceError):
        MarketDataProviderFactory.create()
