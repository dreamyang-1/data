"""Request-scoped, public progress events for the SSE API.

The callback is held in a ContextVar so concurrent requests cannot observe one
another's events.  Business services may emit bounded, user-safe milestones
without depending on FastAPI or an asyncio queue.  No prompts, model reasoning,
SQL text, credentials, or raw rows are allowed in these events.
"""
from __future__ import annotations

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterator


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
_progress_callback: ContextVar[ProgressCallback | None] = ContextVar(
    "data_agent_progress_callback", default=None
)
_task_progress_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "data_agent_task_progress_context", default=None
)


@contextmanager
def progress_scope(callback: ProgressCallback) -> Iterator[None]:
    """Bind a progress consumer to the current asynchronous request context."""

    token = _progress_callback.set(callback)
    try:
        yield
    finally:
        _progress_callback.reset(token)


@contextmanager
def task_progress_scope(**metadata: Any) -> Iterator[None]:
    """Tag progress emitted by one DAG child without changing its workflow.

    ``asyncio`` copies ContextVars into every created task, so concurrent DAG
    branches keep independent task identifiers while still publishing to the
    parent request's callback.  The transport can then suppress or group child
    intent traces instead of nondeterministically presenting one as the root
    intent.
    """

    context = {
        key: value
        for key, value in metadata.items()
        if value is not None
    }
    token = _task_progress_context.set(context or None)
    try:
        yield
    finally:
        _task_progress_context.reset(token)


async def emit_progress(
    stage: str,
    status: str,
    message: str,
    **details: Any,
) -> None:
    """Emit one sanitized milestone when the caller requested streaming."""

    callback = _progress_callback.get()
    if callback is None:
        return
    event: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "message": message[:2000],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    task_context = _task_progress_context.get() or {}
    for key, value in task_context.items():
        if isinstance(value, str):
            event[key] = value[:1000]
        elif isinstance(value, (int, float, bool)) or value is None:
            event[key] = value
    # Only callers in this package choose details.  Keep the envelope bounded
    # even if a future adapter accidentally supplies an oversized value.
    for key, value in list(details.items())[:10]:
        if isinstance(value, str):
            event[key] = value[:1000]
        elif isinstance(value, (int, float, bool)) or value is None:
            event[key] = value
        elif isinstance(value, list):
            event[key] = value[:20]
    result = callback(event)
    if inspect.isawaitable(result):
        await result
