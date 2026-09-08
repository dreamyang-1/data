"""Bounded admission, queueing, and rate-limit retry for ASL generation."""
from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar


T = TypeVar("T")


class AslCapacityExhausted(RuntimeError):
    """The local bounded ASL queue could not admit or schedule a request."""

    def __init__(self, retry_after_seconds: int = 5) -> None:
        super().__init__("ASL generation capacity is exhausted")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class AslUpstreamRateLimited(RuntimeError):
    """The upstream model remained rate-limited after safe bounded retries."""

    def __init__(self, retry_after_seconds: int = 5) -> None:
        super().__init__("upstream model rate limit was not recovered")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class AslGenerationDeadlineExceeded(RuntimeError):
    """The caller-facing ASL deadline elapsed while work was still running."""


@dataclass(frozen=True)
class AslCapacityPolicy:
    max_concurrency: int
    queue_timeout_seconds: float
    operation_timeout_seconds: float
    request_deadline_seconds: float = 140.0
    max_waiters: int = 8
    max_rate_limit_retries: int = 1
    retry_base_seconds: float = 1.0
    retry_max_delay_seconds: float = 10.0
    local_retry_after_seconds: int = 5

    def __post_init__(self) -> None:
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if self.max_waiters < 0:
            raise ValueError("max_waiters must not be negative")
        if self.queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        if self.operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        if self.request_deadline_seconds <= self.operation_timeout_seconds:
            raise ValueError("request deadline must reserve one operation timeout")
        if self.max_rate_limit_retries < 0:
            raise ValueError("max_rate_limit_retries must not be negative")
        if self.retry_base_seconds <= 0 or self.retry_max_delay_seconds <= 0:
            raise ValueError("retry delays must be positive")


def build_asl_capacity_policy(
    *,
    max_concurrency: int,
    queue_timeout_seconds: float,
    llm_timeout_seconds: float,
    request_deadline_seconds: float = 85.0,
) -> AslCapacityPolicy:
    """Build a startup-safe policy while reserving time for common fanout."""
    # ASL main may invoke the model twice for transport-format correction. Very
    # large operator-configured SDK timeouts must not make the API fail at import
    # or consume the caller's whole deadline, so the caller-facing worker budget
    # is capped and enforced separately by AslCapacityController.
    # Reserve 40 seconds of the production 85-second budget for two normal
    # 5-15 second requests ahead of this one, plus scheduling/network margin.
    # The ratio also keeps small synthetic policies useful in concurrency tests.
    queue_reserve = min(40.0, request_deadline_seconds / 2.0)
    operation_cap = request_deadline_seconds - queue_reserve
    operation_budget = min(
        operation_cap,
        max(request_deadline_seconds / 3.0, llm_timeout_seconds * 2.0),
    )
    return AslCapacityPolicy(
        max_concurrency=max_concurrency,
        queue_timeout_seconds=queue_timeout_seconds,
        operation_timeout_seconds=operation_budget,
        request_deadline_seconds=request_deadline_seconds,
        max_waiters=8,
        max_rate_limit_retries=1,
    )


@dataclass(frozen=True)
class _RateLimitSignal:
    retry_after_seconds: float | None


