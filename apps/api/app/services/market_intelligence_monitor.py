"""Stateful market-intelligence monitor for alerting on regime and feed changes."""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import select

from app.db.models import AppSettings
from app.services.alert_service import (
    alert_feed_health_issue,
    alert_feed_health_recovered,
    alert_regime_shift,
)
from app.services.feed_health import get_feed_health_snapshot
from app.services.market_regime import MarketRegimeService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class MarketIntelligenceMonitor:
    STATE_KEY: ClassVar[str] = "market_intelligence_monitor"
    ALERT_REGIMES: ClassVar[set[str]] = {"risk_off", "unsafe", "high_volatility"}

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def evaluate_and_alert(self) -> dict[str, Any]:
        regime = await MarketRegimeService().evaluate()
        feed_health = get_feed_health_snapshot()
        settings_row = await self._get_settings()
        previous = dict((settings_row.extra or {}).get(self.STATE_KEY, {}))

        await self._maybe_alert_regime(previous, regime)
        await self._maybe_alert_feed(previous, feed_health)

        extra = dict(settings_row.extra or {})
        extra[self.STATE_KEY] = {
            "last_regime": regime.get("regime"),
            "last_feed_status": feed_health.get("status"),
            "last_feed_symbols": self._affected_symbols(feed_health),
        }
        settings_row.extra = extra
        await self.db.flush()

        return {
            "regime": regime,
            "feed_health": deepcopy(feed_health),
        }

    async def _get_settings(self) -> AppSettings:
        result = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        settings_row = result.scalar_one_or_none()
        if not settings_row:
            settings_row = AppSettings(id=1)
            self.db.add(settings_row)
            await self.db.flush()
        return settings_row

    async def _maybe_alert_regime(
        self,
        previous: dict[str, Any],
        regime: dict[str, Any],
    ) -> None:
        previous_regime = str(previous.get("last_regime") or "")
        current_regime = str(regime.get("regime") or "unknown")
        if previous_regime == current_regime:
            return
        if current_regime not in self.ALERT_REGIMES and previous_regime not in self.ALERT_REGIMES:
            return
        await alert_regime_shift(
            self.db,
            previous_regime=previous_regime or None,
            current_regime=current_regime,
            detail=str(regime.get("detail") or ""),
            suppressed_strategies=list(regime.get("suppressed_strategies", [])),
        )

    async def _maybe_alert_feed(
        self,
        previous: dict[str, Any],
        feed_health: dict[str, Any],
    ) -> None:
        previous_status = str(previous.get("last_feed_status") or "unknown")
        current_status = str(feed_health.get("status") or "unknown")
        previous_symbols = list(previous.get("last_feed_symbols") or [])
        current_symbols = self._affected_symbols(feed_health)

        if current_status in {"ok", "fallback"}:
            if previous_status not in {"ok", "fallback", "unknown"}:
                await alert_feed_health_recovered(
                    self.db,
                    provider=str(feed_health.get("provider") or "unknown"),
                )
            return

        if previous_status == current_status and previous_symbols == current_symbols:
            return

        await alert_feed_health_issue(
            self.db,
            status=current_status,
            provider=str(feed_health.get("provider") or "unknown"),
            detail=str(feed_health.get("detail") or ""),
            affected_symbols=current_symbols,
        )

    def _affected_symbols(self, feed_health: dict[str, Any]) -> list[str]:
        affected = [
            str(symbol.get("ticker") or "").upper()
            for symbol in feed_health.get("symbols", [])
            if str(symbol.get("status") or "unknown") not in {"ok", "fallback"}
        ]
        return sorted(symbol for symbol in affected if symbol)[:10]
