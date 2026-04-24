"""
Circuit breaker for broker API calls.
Prevents hammering a down API and automatically activates the kill switch
after a configurable number of consecutive failures.

State machine:
  CLOSED  → normal operation
  OPEN    → failing fast, not calling broker
  HALF_OPEN → testing if broker has recovered
"""
from __future__ import annotations

import asyncio
import time
from enum import Enum
from functools import wraps
from typing import Any, Callable

import structlog

log = structlog.get_logger()


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(Exception):
    """Raised when circuit is open and call is rejected."""


class CircuitBreaker:
    """
    Thread-safe async circuit breaker.

    After `failure_threshold` consecutive failures, circuit opens.
    After `recovery_timeout` seconds, one test call is allowed (HALF_OPEN).
    On success, circuit closes. On failure, resets the recovery timer.
    """

    def __init__(
        self,
        name: str = "broker",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        auto_kill_switch: bool = True,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.auto_kill_switch = auto_kill_switch

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    async def _check_state(self) -> None:
        """Transition OPEN → HALF_OPEN if recovery timeout has elapsed."""
        async with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    log.info("circuit_breaker.half_open", name=self.name)

    async def _record_success(self) -> None:
        async with self._lock:
            self._failure_count = 0
            if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
                self._state = CircuitState.CLOSED
                log.info("circuit_breaker.closed", name=self.name)

    async def _record_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._failure_count >= self.failure_threshold:
                prev_state = self._state
                self._state = CircuitState.OPEN
                if prev_state != CircuitState.OPEN:
                    log.error(
                        "circuit_breaker.opened",
                        name=self.name,
                        failures=self._failure_count,
                    )
                    if self.auto_kill_switch:
                        await self._activate_kill_switch()

    async def _activate_kill_switch(self) -> None:
        """Automatically activate kill switch when circuit opens."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.risk.engine import activate_kill_switch
            async with AsyncSessionLocal() as db:
                await activate_kill_switch(db, actor=f"circuit_breaker:{self.name}")
                await db.commit()
            log.critical(
                "circuit_breaker.kill_switch_activated",
                name=self.name,
                reason="consecutive broker failures",
            )
        except Exception as exc:
            log.error("circuit_breaker.kill_switch_failed", error=str(exc))

    async def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """
        Execute func through the circuit breaker.
        Raises CircuitBreakerError if circuit is OPEN.
        """
        await self._check_state()

        if self._state == CircuitState.OPEN:
            raise CircuitBreakerError(
                f"Circuit breaker [{self.name}] is OPEN. "
                f"Broker calls blocked. Kill switch may be active."
            )

        try:
            result = await func(*args, **kwargs)
            await self._record_success()
            return result
        except CircuitBreakerError:
            raise
        except Exception as exc:
            await self._record_failure()
            log.warning(
                "circuit_breaker.failure",
                name=self.name,
                failure_count=self._failure_count,
                error=str(exc),
            )
            raise

    def reset(self) -> None:
        """Manually reset circuit to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        log.info("circuit_breaker.manual_reset", name=self.name)


# ── Singleton circuit breakers ────────────────────────────────────────────────

_broker_circuit = CircuitBreaker(
    name="trading212",
    failure_threshold=5,
    recovery_timeout=60.0,
    auto_kill_switch=True,
)

_market_data_circuit = CircuitBreaker(
    name="market_data",
    failure_threshold=3,
    recovery_timeout=120.0,
    auto_kill_switch=False,  # Stale data warning, not kill switch
)


def get_broker_circuit() -> CircuitBreaker:
    return _broker_circuit


def get_market_data_circuit() -> CircuitBreaker:
    return _market_data_circuit
