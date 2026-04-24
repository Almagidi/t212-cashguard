from __future__ import annotations

from decimal import Decimal

from app.services.signal_allocator import AllocatorCandidate, SignalAllocator


def _candidate(
    *,
    ticker: str = "AAPL",
    confidence: Decimal = Decimal("0.82"),
    quantity: Decimal = Decimal("10"),
    entry_price: Decimal = Decimal("100"),
    stop_price: Decimal | None = Decimal("97"),
    take_profit_price: Decimal | None = Decimal("109"),
    watchlist_score: float = 82.0,
) -> AllocatorCandidate:
    return AllocatorCandidate(
        ticker=ticker,
        side="buy",
        strategy_id="strategy-1",
        strategy_name="Opening Range Breakout",
        strategy_type="orb",
        signal_type="entry",
        confidence=confidence,
        entry_price=entry_price,
        quantity=quantity,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        watchlist_context={
            "score": watchlist_score,
            "catalyst_score": 0.70,
            "feed_status": "ok",
            "pre_market_rvol": 2.4,
        },
    )


def test_allocator_accepts_high_quality_candidate():
    allocator = SignalAllocator()
    decision = allocator.allocate_one(
        _candidate(),
        account_value=Decimal("100000"),
        available_cash=Decimal("50000"),
        current_positions=[],
        regime={
            "regime": "trending_up",
            "active_strategies": ["orb"],
            "suppressed_strategies": [],
        },
        state=allocator.new_state(),
    )

    assert decision.status == "allocated"
    assert decision.score >= decision.threshold
    assert "Allocated" in decision.reason
    assert decision.projected_gross_exposure_pct == 1.0


def test_allocator_rejects_duplicate_symbol_in_same_run():
    allocator = SignalAllocator()
    state = allocator.new_state()

    first = allocator.allocate_one(
        _candidate(ticker="MSFT"),
        account_value=Decimal("100000"),
        available_cash=Decimal("50000"),
        current_positions=[],
        regime={"regime": "trending_up", "active_strategies": ["orb"]},
        state=state,
    )
    second = allocator.allocate_one(
        _candidate(ticker="MSFT", confidence=Decimal("0.95")),
        account_value=Decimal("100000"),
        available_cash=Decimal("50000"),
        current_positions=[],
        regime={"regime": "trending_up", "active_strategies": ["orb"]},
        state=state,
    )

    assert first.status == "allocated"
    assert second.status == "rejected"
    assert "same symbol already won allocation" in second.reason


def test_allocator_rejects_when_regime_cap_would_be_breached():
    allocator = SignalAllocator()
    decision = allocator.allocate_one(
        _candidate(
            ticker="NVDA",
            quantity=Decimal("300"),
            entry_price=Decimal("100"),
            stop_price=Decimal("96"),
            take_profit_price=Decimal("112"),
        ),
        account_value=Decimal("100000"),
        available_cash=Decimal("50000"),
        current_positions=[],
        regime={"regime": "risk_off", "active_strategies": [], "suppressed_strategies": []},
        state=allocator.new_state(),
    )

    assert decision.status == "rejected"
    assert decision.projected_gross_exposure_pct == 30.0
    assert decision.regime_cap_pct == 25.0
    assert "regime cap" in decision.reason


def test_allocator_applies_sector_and_correlation_proxy_penalties():
    allocator = SignalAllocator()
    decision = allocator.allocate_one(
        _candidate(ticker="AAPL", confidence=Decimal("0.92")),
        account_value=Decimal("100000"),
        available_cash=Decimal("50000"),
        current_positions=[
            {"ticker": "MSFT", "quantity": 50, "currentPrice": 400},
            {"ticker": "NVDA", "quantity": 20, "currentPrice": 900},
        ],
        regime={"regime": "trending_up", "active_strategies": ["orb"]},
        state=allocator.new_state(),
    )

    assert decision.penalties["sector_overlap"] > 0
    assert decision.penalties["correlation_proxy"] > 0


def test_allocator_payload_is_ui_safe_and_explainable():
    allocator = SignalAllocator()
    decision = allocator.allocate_one(
        _candidate(ticker="TSLA"),
        account_value=Decimal("100000"),
        available_cash=Decimal("50000"),
        current_positions=[],
        regime={"regime": "trending_up", "active_strategies": ["orb"]},
        state=allocator.new_state(),
    )
    payload = decision.to_payload()

    assert payload["ticker"] == "TSLA"
    assert payload["status"] == "allocated"
    assert isinstance(payload["components"], dict)
    assert isinstance(payload["penalties"], dict)
    assert payload["generated_at"]
