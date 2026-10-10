"""Unit tests for the Celery dead-letter queue handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.workers.dead_letter import _DLQ_KEY, _DLQ_MAX, handle_task_failure


def _make_sender(name: str, max_retries: int, retries: int):
    request = type("Request", (), {"retries": retries})()
    return type("Task", (), {"name": name, "max_retries": max_retries, "request": request})()


def _call_handler(sender, *, task_id="t-1", exception=None, traceback=None, einfo=None):
    exc = exception or RuntimeError("boom")
    handle_task_failure(
        sender=sender,
        task_id=task_id,
        exception=exc,
        args=(),
        kwargs={},
        traceback=traceback,
        einfo=einfo,
    )


def _captured_payloads(mock_redis: MagicMock) -> list[dict]:
    import json

    return [json.loads(call.args[1]) for call in mock_redis.lpush.call_args_list]


def _worker_main_failure():
    """(exception, einfo) as Celery's worker main process passes them for a failed pool job.

    ``Request.on_failure`` unwraps billiard's ``ExceptionWithTraceback`` before it sends
    ``task_failure`` and passes ``einfo.traceback``, which is a formatted string.
    """
    from billiard.einfo import ExceptionInfo

    try:
        raise ValueError("not enough values to unpack (expected 3, got 0)")
    except ValueError:
        einfo = ExceptionInfo()
    return getattr(einfo.exception, "exc", einfo.exception), einfo


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


# ── Failures reported by the worker's main process ────────────────────────────


def test_worker_main_failure_with_formatted_traceback_is_dead_lettered():
    """Celery passes a pre-formatted string traceback when the main process reports a failure."""
    exception, einfo = _worker_main_failure()
    assert isinstance(einfo.traceback, str)
    sender = _make_sender("app.workers.tasks.run_strategy_signals", max_retries=0, retries=0)
    mock_redis = MagicMock()
    mock_prom = MagicMock()

    with (
        patch("app.workers.dead_letter._sync_redis", return_value=mock_redis),
        patch("app.api.metrics.record_task_failure", mock_prom),
    ):
        _call_handler(
            sender,
            task_id="task-main",
            exception=exception,
            traceback=einfo.traceback,
            einfo=einfo,
        )

    (payload,) = _captured_payloads(mock_redis)
    assert payload["task_id"] == "task-main"
    assert payload["exception_type"] == "ValueError"
    assert payload["traceback"] == einfo.traceback.rstrip().splitlines()[-3:]
    assert "ValueError: not enough values to unpack" in payload["traceback"][-1]
    mock_prom.assert_called_once_with(task_name="app.workers.tasks.run_strategy_signals")


def test_worker_main_failure_is_final_even_when_the_task_allows_retries():
    """The main process reports a failure only after the task can no longer retry itself,
    and its task object carries no retry count, so the retry counters say nothing here."""
    exception, einfo = _worker_main_failure()
    sender = _make_sender("app.workers.tasks.reconcile_pending_orders", max_retries=3, retries=0)
    mock_redis = MagicMock()

    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender, exception=exception, traceback=einfo.traceback, einfo=einfo)

    mock_redis.lpush.assert_called_once()


def test_task_failure_with_traceback_object_keeps_the_last_frames():
    sender = _make_sender("my.task", max_retries=0, retries=0)
    mock_redis = MagicMock()
    try:
        raise KeyError("missing")
    except KeyError as exc:
        with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
            _call_handler(sender, exception=exc, traceback=exc.__traceback__)

    (payload,) = _captured_payloads(mock_redis)
    assert payload["traceback"][-1].startswith("KeyError: 'missing'")
    assert any("test_dead_letter.py" in line for line in payload["traceback"])


def test_task_failure_with_traceback_object_still_respects_remaining_retries():
    sender = _make_sender("my.task", max_retries=3, retries=1)
    mock_redis = MagicMock()
    try:
        raise KeyError("missing")
    except KeyError as exc:
        with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
            _call_handler(sender, exception=exc, traceback=exc.__traceback__)

    mock_redis.lpush.assert_not_called()


def test_unrecognised_traceback_value_still_dead_letters():
    sender = _make_sender("my.task", max_retries=0, retries=0)
    mock_redis = MagicMock()

    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender, exception=RuntimeError("boom"), traceback=object())

    (payload,) = _captured_payloads(mock_redis)
    assert payload["traceback"] == ["RuntimeError: boom"]


@pytest.mark.parametrize("failure", ["time_limit", "worker_lost"])
def test_hard_time_limit_and_lost_worker_are_dead_lettered(failure: str):
    """The two failures the worker main process reports most often in production."""
    from billiard.einfo import ExceptionInfo
    from billiard.exceptions import TimeLimitExceeded, WorkerLostError

    exception = (
        TimeLimitExceeded(120)
        if failure == "time_limit"
        else WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL)")
    )
    try:
        raise exception
    except type(exception):
        einfo = ExceptionInfo()
    sender = _make_sender("app.workers.tasks.check_eod_flatten", max_retries=3, retries=0)
    mock_redis = MagicMock()

    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender, exception=exception, traceback=einfo.traceback, einfo=einfo)

    (payload,) = _captured_payloads(mock_redis)
    assert payload["exception_type"] == type(exception).__name__
    assert f"{type(exception).__name__}: " in payload["traceback"][-1]


@pytest.mark.parametrize("with_traceback", ["string", "object", "none"])
def test_exception_with_a_broken_str_is_still_dead_lettered(with_traceback: str):
    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no str")

    try:
        raise Unprintable
    except Unprintable as exc:
        exception = exc
    traceback = {
        "string": "Traceback (most recent call last):\n  frame\nUnprintable",
        "object": exception.__traceback__,
        "none": None,
    }[with_traceback]
    sender = _make_sender("my.task", max_retries=0, retries=0)
    mock_redis = MagicMock()

    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender, exception=exception, traceback=traceback)

    (payload,) = _captured_payloads(mock_redis)
    assert payload["exception_type"] == "Unprintable"
    assert payload["exception"] == "<unprintable Unprintable>"
    assert payload["traceback"]


def test_exception_message_is_bounded():
    sender = _make_sender("my.task", max_retries=0, retries=0)
    mock_redis = MagicMock()

    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender, exception=RuntimeError("x" * 10_000))

    (payload,) = _captured_payloads(mock_redis)
    assert len(payload["exception"]) == 2_000


def test_both_traceback_forms_are_stored_as_lines_without_newlines():
    exception, einfo = _worker_main_failure()
    sender = _make_sender("my.task", max_retries=0, retries=0)
    mock_redis = MagicMock()

    with patch("app.workers.dead_letter._sync_redis", return_value=mock_redis):
        _call_handler(sender, exception=exception, traceback=einfo.traceback, einfo=einfo)
        _call_handler(sender, exception=exception, traceback=exception.__traceback__)

    from_string, from_object = _captured_payloads(mock_redis)
    assert from_string["traceback"] == from_object["traceback"]
    assert all("\n" not in line for line in from_object["traceback"])
