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
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
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


def value_metadata(value: Any) -> dict[str, Any]:
    """Bound business content and strip credential fields before serialization."""
    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(k): "[REDACTED]" if re.search(
                    r"(?i)authorization|password|secret|token|api[_-]?key|credential|signed_url", str(k)
                ) else clean(v) for k, v in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item[:50]]
        return item

    return text_metadata(json.dumps(clean(value), ensure_ascii=False, default=str))


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


# 请求级结构节点登记：trace_id → {key: span}。
# 规划/分类等代码跑在独立 asyncio 任务里，contextvar 登记传不回 API
# 入口所在的任务，按 OTel trace 归桶才能保证请求结束时统一关闭
_structural_by_trace: dict[int, dict] = {}
_structural_lock = threading.RLock()
_structural_activity: dict[int, dict[str, int]] = {}
_structural_parents: dict[int, dict[str, str]] = {}

# Same logical nodes as the public progress renderer. Only visited nodes exist.
_STAGES = {
    "comprehension": ("意图识别", 1),
    "task_planning": ("任务拆分与规划", 2),
    "parsing": ("解析校验", 3),
    "execution": ("调度执行", 4),
    "validation": ("结果校验", 5),
    "insight": ("数据洞察分析", 6),
    "final_output": ("最终输出", 7),
}
_PROGRESS_STAGES = {
    "INTENT_RECOGNITION": "comprehension", "FILE_INSPECTION": "comprehension",
    "TASK_PLANNING": "task_planning", "ASL_GENERATION": "parsing",
    "DATA_RETRIEVAL": "execution", "SEMANTIC_QUERY_PLANNING": "execution",
    "SQL_TRANSLATION": "execution", "SQL_EXECUTION": "execution",
    "ANALYSIS_TOOL": "execution", "KNOWLEDGE_RETRIEVAL": "execution",
    "EXTERNAL_SEARCH": "execution", "RELIABILITY_CHECK": "validation",
    "INSIGHT_ANALYSIS": "insight", "FINAL_OUTPUT": "final_output",
}
_OPERATIONS = {
    "v1.intent_recognition": ("comprehension", "问题意图判断"),
    "v1.question_rewrite": ("comprehension", "问题补全"),
    "v1.task_decomposition": ("task_planning", "任务拆分"),
    "semantic.contract.validation": ("parsing", "参数契约校验"),
    "v1.semantic_binding": ("parsing", "语义参数绑定"),
    "upstream.oagnet.asl_generation": ("parsing", "ASL生成"),
    "upstream.sql.translation": ("execution", "SQL翻译"),
    "upstream.sql.execution": ("execution", "SQL执行"),
    "analysis.deterministic": ("execution", "结果计算"),
    "validation.result_reliability": ("validation", "结果可信度校验"),
    "analysis.synthesis": ("insight", "数据洞察生成"),
    "chart.specification": ("final_output", "图表规划"),
    "mcp.chart_render": ("final_output", "图表渲染"),
    "chart.inline_render": ("final_output", "图表渲染"),
}
_active_operation: ContextVar[str | None] = ContextVar("langfuse_operation", default=None)


def _touch_structural(key: str, at: int | None = None) -> None:
    trace_key = _current_trace_key()
    if trace_key is None:
        return
    with _structural_lock:
        activity = _structural_activity.setdefault(trace_key, {})
        parents = _structural_parents.get(trace_key, {})
        instant = at or time.time_ns()
        while key and key != "request":
            activity[key] = max(activity.get(key, 0), instant)
            key = parents.get(key, "")


def _current_trace_key() -> int | None:
    """当前 OTel 上下文的 trace id；不在有效 span 内返回 None。"""
    try:
        from opentelemetry import trace as otel_trace

        span_context = otel_trace.get_current_span().get_span_context()
        if span_context is not None and span_context.is_valid:
            return span_context.trace_id
    except Exception:
        logger.debug("Langfuse trace key resolve failed", exc_info=True)
    return None