def parse_retry_after_seconds(
    value: Any,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse Retry-After delta-seconds or HTTP-date without accepting negatives."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            target = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        reference = now or datetime.now(timezone.utc)
        seconds = (target - reference).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _header_value(headers: Any, name: str) -> Any:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            value = getter(name.lower())
        if value is not None:
            return value
    if isinstance(headers, Mapping):
        expected = name.casefold()
        for key, value in headers.items():
            if str(key).casefold() == expected:
                return value
    return None


def _rate_limit_signal(exc: BaseException) -> _RateLimitSignal | None:
    """Recognize OpenAI-compatible 429 exceptions, including wrapped causes."""
    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        response = getattr(current, "response", None)
        status_code = getattr(current, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        is_rate_limit = status_code == 429 or type(current).__name__ == "RateLimitError"
        if is_rate_limit:
            headers = getattr(response, "headers", None)
            if headers is None:
                headers = getattr(current, "headers", None)
            retry_after = parse_retry_after_seconds(
                _header_value(headers, "Retry-After")
            )
            return _RateLimitSignal(retry_after_seconds=retry_after)
        current = current.__cause__ or current.__context__
    return None


class AslCapacityController:
    """Run ASL work with one bounded local queue and deadline-aware 429 retries."""

    def __init__(
        self,
        policy: AslCapacityPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.policy = policy
        self._clock = clock
        self._sleeper = sleeper
        self._jitter = jitter
        self._slots = threading.BoundedSemaphore(policy.max_concurrency)
        self._admission = threading.BoundedSemaphore(
            policy.max_concurrency + policy.max_waiters
        )
        self._executor = ThreadPoolExecutor(
            max_workers=policy.max_concurrency,
            thread_name_prefix="oagnet-asl",
        )

    def run(self, operation: Callable[[], T]) -> T:
        started = self._clock()
        if not self._admission.acquire(blocking=False):
            raise AslCapacityExhausted(self.policy.local_retry_after_seconds)
        slot_acquired = False
        release_deferred = False
        released = False
        release_lock = threading.Lock()

        def release_capacity(_future=None) -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
                if slot_acquired:
                    self._slots.release()
                self._admission.release()

        try:
            # Always reserve enough deadline for the admitted request's own LLM call.
            queue_budget = min(
                self.policy.queue_timeout_seconds,
                self.policy.request_deadline_seconds
                - self.policy.operation_timeout_seconds,
            )
            slot_acquired = self._slots.acquire(timeout=max(0.0, queue_budget))
            if not slot_acquired:
                raise AslCapacityExhausted(self.policy.local_retry_after_seconds)
            future = self._executor.submit(
                self._run_with_rate_limit_retry, operation, started
            )
            remaining = max(
                0.0,
                self.policy.request_deadline_seconds - (self._clock() - started),
            )
            try:
                return future.result(timeout=remaining)
            except FutureTimeoutError as exc:
                # concurrent.futures.TimeoutError aliases built-in TimeoutError.
                # Preserve a model-raised TimeoutError as an ordinary generation
                # failure instead of inventing a caller deadline expiration.
                if future.done():
                    return future.result()
                # Python cannot safely kill a running model thread. Keep its local
                # slot until it really exits, so a timed-out call never creates
                # accidental upstream concurrency, while the HTTP caller returns
                # at the hard request deadline.
                release_deferred = True
                future.add_done_callback(release_capacity)
                raise AslGenerationDeadlineExceeded(
                    "ASL generation exceeded its request deadline"
                ) from exc
        finally:
            if not release_deferred:
                release_capacity()

    def _run_with_rate_limit_retry(
        self,
        operation: Callable[[], T],
        started: float,
    ) -> T:
        retries_used = 0
        while True:
            try:
                return operation()
            except Exception as exc:
                signal = _rate_limit_signal(exc)
                if signal is None:
                    raise
                retry_after = signal.retry_after_seconds
                fallback_delay = min(
                    self.policy.retry_max_delay_seconds,
                    self.policy.retry_base_seconds * (2**retries_used),
                )
                delay = (
                    retry_after
                    if retry_after is not None
                    else self._jitter(fallback_delay * 0.8, fallback_delay * 1.2)
                )
                response_retry_after = max(1, math.ceil(retry_after or fallback_delay))
                remaining = self.policy.request_deadline_seconds - (
                    self._clock() - started
                )
                can_retry = (
                    retries_used < self.policy.max_rate_limit_retries
                    and delay <= self.policy.retry_max_delay_seconds
                    and remaining
                    > delay + self.policy.operation_timeout_seconds
                )
                if not can_retry:
                    raise AslUpstreamRateLimited(response_retry_after) from exc
                self._sleeper(delay)
                retries_used += 1
