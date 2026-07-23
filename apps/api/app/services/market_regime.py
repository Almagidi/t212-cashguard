"""Market regime classification from live market data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from statistics import pstdev
from typing import Any, Final

from app.market_data import get_live_provider
from app.services.feed_health import get_feed_health_snapshot

_CACHE_TTL_SECONDS: Final = 60
_cached_regime: dict[str, Any] | None = None
_last_evaluated_at: datetime | None = None


@dataclass
class _SeriesSnapshot:
    ticker: str
    closes: list[Decimal]
    intraday_closes: list[Decimal]
    intraday_volumes: list[Decimal]


class MarketRegimeService:
    BENCHMARKS = ("SPY", "QQQ", "IWM")

    async def evaluate(self) -> dict[str, Any]:
        """
        Evaluate current market regime.
        Results are cached for 60 seconds to avoid redundant network I/O
        to market data providers.
        """
        global _cached_regime, _last_evaluated_at

        now = datetime.now(UTC)
        feed = get_feed_health_snapshot()
        if feed["status"] in {"stale", "error"}:
            _cached_regime = None
            _last_evaluated_at = None
            return self._build_payload(
                regime="unsafe",
                label="Unsafe / Feed Risk",
                color="red",
                confidence=0.95,
                adx=8.0,
                vol_percentile=95.0,
                breadth_pct=0.0,
                primary_trend="unsafe",
                active_strategies=[],
                suppressed_strategies=[
                    "orb",
                    "opening_fade",
                    "vwap_reclaim",
                    "closing_momentum",
                    "intraday_periodicity",
                ],
                detail="Primary market-data feed is stale or errored; new entries should remain paused.",
            )

        if (
            _cached_regime is not None
            and _last_evaluated_at is not None
            and (now - _last_evaluated_at).total_seconds() < _CACHE_TTL_SECONDS
        ):
            return _cached_regime

        snapshots = await self._load_snapshots()
        if not snapshots:
            return self._build_payload(
                regime="unknown",
                label="Unknown",
                color="zinc",
                confidence=0.15,
                adx=10.0,
                vol_percentile=50.0,
                breadth_pct=0.0,
                primary_trend="unknown",
                active_strategies=[],
                suppressed_strategies=[],
                detail="Not enough benchmark data to classify regime.",
            )

        breadth = self._breadth_pct(snapshots)
        spy = next((item for item in snapshots if item.ticker == "SPY"), snapshots[0])
        short_ema = self._ema(spy.closes, 10)
        medium_ema = self._ema(spy.closes, 20)
        long_ema = self._ema(spy.closes, 50)
        last_close = spy.closes[-1]
        intraday_vwap = self._vwap(spy.intraday_closes, spy.intraday_volumes)
        realized_vol = self._realized_vol_pct(spy.closes[-20:])
        vol_percentile = max(5.0, min(99.0, realized_vol * 4.0))
        trend_strength = min(
            50.0,
            float(
                abs((short_ema - medium_ema) / max(last_close, Decimal("0.01"))) * Decimal("1200")
            )
            + abs(breadth - 0.5) * 35.0,
        )

        regime = "ranging"
        label = "Range-Bound"
        color = "yellow"
        primary_trend = "mixed"

        if breadth <= 0.34 and last_close < medium_ema and short_ema < medium_ema:
            primary_trend = "down"
            if vol_percentile >= 70:
                regime, label, color = "risk_off", "Risk-Off", "red"
            else:
                regime, label, color = "trending_down", "Trending Down", "red"
        elif (
            breadth >= 0.67
            and last_close > medium_ema
            and short_ema > medium_ema
            and last_close >= intraday_vwap
        ):
            primary_trend = "up"
            regime, label, color = "trending_up", "Trending Up", "emerald"
        elif vol_percentile >= 78:
            regime, label, color = "high_volatility", "High Volatility", "orange"
        elif last_close < long_ema and breadth < 0.5:
            regime, label, color = "risk_off", "Risk-Off", "red"
            primary_trend = "down"

        confidence = max(0.2, min(0.95, trend_strength / 50.0))
        active, suppressed = self._strategy_policy(regime)
        detail = (
            f"SPY {last_close:.2f} vs EMA20 {medium_ema:.2f}; breadth {breadth * 100:.0f}%; "
            f"vol score {vol_percentile:.1f}."
        )
        payload = self._build_payload(
            regime=regime,
            label=label,
            color=color,
            confidence=confidence,
            adx=round(trend_strength, 1),
            vol_percentile=round(vol_percentile, 1),
            breadth_pct=round(breadth * 100.0, 1),
            primary_trend=primary_trend,
            active_strategies=active,
            suppressed_strategies=suppressed,
            detail=detail,
        )

        # Cache the successful evaluation
        if regime != "unknown":
            _cached_regime = payload
            _last_evaluated_at = now

        return payload

    async def _load_snapshots(self) -> list[_SeriesSnapshot]:
        provider = get_live_provider()
        snapshots: list[_SeriesSnapshot] = []
        if hasattr(provider, "__aenter__"):
            async with provider as md:
                for ticker in self.BENCHMARKS:
                    daily = await md.get_bars(ticker, multiplier=1, timespan="day", limit=90)
                    intraday = await md.get_bars(ticker, multiplier=5, timespan="minute", limit=78)
                    snapshot = self._snapshot_from_bars(ticker, daily, intraday)
                    if snapshot is not None:
                        snapshots.append(snapshot)
        else:
            # Mock provider has no async context manager (see strategy_runner.py's
            # identical hasattr("__aenter__") branch for _fetch_market_context) —
            # reuse its sync get_ohlcv() so mock mode gets real snapshot data
            # instead of silently falling through to an empty snapshot list.
            for ticker in self.BENCHMARKS:
                daily = provider.get_ohlcv(ticker, interval_minutes=1440, bars=90)
                intraday = provider.get_ohlcv(ticker, interval_minutes=5, bars=78)
                snapshot = self._snapshot_from_bars(ticker, daily, intraday)
                if snapshot is not None:
                    snapshots.append(snapshot)
        return snapshots

    def _snapshot_from_bars(self, ticker: str, daily: Any, intraday: Any) -> _SeriesSnapshot | None:
        closes = [
            Decimal(str(self._bar_field(bar, "close")))
            for bar in daily
            if self._bar_field(bar, "close") is not None
        ]
        intraday_closes = [
            Decimal(str(self._bar_field(bar, "close")))
            for bar in intraday
            if self._bar_field(bar, "close") is not None
        ]
        intraday_volumes = [
            Decimal(str(self._bar_field(bar, "volume")))
            for bar in intraday
            if self._bar_field(bar, "volume") is not None
        ]
        if len(closes) >= 20 and intraday_closes:
            return _SeriesSnapshot(ticker, closes, intraday_closes, intraday_volumes)
        return None

    def _bar_field(self, bar: Any, field: str) -> Any:
        if isinstance(bar, dict):
            return bar.get(field)
        return getattr(bar, field, None)

    def _ema(self, values: list[Decimal], period: int) -> Decimal:
        if not values:
            return Decimal("0")
        alpha = Decimal("2") / Decimal(period + 1)
        ema = values[0]
        for value in values[1:]:
            ema = (value * alpha) + (ema * (Decimal("1") - alpha))
        return ema

    def _vwap(self, prices: list[Decimal], volumes: list[Decimal]) -> Decimal:
        if not prices or not volumes or len(prices) != len(volumes):
            return prices[-1] if prices else Decimal("0")
        total_volume = sum(volumes, Decimal("0"))
        if total_volume <= 0:
            return prices[-1]
        total = sum((price * volume) for price, volume in zip(prices, volumes, strict=True))
        return total / total_volume

    def _realized_vol_pct(self, closes: list[Decimal]) -> float:
        if len(closes) < 3:
            return 0.0
        returns: list[float] = []
        for prev, curr in pairwise(closes):
            if prev <= 0:
                continue
            returns.append(float((curr - prev) / prev))
        if len(returns) < 2:
            return 0.0
        return float(pstdev(returns) * 100 * (252**0.5))

    def _breadth_pct(self, snapshots: list[_SeriesSnapshot]) -> float:
        votes = 0
        for snapshot in snapshots:
            ema20 = self._ema(snapshot.closes, 20)
            if snapshot.closes[-1] >= ema20:
                votes += 1
        return votes / max(len(snapshots), 1)

    def _strategy_policy(self, regime: str) -> tuple[list[str], list[str]]:
        if regime == "trending_up":
            return (
                ["orb", "closing_momentum", "intraday_periodicity", "vwap_reclaim"],
                ["opening_fade"],
            )
        if regime in {"trending_down", "risk_off", "unsafe"}:
            return (
                ["opening_fade"],
                ["orb", "closing_momentum", "intraday_periodicity"],
            )
        if regime == "high_volatility":
            return (
                ["opening_fade", "vwap_reclaim"],
                ["closing_momentum"],
            )
        return (
            ["vwap_reclaim", "opening_fade"],
            ["closing_momentum"],
        )

    def _build_payload(
        self,
        *,
        regime: str,
        label: str,
        color: str,
        confidence: float,
        adx: float,
        vol_percentile: float,
        breadth_pct: float,
        primary_trend: str,
        active_strategies: list[str],
        suppressed_strategies: list[str],
        detail: str,
    ) -> dict[str, Any]:
        return {
            "regime": regime,
            "label": label,
            "color": color,
            "adx": adx,
            "vol_percentile": vol_percentile,
            "confidence": round(confidence, 2),
            "breadth_pct": breadth_pct,
            "primary_trend": primary_trend,
            "active_strategies": active_strategies,
            "suppressed_strategies": suppressed_strategies,
            "detail": detail,
        }
