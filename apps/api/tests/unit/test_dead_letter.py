"""Unit tests for the Celery dead-letter queue handler."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.workers.dead_letter import _DLQ_KEY, _DLQ_MAX, handle_task_failure


def _make_sender(name: str, max_retries: int, retries: int):
    request = type("Request", (), {"retries": retries})()
    return type("Task", (), {"name": name, "max_retries": max_retries, "request": request})()


def _call_handler(sender, *, task_id="t-1", exception=None):
    exc = exception or RuntimeError("boom")
    handle_task_failure(
        sender=sender,
        task_id=task_id,
        exception=exc,
        args=(),
        kwargs={},
        traceback=None,
        einfo=None,
    )


# ── Retry-skip logic ──────────────────────────────────────────────────────────

def test_skips_when_retries_remain():
    sender = _make_sender("app.workers.tasks.reconcile_pending_orders", max_retries=3, retries=1)
    mock_redis = MagicMock()
    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender)
    mock_redis.lpush.assert_not_called()


def test_skips_when_max_retries_is_none():
    """Tasks with max_retries=None retry forever and must never be dead-lettered."""
    sender = _make_sender("app.workers.tasks.some_task", max_retries=None, retries=0)
    mock_redis = MagicMock()
    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender)
    mock_redis.lpush.assert_not_called()


def test_dead_letters_on_final_retry():
    """max_retries=3, retries=3 → final attempt → should write to DLQ."""
    sender = _make_sender("app.workers.tasks.reconcile_pending_orders", max_retries=3, retries=3)
    mock_redis = MagicMock()
    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender, task_id="task-final")
    mock_redis.lpush.assert_called_once()
    mock_redis.ltrim.assert_called_once_with(_DLQ_KEY, 0, _DLQ_MAX - 1)


def test_dead_letters_zero_retry_task():
    """max_retries=0 tasks are dead-lettered on the first (and only) failure."""
    sender = _make_sender("app.workers.tasks.run_strategy_signals", max_retries=0, retries=0)
    mock_redis = MagicMock()
    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender)
    mock_redis.lpush.assert_called_once()


# ── Payload content ───────────────────────────────────────────────────────────

def test_payload_contains_expected_fields():
    import json

    sender = _make_sender("my.task", max_retries=0, retries=0)
    captured = {}

    def fake_redis():
        r = MagicMock()
        def lpush(key, payload):
            captured["payload"] = json.loads(payload)
        r.lpush.side_effect = lpush
        return r

    with patch("app.workers.dead_letter._sync_redis", side_effect=fake_redis):
        _call_handler(sender, task_id="task-abc", exception=ValueError("bad value"))

    p = captured["payload"]
    assert p["task_id"] == "task-abc"
    assert p["task_name"] == "my.task"
    assert p["exception_type"] == "ValueError"
    assert "bad value" in p["exception"]
    assert "failed_at" in p


# ── Resilience ────────────────────────────────────────────────────────────────

def test_redis_unavailability_does_not_raise():
    sender = _make_sender("my.task", max_retries=0, retries=0)
    with patch("app.workers.dead_letter._sync_redis", side_effect=ConnectionError("Redis down")):
        _call_handler(sender)  # must not raise


def test_prometheus_unavailability_does_not_raise():
    sender = _make_sender("my.task", max_retries=0, retries=0)
    mock_redis = MagicMock()
    with (
        patch("app.workers.dead_letter._sync_redis", return_value=mock_redis),
        patch("app.api.metrics.record_task_failure", side_effect=RuntimeError("prom down")),
    ):
        _call_handler(sender)  # must not raise


# ── Prometheus counter ────────────────────────────────────────────────────────

def test_prometheus_counter_incremented():
    sender = _make_sender("my.task", max_retries=0, retries=0)
    mock_redis = MagicMock()
    mock_prom = MagicMock()
    with (
        patch("app.workers.dead_letter._sync_redis", return_value=mock_redis),
        patch("app.api.metrics.record_task_failure", mock_prom),
    ):
        _call_handler(sender, task_id="t-prom")
    mock_prom.assert_called_once_with(task_name="my.task")
