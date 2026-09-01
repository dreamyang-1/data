"""Append-only, bounded session event log for request replay and diagnostics."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field
from redis.asyncio import Redis

from app.domain.models import StrictModel


class SessionEventType(StrEnum):
    USER_QUERY = "USER_QUERY"
    TURN_ADMISSION = "TURN_ADMISSION"
    CONTEXT_MERGE = "CONTEXT_MERGE"
    QUERY_RESOLUTION = "QUERY_RESOLUTION"
    QUERY_REWRITE = "QUERY_REWRITE"
    INTENT_RESULT = "INTENT_RESULT"
    SEMANTIC_CONTEXT = "SEMANTIC_CONTEXT"
    SEMANTIC_PLAN = "SEMANTIC_PLAN"
    CLARIFICATION = "CLARIFICATION"
    ANALYSIS_PLAN = "ANALYSIS_PLAN"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    PYTHON_ANALYSIS = "PYTHON_ANALYSIS"
    MEMORY_RECALL = "MEMORY_RECALL"
    MEMORY_WRITE = "MEMORY_WRITE"
    VALIDATION_RESULT = "VALIDATION_RESULT"
    FINAL_INSIGHT = "FINAL_INSIGHT"
    ERROR = "ERROR"
    COMPACTION = "COMPACTION"
    TRACE_SUMMARY = "TRACE_SUMMARY"


class SessionEvent(StrictModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=100)
    message_id: str | None = Field(default=None, max_length=128)
    event_type: SessionEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionEventStore(Protocol):
    async def append(self, event: SessionEvent) -> None: ...

    async def list_events(
        self,
        tenant_id: str,
        user_id: str,
        application_id: str,
        session_id: str,
        *,
        limit: int = 100,
        trace_id: str | None = None,
    ) -> list[SessionEvent]: ...


class EventPayloadSanitizer:
    _SENSITIVE = (
        "authorization", "token", "password", "secret", "api_key", "apikey",
        "cookie", "credential",
    )
    _BULK = ("rows", "raw_rows", "raw_result", "dataset_rows")

    @classmethod
    def sanitize(cls, payload: Mapping[str, Any], *, max_bytes: int = 32_768) -> dict[str, Any]:
        value = cls._value(dict(payload), 0)
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) <= max_bytes:
            return value
        return {
            "truncated": True,
            "original_bytes": len(encoded),
            "keys": list(value)[:50],
            "summary": "事件载荷超过安全上限，已仅保留结构摘要",
        }

    @classmethod
    def _value(cls, value: Any, depth: int) -> Any:
        if depth >= 5:
            return "<depth-limited>"
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for raw_key, child in list(value.items())[:50]:
                key = str(raw_key)[:100]
                folded = key.casefold()
                if any(marker in folded for marker in cls._SENSITIVE):
                    result[key] = "<redacted>"
                elif folded in cls._BULK and isinstance(child, list):
                    result[key] = {"item_count": len(child), "content_omitted": True}
                else:
                    result[key] = cls._value(child, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [cls._value(item, depth + 1) for item in list(value)[:50]]
        if isinstance(value, str):
            return value[:4000]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:1000]


class InMemorySessionEventStore:
    def __init__(self, *, max_events_per_session: int = 500) -> None:
        self.max_events_per_session = max_events_per_session
        self._events: dict[tuple[str, str, str, str], list[SessionEvent]] = {}
        self._lock = asyncio.Lock()

    async def append(self, event: SessionEvent) -> None:
        safe = event.model_copy(
            update={"payload": EventPayloadSanitizer.sanitize(event.payload)}, deep=True
        )
        key = (safe.tenant_id, safe.user_id, safe.application_id, safe.session_id)
        async with self._lock:
            values = self._events.setdefault(key, [])
            values.append(safe)
            del values[:-self.max_events_per_session]

    async def list_events(
        self,
        tenant_id: str,
        user_id: str,
        application_id: str,
        session_id: str,
        *,
        limit: int = 100,
        trace_id: str | None = None,
    ) -> list[SessionEvent]:
        key = (tenant_id, user_id, application_id, session_id)
        async with self._lock:
            values = list(self._events.get(key, []))
        if trace_id is not None:
            values = [item for item in values if item.trace_id == trace_id]
        return [item.model_copy(deep=True) for item in values[-max(1, min(limit, 500)):]]


class RedisSessionEventStore:
    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str,
        ttl_seconds: int,
        max_events_per_session: int = 500,
    ) -> None:
        self.redis = redis
        self.prefix = prefix.rstrip(":")
        self.ttl_seconds = ttl_seconds
        self.max_events_per_session = max_events_per_session

    def _key(self, *parts: str) -> str:
        digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
        return f"{self.prefix}:events:{digest}"

    async def append(self, event: SessionEvent) -> None:
        safe = event.model_copy(
            update={"payload": EventPayloadSanitizer.sanitize(event.payload)}, deep=True
        )
        key = self._key(
            safe.tenant_id, safe.user_id, safe.application_id, safe.session_id
        )
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.rpush(key, safe.model_dump_json())
        pipeline.ltrim(key, -self.max_events_per_session, -1)
        pipeline.expire(key, self.ttl_seconds)
        await pipeline.execute()

    async def list_events(
        self,
        tenant_id: str,
        user_id: str,
        application_id: str,
        session_id: str,
        *,
        limit: int = 100,
        trace_id: str | None = None,
    ) -> list[SessionEvent]:
        key = self._key(tenant_id, user_id, application_id, session_id)
        bounded_limit = max(1, min(limit, 500))
        raw = await self.redis.lrange(
            key, -(500 if trace_id is not None else bounded_limit), -1
        )
        values: list[SessionEvent] = []
        for item in raw:
            try:
                event = SessionEvent.model_validate_json(item)
            except (ValueError, TypeError):
                continue
            if trace_id is None or event.trace_id == trace_id:
                values.append(event)
        return values[-bounded_limit:]
