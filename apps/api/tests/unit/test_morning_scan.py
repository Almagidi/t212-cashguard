from __future__ import annotations

import pytest

from app.scanner.morning_scan import MorningScanner, ScanCandidate


async def _fake_scan_candidates() -> list[ScanCandidate]:
    return [
        ScanCandidate(
            ticker="AAPL",
            score=95.0,
            pre_market_rvol=2.2,
            gap_pct=1.4,
            atr_pct=0.0,
            current_price=190.0,
            avg_volume_30d=1000000,
            has_earnings=False,
            earnings_date=None,
            reason="Base candidate",
            strategy_type="orb",
        ),
        ScanCandidate(
            ticker="MSFT",
            score=90.0,
            pre_market_rvol=2.0,
            gap_pct=1.1,
            atr_pct=0.0,
            current_price=420.0,
            avg_volume_30d=900000,
            has_earnings=False,
            earnings_date=None,
            reason="Base candidate",
            strategy_type="orb",
        ),
    ]


@pytest.mark.asyncio
async def test_morning_scan_applies_catalyst_overlay(monkeypatch):
    from app.core.config import settings
    from app.scanner import morning_scan as scan_module

    scanner = MorningScanner()
    monkeypatch.setattr(settings, "ALPACA_API_KEY", "test")
    monkeypatch.setattr(settings, "ALPACA_API_SECRET", "test")

    async def fake_scan_alpaca(*args, **kwargs):
        del args, kwargs
        return await _fake_scan_candidates()

    monkeypatch.setattr(scanner, "_scan_alpaca", fake_scan_alpaca)

    async def fake_news(self, tickers, *, limit=8):
        del tickers, limit
        return [
            {
                "id": "n1",
                "source": "benzinga",
                "title": "Microsoft raises guidance",
                "summary": "Fresh catalyst",
                "tickers": ["MSFT"],
                "event_type": "guidance",
                "catalyst_score": 0.9,
            }
        ]

    monkeypatch.setattr(scan_module.NewsIntelligenceService, "get_watchlist_intelligence", fake_news)

    results = await scanner.scan(["AAPL", "MSFT"], max_results=2)

    assert [candidate.ticker for candidate in results] == ["MSFT", "AAPL"]
    assert results[0].catalyst_score == 0.9
    assert "Catalyst guidance" in results[0].reason