def register_structural_span(key: str, observation: Any) -> None:
    """登记请求级结构节点，后续判定/规划节点挂到它下面。"""
    if observation is None or observation is _NULL_OBSERVATION:
        return
    trace_key = _current_trace_key()
    if trace_key is None:
        return
    with _structural_lock:
        bucket = _structural_by_trace.setdefault(trace_key, {})
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = observation
            return
    # 并行分支重复创建的多余节点直接关掉，不留没人关闭的孤儿 span
    try:
        observation.end()
    except Exception:
        logger.debug("Langfuse duplicate structural span close failed: %s", key, exc_info=True)


def get_structural_span(key: str) -> Any | None:
    trace_key = _current_trace_key()
    if trace_key is None:
        return None
    with _structural_lock:
        return _structural_by_trace.get(trace_key, {}).get(key)


def _resolve_structural_parent(parent_key: Any) -> Any | None:
    keys = (parent_key,) if isinstance(parent_key, str) else tuple(parent_key or ())
    for candidate in keys:
        span = get_structural_span(candidate)
        if span is not None:
            return span
    return None


def ensure_structural_span(
    key: str, *, name: str, parent_key: Any = None, input: Any = None
) -> Any:
    """取已登记的结构节点；没有就创建一个并登记。

    创建的节点不占当前上下文、不在这里关闭，统一由 end_structural_spans
    在请求结束时收尾；parent_key 给定时挂到对应结构节点下（可传元组
    按顺序回退），否则挂当前上下文。
    """
    if not is_enabled():
        return _NULL_OBSERVATION
    # Events are exported on a worker while DAG tasks run concurrently. Resolve
    # and create atomically so they share exactly one parent per request/stage.
    with _structural_lock:
        existing = get_structural_span(key)
        if existing is not None:
            return existing
        metadata = {}
        if key in _STAGES:
            name, order = _STAGES[key]
            parent_key = "request"
            metadata = {"stage": key, "stage_order": order}
        parent = _resolve_structural_parent(parent_key)
        try:
            factory = parent if parent is not None else _client
            observation = factory.start_observation(
                as_type="span", name=name, input=input, metadata=metadata
            )
        except Exception:
            logger.debug("Langfuse structural span creation failed: %s", name, exc_info=True)
            return _NULL_OBSERVATION
        register_structural_span(key, observation)
        trace_key = _current_trace_key()
        if trace_key is not None:
            bucket = _structural_by_trace.get(trace_key, {})
            parent_name = next((k for k, v in bucket.items() if v is parent), "")
            _structural_parents.setdefault(trace_key, {})[key] = parent_name
            _touch_structural(key)
        return observation


def end_structural_spans() -> None:
    """请求结束时统一关闭本轮登记的结构节点，由 API 入口的 finally 调用。"""
    trace_key = _current_trace_key()
    if trace_key is None:
        return
    with _structural_lock:
        spans = _structural_by_trace.pop(trace_key, {})
        activity = _structural_activity.pop(trace_key, {})
        _structural_parents.pop(trace_key, None)
    for key, observation in reversed(list(spans.items())):
        if key == "request":  # Owned by the API context manager.
            continue
        end = getattr(observation, "end", None)
        if not callable(end):
            continue
        try:
            if activity.get(key):
                end(end_time=activity[key])
            else:
                end()
        except Exception:
            logger.debug("Langfuse structural span close failed: %s", key, exc_info=True)


@contextmanager
def structural_child(
    parent_key: Any, *, as_type: str = "span", name: str, input: Any = None, **kwargs: Any
) -> Iterator[Any]:
    """在指定结构节点下建子节点并置为当前，子级模型调用自动嵌套。

    父节点已关闭也能挂靠（OTel 上下文不依赖父级存活）；parent_key 可传
    元组按顺序回退，全部缺失时退回当前上下文嵌套，树保持未归拢时的结构。
    """
    parent = _resolve_structural_parent(parent_key)
    if parent is None or not is_enabled():
        with observe_span(as_type=as_type, name=name, input=input, **kwargs) as observation:
            yield observation
        return
    try:
        manager = parent.start_as_current_observation(
            as_type=as_type, name=name, input=input, **kwargs
        )
        observation = manager.__enter__()
    except Exception:
        logger.debug("Langfuse structural child creation failed: %s", name, exc_info=True)
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
    finally:
        keys = (parent_key,) if isinstance(parent_key, str) else tuple(parent_key or ())
        for key in keys:
            if get_structural_span(key) is parent:
                _touch_structural(key)
                break


