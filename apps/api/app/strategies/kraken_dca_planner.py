"""
Kraken BTC/ETH DCA Planner — schedule-driven accumulation scaffold.

Strategy #1 of the approved Kraken ladder: BTC/ETH DCA accumulation.

PAPER_ONLY = True
RUNNABLE = False  — research/design scaffold; not wired into strategy_runner.

─── Why DCA does not use generate_signal() ────────────────────────────────────

The generate_signal() interface is a one-shot, bar-triggered signal model:
given a bar array for the current moment, return an entry signal or None.

DCA accumulation is structurally different:
  - It is SCHEDULE-DRIVEN: buy every N days regardless of bar pattern.
  - It is ALLOCATION-POLICY-DRIVEN: buy a fixed USD amount per interval,
    optionally scaled up on price dips.
  - There is no meaningful "breakout level", "stop loss at entry", or
    "take profit target" to inject into the signal interface.

Forcing DCA into generate_signal() would require either:
  (a) Always returning a signal → ignores schedule entirely; buys every tick.
  (b) Using fake bar conditions as a proxy for "time has elapsed" → lies about
      the strategy's actual logic and is untestable.
  (c) Returning None until some arbitrary bar count → meaningless and fragile.

─── Domain model overview ─────────────────────────────────────────────────────

DCAConfig   — per-plan policy: ticker, cadence, allocation, cash limits.
DCAState    — per-plan persistent state: last buy date, totals, execution count.
DCADecision — evaluation result with an explicit DCADecisionCode.

─── Evaluation interface ──────────────────────────────────────────────────────

Primary API (scheduler-facing, typed):
  evaluate_plan(config, state, current_price, available_cash, account_value,
                bars, now) → DCADecision

Legacy API (backwards-compatible, param-dict style):
  evaluate(ticker, current_price, account_value, last_buy_date, bars,
           current_date, available_cash) → DCADecision

─── Deployment prerequisites ──────────────────────────────────────────────────

This planner must NOT be wired into strategy_runner._make_engine() or
celery_app.beat_schedule until ALL of the following are in place:

  1. A dca_plan_states DB table exists (migration) storing DCAState fields:
       ticker, venue, last_buy_at, last_decision_at, total_allocated_usd,
       executions_count, last_decision_code, last_reason
  2. A scheduler (Celery beat or equivalent) calls evaluate_plan() on the
     correct cadence — NOT on every 5-minute runner tick.
  3. A DCA position tracker updates DCAState.last_buy_at per ticker in the DB.
  4. The paper-only execution path for schedule-driven Kraken orders is
     validated end-to-end.
  5. This module has passed: scaffold → paper_simulation → demo → live_approved.

─── Current status ────────────────────────────────────────────────────────────
  RUNNABLE = False: do not wire into any execution path.
  PAPER_ONLY = True: only paper simulation is permitted when RUNNABLE becomes True.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from app.strategies.indicators import Bar, ema_of_closes

APPROVED_TICKERS: frozenset[str] = frozenset({"BTC/USD", "ETH/USD"})

DEFAULT_PARAMS: dict[str, Any] = {
    "interval_days": 7,              # buy every N calendar days
    "base_allocation_usd": 100,      # USD per scheduled buy
    "dip_threshold_pct": 5.0,        # price must be >N% below EMA20 to qualify as dip
    "dip_multiplier": 2.0,           # allocate 2x base on confirmed dips
    "dip_ema_period": 20,
    "enable_dip_enhancement": True,
    "min_cash_reserve_usd": 500,     # block if available_cash < this
    "max_position_pct": 25.0,        # block if single buy > this % of account_value
}


# ── Decision codes ─────────────────────────────────────────────────────────────

class DCADecisionCode(str, Enum):
    """
    Explicit operator-readable outcome code for every DCA evaluation.

    Replaces the implicit bool+string approach with deterministic,
    machine-comparable codes a scheduler can act on without parsing
    human-readable reason strings.

    A scheduler MUST only act when code == BUY_DUE.
    All other codes mean "do not buy; see DCADecision.reason".
    """
    BUY_DUE = "BUY_DUE"
    SKIP_ALREADY_BOUGHT_THIS_WINDOW = "SKIP_ALREADY_BOUGHT_THIS_WINDOW"
    BLOCKED_LOW_CASH = "BLOCKED_LOW_CASH"
    BLOCKED_POLICY = "BLOCKED_POLICY"


# ── Domain model ───────────────────────────────────────────────────────────────

@dataclass
class DCAConfig:
    """
    Per-plan DCA policy configuration.

    One DCAConfig represents the accumulation policy for a single ticker
    at a single venue. Multiple configs may be active simultaneously.

    Persistence note:
      Intended to be stored in a future dca_configs table.
      Until that migration exists, instantiate directly from DEFAULT_PARAMS.
    """
    ticker: str
    cadence_days: int = 7
    base_allocation_usd: Decimal = field(default_factory=lambda: Decimal("100"))
    enable_dip_enhancement: bool = True
    dip_threshold_pct: float = 5.0
    dip_multiplier: float = 2.0
    dip_ema_period: int = 20
    min_cash_reserve_usd: Decimal = field(default_factory=lambda: Decimal("500"))
    max_position_pct: float = 25.0
    paper_only: bool = True
    enabled: bool = True
    venue: str = "kraken"


@dataclass
class DCAState:
    """
    Per-plan persistent accumulation state.

    Tracks execution history required to enforce cadence and budget constraints.

    Persistence contract (REQUIRED before DCA can run autonomously):
      This dataclass defines the minimum state that MUST be written to a DB
      table after every BUY_DUE evaluation. Until a migration creates the
      dca_plan_states table, this object must be reconstructed from
      caller-supplied data on every evaluation call.

    Required future table columns:
      ticker              VARCHAR(50)    NOT NULL
      venue               VARCHAR(50)    NOT NULL
      last_buy_at         DATE           NULL
      last_decision_at    DATE           NULL
      total_allocated_usd NUMERIC(20,8)  NOT NULL DEFAULT 0
      executions_count    INT            NOT NULL DEFAULT 0
      last_decision_code  VARCHAR(50)    NULL
      last_reason         TEXT           NULL
    """
    ticker: str
    venue: str = "kraken"
    last_buy_at: date | None = None
    last_decision_at: date | None = None
    total_allocated_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    executions_count: int = 0
    last_decision_code: str | None = None
    last_reason: str | None = None


# ── Evaluation output ──────────────────────────────────────────────────────────

@dataclass
class DCADecision:
    """
    Result of a single DCA evaluation.

    Fields:
      code                — machine-comparable outcome (use this in schedulers).
                            A scheduler must only act when code == BUY_DUE.
      should_accumulate   — True iff code == BUY_DUE.
      amount_usd          — intended buy size; Decimal("0") when not buying.
      mode                — "scheduled" | "dip_enhanced" | "skip".
      reason              — human-readable explanation.
      next_scheduled_date — ISO date of the next planned buy, when known.
    """
    code: DCADecisionCode           # always set; primary discriminator for schedulers
    should_accumulate: bool         # True iff code == BUY_DUE
    amount_usd: Decimal
    mode: str                       # "scheduled" | "dip_enhanced" | "skip"
    reason: str
    next_scheduled_date: str | None = None


# ── Planner ────────────────────────────────────────────────────────────────────

class KrakenDCAPlanner:
    """
    Schedule-driven BTC/ETH accumulation planner for Kraken.
    Strategy #1 of the approved Kraken ladder.

    RUNNABLE = False: not wired into strategy_runner or celery beat_schedule.
    PAPER_ONLY = True: paper simulation only when RUNNABLE is promoted.

    This class intentionally does NOT implement generate_signal().
    It must not be passed to StrategyRunner._make_engine().

    Call evaluate_plan() from a future scheduler task; never from the
    bar-triggered 5-minute runner tick.
    """

    VENUE = "kraken"
    PAPER_ONLY = True
    RUNNABLE: bool = False   # must remain False until all deployment prerequisites are met
    APPROVED_TICKERS = APPROVED_TICKERS

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**DEFAULT_PARAMS, **(params or {})}

    # ── Primary (typed) API ───────────────────────────────────────────────────

    def evaluate_plan(
        self,
        config: DCAConfig,
        state: DCAState,
        current_price: Decimal,
        available_cash: Decimal,
        account_value: Decimal,
        bars: list[Bar] | None = None,
        now: date | None = None,
    ) -> DCADecision:
        """
        Evaluate a single DCA plan against its current state.

        This is the method a future Celery beat task should call.

        SCHEDULER CONTRACT (not yet wired — see module deployment prerequisites):
          1. Load all enabled DCAConfig rows from dca_configs table.
          2. Load matching DCAState rows from dca_plan_states table.
          3. Fetch current prices via KrakenProvider.
          4. Fetch available_cash and account_value from account snapshot.
          5. For each plan: decision = planner.evaluate_plan(config, state, ...).
          6. If decision.code == BUY_DUE and config.paper_only:
               - Record paper DCA action in audit_log.
               - Write updated DCAState back to DB (last_buy_at, totals).
               - Do NOT place a real order until RUNNABLE is promoted.

        Returns DCADecision with an explicit DCADecisionCode.
        A scheduler must only act when code == BUY_DUE.
        """
        today = now or date.today()

        if not config.enabled:
            return DCADecision(
                should_accumulate=False,
                amount_usd=Decimal("0"),
                mode="skip",
                reason="Plan is disabled",
                code=DCADecisionCode.BLOCKED_POLICY,
            )

        if config.ticker not in APPROVED_TICKERS:
            return DCADecision(
                should_accumulate=False,
                amount_usd=Decimal("0"),
                mode="skip",
                reason=(
                    f"{config.ticker!r} is not in the approved DCA ticker list "
                    f"{sorted(APPROVED_TICKERS)}"
                ),
                code=DCADecisionCode.BLOCKED_POLICY,
            )

        if float(current_price) <= 0:
            return DCADecision(
                should_accumulate=False,
                amount_usd=Decimal("0"),
                mode="skip",
                reason="current_price must be positive",
                code=DCADecisionCode.BLOCKED_POLICY,
            )

        # Cash reserve gate: block if free cash is below the configured floor
        if available_cash < config.min_cash_reserve_usd:
            return DCADecision(
                should_accumulate=False,
                amount_usd=Decimal("0"),
                mode="skip",
                reason=(
                    f"Available cash {float(available_cash):.2f} USD is below "
                    f"minimum reserve {float(config.min_cash_reserve_usd):.2f} USD"
                ),
                code=DCADecisionCode.BLOCKED_LOW_CASH,
            )

        # Cadence gate: block if still within the accumulation window
        if state.last_buy_at is not None:
            days_since = (today - state.last_buy_at).days
            next_due = state.last_buy_at + timedelta(days=config.cadence_days)
            if days_since < config.cadence_days:
                return DCADecision(
                    should_accumulate=False,
                    amount_usd=Decimal("0"),
                    mode="skip",
                    reason=(
                        f"Next scheduled buy is {next_due.isoformat()} "
                        f"({config.cadence_days - days_since} days away)"
                    ),
                    next_scheduled_date=next_due.isoformat(),
                    code=DCADecisionCode.SKIP_ALREADY_BOUGHT_THIS_WINDOW,
                )

        next_scheduled = (today + timedelta(days=config.cadence_days)).isoformat()
        base_amount = config.base_allocation_usd

        # Max position gate: block if single buy exceeds allocation cap
        if account_value > 0:
            max_alloc = Decimal(str(float(account_value) * config.max_position_pct / 100))
            if base_amount > max_alloc:
                return DCADecision(
                    should_accumulate=False,
                    amount_usd=Decimal("0"),
                    mode="skip",
                    reason=(
                        f"Allocation {float(base_amount):.0f} USD exceeds max position cap "
                        f"({config.max_position_pct}% of {float(account_value):.0f} USD = "
                        f"{float(max_alloc):.0f} USD)"
                    ),
                    code=DCADecisionCode.BLOCKED_POLICY,
                )

        # Dip enhancement: optional scaling on confirmed price dips vs EMA
        if (
            config.enable_dip_enhancement
            and bars
            and len(bars) >= config.dip_ema_period
        ):
            ema_val = ema_of_closes(bars, config.dip_ema_period)
            if ema_val > 0:
                pct_below = float((ema_val - current_price) / ema_val * 100)
                if pct_below >= config.dip_threshold_pct:
                    enhanced = base_amount * Decimal(str(config.dip_multiplier))
                    return DCADecision(
                        should_accumulate=True,
                        amount_usd=enhanced,
                        mode="dip_enhanced",
                        reason=(
                            f"Scheduled buy with dip enhancement: price is "
                            f"{pct_below:.1f}% below EMA{config.dip_ema_period} "
                            f"({float(ema_val):.2f}); allocating {float(enhanced):.0f} USD"
                        ),
                        next_scheduled_date=next_scheduled,
                        code=DCADecisionCode.BUY_DUE,
                    )

        return DCADecision(
            should_accumulate=True,
            amount_usd=base_amount,
            mode="scheduled",
            reason=f"Scheduled accumulation: {float(base_amount):.0f} USD for {config.ticker}",
            next_scheduled_date=next_scheduled,
            code=DCADecisionCode.BUY_DUE,
        )

    # ── Legacy (backwards-compatible) API ────────────────────────────────────

    def evaluate(
        self,
        ticker: str,
        current_price: Decimal,
        account_value: Decimal,
        last_buy_date: str | None,
        bars: list[Bar] | None = None,
        current_date: str | None = None,
        available_cash: Decimal | None = None,
    ) -> DCADecision:
        """
        Evaluate whether to accumulate for this ticker on the given date.

        Legacy string-param API preserved for backwards compatibility.
        Prefer evaluate_plan() for new callers.

        Args:
            ticker:         Must be in APPROVED_TICKERS.
            current_price:  Current market price.
            account_value:  Total account value (used for max_position_pct check).
            last_buy_date:  ISO date of the last accumulation, or None (first buy).
            bars:           Optional bar history for dip detection via EMA.
            current_date:   ISO date for evaluation. Defaults to today.
                            Pass explicitly in tests to avoid date coupling.
            available_cash: If provided and < min_cash_reserve_usd, returns
                            BLOCKED_LOW_CASH. If None, cash check is skipped
                            (maintains backwards compatibility with existing callers).
        """
        if ticker not in APPROVED_TICKERS:
            return DCADecision(
                should_accumulate=False,
                amount_usd=Decimal("0"),
                mode="skip",
                reason=f"{ticker!r} is not in the approved DCA ticker list {sorted(APPROVED_TICKERS)}",
                code=DCADecisionCode.BLOCKED_POLICY,
            )

        if float(current_price) <= 0:
            return DCADecision(
                should_accumulate=False,
                amount_usd=Decimal("0"),
                mode="skip",
                reason="current_price must be positive",
                code=DCADecisionCode.BLOCKED_POLICY,
            )

        # Cash reserve check (only when available_cash is explicitly provided)
        min_reserve = Decimal(str(self.params["min_cash_reserve_usd"]))
        if available_cash is not None and available_cash < min_reserve:
            return DCADecision(
                should_accumulate=False,
                amount_usd=Decimal("0"),
                mode="skip",
                reason=(
                    f"Available cash {float(available_cash):.2f} USD is below "
                    f"minimum reserve {float(min_reserve):.2f} USD"
                ),
                code=DCADecisionCode.BLOCKED_LOW_CASH,
            )

        today = date.fromisoformat(current_date) if current_date else date.today()
        interval = int(self.params["interval_days"])
        base_amount = Decimal(str(self.params["base_allocation_usd"]))

        # Cadence check
        if last_buy_date is not None:
            last_date = date.fromisoformat(last_buy_date)
            days_since = (today - last_date).days
            next_due = last_date + timedelta(days=interval)
            if days_since < interval:
                return DCADecision(
                    should_accumulate=False,
                    amount_usd=Decimal("0"),
                    mode="skip",
                    reason=(
                        f"Next scheduled buy is {next_due.isoformat()} "
                        f"({interval - days_since} days away)"
                    ),
                    next_scheduled_date=next_due.isoformat(),
                    code=DCADecisionCode.SKIP_ALREADY_BOUGHT_THIS_WINDOW,
                )

        next_scheduled = (today + timedelta(days=interval)).isoformat()

        # Dip enhancement
        if (
            self.params["enable_dip_enhancement"]
            and bars
            and len(bars) >= int(self.params["dip_ema_period"])
        ):
            ema_val = ema_of_closes(bars, int(self.params["dip_ema_period"]))
            if ema_val > 0:
                pct_below = float((ema_val - current_price) / ema_val * 100)
                if pct_below >= float(self.params["dip_threshold_pct"]):
                    enhanced = base_amount * Decimal(str(self.params["dip_multiplier"]))
                    return DCADecision(
                        should_accumulate=True,
                        amount_usd=enhanced,
                        mode="dip_enhanced",
                        reason=(
                            f"Scheduled buy with dip enhancement: price is "
                            f"{pct_below:.1f}% below EMA{self.params['dip_ema_period']} "
                            f"({float(ema_val):.2f}); allocating {float(enhanced):.0f} USD"
                        ),
                        next_scheduled_date=next_scheduled,
                        code=DCADecisionCode.BUY_DUE,
                    )

        return DCADecision(
            should_accumulate=True,
            amount_usd=base_amount,
            mode="scheduled",
            reason=f"Scheduled accumulation: {float(base_amount):.0f} USD for {ticker}",
            next_scheduled_date=next_scheduled,
            code=DCADecisionCode.BUY_DUE,
        )


# ── Scheduler contract (documentation only; not wired) ────────────────────────

class DcaSchedulerContract(Protocol):
    """
    Interface a future Celery beat task must satisfy to drive paper DCA.

    NOT wired into celery_app.beat_schedule. NOT referenced by strategy_runner.
    This Protocol is a documentation contract specifying what a scheduler task
    must implement before DCA can run autonomously.

    Deployment prerequisites (all must be true before wiring):
      1. dca_plan_states DB table exists (migration 0013 or later), storing
         DCAState fields: ticker, venue, last_buy_at, last_decision_at,
         total_allocated_usd, executions_count, last_decision_code, last_reason.
      2. dca_configs DB table (or config-file equivalent) is populated.
      3. KrakenProvider price fetch is stable and health-monitored.
      4. Paper execution path for Kraken DCA is validated end-to-end.
      5. KrakenDCAPlanner.RUNNABLE has been explicitly promoted to True
         after full operator review and sign-off.

    Intended scheduler cadence: daily crontab (not the 5-minute strategy runner tick).
    """

    async def evaluate_due_plans(self, now: date) -> list[DCADecision]:
        """
        For each enabled DCAConfig, load matching DCAState from DB, call
        KrakenDCAPlanner.evaluate_plan(), and return all resulting DCADecision objects.

        For any decision where code == BUY_DUE and config.paper_only is True:
          - Append a paper DCA record to audit_logs (venue, ticker, amount_usd, mode, reason).
          - Update DCAState.last_buy_at and increment executions_count in dca_plan_states.
          - Do NOT place a real Kraken order until RUNNABLE is promoted to True.
        """
        ...
