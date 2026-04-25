"""
Unit tests for the async circuit breaker.

All tests use unittest.mock to avoid the DB/kill-switch side-effect;
the _activate_kill_switch path is tested by asserting it is called
when auto_kill_switch=True and skipped when False.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitState,
    get_broker_circuit,
    get_market_data_circuit,
)


# ── helpers ───────────────────────────────────────────────────────────────────

async def _ok(*_a, **_kw) -> str:
    return "ok"


async def _fail(*_a, **_kw) -> None:
    raise ValueError("broker down")


def _make(
    *,
    threshold: int = 3,
    recovery: float = 60.0,
    auto_kill: bool = False,
    name: str = "test",
) -> CircuitBreaker:
    return CircuitBreaker(
        name=name,
        failure_threshold=threshold,
        recovery_timeout=recovery,
        auto_kill_switch=auto_kill,
    )


# ── initial state ─────────────────────────────────────────────────────────────

class TestInitialState:
    def test_starts_closed(self):
        cb = _make()
        assert cb.state == CircuitState.CLOSED
        assert not cb.is_open

    def test_name_stored(self):
        cb = _make(name="my_service")
        assert cb.name == "my_service"


# ── success path ──────────────────────────────────────────────────────────────

class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_successful_call_returns_result(self):
        cb = _make()
        result = await cb.call(_ok)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_failure_count_stays_zero_on_success(self):
        cb = _make()
        await cb.call(_ok)
        assert cb._failure_count == 0
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_multiple_successes_keep_circuit_closed(self):
        cb = _make(threshold=2)
        for _ in range(5):
            await cb.call(_ok)
        assert cb.state == CircuitState.CLOSED


# ── failure accumulation ──────────────────────────────────────────────────────

class TestFailureAccumulation:
    @pytest.mark.asyncio
    async def test_failure_increments_count(self):
        cb = _make(threshold=5)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        assert cb._failure_count == 1
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_below_threshold_stays_closed(self):
        cb = _make(threshold=3)
        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.call(_fail)
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 2

    @pytest.mark.asyncio
    async def test_reaching_threshold_opens_circuit(self):
        cb = _make(threshold=3)
        for _ in range(3):
            with pytest.raises(ValueError):
                await cb.call(_fail)
        assert cb.state == CircuitState.OPEN
        assert cb.is_open

    @pytest.mark.asyncio
    async def test_open_circuit_raises_circuit_breaker_error(self):
        cb = _make(threshold=1)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        assert cb.is_open
        with pytest.raises(CircuitBreakerError):
            await cb.call(_ok)

    @pytest.mark.asyncio
    async def test_circuit_breaker_error_not_counted_as_failure(self):
        cb = _make(threshold=1)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        count_before = cb._failure_count
        with pytest.raises(CircuitBreakerError):
            await cb.call(_ok)
        assert cb._failure_count == count_before


# ── recovery / half-open ──────────────────────────────────────────────────────

class TestRecovery:
    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self):
        cb = _make(threshold=1, recovery=0.01)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        assert cb.is_open
        await asyncio.sleep(0.02)
        await cb._check_state()
        assert cb.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_success_in_half_open_closes_circuit(self):
        cb = _make(threshold=1, recovery=0.01)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        await asyncio.sleep(0.02)
        result = await cb.call(_ok)
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0

    @pytest.mark.asyncio
    async def test_failure_in_half_open_reopens_circuit(self):
        cb = _make(threshold=1, recovery=0.01)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        await asyncio.sleep(0.02)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_timeout_not_elapsed_stays_open(self):
        cb = _make(threshold=1, recovery=9999.0)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        await cb._check_state()
        assert cb.state == CircuitState.OPEN


# ── manual reset ──────────────────────────────────────────────────────────────

class TestManualReset:
    @pytest.mark.asyncio
    async def test_reset_closes_open_circuit(self):
        cb = _make(threshold=1)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        assert cb.is_open
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0

    @pytest.mark.asyncio
    async def test_calls_succeed_after_reset(self):
        cb = _make(threshold=1)
        with pytest.raises(ValueError):
            await cb.call(_fail)
        cb.reset()
        result = await cb.call(_ok)
        assert result == "ok"


# ── auto kill-switch ──────────────────────────────────────────────────────────

class TestAutoKillSwitch:
    @pytest.mark.asyncio
    async def test_kill_switch_called_when_enabled(self):
        cb = _make(threshold=1, auto_kill=True)
        with patch.object(cb, "_activate_kill_switch", new_callable=AsyncMock) as mock_ks:
            with pytest.raises(ValueError):
                await cb.call(_fail)
            mock_ks.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kill_switch_not_called_when_disabled(self):
        cb = _make(threshold=1, auto_kill=False)
        with patch.object(cb, "_activate_kill_switch", new_callable=AsyncMock) as mock_ks:
            with pytest.raises(ValueError):
                await cb.call(_fail)
            mock_ks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kill_switch_not_called_again_once_open(self):
        cb = _make(threshold=2, auto_kill=True)
        with patch.object(cb, "_activate_kill_switch", new_callable=AsyncMock) as mock_ks:
            for _ in range(4):
                with pytest.raises((ValueError, CircuitBreakerError)):
                    await cb.call(_fail)
            # Only one activation: when circuit first opened at threshold
            mock_ks.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_activate_kill_switch_handles_db_error_gracefully(self):
        cb = _make(threshold=1, auto_kill=True)
        # _activate_kill_switch uses a lazy `from app.db.session import ...`
        # so we patch the source symbol; its internal try/except swallows the error
        with patch("app.db.session.AsyncSessionLocal", side_effect=RuntimeError("no db")):
            with pytest.raises(ValueError):
                await cb.call(_fail)
            # Circuit is OPEN even though the kill-switch DB call failed
            assert cb.is_open


# ── kwargs forwarding ──────────────────────────────────────────────────────────

class TestCallForwarding:
    @pytest.mark.asyncio
    async def test_args_and_kwargs_forwarded_to_func(self):
        received: dict = {}

        async def capture(a, b, *, flag):
            received.update({"a": a, "b": b, "flag": flag})
            return "done"

        cb = _make()
        result = await cb.call(capture, 1, 2, flag=True)
        assert result == "done"
        assert received == {"a": 1, "b": 2, "flag": True}


# ── singleton accessors ───────────────────────────────────────────────────────

class TestSingletons:
    def test_get_broker_circuit_returns_circuit_breaker(self):
        cb = get_broker_circuit()
        assert isinstance(cb, CircuitBreaker)
        assert cb.name == "trading212"
        assert cb.failure_threshold == 5
        assert cb.auto_kill_switch is True

    def test_get_market_data_circuit_returns_circuit_breaker(self):
        cb = get_market_data_circuit()
        assert isinstance(cb, CircuitBreaker)
        assert cb.name == "market_data"
        assert cb.failure_threshold == 3
        assert cb.auto_kill_switch is False

    def test_singletons_are_same_instance(self):
        assert get_broker_circuit() is get_broker_circuit()
        assert get_market_data_circuit() is get_market_data_circuit()