@contextmanager
def generation_under(
    parent: Any, *, name: str, model: Any = None, input: Any = None, **kwargs: Any
) -> Iterator[Any]:
    """把模型调用 generation 建到指定 span 下面。

    parent 是真实 span 时建显式子节点（不占当前上下文，跨阶段归拢用）；
    否则退回当前上下文嵌套。
    """
    if parent is None or parent is _NULL_OBSERVATION or not is_enabled():
        with observe_span(
            as_type="generation", name=name, model=model, input=input, **kwargs
        ) as observation:
            yield observation
        return
    try:
        observation = parent.start_observation(
            as_type="generation", name=name, model=model, input=input, **kwargs
        )
    except Exception:
        logger.debug("Langfuse generation creation failed: %s", name, exc_info=True)
        yield _NULL_OBSERVATION
        return
    try:
        yield observation
    finally:
        try:
            observation.end()
        except Exception:
            logger.debug("Langfuse generation close failed: %s", name, exc_info=True)
        trace_key = _current_trace_key()
        with _structural_lock:
            for key, registered in _structural_by_trace.get(trace_key, {}).items():
                if registered is parent:
                    _touch_structural(key)
                    break


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
    stage = {
        "analysis-synthesis": "insight", "analysis-synthesis-combined": "insight",
        "analysis-evidence-review": "validation", "metric-definition-answer": "final_output",
    }.get(name)
    active_stage = _OPERATIONS.get(_active_operation.get(), (None, None))[0]
    parent_key = _stage_parent(stage) if stage and active_stage != stage else None
    manager = structural_child(parent_key, as_type="generation", name=name, model=model, input=generation_input) if parent_key else observe_span(
        as_type="generation",
        name=name,
        model=model,
        input=generation_input,
    )
    with manager as observation:
        yield _GenerationRecorder(observation)


def _task_context() -> dict[str, Any]:
    from app.services.progress import _task_progress_context
    return _task_progress_context.get() or {}


def _stage_parent(stage: str, context: dict[str, Any] | None = None) -> str:
    context = _task_context() if context is None else context
    ensure_structural_span(stage, name=_STAGES[stage][0])
    task_id = str(context.get("task_id") or "")
    if not task_id:
        return stage
    key = stage + ":task:" + hash_identifier(task_id)
    index = context.get("task_index")
    ensure_structural_span(
        key, name=f"任务{index + 1}" if isinstance(index, int) else "子任务",
        parent_key=stage, input={
            "task_id": hash_identifier(task_id), "task_index": index,
            "task_count": context.get("task_count"),
            "question": text_metadata(context.get("task_question")),
        },
    )
    return key


def _safe_update(observation: Any, **kwargs: Any) -> None:
    try:
        observation.update(**kwargs)
    except Exception:
        logger.debug("Langfuse update failed", exc_info=True)


@contextmanager
def trace_operation(operation: str, attributes: Any = None) -> Iterator[Any]:
    """Attach actual timed calls to their logical stage, including parallel tasks."""
    spec = _OPERATIONS.get(operation)
    if not is_enabled() or spec is None:
        yield _NULL_OBSERVATION
        return
    parent_key = _stage_parent(spec[0])
    with structural_child(parent_key, name=spec[1], input=value_metadata(attributes or {})) as span:
        token = _active_operation.set(operation)
        try:
            yield span
        except BaseException as exc:
            status = "CANCELLED" if type(exc).__name__ == "CancelledError" else "FAILED"
            _safe_update(span, output={"status": status, "error_type": type(exc).__name__}, level="ERROR")
            raise
        else:
            _safe_update(span, output={"status": "COMPLETED"})
        finally:
            _active_operation.reset(token)


