"""Optional, privacy-safe Langfuse instrumentation for the data agent.

The application calls OpenAI-compatible endpoints with ``httpx`` instead of an
SDK integration, so generations are recorded explicitly.  This module is a
strictly optional adapter: missing packages, configuration, or an unavailable
Langfuse server must never change a business response.

Content collection is disabled by default.  In that mode questions, prompts,
answers, SQL-like tool output, and identifiers are represented by bounded
metadata and salted hashes only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import Settings

try:  # The service remains importable before optional deployment deps are installed.
    from langfuse import Langfuse as _Langfuse
    from langfuse import propagate_attributes as _propagate_attributes
except Exception:  # pragma: no cover - exercised in environments without the SDK
    _Langfuse = None
    _propagate_attributes = None


logger = logging.getLogger(__name__)
_lock = threading.RLock()
_client: Any | None = None
_enabled = False
_capture_content = False
_max_content_chars = 1000
_hash_key = b"data-analysis-agent-observability"

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|secret|password|token)\s*[:=]\s*)[^\s,;]+"),
)


class _NullObservation:
    def update(self, *_args: Any, **_kwargs: Any) -> "_NullObservation":
        return self

    def end(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _NullGenerationRecorder:
    def set_response(self, _payload: Any = None) -> None:
        return None


_NULL_OBSERVATION = _NullObservation()
_NULL_GENERATION = _NullGenerationRecorder()


def is_enabled() -> bool:
    return bool(_enabled and _client is not None)


def _secret_value(value: Any) -> str:
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value)


def _safe_shutdown(client: Any | None) -> None:
    if client is None:
        return
    try:
        shutdown = getattr(client, "shutdown", None)
        if callable(shutdown):
            shutdown()
        else:
            flush = getattr(client, "flush", None)
            if callable(flush):
                flush()
    except Exception:
        logger.debug("Langfuse shutdown failed", exc_info=True)


def configure_langfuse(settings: Settings) -> bool:
    """Configure one process-wide client from the effective application settings."""

    global _client, _enabled, _capture_content, _max_content_chars, _hash_key
    public_key = _secret_value(settings.langfuse_public_key).strip()
    secret_key = _secret_value(settings.langfuse_secret_key).strip()
    host = str(settings.langfuse_host or "").strip()
    requested = bool(
        settings.langfuse_enabled
        and settings.env != "test"
        and not os.getenv("PYTEST_CURRENT_TEST")
        and public_key
        and secret_key
        and host
    )
    with _lock:
        previous = _client
        _client = None
        _enabled = False
        _capture_content = bool(settings.langfuse_capture_content)
        _max_content_chars = int(settings.langfuse_max_content_chars)
        salt = _secret_value(settings.langfuse_hash_salt).strip() or secret_key
        _hash_key = (salt or "data-analysis-agent-observability").encode("utf-8")
        if not requested:
            _safe_shutdown(previous)
            return False
        if _Langfuse is None:
            logger.warning("Langfuse is configured but the langfuse package is unavailable")
            _safe_shutdown(previous)
            return False
        try:
            _client = _Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
                tracing_enabled=True,
                sample_rate=float(settings.langfuse_sample_rate),
                environment=settings.langfuse_environment or settings.env,
                release=settings.langfuse_release or None,
            )
            _enabled = True
        except Exception:
            _client = None
            logger.exception("Langfuse client initialization failed; tracing disabled")
        finally:
            if previous is not _client:
                _safe_shutdown(previous)
        return _enabled


def flush() -> None:
    """Flush pending telemetry. Call at process shutdown, not per request."""

    client = _client
    if not is_enabled() or client is None:
        return
    try:
        client.flush()
    except Exception:
        logger.debug("Langfuse flush failed", exc_info=True)


def shutdown_langfuse() -> None:
    global _client, _enabled
    with _lock:
        client = _client
        _client = None
        _enabled = False
    _safe_shutdown(client)


def hash_identifier(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "hmac-sha256:" + hmac.new(
        _hash_key, text.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(r"\1[REDACTED]", result)
    return result[:_max_content_chars]


def text_metadata(value: Any, *, allow_content: bool | None = None) -> dict[str, Any]:
    text = str(value or "")
    payload: dict[str, Any] = {
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
    }
    include = _capture_content if allow_content is None else allow_content
    if include and text:
        payload["content"] = _redact_text(text)
        payload["truncated"] = len(text) > _max_content_chars
    return payload


def summarize_value(value: Any) -> dict[str, Any]:
    """Return shape-only metadata for potentially sensitive tool/result values."""

    if value is None:
        return {"type": "null"}
    if isinstance(value, dict):
        encoded = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        return {
            "type": "object",
            "keys": sorted(str(key)[:80] for key in value)[:30],
            "items": len(value),
            **text_metadata(encoded, allow_content=False),
        }
    if isinstance(value, (list, tuple)):
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        return {
            "type": "array",
            "items": len(value),
            **text_metadata(encoded, allow_content=False),
        }
    if isinstance(value, (int, float, bool)):
        return {"type": type(value).__name__}
    return {"type": type(value).__name__, **text_metadata(value, allow_content=False)}


def _message_metadata(messages: Any) -> dict[str, Any]:
    if not isinstance(messages, list):
        return summarize_value(messages)
    items: list[dict[str, Any]] = []
    for message in messages[:20]:
        if not isinstance(message, dict):
            items.append(summarize_value(message))
            continue
        items.append({
            "role": str(message.get("role") or "")[:30],
            "content": text_metadata(message.get("content")),
        })
    return {"count": len(messages), "messages": items, "truncated": len(messages) > 20}


def _tool_metadata(tools: Any) -> dict[str, Any] | None:
    if not isinstance(tools, list):
        return None if tools is None else summarize_value(tools)
    names: list[str] = []
    for tool in tools[:50]:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"])[:100])
        elif tool.get("name"):
            names.append(str(tool["name"])[:100])
    return {"count": len(tools), "names": names, "truncated": len(tools) > 50}


@contextmanager
def observe_span(
    *, as_type: str = "span", name: str, input: Any = None, **kwargs: Any
) -> Iterator[Any]:
    if not is_enabled():
        yield _NULL_OBSERVATION
        return
    try:
        manager = _client.start_as_current_observation(
            as_type=as_type,
            name=name,
            input=input,
            **kwargs,
        )
        observation = manager.__enter__()
    except Exception:
        logger.debug("Langfuse observation creation failed: %s", name, exc_info=True)
        yield _NULL_OBSERVATION
        return
    try:
        yield observation
    except BaseException as exc:
        try:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            logger.debug("Langfuse observation close failed: %s", name, exc_info=True)
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            logger.debug("Langfuse observation close failed: %s", name, exc_info=True)


@contextmanager
def trace_attributes(**kwargs: Any) -> Iterator[None]:
    if not is_enabled() or _propagate_attributes is None:
        yield
        return
    try:
        manager = _propagate_attributes(**kwargs)
        manager.__enter__()
    except Exception:
        logger.debug("Langfuse attribute propagation failed", exc_info=True)
        yield
        return
    try:
        yield
    except BaseException as exc:
        try:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            logger.debug("Langfuse attribute close failed", exc_info=True)
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            logger.debug("Langfuse attribute close failed", exc_info=True)


def _extract_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}

    def value(*keys: str) -> int:
        for key in keys:
            found = usage.get(key)
            if isinstance(found, (int, float)):
                return int(found)
        return 0

    result = {
        "input": value("prompt_tokens", "input_tokens"),
        "output": value("completion_tokens", "output_tokens"),
        "total": value("total_tokens"),
    }
    if not result["total"]:
        result["total"] = result["input"] + result["output"]
    return result


def _generation_output(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return summarize_value(payload)
    first = choices[0]
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    return {
        "finish_reason": first.get("finish_reason"),
        "role": str(message.get("role") or "assistant")[:30],
        "content": text_metadata(message.get("content")),
        "has_tool_calls": bool(message.get("tool_calls")),
    }


class _GenerationRecorder:
    def __init__(self, observation: Any) -> None:
        self._observation = observation

    def set_response(self, payload: Any = None) -> None:
        if not isinstance(payload, dict):
            return
        try:
            self._observation.update(
                output=_generation_output(payload),
                usage_details=_extract_usage(payload),
            )
        except Exception:
            logger.debug("Langfuse generation update failed", exc_info=True)


@contextmanager
def trace_generation(
    *,
    name: str,
    model: str | None = None,
    messages: Any = None,
    tools: Any = None,
) -> Iterator[Any]:
    if not is_enabled():
        yield _NULL_GENERATION
        return
    generation_input: dict[str, Any] = {"messages": _message_metadata(messages)}
    tool_info = _tool_metadata(tools)
    if tool_info is not None:
        generation_input["tools"] = tool_info
    with observe_span(
        as_type="generation",
        name=name,
        model=model,
        input=generation_input,
    ) as observation:
        yield _GenerationRecorder(observation)


class StageSpanTracker:
    """Convert public progress events into discrete, concurrency-safe spans."""

    def __init__(self) -> None:
        self._last_event: tuple[str, str, str] | None = None

    def handle(self, event: dict[str, Any]) -> None:
        if not is_enabled() or not isinstance(event, dict):
            return
        stage = str(event.get("stage") or "").strip()[:100]
        status = str(event.get("status") or "").strip().upper()[:30]
        task_id = str(event.get("task_id") or "").strip()
        if not stage:
            return
        fingerprint = (stage, status, task_id)
        if fingerprint == self._last_event:
            return
        self._last_event = fingerprint
        payload = {
            "stage": stage,
            "status": status,
            "message": text_metadata(event.get("message"), allow_content=False),
            "task_id": hash_identifier(task_id),
        }
        level = "ERROR" if status in {"FAILED", "ERROR"} else "DEFAULT"
        try:
            with observe_span(as_type="span", name=f"stage:{stage}", input=payload) as span:
                span.update(output={"status": status}, level=level)
        except Exception:
            logger.debug("Langfuse progress span failed: %s", stage, exc_info=True)

    def close_all(self) -> None:
        self._last_event = None
