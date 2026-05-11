"""Optional structured watchlist news and catalyst scoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import settings


class NewsIntelligenceService:
    async def get_watchlist_intelligence(
        self,
        tickers: list[str],
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        symbols = [ticker.upper() for ticker in tickers if ticker]
        if not symbols:
            return []

        if settings.APP_MODE == "mock" or settings.MARKET_DATA_PROVIDER == "mock":
            return []

        items = await self._fetch_benzinga(symbols, limit=limit)
        if items:
            return items[:limit]

        items = await self._fetch_polygon(symbols, limit=limit)
        return items[:limit]

    async def _fetch_benzinga(self, tickers: list[str], *, limit: int) -> list[dict[str, Any]]:
        if not settings.BENZINGA_API_KEY:
            return []
        params = {
            "token": settings.BENZINGA_API_KEY,
            "tickers": ",".join(tickers),
            "pagesize": limit,
            "displayOutput": "full",
            "sort": "updated:desc",
        }
        async with httpx.AsyncClient(base_url=settings.BENZINGA_BASE_URL, timeout=10.0) as client:
            resp = await client.get("/api/v2/news", params=params)
            resp.raise_for_status()
            payload = resp.json()

        articles = payload if isinstance(payload, list) else payload.get("data", [])
        return [self._normalize_benzinga(article) for article in articles if article]

    async def _fetch_polygon(self, tickers: list[str], *, limit: int) -> list[dict[str, Any]]:
        if not settings.POLYGON_API_KEY:
            return []
        params = {
            "apiKey": settings.POLYGON_API_KEY,
            "ticker": ",".join(tickers),
            "limit": limit,
            "order": "desc",
            "sort": "published_utc",
        }
        async with httpx.AsyncClient(base_url="https://api.polygon.io", timeout=10.0) as client:
            resp = await client.get("/v2/reference/news", params=params)
            resp.raise_for_status()
            payload = resp.json()

        return [self._normalize_polygon(item) for item in payload.get("results", []) if item]

    def _normalize_benzinga(self, article: dict[str, Any]) -> dict[str, Any]:
        tickers = [
            str(item.get("name", "")).upper()
            for item in article.get("stocks", [])
            if item.get("name")
        ]
        published = self._parse_datetime(article.get("created"))
        title = str(article.get("title") or "").strip()
        summary = str(article.get("teaser") or article.get("body") or "").strip()
        event_type = self._classify_event(title, summary)
        urgency = self._score_urgency(published)
        credibility = 0.85
        sentiment = self._sentiment_score(title, summary)
        return {
            "id": str(article.get("id") or article.get("url") or title),
            "source": "benzinga",
            "title": title,
            "summary": summary[:400],
            "url": article.get("url"),
            "published_at": published,
            "tickers": tickers,
            "event_type": event_type,
            "sentiment_score": sentiment,
            "urgency_score": urgency,
            "credibility_score": credibility,
            "impact_horizon": self._impact_horizon(event_type),
            "catalyst_score": round(
                (urgency * 0.35) + (credibility * 0.30) + (abs(sentiment) * 0.35), 2
            ),
        }

    def _normalize_polygon(self, article: dict[str, Any]) -> dict[str, Any]:
        title = str(article.get("title") or "").strip()
        summary = str(article.get("description") or article.get("summary") or "").strip()
        published = self._parse_datetime(article.get("published_utc"))
        event_type = self._classify_event(title, summary)
        urgency = self._score_urgency(published)
        credibility = 0.75
        sentiment = self._sentiment_score(title, summary)
        return {
            "id": str(article.get("id") or article.get("article_url") or title),
            "source": "polygon",
            "title": title,
            "summary": summary[:400],
            "url": article.get("article_url"),
            "published_at": published,
            "tickers": [str(item).upper() for item in article.get("tickers", [])],
            "event_type": event_type,
            "sentiment_score": sentiment,
            "urgency_score": urgency,
            "credibility_score": credibility,
            "impact_horizon": self._impact_horizon(event_type),
            "catalyst_score": round(
                (urgency * 0.35) + (credibility * 0.30) + (abs(sentiment) * 0.35), 2
            ),
        }

    def _parse_datetime(self, value: Any) -> str | None:
        if not value:
            return None
        raw = str(value).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw).astimezone(UTC).isoformat()
        except ValueError:
            return None

    def _score_urgency(self, published_at: str | None) -> float:
        if not published_at:
            return 0.25
        ts = datetime.fromisoformat(published_at)
        age = datetime.now(UTC) - ts
        if age <= timedelta(hours=1):
            return 1.0
        if age <= timedelta(hours=6):
            return 0.8
        if age <= timedelta(days=1):
            return 0.55
        return 0.3

    def _impact_horizon(self, event_type: str) -> str:
        if event_type in {"earnings", "guidance", "m&a", "legal_regulatory"}:
            return "multi_day"
        if event_type in {"analyst_note", "product_launch", "sector_readthrough"}:
            return "intraday_to_multi_day"
        return "intraday"

    def _classify_event(self, title: str, summary: str) -> str:
        text = f"{title} {summary}".lower()
        if "earnings" in text or "eps" in text:
            return "earnings"
        if "guidance" in text:
            return "guidance"
        if "analyst" in text or "price target" in text or "upgrade" in text or "downgrade" in text:
            return "analyst_note"
        if "merger" in text or "acquisition" in text or "buyout" in text:
            return "m&a"
        if "sec" in text or "lawsuit" in text or "regulator" in text:
            return "legal_regulatory"
        if "launch" in text or "product" in text:
            return "product_launch"
        if "sector" in text or "peer" in text:
            return "sector_readthrough"
        return "general"

    def _sentiment_score(self, title: str, summary: str) -> float:
        text = f"{title} {summary}".lower()
        positives = ("beats", "upgrade", "surge", "strong", "record", "growth", "wins")
        negatives = ("misses", "downgrade", "cuts", "warns", "probe", "lawsuit", "falls")
        score = 0.0
        if any(word in text for word in positives):
            score += 0.6
        if any(word in text for word in negatives):
            score -= 0.6
        return max(-1.0, min(1.0, score))