@contextmanager
def trace_http_call(method: str, path: str, payload: Any) -> Iterator[Any]:
    # No headers, base URL, bearer token or signed URL is passed to this hook.
    if not is_enabled() or not _active_operation.get():
        yield _NULL_OBSERVATION
        return
    with observe_span(as_type="tool", name=f"{method} {path.split('?')[0]}", input=value_metadata(payload)) as span:
        try:
            yield span
        except BaseException as exc:
            _safe_update(span, output={"status": "FAILED", "error_type": type(exc).__name__, "code": getattr(exc, "code", None)}, level="ERROR")
            raise


def record_http_result(span: Any, result: Any) -> None:
    if span is not _NULL_OBSERVATION:
        _safe_update(span, output=value_metadata(result))


class StageSpanTracker:
    """Export ordered progress off the event loop; group by request/stage/task."""

    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = None
        self._events: dict[str, list[dict[str, Any]]] = {}

    def enqueue(self, event: dict[str, Any]) -> None:
        if not is_enabled():
            return
        try:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="langfuse-progress")
            # Copy OTel context AND DAG task metadata; worker threads must never
            # inherit another request's trace or change the business context.
            self._executor.submit(copy_context().run, self.handle, dict(event))
        except Exception:
            logger.debug("Langfuse progress enqueue failed", exc_info=True)

    def handle(self, event: dict[str, Any]) -> None:
        if not is_enabled() or not isinstance(event, dict):
            return
        stage = _PROGRESS_STAGES.get(str(event.get("stage") or ""))
        if not stage:
            return
        try:
            key = _stage_parent(stage, event)
            status = str(event.get("status") or "").upper()[:30]
            item = {
                "stage": event.get("stage"), "status": status,
                "task_index": event.get("task_index"),
                "message": text_metadata(event.get("message")),
            }
            for target in dict.fromkeys((stage, key)):
                events = self._events.setdefault(target, [])
                if not events or events[-1] != item:
                    events.append(item)
                del events[:-40]
                span = get_structural_span(target)
                if span is not None:
                    if len(events) == 1 and target == stage:
                        _safe_update(span, input={"first_event": item})
                    failures = sum(e["status"] in {"FAILED", "ERROR"} for e in events)
                    _safe_update(span, output={"status": status, "events": list(events), "failed_events": failures},
                                 level="ERROR" if status in {"FAILED", "ERROR"} else "WARNING" if failures else "DEFAULT")
                _touch_structural(target)
        except Exception:
            logger.debug("Langfuse progress span failed: %s", stage, exc_info=True)

    def finish(self, response: Any = None, *, status: str = "COMPLETED") -> None:
        """Drain before closing parents. Called off-loop while root is current."""
        self.close_all()
        if not is_enabled():
            return
        if response is not None:
            planning = get_structural_span("task_planning")
            if planning is not None and response.task_plan is not None:
                _safe_update(planning, output={
                    "plan": value_metadata(response.task_plan.model_dump(mode="json")),
                    "events": self._events.get("task_planning", []),
                })
            span = ensure_structural_span("final_output", name="最终输出")
            _safe_update(span, input={"intent": response.intent.value}, output={
                "status": response.status, "answer": text_metadata(response.answer),
                "evidence_count": len(response.evidence), "file_count": len(response.files),
                "chart_count": len(response.chart_specs),
                "tasks": [{"task_id": hash_identifier(t.task_id), "status": t.status} for t in response.task_results],
            })
            _touch_structural("final_output")
        elif status != "COMPLETED":
            for key, events in self._events.items():
                if events and events[-1]["status"] == "RUNNING":
                    span = get_structural_span(key)
                    if span is not None:
                        _safe_update(span, output={"status": status, "events": events}, level="ERROR")
                        _touch_structural(key)

    def close_all(self) -> None:
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)
