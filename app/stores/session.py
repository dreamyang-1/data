from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Protocol

from pydantic import ValidationError
from redis.asyncio import Redis

from app.domain.models import AgentResponse, CanonicalAnalysisRequest, PendingState

logger = logging.getLogger(__name__)


def _task_frame_fingerprint(request: CanonicalAnalysisRequest) -> str:
    """Identify the logical task without retaining duplicate frame snapshots."""
    payload = {
        "question": request.rewritten_question or request.original_question,
        "intent": request.primary_intent.value,
        "entity": request.entity,
        "metrics": [item.model_dump(mode="json") for item in request.metrics],
        "dimensions": request.dimensions,
        "filters": request.filters,
        "time_range": (
            request.time_range.model_dump(mode="json")
            if request.time_range is not None else None
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SessionConflictError(RuntimeError):
    pass


class MessageIdReuseConflictError(RuntimeError):
    """Raised when one message id is reused for a different effective request."""

    code = "MESSAGE_ID_REUSE_CONFLICT"

    def __init__(self, message_id: str, *, legacy_cache: bool = False) -> None:
        if legacy_cache:
            message = (
                f"message_id '{message_id}' belongs to a legacy cached request whose "
                "payload cannot be verified; retry with a new message_id"
            )
        else:
            message = (
                f"message_id '{message_id}' was already used with a different request "
                "payload; each new user message must use a new message_id"
            )
        super().__init__(message)


class SessionStore(Protocol):
    async def get_pending(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> PendingState | None: ...
    async def put_pending(self, state: PendingState, *, expected_version: int) -> None: ...
    async def clear_pending(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, *, expected_version: int | None = None) -> bool: ...
    async def get_response(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str, request_fingerprint: str) -> AgentResponse | None: ...
    async def put_response(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str, request_fingerprint: str, response: AgentResponse) -> AgentResponse: ...
    async def get_last_request(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> CanonicalAnalysisRequest | None: ...
    async def put_last_request(self, request: CanonicalAnalysisRequest) -> None: ...
    async def get_task_frame(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> CanonicalAnalysisRequest | None: ...
    async def put_task_frame(self, request: CanonicalAnalysisRequest) -> None: ...
    async def get_recent_task_frames(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, *, limit: int) -> list[CanonicalAnalysisRequest]: ...
    async def put_dataset_reference(self, reference: dict, *, recent_limit: int) -> None: ...
    async def get_recent_dataset_references(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, *, limit: int) -> list[dict]: ...
    async def list_expired_dataset_references(self, *, now_epoch: float, limit: int) -> list[dict]: ...
    async def delete_dataset_reference(self, dataset_id: str) -> None: ...
    async def get_dag_checkpoint(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str) -> dict | None: ...
    async def put_dag_checkpoint(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str, checkpoint: dict) -> None: ...
    async def delete_dag_checkpoint(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str) -> None: ...
    async def get_dag_pending(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> dict | None: ...
    async def put_dag_pending(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, state: dict, *, expected_version: int) -> None: ...
    async def clear_dag_pending(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, *, expected_version: int | None = None) -> bool: ...
    async def claim_message_execution(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str, request_fingerprint: str, owner_token: str, *, ttl_seconds: int) -> bool: ...
    async def release_message_execution(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str, owner_token: str) -> None: ...
    async def put_report_reference(self, reference: dict) -> None: ...
    async def list_expired_report_references(self, *, now_epoch: float, limit: int) -> list[dict]: ...
    async def delete_report_reference(self, report_id: str) -> None: ...


class InMemorySessionStore:
    """TTL-aware development/test implementation with the production contract."""

    def __init__(self, ttl_seconds: int = 7200, response_ttl_seconds: int = 600) -> None:
        self.ttl_seconds = ttl_seconds
        # An idempotency key must remain reserved for at least as long as the
        # conversation that may retry it. Otherwise an old message can be
        # interpreted as a new clarification after the response cache expires.
        self.response_ttl_seconds = max(response_ttl_seconds, ttl_seconds)
        self._states: dict[tuple[str, str, str, str], tuple[float, PendingState]] = {}
        self._responses: dict[tuple[str, str, str, str, str], tuple[float, AgentResponse]] = {}
        self._response_fingerprints: dict[
            tuple[str, str, str, str, str], tuple[float, str]
        ] = {}
        self._last_requests: dict[tuple[str, str, str, str], tuple[float, CanonicalAnalysisRequest]] = {}
        self._task_frames: dict[tuple[str, str, str, str], tuple[float, CanonicalAnalysisRequest]] = {}
        self._recent_task_frames: dict[
            tuple[str, str, str, str], list[tuple[float, CanonicalAnalysisRequest]]
        ] = {}
        self._dataset_references: dict[str, dict] = {}
        self._conversation_datasets: dict[tuple[str, str, str, str], list[str]] = {}
        self._dag_checkpoints: dict[tuple[str, str, str, str, str], tuple[float, dict]] = {}
        self._dag_pending: dict[tuple[str, str, str, str], tuple[float, dict]] = {}
        self._message_executions: dict[
            tuple[str, str, str, str, str], tuple[float, str, str]
        ] = {}
        self._report_references: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def get_pending(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> PendingState | None:
        key = (tenant_id, user_id, application_id, conversation_id)
        async with self._lock:
            item = self._states.get(key)
            if not item or item[0] <= time.monotonic():
                self._states.pop(key, None)
                return None
            return item[1].model_copy(deep=True)

    async def put_pending(self, state: PendingState, *, expected_version: int) -> None:
        key = (state.request.tenant_id, state.request.user_id, state.request.application_id, state.request.conversation_id)
        async with self._lock:
            item = self._states.get(key)
            current = item[1].state_version if item and item[0] > time.monotonic() else 0
            if current != expected_version:
                raise SessionConflictError(f"expected state version {expected_version}, got {current}")
            self._states[key] = (time.monotonic() + self.ttl_seconds, state.model_copy(deep=True))

    async def clear_pending(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, *, expected_version: int | None = None) -> bool:
        async with self._lock:
            key = (tenant_id, user_id, application_id, conversation_id)
            item = self._states.get(key)
            if item is None:
                return True
            if expected_version is not None and item[1].state_version != expected_version:
                return False
            self._states.pop(key, None)
            return True

    async def get_response(
        self,
        tenant_id: str,
        user_id: str,
        application_id: str,
        conversation_id: str,
        message_id: str,
        request_fingerprint: str,
    ) -> AgentResponse | None:
        key = (tenant_id, user_id, application_id, conversation_id, message_id)
        async with self._lock:
            now = time.monotonic()
            item = self._responses.get(key)
            fingerprint_item = self._response_fingerprints.get(key)
            if item and item[0] <= now:
                self._responses.pop(key, None)
                item = None
            if fingerprint_item and fingerprint_item[0] <= now:
                self._response_fingerprints.pop(key, None)
                fingerprint_item = None

            # A cached response without a fingerprint was produced by an older
            # release. Its payload cannot be proven equal, so returning it would
            # recreate the silent stale-answer bug this guard is meant to stop.
            if item and not fingerprint_item:
                raise MessageIdReuseConflictError(message_id, legacy_cache=True)
            if fingerprint_item and fingerprint_item[1] != request_fingerprint:
                raise MessageIdReuseConflictError(message_id)
            if not fingerprint_item:
                self._response_fingerprints[key] = (
                    now + self.response_ttl_seconds,
                    request_fingerprint,
                )
            return item[1].model_copy(deep=True) if item else None

    async def put_response(
        self,
        tenant_id: str,
        user_id: str,
        application_id: str,
        conversation_id: str,
        message_id: str,
        request_fingerprint: str,
        response: AgentResponse,
    ) -> AgentResponse:
        key = (tenant_id, user_id, application_id, conversation_id, message_id)
        async with self._lock:
            now = time.monotonic()
            fingerprint_item = self._response_fingerprints.get(key)
            if fingerprint_item and fingerprint_item[0] <= now:
                self._response_fingerprints.pop(key, None)
                fingerprint_item = None
            existing = self._responses.get(key)
            if existing and existing[0] <= now:
                self._responses.pop(key, None)
                existing = None
            if existing and not fingerprint_item:
                raise MessageIdReuseConflictError(message_id, legacy_cache=True)
            if fingerprint_item and fingerprint_item[1] != request_fingerprint:
                raise MessageIdReuseConflictError(message_id)

            expires_at = now + self.response_ttl_seconds
            self._response_fingerprints[key] = (expires_at, request_fingerprint)
            if existing:
                # Concurrent identical requests converge on the first response.
                return existing[1].model_copy(deep=True)
            stored = response.model_copy(deep=True)
            self._responses[key] = (
                expires_at,
                stored,
            )
            return stored.model_copy(deep=True)

    async def get_last_request(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> CanonicalAnalysisRequest | None:
        key = (tenant_id, user_id, application_id, conversation_id)
        async with self._lock:
            item = self._last_requests.get(key)
            if not item or item[0] <= time.monotonic():
                self._last_requests.pop(key, None)
                return None
            return item[1].model_copy(deep=True)

    async def put_last_request(self, request: CanonicalAnalysisRequest) -> None:
        async with self._lock:
            self._last_requests[(request.tenant_id, request.user_id, request.application_id, request.conversation_id)] = (
                time.monotonic() + self.ttl_seconds, request.model_copy(deep=True)
            )

    async def get_task_frame(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> CanonicalAnalysisRequest | None:
        key = (tenant_id, user_id, application_id, conversation_id)
        async with self._lock:
            item = self._task_frames.get(key)
            if not item or item[0] <= time.monotonic():
                self._task_frames.pop(key, None)
                return None
            return item[1].model_copy(deep=True)

    async def put_task_frame(self, request: CanonicalAnalysisRequest) -> None:
        # A task frame contains only the normalized request contract. Query SQL,
        # result rows, files and credentials never enter conversational memory.
        frame = request.model_copy(deep=True, update={"asl_template": None})
        async with self._lock:
            key = (request.tenant_id, request.user_id, request.application_id, request.conversation_id)
            self._task_frames[key] = (
                time.monotonic() + self.ttl_seconds, frame
            )
            frames = self._recent_task_frames.setdefault(key, [])
            fingerprint = _task_frame_fingerprint(frame)
            frames[:] = [
                item for item in frames
                if item[0] > time.monotonic()
                and _task_frame_fingerprint(item[1]) != fingerprint
            ]
            frames.insert(0, (time.monotonic() + self.ttl_seconds, frame))
            del frames[12:]

    async def get_recent_task_frames(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, *, limit: int,
    ) -> list[CanonicalAnalysisRequest]:
        key = (tenant_id, user_id, application_id, conversation_id)
        async with self._lock:
            now = time.monotonic()
            frames = [
                item for item in self._recent_task_frames.get(key, [])
                if item[0] > now
            ]
            self._recent_task_frames[key] = frames
            return [item[1].model_copy(deep=True) for item in frames[:max(0, limit)]]

    async def put_dataset_reference(self, reference: dict, *, recent_limit: int) -> None:
        scope = reference["scope"]
        key = (
            scope["tenant_id"], scope["user_id"], scope["application_id"],
            scope["conversation_id"],
        )
        async with self._lock:
            dataset_id = str(reference["dataset_id"])
            self._dataset_references[dataset_id] = json.loads(json.dumps(reference))
            ids = self._conversation_datasets.setdefault(key, [])
            if dataset_id in ids:
                ids.remove(dataset_id)
            ids.insert(0, dataset_id)
            del ids[recent_limit:]

    async def get_recent_dataset_references(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, *, limit: int,
    ) -> list[dict]:
        key = (tenant_id, user_id, application_id, conversation_id)
        now = datetime.now(timezone.utc)
        async with self._lock:
            result: list[dict] = []
            for dataset_id in self._conversation_datasets.get(key, [])[:limit]:
                reference = self._dataset_references.get(dataset_id)
                if reference and datetime.fromisoformat(reference["expires_at"]) > now:
                    result.append(json.loads(json.dumps(reference)))
            return result

    async def list_expired_dataset_references(
        self, *, now_epoch: float, limit: int
    ) -> list[dict]:
        async with self._lock:
            result = [
                json.loads(json.dumps(reference))
                for reference in self._dataset_references.values()
                if datetime.fromisoformat(reference["expires_at"]).timestamp() <= now_epoch
            ]
            return result[:limit]

    async def delete_dataset_reference(self, dataset_id: str) -> None:
        async with self._lock:
            self._dataset_references.pop(dataset_id, None)
            for ids in self._conversation_datasets.values():
                if dataset_id in ids:
                    ids.remove(dataset_id)

    async def get_dag_checkpoint(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str) -> dict | None:
        key = (tenant_id, user_id, application_id, conversation_id, message_id)
        async with self._lock:
            item = self._dag_checkpoints.get(key)
            if not item or item[0] <= time.monotonic():
                self._dag_checkpoints.pop(key, None)
                return None
            return json.loads(json.dumps(item[1]))

    async def put_dag_checkpoint(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str, checkpoint: dict) -> None:
        key = (tenant_id, user_id, application_id, conversation_id, message_id)
        async with self._lock:
            self._dag_checkpoints[key] = (
                time.monotonic() + self.ttl_seconds,
                json.loads(json.dumps(checkpoint)),
            )

    async def delete_dag_checkpoint(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str) -> None:
        async with self._lock:
            self._dag_checkpoints.pop(
                (tenant_id, user_id, application_id, conversation_id, message_id), None
            )

    async def get_dag_pending(
        self, tenant_id: str, user_id: str, application_id: str, conversation_id: str
    ) -> dict | None:
        key = (tenant_id, user_id, application_id, conversation_id)
        async with self._lock:
            item = self._dag_pending.get(key)
            if not item or item[0] <= time.monotonic():
                self._dag_pending.pop(key, None)
                return None
            return json.loads(json.dumps(item[1]))

    async def put_dag_pending(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, state: dict, *, expected_version: int,
    ) -> None:
        key = (tenant_id, user_id, application_id, conversation_id)
        async with self._lock:
            current = self._dag_pending.get(key)
            if current and current[0] <= time.monotonic():
                self._dag_pending.pop(key, None)
                current = None
            current_version = int(current[1].get("state_version", 0)) if current else 0
            if current_version != expected_version:
                raise SessionConflictError("DAG clarification state changed concurrently")
            state_version = state.get("state_version")
            if not isinstance(state_version, int) or state_version != expected_version + 1:
                raise ValueError("DAG state_version must advance by exactly one")
            self._dag_pending[key] = (
                time.monotonic() + self.ttl_seconds,
                json.loads(json.dumps(state)),
            )

    async def clear_dag_pending(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, *, expected_version: int | None = None,
    ) -> bool:
        key = (tenant_id, user_id, application_id, conversation_id)
        async with self._lock:
            current = self._dag_pending.get(key)
            if not current or current[0] <= time.monotonic():
                self._dag_pending.pop(key, None)
                return True
            if expected_version is not None and int(current[1].get("state_version", 0)) != expected_version:
                return False
            self._dag_pending.pop(key, None)
            return True

    async def claim_message_execution(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, message_id: str, request_fingerprint: str,
        owner_token: str, *, ttl_seconds: int,
    ) -> bool:
        key = (tenant_id, user_id, application_id, conversation_id, message_id)
        async with self._lock:
            now = time.monotonic()
            existing = self._message_executions.get(key)
            if existing and existing[0] > now:
                return False
            self._message_executions[key] = (
                now + max(1, ttl_seconds), request_fingerprint, owner_token
            )
            return True

    async def release_message_execution(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, message_id: str, owner_token: str,
    ) -> None:
        key = (tenant_id, user_id, application_id, conversation_id, message_id)
        async with self._lock:
            existing = self._message_executions.get(key)
            if existing and existing[2] == owner_token:
                self._message_executions.pop(key, None)

    async def put_report_reference(self, reference: dict) -> None:
        async with self._lock:
            self._report_references[str(reference["report_id"])] = json.loads(
                json.dumps(reference)
            )

    async def list_expired_report_references(
        self, *, now_epoch: float, limit: int
    ) -> list[dict]:
        async with self._lock:
            return [
                json.loads(json.dumps(item))
                for item in self._report_references.values()
                if datetime.fromisoformat(item["expires_at"]).timestamp() <= now_epoch
            ][:limit]

    async def delete_report_reference(self, report_id: str) -> None:
        async with self._lock:
            self._report_references.pop(report_id, None)


class RedisSessionStore:
    """Redis-backed short-term memory shared by all service instances."""

    _CAS_SCRIPT = """
local current = redis.call('GET', KEYS[1])
local version = 0
if current then
  local decoded = cjson.decode(current)
  version = tonumber(decoded['state_version'])
end
if version ~= tonumber(ARGV[1]) then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""

    _CLEAR_PENDING_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then return 1 end
if ARGV[1] ~= '' then
  local decoded = cjson.decode(current)
  if tonumber(decoded['state_version']) ~= tonumber(ARGV[1]) then return 0 end
end
redis.call('DEL', KEYS[1])
return 1
"""

    _GET_RESPONSE_SCRIPT = """
local fingerprint = redis.call('GET', KEYS[2])
local response = redis.call('GET', KEYS[1])
if not fingerprint then
  if response then return {'LEGACY'} end
  redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2], 'NX')
  fingerprint = redis.call('GET', KEYS[2])
end
if fingerprint ~= ARGV[1] then return {'CONFLICT'} end
if response then return {'HIT', response} end
return {'MISS'}
"""

    _PUT_RESPONSE_SCRIPT = """
local fingerprint = redis.call('GET', KEYS[2])
local response = redis.call('GET', KEYS[1])
if response and not fingerprint then return {'LEGACY'} end
if fingerprint and fingerprint ~= ARGV[1] then return {'CONFLICT'} end
if response then
  redis.call('EXPIRE', KEYS[1], ARGV[3])
  redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[3])
  return {'HIT', response}
end
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3], 'NX')
response = redis.call('GET', KEYS[1])
return {'STORED', response}
"""

    _RELEASE_EXECUTION_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

    def __init__(self, redis: Redis, *, ttl_seconds: int, response_ttl_seconds: int = 600, prefix: str) -> None:
        self.redis, self.ttl_seconds = redis, ttl_seconds
        self.response_ttl_seconds = max(response_ttl_seconds, ttl_seconds)
        self.prefix = prefix.rstrip(":")

    @classmethod
    def from_url(cls, url: str, *, ttl_seconds: int, response_ttl_seconds: int = 600, prefix: str) -> "RedisSessionStore":
        return cls(Redis.from_url(url, encoding="utf-8", decode_responses=True), ttl_seconds=ttl_seconds, response_ttl_seconds=response_ttl_seconds, prefix=prefix)

    def _key(self, kind: str, *parts: str) -> str:
        digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
        return f"{self.prefix}:{kind}:{digest}"

    async def get_pending(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> PendingState | None:
        key = self._key("pending", tenant_id, user_id, application_id, conversation_id)
        raw = await self.redis.get(key)
        if not raw:
            return None
        try:
            return PendingState.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError):
            logger.error("discarding invalid pending session state: key=%s", key)
            await self.redis.delete(key)
            return None

    async def put_pending(self, state: PendingState, *, expected_version: int) -> None:
        key = self._key("pending", state.request.tenant_id, state.request.user_id, state.request.application_id, state.request.conversation_id)
        result = await self.redis.eval(self._CAS_SCRIPT, 1, key, expected_version, state.model_dump_json(), self.ttl_seconds)
        if int(result) != 1:
            raise SessionConflictError("pending conversation state changed concurrently")

    async def clear_pending(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, *, expected_version: int | None = None) -> bool:
        key = self._key("pending", tenant_id, user_id, application_id, conversation_id)
        result = await self.redis.eval(
            self._CLEAR_PENDING_SCRIPT,
            1,
            key,
            "" if expected_version is None else expected_version,
        )
        return int(result) == 1

    async def get_response(
        self,
        tenant_id: str,
        user_id: str,
        application_id: str,
        conversation_id: str,
        message_id: str,
        request_fingerprint: str,
    ) -> AgentResponse | None:
        response_key = self._key(
            "response", tenant_id, user_id, application_id, conversation_id, message_id
        )
        fingerprint_key = self._key(
            "response-fingerprint",
            tenant_id,
            user_id,
            application_id,
            conversation_id,
            message_id,
        )
        result = await self.redis.eval(
            self._GET_RESPONSE_SCRIPT,
            2,
            response_key,
            fingerprint_key,
            request_fingerprint,
            self.response_ttl_seconds,
        )
        state = result[0]
        if state == "LEGACY":
            raise MessageIdReuseConflictError(message_id, legacy_cache=True)
        if state == "CONFLICT":
            raise MessageIdReuseConflictError(message_id)
        if state != "HIT":
            return None
        try:
            return AgentResponse.model_validate_json(result[1])
        except (ValidationError, ValueError, TypeError) as exc:
            # Keep the fingerprint reservation: re-executing an already handled
            # message could repeat an expensive query or other side effect.
            raise MessageIdReuseConflictError(message_id, legacy_cache=True) from exc

    async def put_response(
        self,
        tenant_id: str,
        user_id: str,
        application_id: str,
        conversation_id: str,
        message_id: str,
        request_fingerprint: str,
        response: AgentResponse,
    ) -> AgentResponse:
        response_key = self._key(
            "response", tenant_id, user_id, application_id, conversation_id, message_id
        )
        fingerprint_key = self._key(
            "response-fingerprint",
            tenant_id,
            user_id,
            application_id,
            conversation_id,
            message_id,
        )
        result = await self.redis.eval(
            self._PUT_RESPONSE_SCRIPT,
            2,
            response_key,
            fingerprint_key,
            request_fingerprint,
            response.model_dump_json(),
            self.response_ttl_seconds,
        )
        state = result[0]
        if state == "LEGACY":
            raise MessageIdReuseConflictError(message_id, legacy_cache=True)
        if state == "CONFLICT":
            raise MessageIdReuseConflictError(message_id)
        # For concurrent identical requests, return the response that won the
        # atomic cache write so all callers observe exactly the same result.
        return AgentResponse.model_validate_json(result[1])

    async def get_last_request(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> CanonicalAnalysisRequest | None:
        key = self._key("last-request", tenant_id, user_id, application_id, conversation_id)
        raw = await self.redis.get(key)
        if not raw:
            return None
        try:
            return CanonicalAnalysisRequest.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError):
            logger.error("discarding invalid last-request session state: key=%s", key)
            await self.redis.delete(key)
            return None

    async def put_last_request(self, request: CanonicalAnalysisRequest) -> None:
        await self.redis.set(
            self._key("last-request", request.tenant_id, request.user_id, request.application_id, request.conversation_id),
            request.model_dump_json(), ex=self.ttl_seconds,
        )

    async def get_task_frame(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str) -> CanonicalAnalysisRequest | None:
        key = self._key("task-frame", tenant_id, user_id, application_id, conversation_id)
        raw = await self.redis.get(key)
        if not raw:
            return None
        try:
            return CanonicalAnalysisRequest.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError):
            logger.error("discarding invalid task-frame session state: key=%s", key)
            await self.redis.delete(key)
            return None

    async def put_task_frame(self, request: CanonicalAnalysisRequest) -> None:
        frame = request.model_copy(deep=True, update={"asl_template": None})
        current_key = self._key("task-frame", request.tenant_id, request.user_id, request.application_id, request.conversation_id)
        recent_key = self._key("task-frames", request.tenant_id, request.user_id, request.application_id, request.conversation_id)
        encoded = frame.model_dump_json()
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(current_key, encoded, ex=self.ttl_seconds)
            pipe.lrem(recent_key, 0, encoded)
            pipe.lpush(recent_key, encoded)
            pipe.ltrim(recent_key, 0, 11)
            pipe.expire(recent_key, self.ttl_seconds)
            await pipe.execute()

    async def get_recent_task_frames(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, *, limit: int,
    ) -> list[CanonicalAnalysisRequest]:
        if limit <= 0:
            return []
        key = self._key("task-frames", tenant_id, user_id, application_id, conversation_id)
        values = await self.redis.lrange(key, 0, limit - 1)
        result: list[CanonicalAnalysisRequest] = []
        for raw in values:
            try:
                result.append(CanonicalAnalysisRequest.model_validate_json(raw))
            except (ValidationError, ValueError, TypeError):
                logger.warning("discarding invalid recalled task frame: key=%s", key)
        return result

    @property
    def _dataset_expiry_key(self) -> str:
        return f"{self.prefix}:dataset-expiry"

    @property
    def _report_expiry_key(self) -> str:
        return f"{self.prefix}:report-expiry"

    async def put_dataset_reference(self, reference: dict, *, recent_limit: int) -> None:
        dataset_id = str(reference["dataset_id"])
        scope = reference["scope"]
        expires_at = datetime.fromisoformat(reference["expires_at"])
        retention_seconds = max(
            3600,
            int(expires_at.timestamp() - datetime.now(timezone.utc).timestamp()) + 86400,
        )
        reference_key = self._key("dataset-ref", dataset_id)
        recent_key = self._key(
            "dataset-recent", scope["tenant_id"], scope["user_id"],
            scope["application_id"], scope["conversation_id"],
        )
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(reference_key, json.dumps(reference, ensure_ascii=False), ex=retention_seconds)
            pipe.lrem(recent_key, 0, dataset_id)
            pipe.lpush(recent_key, dataset_id)
            pipe.ltrim(recent_key, 0, recent_limit - 1)
            pipe.expire(recent_key, self.ttl_seconds)
            pipe.zadd(self._dataset_expiry_key, {dataset_id: expires_at.timestamp()})
            await pipe.execute()

    async def get_recent_dataset_references(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, *, limit: int,
    ) -> list[dict]:
        recent_key = self._key(
            "dataset-recent", tenant_id, user_id, application_id, conversation_id
        )
        dataset_ids = await self.redis.lrange(recent_key, 0, limit - 1)
        if not dataset_ids:
            return []
        values = await self.redis.mget(
            [self._key("dataset-ref", dataset_id) for dataset_id in dataset_ids]
        )
        now = datetime.now(timezone.utc)
        result: list[dict] = []
        stale_ids: list[str] = []
        for dataset_id, raw in zip(dataset_ids, values):
            if not raw:
                stale_ids.append(dataset_id)
                continue
            try:
                reference = json.loads(raw)
                if datetime.fromisoformat(reference["expires_at"]) <= now:
                    continue
                result.append(reference)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                stale_ids.append(dataset_id)
        if stale_ids:
            async with self.redis.pipeline(transaction=True) as pipe:
                for dataset_id in stale_ids:
                    pipe.lrem(recent_key, 0, dataset_id)
                await pipe.execute()
        return result

    async def list_expired_dataset_references(
        self, *, now_epoch: float, limit: int
    ) -> list[dict]:
        dataset_ids = await self.redis.zrangebyscore(
            self._dataset_expiry_key, "-inf", now_epoch, start=0, num=limit
        )
        if not dataset_ids:
            return []
        values = await self.redis.mget(
            [self._key("dataset-ref", dataset_id) for dataset_id in dataset_ids]
        )
        missing = [dataset_id for dataset_id, raw in zip(dataset_ids, values) if not raw]
        if missing:
            await self.redis.zrem(self._dataset_expiry_key, *missing)
        result: list[dict] = []
        invalid_ids: list[str] = []
        for dataset_id, raw in zip(dataset_ids, values):
            if not raw:
                continue
            try:
                result.append(json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.error("discarding invalid dataset reference")
                invalid_ids.append(dataset_id)
        if invalid_ids:
            await self.redis.zrem(self._dataset_expiry_key, *invalid_ids)
        return result

    async def delete_dataset_reference(self, dataset_id: str) -> None:
        reference_key = self._key("dataset-ref", dataset_id)
        raw = await self.redis.get(reference_key)
        recent_key: str | None = None
        if raw:
            try:
                reference = json.loads(raw)
                scope = reference["scope"]
                recent_key = self._key(
                    "dataset-recent", scope["tenant_id"], scope["user_id"],
                    scope["application_id"], scope["conversation_id"],
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                logger.error("invalid dataset reference during cleanup: %s", dataset_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(reference_key)
            pipe.zrem(self._dataset_expiry_key, dataset_id)
            if recent_key is not None:
                pipe.lrem(recent_key, 0, dataset_id)
            await pipe.execute()

    async def get_dag_checkpoint(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str) -> dict | None:
        key = self._key("dag-checkpoint", tenant_id, user_id, application_id, conversation_id, message_id)
        raw = await self.redis.get(key)
        if not raw:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.error("discarding invalid DAG checkpoint: key=%s", key)
            await self.redis.delete(key)
            return None

    async def put_dag_checkpoint(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str, checkpoint: dict) -> None:
        key = self._key("dag-checkpoint", tenant_id, user_id, application_id, conversation_id, message_id)
        await self.redis.set(
            key, json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":")),
            ex=self.ttl_seconds,
        )

    async def delete_dag_checkpoint(self, tenant_id: str, user_id: str, application_id: str, conversation_id: str, message_id: str) -> None:
        await self.redis.delete(
            self._key("dag-checkpoint", tenant_id, user_id, application_id, conversation_id, message_id)
        )

    async def get_dag_pending(
        self, tenant_id: str, user_id: str, application_id: str, conversation_id: str
    ) -> dict | None:
        key = self._key("dag-pending", tenant_id, user_id, application_id, conversation_id)
        raw = await self.redis.get(key)
        if not raw:
            return None
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("state_version"), int):
                raise ValueError("invalid DAG pending envelope")
            return value
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.error("discarding invalid DAG pending state: key=%s", key)
            await self.redis.delete(key)
            return None

    async def put_dag_pending(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, state: dict, *, expected_version: int,
    ) -> None:
        if state.get("state_version") != expected_version + 1:
            raise ValueError("DAG state_version must advance by exactly one")
        key = self._key("dag-pending", tenant_id, user_id, application_id, conversation_id)
        result = await self.redis.eval(
            self._CAS_SCRIPT, 1, key, expected_version,
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            self.ttl_seconds,
        )
        if int(result) != 1:
            raise SessionConflictError("DAG clarification state changed concurrently")

    async def clear_dag_pending(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, *, expected_version: int | None = None,
    ) -> bool:
        key = self._key("dag-pending", tenant_id, user_id, application_id, conversation_id)
        result = await self.redis.eval(
            self._CLEAR_PENDING_SCRIPT,
            1,
            key,
            "" if expected_version is None else expected_version,
        )
        return int(result) == 1

    async def claim_message_execution(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, message_id: str, request_fingerprint: str,
        owner_token: str, *, ttl_seconds: int,
    ) -> bool:
        key = self._key(
            "message-execution", tenant_id, user_id, application_id,
            conversation_id, message_id,
        )
        # Fingerprint reservation/conflict validation happens atomically in
        # get_response before this call. The owner token is used only for a
        # compare-and-delete release so one worker cannot unlock another.
        result = await self.redis.set(
            key, owner_token, ex=max(1, ttl_seconds), nx=True
        )
        return bool(result)

    async def release_message_execution(
        self, tenant_id: str, user_id: str, application_id: str,
        conversation_id: str, message_id: str, owner_token: str,
    ) -> None:
        key = self._key(
            "message-execution", tenant_id, user_id, application_id,
            conversation_id, message_id,
        )
        await self.redis.eval(self._RELEASE_EXECUTION_SCRIPT, 1, key, owner_token)

    async def put_report_reference(self, reference: dict) -> None:
        report_id = str(reference["report_id"])
        expires_at = datetime.fromisoformat(reference["expires_at"])
        retention_seconds = max(
            3600,
            int(expires_at.timestamp() - datetime.now(timezone.utc).timestamp()) + 86400,
        )
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(
                self._key("report-ref", report_id),
                json.dumps(reference, ensure_ascii=False),
                ex=retention_seconds,
            )
            pipe.zadd(self._report_expiry_key, {report_id: expires_at.timestamp()})
            await pipe.execute()

    async def list_expired_report_references(
        self, *, now_epoch: float, limit: int
    ) -> list[dict]:
        report_ids = await self.redis.zrangebyscore(
            self._report_expiry_key, "-inf", now_epoch, start=0, num=limit
        )
        if not report_ids:
            return []
        values = await self.redis.mget(
            [self._key("report-ref", report_id) for report_id in report_ids]
        )
        result: list[dict] = []
        stale: list[str] = []
        for report_id, raw in zip(report_ids, values):
            if not raw:
                stale.append(report_id)
                continue
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    result.append(value)
                else:
                    stale.append(report_id)
            except (TypeError, ValueError, json.JSONDecodeError):
                stale.append(report_id)
        if stale:
            await self.redis.zrem(self._report_expiry_key, *stale)
        return result

    async def delete_report_reference(self, report_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._key("report-ref", report_id))
            pipe.zrem(self._report_expiry_key, report_id)
            await pipe.execute()
