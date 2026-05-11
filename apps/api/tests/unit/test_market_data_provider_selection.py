from __future__ import annotations


def test_market_data_provider_mock_overrides_external_credentials(monkeypatch):
    from app.core.config import settings
    from app.market_data import get_live_provider, get_provider_name
    from app.market_data.mock_provider import MockMarketDataProvider

    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")
    monkeypatch.setattr(settings, "ALPACA_API_KEY", "configured")
    monkeypatch.setattr(settings, "ALPACA_API_SECRET", "configured")
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "configured")

    assert get_provider_name() == "mock"
    assert isinstance(get_live_provider(), MockMarketDataProvider)
