from __future__ import annotations

from app.services.news_intelligence import NewsIntelligenceService


def test_news_intelligence_normalizes_benzinga_article():
    service = NewsIntelligenceService()
    payload = service._normalize_benzinga({
        "id": 123,
        "title": "Apple beats earnings and raises guidance",
        "teaser": "Strong iPhone growth sends shares higher.",
        "url": "https://example.com/news/apple",
        "created": "2026-04-11T08:30:00Z",
        "stocks": [{"name": "AAPL"}],
    })

    assert payload["source"] == "benzinga"
    assert payload["event_type"] == "earnings"
    assert payload["tickers"] == ["AAPL"]
    assert payload["catalyst_score"] > 0


def test_news_intelligence_normalizes_polygon_article():
    service = NewsIntelligenceService()
    payload = service._normalize_polygon({
        "id": "abc",
        "title": "Analyst upgrade lifts Nvidia shares",
        "description": "Broker raises price target after strong demand signals.",
        "article_url": "https://example.com/news/nvda",
        "published_utc": "2026-04-11T09:30:00Z",
        "tickers": ["NVDA"],
    })

    assert payload["source"] == "polygon"
    assert payload["event_type"] == "analyst_note"
    assert payload["tickers"] == ["NVDA"]
    assert payload["sentiment_score"] > 0
