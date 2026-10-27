"""Bounded admission, queueing, and rate-limit retry for ASL generation."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import random
import threading
import time
from collections.abc import Callable, Mapping
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar


T = TypeVar("T")
logger = logging.getLogger("app.capacity")


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
    queue_timeout_seconds: float | None
    operation_timeout_seconds: float | None
    request_deadline_seconds: float | None = 140.0
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
        if self.queue_timeout_seconds is not None and self.queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        if self.operation_timeout_seconds is not None and self.operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        if (self.request_deadline_seconds is not None
                and self.request_deadline_seconds <= (self.operation_timeout_seconds or 0)):
            raise ValueError("request deadline must reserve one operation timeout")
        if self.max_rate_limit_retries < 0:
            raise ValueError("max_rate_limit_retries must not be negative")
        if self.retry_base_seconds <= 0 or self.retry_max_delay_seconds <= 0:
            raise ValueError("retry delays must be positive")


def build_asl_capacity_policy(
    *,
    max_concurrency: int,
    queue_timeout_seconds: float | None,
    llm_timeout_seconds: float | None,
    request_deadline_seconds: float | None = 85.0,
    max_waiters: int = 32,
) -> AslCapacityPolicy:
    """Build a startup-safe policy while reserving time for common fanout."""
    if request_deadline_seconds is None:
        return AslCapacityPolicy(max_concurrency=max_concurrency,
            queue_timeout_seconds=queue_timeout_seconds, operation_timeout_seconds=None,
            request_deadline_seconds=None, max_waiters=max_waiters)
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
        max(request_deadline_seconds / 3.0, (llm_timeout_seconds or 0) * 2.0),
    )
    return AslCapacityPolicy(
        max_concurrency=max_concurrency,
        queue_timeout_seconds=queue_timeout_seconds,
        operation_timeout_seconds=operation_budget,
        request_deadline_seconds=request_deadline_seconds,
        max_waiters=max_waiters,
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


@dataclass(eq=False)
class _Job:
    operation: Callable[[], Any]
    enqueued: float
    context: Any = field(default_factory=contextvars.copy_context)
    started: Future = field(default_factory=Future)
    result: Future = field(default_factory=Future)
    abandoned: threading.Event = field(default_factory=threading.Event)
    running_since: float | None = None


class AslCapacityController:
    """One bounded FIFO scheduler shared by synchronous and asynchronous callers.

    Waiting HTTP callers consume no worker thread. Cancellation removes queued
    jobs immediately; running jobs retain capacity until their worker exits.
    """

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
        self._lock = threading.RLock()
        self._queue: deque[_Job] = deque()
        self._running: set[_Job] = set()
        self._counts = dict(admitted=0, rejected=0, completed=0, failed=0,
                            cancelled=0, upstream_rate_limits=0)
        self._executor = ThreadPoolExecutor(
            max_workers=policy.max_concurrency, thread_name_prefix="oagnet-asl",
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            return {**self._counts, "active": len(self._running),
                    "waiting": len(self._queue),
                    "max_concurrency": self.policy.max_concurrency,
                    "max_waiters": self.policy.max_waiters,
                    "oldest_wait_seconds": max(0.0, now - self._queue[0].enqueued) if self._queue else 0.0,
                    "longest_running_seconds": max(
                        (now - job.running_since for job in self._running), default=0.0)}

    def _enqueue(self, operation: Callable[[], T]) -> _Job:
        job = _Job(operation, self._clock())
        with self._lock:
            if (len(self._running) >= self.policy.max_concurrency
                    and len(self._queue) >= self.policy.max_waiters):
                self._counts["rejected"] += 1
                logger.warning("ASL capacity rejected: %s", self.snapshot())
                raise AslCapacityExhausted(self.policy.local_retry_after_seconds)
            self._counts["admitted"] += 1
            self._queue.append(job)
            self._dispatch()
        return job

    def _dispatch(self) -> None:
        # Called with the scheduler lock; only active jobs enter the executor.
        while self._queue and len(self._running) < self.policy.max_concurrency:
            job = self._queue.popleft()
            job.running_since = self._clock()
            self._running.add(job)
            job.started.set_result(None)
            try:
                worker = self._executor.submit(
                    job.context.run, self._run_with_rate_limit_retry,
                    job.operation, job.enqueued, job.abandoned,
                )
            except Exception as exc:
                self._running.remove(job)
                self._counts["failed"] += 1
                job.result.set_exception(exc)
                continue
            worker.add_done_callback(lambda future, current=job: self._finished(current, future))

    def _finished(self, job: _Job, worker: Future) -> None:
        with self._lock:
            self._running.remove(job)
            error = worker.exception()
            self._counts["failed" if error else "completed"] += 1
            if not job.result.done():
                if error:
                    job.result.set_exception(error)
                else:
                    job.result.set_result(worker.result())
            logger.info("ASL capacity finished queue_seconds=%.3f run_seconds=%.3f state=%s",
                        job.running_since - job.enqueued,
                        self._clock() - job.running_since, self.snapshot())
            self._dispatch()

    def _abandon(self, job: _Job) -> None:
        with self._lock:
            if job.result.done():
                return
            job.abandoned.set()
            self._counts["cancelled"] += 1
            job.result.cancel()
            if job in self._queue:
                self._queue.remove(job)
                job.started.cancel()
            # A running worker is deliberately not removed from _running.

    def _queue_budget(self) -> float | None:
        budget = self.policy.queue_timeout_seconds
        if self.policy.request_deadline_seconds is not None:
            reserved = self.policy.request_deadline_seconds - (self.policy.operation_timeout_seconds or 0)
            budget = min(budget, reserved) if budget is not None else reserved
        return budget

    def _remaining(self, job: _Job) -> float | None:
        deadline = self.policy.request_deadline_seconds
        return None if deadline is None else max(0.0, deadline - (self._clock() - job.enqueued))

    def run(self, operation: Callable[[], T]) -> T:
        """Compatibility entry point using the same scheduler, not a second queue."""
        job = self._enqueue(operation)
        try:
            try:
                job.started.result(timeout=self._queue_budget())
            except FutureTimeoutError as exc:
                raise AslCapacityExhausted(self.policy.local_retry_after_seconds) from exc
            try:
                return job.result.result(timeout=self._remaining(job))
            except FutureTimeoutError as exc:
                if job.result.done():
                    return job.result.result()
                raise AslGenerationDeadlineExceeded("ASL generation exceeded its request deadline") from exc
        except BaseException:
            self._abandon(job)
            raise

    async def arun(self, operation: Callable[[], T], *, disconnected=None) -> T:
        job = self._enqueue(operation)
        started = asyncio.wrap_future(job.started)
        result = asyncio.wrap_future(job.result)
        # A cancelled HTTP waiter may never retrieve a late worker exception.
        for future in (started, result):
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        monitor = None
        caller = asyncio.current_task()
        if disconnected is not None:
            async def watch_disconnect():
                while not result.done():
                    if await disconnected():
                        caller.cancel()
                        return
                    await asyncio.sleep(0.25)
            monitor = asyncio.create_task(watch_disconnect())
        try:
            try:
                await asyncio.wait_for(asyncio.shield(started), self._queue_budget())
            except asyncio.TimeoutError as exc:
                raise AslCapacityExhausted(self.policy.local_retry_after_seconds) from exc
            try:
                return await asyncio.wait_for(asyncio.shield(result), self._remaining(job))
            except asyncio.TimeoutError as exc:
                if result.done():
                    return result.result()
                raise AslGenerationDeadlineExceeded("ASL generation exceeded its request deadline") from exc
        except BaseException:
            self._abandon(job)
            raise
        finally:
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    def _run_with_rate_limit_retry(
        self,
        operation: Callable[[], T],
        started: float,
        abandoned: threading.Event | None = None,
    ) -> T:
        retries_used = 0
        while True:
            try:
                return operation()
            except Exception as exc:
                signal = _rate_limit_signal(exc)
                if signal is None:
                    raise
                with self._lock:
                    self._counts["upstream_rate_limits"] += 1
                if abandoned is not None and abandoned.is_set():
                    raise AslUpstreamRateLimited() from exc
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
                remaining = (float('inf') if self.policy.request_deadline_seconds is None
                    else self.policy.request_deadline_seconds - (self._clock() - started))
                can_retry = (
                    retries_used < self.policy.max_rate_limit_retries
                    and delay <= self.policy.retry_max_delay_seconds
                    and remaining
                    > delay + (self.policy.operation_timeout_seconds or 0)
                )
                if not can_retry:
                    raise AslUpstreamRateLimited(response_retry_after) from exc
                self._sleeper(delay)
                if abandoned is not None and abandoned.is_set():
                    raise AslUpstreamRateLimited(response_retry_after) from exc
                retries_used += 1
