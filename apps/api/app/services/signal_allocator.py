"""Portfolio-level allocation gate for strategy signals.

The allocator sits between "a strategy found a valid setup" and "the broker
may receive an order". RiskEngine still has final authority, but this layer
decides whether a setup deserves scarce portfolio capital in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from app.core.config import settings
from app.risk.correlation import get_sector

AllocationStatus = Literal["allocated", "rejected"]


def _clamp(value: float, floor: float = 0.0, ceiling: float = 1.0) -> float:
    return max(floor, min(value, ceiling))


def _as_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if value is None:
            return default
        return Decimal(str(value))
    except Exception:
        return default


@dataclass(slots=True)
class AllocatorCandidate:
    ticker: str
    side: str
    strategy_id: str
    strategy_name: str
    strategy_type: str
    signal_type: str
    confidence: Decimal | None
    entry_price: Decimal
    quantity: Decimal
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    watchlist_context: dict[str, Any] = field(default_factory=dict)
    target_weight: Decimal | None = None
    delta_value: Decimal | None = None

    @property
    def notional(self) -> Decimal:
        return abs(self.entry_price * self.quantity)

    @property
    def estimated_risk(self) -> Decimal:
        if self.stop_price is not None and self.entry_price > 0:
            return abs(self.entry_price - self.stop_price) * abs(self.quantity)
        # Portfolio sleeves do not always have an explicit stop. Use a
        # conservative placeholder so allocator heat still accounts for them.
        return self.notional * Decimal("0.02")


@dataclass(slots=True)
class AllocationState:
    accepted_tickers: set[str] = field(default_factory=set)
    allocated_value: Decimal = Decimal("0")
    allocated_risk: Decimal = Decimal("0")
    decisions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class AllocationDecision:
    ticker: str
    side: str
    strategy_id: str
    strategy_name: str
    strategy_type: str
    signal_type: str
    status: AllocationStatus
    score: float
    threshold: float
    reason: str
    rank: int | None
    components: dict[str, float]
    penalties: dict[str, float]
    allocated_risk_pct: float
    projected_gross_exposure_pct: float
    projected_symbol_exposure_pct: float
    regime_cap_pct: float
    generated_at: datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "strategy_type": self.strategy_type,
            "signal_type": self.signal_type,
            "status": self.status,
            "score": round(self.score, 4),
            "threshold": round(self.threshold, 4),
            "reason": self.reason,
            "rank": self.rank,
            "components": {key: round(value, 4) for key, value in self.components.items()},
            "penalties": {key: round(value, 4) for key, value in self.penalties.items()},
            "allocated_risk_pct": round(self.allocated_risk_pct, 4),
            "projected_gross_exposure_pct": round(self.projected_gross_exposure_pct, 4),
            "projected_symbol_exposure_pct": round(self.projected_symbol_exposure_pct, 4),
            "regime_cap_pct": round(self.regime_cap_pct, 4),
            "generated_at": self.generated_at.isoformat(),
        }


class SignalAllocator:
    """Scores and gates simultaneous strategy candidates against portfolio limits."""

    def new_state(self) -> AllocationState:
        return AllocationState()

    def allocate_one(
        self,
        candidate: AllocatorCandidate,
        *,
        account_value: Decimal,
        available_cash: Decimal,
        current_positions: list[dict[str, Any]],
        regime: dict[str, Any] | None = None,
        state: AllocationState | None = None,
        rank: int | None = None,
    ) -> AllocationDecision:
        active_state = state or self.new_state()
        decision = self._decide(
            candidate,
            account_value=account_value,
            available_cash=available_cash,
            current_positions=current_positions,
            regime=regime or {},
            state=active_state,
            rank=rank,
        )
        if decision.status == "allocated":
            active_state.accepted_tickers.add(candidate.ticker.upper())
            active_state.allocated_value += candidate.notional if candidate.side == "buy" else Decimal("0")
            active_state.allocated_risk += candidate.estimated_risk if candidate.side == "buy" else Decimal("0")
        active_state.decisions.append(decision.to_payload())
        return decision

    def allocate_batch(
        self,
        candidates: list[AllocatorCandidate],
        *,
        account_value: Decimal,
        available_cash: Decimal,
        current_positions: list[dict[str, Any]],
        regime: dict[str, Any] | None = None,
        state: AllocationState | None = None,
    ) -> list[AllocationDecision]:
        active_state = state or self.new_state()
        prelim = [
            (
                self._pre_score(candidate, regime or {}),
                candidate,
            )
            for candidate in candidates
        ]
        prelim.sort(key=lambda item: (0 if item[1].side == "sell" else 1, -item[0]))
        decisions: list[AllocationDecision] = []
        for rank, (_, candidate) in enumerate(prelim, start=1):
            decisions.append(
                self.allocate_one(
                    candidate,
                    account_value=account_value,
                    available_cash=available_cash,
                    current_positions=current_positions,
                    regime=regime,
                    state=active_state,
                    rank=rank,
                )
            )
        return decisions

    def _decide(
        self,
        candidate: AllocatorCandidate,
        *,
        account_value: Decimal,
        available_cash: Decimal,
        current_positions: list[dict[str, Any]],
        regime: dict[str, Any],
        state: AllocationState,
        rank: int | None,
    ) -> AllocationDecision:
        threshold = float(settings.PORTFOLIO_ALLOCATOR_MIN_SCORE)
        components = self._components(candidate, regime)
        penalties = self._penalties(candidate, current_positions, account_value, state)
        score = _clamp(sum(components.values()) - sum(penalties.values()))
        regime_cap_pct = self._regime_gross_cap_pct(regime)
        existing_gross = self._gross_exposure(current_positions)
        existing_symbol = self._symbol_exposure(candidate.ticker, current_positions)

        projected_gross = existing_gross + state.allocated_value
        projected_symbol = existing_symbol
        if candidate.side == "buy":
            projected_gross += candidate.notional
            projected_symbol += candidate.notional

        projected_gross_pct = self._pct(projected_gross, account_value)
        projected_symbol_pct = self._pct(projected_symbol, account_value)
        run_risk_pct = self._pct(state.allocated_risk + candidate.estimated_risk, account_value)

        blocking_reasons: list[str] = []
        if candidate.quantity <= 0 or candidate.entry_price <= 0:
            blocking_reasons.append("invalid quantity or price")
        if candidate.side == "buy" and candidate.notional > available_cash:
            blocking_reasons.append("insufficient available cash for this allocation")
        if candidate.side == "buy" and candidate.ticker.upper() in state.accepted_tickers:
            blocking_reasons.append("same symbol already won allocation in this run")
        if candidate.side == "buy" and projected_gross_pct > regime_cap_pct:
            blocking_reasons.append(
                f"projected gross exposure {projected_gross_pct:.2f}% exceeds {regime_cap_pct:.2f}% regime cap"
            )
        max_symbol_pct = self._max_symbol_exposure_pct(candidate)
        if candidate.side == "buy" and projected_symbol_pct > max_symbol_pct:
            blocking_reasons.append(
                f"projected symbol exposure {projected_symbol_pct:.2f}% exceeds {max_symbol_pct:.2f}% cap"
            )
        max_run_risk_pct = self._regime_run_risk_cap_pct(regime)
        if candidate.side == "buy" and run_risk_pct > max_run_risk_pct:
            blocking_reasons.append(
                f"allocated run risk {run_risk_pct:.2f}% exceeds {max_run_risk_pct:.2f}% cap"
            )
        if candidate.side == "buy" and score < threshold:
            blocking_reasons.append(f"score {score:.2f} is below allocation threshold {threshold:.2f}")

        if candidate.side == "sell" and not blocking_reasons:
            status: AllocationStatus = "allocated"
            reason = "Allocated: sell/reduction order lowers sleeve or symbol exposure."
        elif blocking_reasons:
            status = "rejected"
            reason = "Lost allocation: " + "; ".join(blocking_reasons) + "."
        else:
            status = "allocated"
            reason = (
                f"Allocated: score {score:.2f} passed {threshold:.2f}; "
                f"projected gross {projected_gross_pct:.2f}% <= {regime_cap_pct:.2f}% cap."
            )

        return AllocationDecision(
            ticker=candidate.ticker.upper(),
            side=candidate.side,
            strategy_id=candidate.strategy_id,
            strategy_name=candidate.strategy_name,
            strategy_type=candidate.strategy_type,
            signal_type=candidate.signal_type,
            status=status,
            score=score,
            threshold=threshold,
            reason=reason,
            rank=rank,
            components=components,
            penalties=penalties,
            allocated_risk_pct=run_risk_pct,
            projected_gross_exposure_pct=projected_gross_pct,
            projected_symbol_exposure_pct=projected_symbol_pct,
            regime_cap_pct=regime_cap_pct,
            generated_at=datetime.now(UTC),
        )

    def _components(self, candidate: AllocatorCandidate, regime: dict[str, Any]) -> dict[str, float]:
        confidence = _clamp(float(candidate.confidence or Decimal("0.5")))
        watchlist_score = _clamp(float(candidate.watchlist_context.get("score", 0.0) or 0.0) / 100.0)
        catalyst_score = _clamp(float(candidate.watchlist_context.get("catalyst_score", 0.0) or 0.0))
        regime_fit = self._regime_fit(candidate.strategy_type, regime)
        risk_reward = self._risk_reward_score(candidate)
        execution_quality = self._execution_quality(candidate.watchlist_context)
        return {
            "confidence": confidence * 0.30,
            "watchlist_quality": watchlist_score * 0.15,
            "catalyst": catalyst_score * 0.10,
            "regime_fit": regime_fit * 0.20,
            "risk_reward": risk_reward * 0.15,
            "execution_quality": execution_quality * 0.10,
        }

    def _penalties(
        self,
        candidate: AllocatorCandidate,
        current_positions: list[dict[str, Any]],
        account_value: Decimal,
        state: AllocationState,
    ) -> dict[str, float]:
        penalties: dict[str, float] = {}
        if candidate.side == "sell":
            return penalties

        if candidate.ticker.upper() in state.accepted_tickers:
            penalties["duplicate_symbol_run"] = 0.40

        existing_symbol_pct = self._pct(self._symbol_exposure(candidate.ticker, current_positions), account_value)
        if existing_symbol_pct > 0:
            penalties["existing_symbol_exposure"] = min(existing_symbol_pct / 100.0, 0.25)

        context = candidate.watchlist_context
        feed_status = str(context.get("feed_status") or context.get("status") or "unknown")
        if feed_status not in {"ok", "fallback", "unknown"}:
            penalties["feed_quality"] = 0.30
        spread_bps = float(context.get("spread_bps", 0.0) or 0.0)
        if spread_bps > 25:
            penalties["wide_spread"] = min(spread_bps / 250.0, 0.25)
        sector_penalty = self._sector_overlap_penalty(candidate, current_positions, account_value)
        if sector_penalty > 0:
            penalties["sector_overlap"] = sector_penalty
        correlation_penalty = self._correlation_proxy_penalty(candidate, current_positions)
        if correlation_penalty > 0:
            penalties["correlation_proxy"] = correlation_penalty
        return penalties

    def _pre_score(self, candidate: AllocatorCandidate, regime: dict[str, Any]) -> float:
        return _clamp(sum(self._components(candidate, regime).values()))

    def _regime_fit(self, strategy_type: str, regime: dict[str, Any]) -> float:
        regime_name = str(regime.get("regime") or "unknown")
        suppressed = {str(item) for item in regime.get("suppressed_strategies", []) if item}
        active = {str(item) for item in regime.get("active_strategies", []) if item}
        if strategy_type in suppressed or regime_name in {"unsafe", "low_liquidity"}:
            return 0.05
        if active and strategy_type in active:
            return 1.0
        if regime_name in {"risk_off", "trending_down"}:
            return 0.35
        if regime_name in {"volatile", "news_driven"}:
            return 0.50
        if regime_name in {"trending_up", "ranging", "mean_reverting"}:
            return 0.75
        return 0.65

    def _risk_reward_score(self, candidate: AllocatorCandidate) -> float:
        if candidate.side == "sell":
            return 1.0
        if candidate.stop_price is None or candidate.take_profit_price is None:
            return 0.65
        downside = abs(candidate.entry_price - candidate.stop_price)
        upside = abs(candidate.take_profit_price - candidate.entry_price)
        if downside <= 0:
            return 0.25
        return _clamp(float(upside / downside) / 3.0)

    def _execution_quality(self, context: dict[str, Any]) -> float:
        feed_status = str(context.get("feed_status") or context.get("status") or "unknown")
        base = 0.70 if feed_status in {"ok", "fallback", "unknown"} else 0.25
        rvol = float(context.get("pre_market_rvol", context.get("relative_volume", 0.0)) or 0.0)
        if rvol >= 2.0:
            base += 0.15
        elif rvol >= 1.2:
            base += 0.08
        spread_bps = float(context.get("spread_bps", 0.0) or 0.0)
        if spread_bps > 25:
            base -= 0.20
        return _clamp(base)

    def _sector_overlap_penalty(
        self,
        candidate: AllocatorCandidate,
        current_positions: list[dict[str, Any]],
        account_value: Decimal,
    ) -> float:
        if account_value <= 0:
            return 0.0
        sector = get_sector(candidate.ticker)
        if sector in {"ETF", "Unknown"}:
            return 0.0
        sector_value = candidate.notional
        for position in current_positions:
            if get_sector(str(position.get("ticker", ""))) != sector:
                continue
            qty = abs(_as_decimal(position.get("quantity")))
            price = (
                _as_decimal(position.get("currentPrice"))
                or _as_decimal(position.get("current_price"))
                or _as_decimal(position.get("averagePrice"))
                or _as_decimal(position.get("avg_price"))
            )
            sector_value += qty * price
        sector_pct = self._pct(sector_value, account_value)
        if sector_pct <= 15.0:
            return 0.0
        return _clamp((sector_pct - 15.0) / 100.0, 0.0, 0.25)

    def _correlation_proxy_penalty(
        self,
        candidate: AllocatorCandidate,
        current_positions: list[dict[str, Any]],
    ) -> float:
        sector = get_sector(candidate.ticker)
        if sector in {"ETF", "Unknown"}:
            return 0.0
        same_sector_count = sum(
            1
            for position in current_positions
            if get_sector(str(position.get("ticker", ""))) == sector
            and _as_decimal(position.get("quantity")) > 0
        )
        broad_overlap = {
            "Technology": {"QQQ", "XLK", "SPY"},
            "Consumer Discretionary": {"QQQ", "XLY", "SPY"},
            "Financials": {"XLF", "SPY"},
            "Energy": {"XLE", "SPY"},
            "Healthcare": {"XLV", "SPY"},
        }
        held_tickers = {str(position.get("ticker", "")).upper() for position in current_positions}
        has_broad_overlap = bool(held_tickers & broad_overlap.get(sector, {"SPY"}))
        penalty = 0.0
        if same_sector_count >= 2:
            penalty += 0.08
        elif same_sector_count == 1:
            penalty += 0.04
        if has_broad_overlap:
            penalty += 0.04
        return _clamp(penalty, 0.0, 0.16)

    def _regime_gross_cap_pct(self, regime: dict[str, Any]) -> float:
        regime_name = str(regime.get("regime") or "unknown")
        if regime_name in {"unsafe", "low_liquidity"}:
            return 0.0
        if regime_name == "risk_off":
            return float(settings.PORTFOLIO_ALLOCATOR_RISK_OFF_GROSS_EXPOSURE_PCT)
        if regime_name in {"volatile", "news_driven"}:
            return float(settings.PORTFOLIO_ALLOCATOR_VOLATILE_GROSS_EXPOSURE_PCT)
        if regime_name == "trending_down":
            return min(float(settings.PORTFOLIO_ALLOCATOR_VOLATILE_GROSS_EXPOSURE_PCT), 40.0)
        return float(settings.PORTFOLIO_ALLOCATOR_BASE_GROSS_EXPOSURE_PCT)

    def _max_symbol_exposure_pct(self, candidate: AllocatorCandidate) -> float:
        if candidate.target_weight is not None:
            return float(settings.PORTFOLIO_ALLOCATOR_MAX_SLEEVE_SYMBOL_EXPOSURE_PCT)
        return float(settings.PORTFOLIO_ALLOCATOR_MAX_SYMBOL_EXPOSURE_PCT)

    def _regime_run_risk_cap_pct(self, regime: dict[str, Any]) -> float:
        base = float(settings.PORTFOLIO_ALLOCATOR_MAX_RUN_RISK_PCT)
        regime_name = str(regime.get("regime") or "unknown")
        if regime_name in {"unsafe", "low_liquidity"}:
            return 0.0
        if regime_name == "risk_off":
            return min(base, 0.50)
        if regime_name in {"volatile", "news_driven", "trending_down"}:
            return min(base, 1.00)
        return base

    def _gross_exposure(self, positions: list[dict[str, Any]]) -> Decimal:
        total = Decimal("0")
        for position in positions:
            qty = abs(_as_decimal(position.get("quantity")))
            if qty <= 0:
                continue
            price = (
                _as_decimal(position.get("currentPrice"))
                or _as_decimal(position.get("current_price"))
                or _as_decimal(position.get("averagePrice"))
                or _as_decimal(position.get("avg_price"))
            )
            total += qty * price
        return total

    def _symbol_exposure(self, ticker: str, positions: list[dict[str, Any]]) -> Decimal:
        total = Decimal("0")
        ticker_upper = ticker.upper()
        for position in positions:
            if str(position.get("ticker", "")).upper() != ticker_upper:
                continue
            qty = abs(_as_decimal(position.get("quantity")))
            price = (
                _as_decimal(position.get("currentPrice"))
                or _as_decimal(position.get("current_price"))
                or _as_decimal(position.get("averagePrice"))
                or _as_decimal(position.get("avg_price"))
            )
            total += qty * price
        return total

    def _pct(self, value: Decimal, account_value: Decimal) -> float:
        if account_value <= 0:
            return 0.0
        return float((value / account_value) * Decimal("100"))
