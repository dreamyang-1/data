"""Content-free timing for the V2-context/V1-execution bridge.

The tracker records only stable operation names, relative monotonic timings,
statuses, and a small allowlist of scalar attributes. It never records prompts,
questions, SQL, result rows, URLs, credentials, or absolute infrastructure
addresses. Instrumentation is deliberately passive: when no request timing
scope is active every operation handle is a no-op.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from typing import Any, Iterator, Literal

from app.domain.models import OperationTiming, ProgressTiming, RequestPerformanceTrace


# Uvicorn configures this logger at INFO in the deployed service, while the
# process root logger commonly remains at WARNING.  Use the server logger so
# the promised request-level trace is actually present in the 8088 log.
logger = logging.getLogger("uvicorn.error")

TimingLayer = Literal[
    "V2_CONTEXT",
    "V1_ORCHESTRATION",
    "UPSTREAM",
    "VALIDATION",
    "ANALYSIS",
    "EXTENSION",
]

_SCALAR_TYPES = (str, int, float, bool, type(None))
_current_tracker: ContextVar[RequestTimingTracker | None] = ContextVar(
    "data_agent_request_timing_tracker",
    default=None,
)


class OperationHandle:
    def __init__(
        self,
        tracker: RequestTimingTracker | None,
        *,
        layer: TimingLayer,
        operation: str,
        attributes: dict[str, Any] | None,
    ) -> None:
        self._tracker = tracker
        self._layer = layer
        self._operation = operation[:100]
        self._started = time.monotonic()
        self._first_result: float | None = None
        self._finished = False
        self._attributes = _safe_attributes(attributes or {})

    def mark_first_result(self) -> None:
        if self._tracker is not None and self._first_result is None:
            self._first_result = time.monotonic()

    def set_attribute(self, key: str, value: Any) -> None:
        if key and isinstance(value, _SCALAR_TYPES):
            self._attributes[str(key)[:80]] = (
                value[:200] if isinstance(value, str) else value
            )

    def finish(
        self,
        status: Literal["COMPLETED", "FAILED", "CANCELLED"],
        *,
        error_type: str | None = None,
    ) -> None:
        if self._tracker is None or self._finished:
            return
        self._finished = True
        self._tracker._record_operation(
            layer=self._layer,
            operation=self._operation,
            started=self._started,
            first_result=self._first_result,
            finished=time.monotonic(),
            status=status,
            error_type=error_type,
            attributes=self._attributes,
        )


class RequestTimingTracker:
    """Concurrency-safe accumulator shared by all tasks in one request."""

    def __init__(self, *, runtime_mode: str) -> None:
        self.runtime_mode = str(runtime_mode or "UNKNOWN")[:50]
        self._started = time.monotonic()
        self._finished: float | None = None
        self._terminal_status = "RUNNING"
        self._operations: list[OperationTiming] = []
        self._progress: list[ProgressTiming] = []
        self._lock = RLock()

    def start_operation(
        self,
        *,
        layer: TimingLayer,
        operation: str,
        attributes: dict[str, Any] | None = None,
    ) -> OperationHandle:
        return OperationHandle(
            self,
            layer=layer,
            operation=operation,
            attributes=attributes,
        )

    def record_progress(self, event: dict[str, Any]) -> None:
        stage = str(event.get("stage") or "").strip()
        status = str(event.get("status") or "").strip().upper()
        if not stage or not status:
            return
        task_index = event.get("task_index")
        if not isinstance(task_index, int) or task_index < 0:
            task_index = None
        with self._lock:
            if len(self._progress) >= 200:
                return
            self._progress.append(
                ProgressTiming(
                    sequence=len(self._progress) + 1,
                    stage=stage[:100],
                    status=status[:30],
                    occurred_after_ms=_milliseconds(
                        time.monotonic() - self._started
                    ),
                    progress_phase=(
                        str(event.get("progress_phase"))[:100]
                        if event.get("progress_phase")
                        else None
                    ),
                    task_index=task_index,
                )
            )

    def _record_operation(
        self,
        *,
        layer: TimingLayer,
        operation: str,
        started: float,
        first_result: float | None,
        finished: float,
        status: Literal["COMPLETED", "FAILED", "CANCELLED"],
        error_type: str | None,
        attributes: dict[str, Any],
    ) -> None:
        with self._lock:
            if len(self._operations) >= 100:
                return
            self._operations.append(
                OperationTiming(
                    sequence=len(self._operations) + 1,
                    layer=layer,
                    operation=operation,
                    started_after_ms=_milliseconds(started - self._started),
                    first_result_after_ms=(
                        _milliseconds(first_result - started)
                        if first_result is not None
                        else None
                    ),
                    duration_ms=_milliseconds(finished - started),
                    status=status,
                    error_type=(error_type or "")[:100] or None,
                    attributes=attributes,
                )
            )

    def finish(self, terminal_status: str) -> RequestPerformanceTrace:
        with self._lock:
            if self._finished is None:
                self._finished = time.monotonic()
                self._terminal_status = str(terminal_status or "UNKNOWN")[:50]
            ordered = sorted(
                self._operations,
                key=lambda item: (item.started_after_ms, item.sequence),
            )
            slow = sorted(
                (
                    item
                    for item in ordered
                    if item.duration_ms >= 3000
                ),
                key=lambda item: item.duration_ms,
                reverse=True,
            )[:20]
            return RequestPerformanceTrace(
                runtime_mode=self.runtime_mode,
                total_duration_ms=_milliseconds(self._finished - self._started),
                terminal_status=self._terminal_status,
                operations=ordered,
                progress=list(self._progress),
                slow_operations=[
                    f"{item.operation}:{item.duration_ms}ms" for item in slow
                ],
            )


def current_timing_tracker() -> RequestTimingTracker | None:
    return _current_tracker.get()


@contextmanager
def timing_scope(
    tracker: RequestTimingTracker | None,
) -> Iterator[RequestTimingTracker | None]:
    token = _current_tracker.set(tracker)
    try:
        yield tracker
    finally:
        _current_tracker.reset(token)


@contextmanager
def track_operation(
    layer: TimingLayer,
    operation: str,
    *,
    attributes: dict[str, Any] | None = None,
) -> Iterator[OperationHandle]:
    tracker = current_timing_tracker()
    handle = OperationHandle(
        tracker,
        layer=layer,
        operation=operation,
        attributes=attributes,
    )
    try:
        yield handle
    except BaseException as exc:
        handle.finish(
            "CANCELLED" if type(exc).__name__ == "CancelledError" else "FAILED",
            error_type=type(exc).__name__,
        )
        raise
    else:
        handle.finish("COMPLETED")


def log_performance_trace(trace: RequestPerformanceTrace) -> None:
    """Write one grep-friendly JSON record without business content."""

    logger.info(
        "bridge_performance_trace=%s",
        json.dumps(trace.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":")),
    )


def _safe_attributes(values: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in list(values.items())[:20]:
        if not isinstance(value, _SCALAR_TYPES):
            continue
        result[str(key)[:80]] = value[:200] if isinstance(value, str) else value
    return result


def _milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1000))
