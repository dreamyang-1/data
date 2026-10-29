import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from capacity_control import (
    AslCapacityController,
    AslCapacityExhausted,
    AslCapacityPolicy,
    AslGenerationDeadlineExceeded,
    AslUpstreamRateLimited,
    build_asl_capacity_policy,
    parse_retry_after_seconds,
)


class _Response:
    status_code = 429

    def __init__(self, retry_after: str | None = None) -> None:
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


class _RateLimitError(Exception):
    status_code = 429

    def __init__(self, retry_after: str | None = None) -> None:
        super().__init__("rate limited")
        self.response = _Response(retry_after)


def _policy(**overrides) -> AslCapacityPolicy:
    values = {
        "max_concurrency": 1,
        "queue_timeout_seconds": 1,
        "operation_timeout_seconds": 0.2,
        "request_deadline_seconds": 2,
        "max_waiters": 2,
        "max_rate_limit_retries": 1,
        "retry_base_seconds": 0.1,
        "retry_max_delay_seconds": 1,
    }
    values.update(overrides)
    return AslCapacityPolicy(**values)


def test_three_concurrent_requests_use_one_slot_without_queue_failure() -> None:
    controller = AslCapacityController(_policy())
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def operation(value: int) -> int:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return value
        finally:
            with state_lock:
                active -= 1

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = sorted(pool.map(lambda value: controller.run(lambda: operation(value)), range(3)))
    elapsed = time.monotonic() - started

    assert results == [0, 1, 2]
    assert max_active == 1
    assert 0.12 <= elapsed < 1


def test_retry_after_is_honored_before_one_bounded_retry() -> None:
    now = [0.0]
    sleeps: list[float] = []
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _RateLimitError("0.4")
        return "ok"

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    controller = AslCapacityController(
        _policy(), clock=lambda: now[0], sleeper=sleep
    )

    assert controller.run(operation) == "ok"
    assert calls == 2
    assert sleeps == [0.4]


def test_missing_retry_after_uses_jittered_exponential_backoff() -> None:
    now = [0.0]
    sleeps: list[float] = []
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _RateLimitError()
        return "ok"

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    controller = AslCapacityController(
        _policy(retry_base_seconds=0.5),
        clock=lambda: now[0],
        sleeper=sleep,
        jitter=lambda _low, high: high,
    )

    assert controller.run(operation) == "ok"
    assert sleeps == [0.6]


def test_retry_is_rejected_when_it_cannot_fit_request_deadline() -> None:
    now = [0.0]
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        now[0] = 1.85
        raise _RateLimitError("0.2")

    controller = AslCapacityController(
        _policy(request_deadline_seconds=2),
        clock=lambda: now[0],
        sleeper=lambda _seconds: pytest.fail("deadline must prevent sleeping"),
    )

    with pytest.raises(AslUpstreamRateLimited) as error:
        controller.run(operation)
    assert calls == 1
    assert error.value.retry_after_seconds == 1


def test_retry_after_supports_http_date_contract() -> None:
    now = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
    assert parse_retry_after_seconds(
        "Fri, 28 Aug 2026 04:00:07 GMT", now=now
    ) == 7
    assert parse_retry_after_seconds("-1", now=now) is None
    assert parse_retry_after_seconds("not-a-date", now=now) is None


def test_large_legal_llm_timeout_builds_bounded_startup_safe_policy() -> None:
    policy = build_asl_capacity_policy(
        max_concurrency=1,
        queue_timeout_seconds=90,
        llm_timeout_seconds=10_000,
    )

    assert policy.operation_timeout_seconds == 45
    assert policy.request_deadline_seconds == 85
    assert policy.operation_timeout_seconds < policy.request_deadline_seconds


def test_model_timeout_exception_is_not_misclassified_as_hard_deadline() -> None:
    controller = AslCapacityController(_policy())

    def model_timeout() -> None:
        raise TimeoutError("model transport timed out")

    with pytest.raises(TimeoutError, match="model transport timed out"):
        controller.run(model_timeout)


def test_production_budget_ratio_handles_three_normal_fanout_calls() -> None:
    # 100x time compression: 0.15s represents a normal 15s model call.
    policy = build_asl_capacity_policy(
        max_concurrency=1,
        queue_timeout_seconds=0.9,
        llm_timeout_seconds=0.45,
        request_deadline_seconds=0.85,
    )
    controller = AslCapacityController(policy)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = sorted(
            pool.map(
                lambda value: controller.run(
                    lambda: (time.sleep(0.15), value)[1]
                ),
                range(3),
            )
        )

    assert results == [0, 1, 2]


def test_hard_deadline_returns_but_keeps_slot_until_model_exits() -> None:
    release_model = threading.Event()
    model_started = threading.Event()
    controller = AslCapacityController(
        _policy(
            queue_timeout_seconds=0.03,
            operation_timeout_seconds=0.02,
            request_deadline_seconds=0.06,
            max_waiters=1,
        )
    )

    def blocked_model() -> str:
        model_started.set()
        release_model.wait(1)
        return "late"

    started = time.monotonic()
    try:
        with pytest.raises(AslGenerationDeadlineExceeded):
            controller.run(blocked_model)
        assert model_started.is_set()
        assert time.monotonic() - started < 0.2

        # The timed-out worker is still alive, so its single upstream slot must
        # remain occupied instead of allowing accidental model concurrency.
        with pytest.raises(AslCapacityExhausted):
            controller.run(lambda: "must not run")
    finally:
        release_model.set()

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            assert controller.run(lambda: "next") == "next"
            break
        except AslCapacityExhausted:
            time.sleep(0.01)
    else:
        pytest.fail("capacity was not released after the timed-out model exited")
