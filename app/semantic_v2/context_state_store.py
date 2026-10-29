"""Stable Redis persistence for the live V2 context bridge.

The key identifies a conversation and its requested authorization scope.  A
catalog generation is recorded inside each turn, never in the key.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from app.domain.models import AgentResponse, ChatRequest, TrustedIdentity
from .authorized_contract import ScopedArtifact, contract_digest
from .pipeline import AuthorizedLogicalPlan
from .state_machine import ConversationState, StateTransitionError


SCHEMA_VERSION = "v2-context-live-state-v1"


@dataclass(frozen=True)
class ContextStateSnapshot:
    key: str
    revision: int
    state_version: int
    envelope: dict[str, Any]
    state: ScopedArtifact | None
    plans: tuple[ScopedArtifact, ...]
    pending: ScopedArtifact | None

    def message(self, message_id: str) -> dict[str, Any] | None:
        value = self.envelope.get("messages", {}).get(message_id)
        return deepcopy(value) if isinstance(value, dict) else None


class RedisContextStateStore:
    """CAS-protected V2 context state with catalog-independent identity."""

    _CAS = """
local current = redis.call('GET', KEYS[1])
local revision = 0
local ttl = tonumber(ARGV[3])
if current then
  local decoded = cjson.decode(current)
  revision = tonumber(decoded['revision'])
  ttl = redis.call('TTL', KEYS[1])
  if ttl <= 0 then return 0 end
end
if revision ~= tonumber(ARGV[1]) then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ttl)
return 1
"""

    _RESERVE = """
local current = redis.call('GET', KEYS[1])
local revision = 0
local ttl = tonumber(ARGV[3])
if current then
  local decoded = cjson.decode(current)
  revision = tonumber(decoded['revision'])
  ttl = redis.call('TTL', KEYS[1])
  if ttl <= 0 then return 0 end
end
if revision ~= tonumber(ARGV[1]) then return 0 end
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ttl)
redis.call('SET', KEYS[2], ARGV[4], 'EX', ARGV[5])
return 1
"""

    def __init__(
        self,
        redis,
        *,
        prefix: str,
        ttl_seconds: int,
        idempotency_ttl_seconds: int,
        max_messages: int = 200,
        max_envelope_bytes: int = 4 * 1024 * 1024,
    ):
        normalized = prefix.rstrip(":")
        if not normalized or ":v2-context-live:" not in normalized:
            raise ValueError("V2_CONTEXT_STATE_NAMESPACE_REQUIRED")
        if not 300 <= ttl_seconds <= 604800:
            raise ValueError("V2_CONTEXT_STATE_TTL_INVALID")
        if idempotency_ttl_seconds < ttl_seconds:
            raise ValueError("V2_CONTEXT_IDEMPOTENCY_TTL_TOO_SHORT")
        self.redis = redis
        self.prefix = normalized
        self.ttl_seconds = ttl_seconds
        self.idempotency_ttl_seconds = idempotency_ttl_seconds
        self.max_messages = max_messages
        self.max_envelope_bytes = max_envelope_bytes

    @staticmethod
    def identity(chat: ChatRequest, trusted: TrustedIdentity) -> dict[str, Any]:
        return {
            "tenant_id": trusted.tenant_id,
            "user_id": trusted.user_id,
            "application_id": chat.application_id,
            "conversation_id": chat.conversation_id,
            "semantic_model_id": chat.semantic_model_id,
            "requested_authorization_scope": (
                chat.authorized_semantic_scope.model_dump(mode="json")
            ),
        }

    def key(self, chat: ChatRequest, trusted: TrustedIdentity) -> str:
        material = {
            "schema_namespace": SCHEMA_VERSION,
            **self.identity(chat, trusted),
        }
        return f"{self.prefix}:conversation:{contract_digest(material)}"

    def _guard_key(self, key: str, message_id: str) -> str:
        return f"{self.prefix}:message:{contract_digest([key, message_id])}"

    @staticmethod
    def request_fingerprint(
        chat: ChatRequest, trusted: TrustedIdentity
    ) -> str:
        return contract_digest(
            {
                "chat": chat.model_dump(
                    mode="json", exclude={"history", "department"}
                ),
                "tenant_id": trusted.tenant_id,
                "user_id": trusted.user_id,
            }
        )

    def _empty(self, identity: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": 0,
            "state_version": 0,
            "state_identity": deepcopy(identity),
            "state": None,
            "plans": {},
            "pending": None,
            "messages": {},
        }

    def _snapshot(
        self, key: str, value: dict[str, Any], identity: dict[str, Any]
    ) -> ContextStateSnapshot:
        if (
            value.get("schema_version") != SCHEMA_VERSION
            or value.get("state_identity") != identity
            or type(value.get("revision")) is not int
            or value["revision"] < 0
            or type(value.get("state_version")) is not int
            or value["state_version"] < 0
            or not isinstance(value.get("plans"), dict)
            or not isinstance(value.get("messages"), dict)
        ):
            raise ValueError("V2_CONTEXT_STATE_INVALID")
        state = None
        if value.get("state") is not None:
            state = ScopedArtifact.model_validate(value["state"])
            if state.kind != "CONVERSATION":
                raise ValueError("V2_CONTEXT_STATE_KIND_INVALID")
            restored = ConversationState.model_validate(state.payload)
            if restored.state_version != value["state_version"]:
                raise ValueError("V2_CONTEXT_STATE_VERSION_INVALID")
        plans = []
        for task_id, raw in value["plans"].items():
            artifact = ScopedArtifact.model_validate(raw)
            plan = AuthorizedLogicalPlan.model_validate(artifact.payload)
            if artifact.kind != "LAST_REQUEST" or plan.task_id != task_id:
                raise ValueError("V2_CONTEXT_PLAN_STATE_INVALID")
            plans.append(artifact)
        pending = (
            ScopedArtifact.model_validate(value["pending"])
            if value.get("pending") is not None
            else None
        )
        if pending is not None and pending.kind != "PENDING":
            raise ValueError("V2_CONTEXT_PENDING_STATE_INVALID")
        for record in value["messages"].values():
            if (
                not isinstance(record, dict)
                or record.get("status") not in {"RUNNING", "SUCCEEDED", "FAILED"}
                or not isinstance(record.get("request_fingerprint"), str)
            ):
                raise ValueError("V2_CONTEXT_MESSAGE_STATE_INVALID")
            if record.get("response") is not None:
                AgentResponse.model_validate(record["response"])
        return ContextStateSnapshot(
            key=key,
            revision=value["revision"],
            state_version=value["state_version"],
            envelope=deepcopy(value),
            state=state,
            plans=tuple(plans),
            pending=pending,
        )

    async def load(
        self, chat: ChatRequest, trusted: TrustedIdentity
    ) -> ContextStateSnapshot:
        identity = self.identity(chat, trusted)
        key = self.key(chat, trusted)
        raw = await self.redis.get(key)
        value = self._empty(identity) if raw is None else json.loads(raw)
        return self._snapshot(key, value, identity)

    async def idempotency_record(
        self, snapshot: ContextStateSnapshot, message_id: str
    ) -> dict[str, Any] | None:
        raw = await self.redis.get(self._guard_key(snapshot.key, message_id))
        if raw is None:
            return None
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("request_fingerprint"), str)
        ):
            raise ValueError("V2_CONTEXT_MESSAGE_GUARD_INVALID")
        return value

    def _encoded(self, value: dict[str, Any]) -> str:
        if len(value.get("messages", {})) > self.max_messages:
            raise ValueError("V2_CONTEXT_MESSAGE_LIMIT_REACHED")
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > self.max_envelope_bytes:
            raise ValueError("V2_CONTEXT_STATE_SIZE_LIMIT_REACHED")
        return encoded

    async def reserve(
        self,
        snapshot: ContextStateSnapshot,
        *,
        chat: ChatRequest,
        trusted: TrustedIdentity,
        request_fingerprint: str,
        next_state: ScopedArtifact | None,
        plan_state: ScopedArtifact | None,
        pending_state: ScopedArtifact | None,
        bridge_route: str,
        catalog_provenance: dict[str, Any] | None,
    ) -> ContextStateSnapshot:
        if snapshot.message(chat.message_id) is not None:
            raise ValueError("V2_CONTEXT_MESSAGE_ALREADY_RESERVED")
        value = deepcopy(snapshot.envelope)
        if next_state is not None:
            state = ConversationState.model_validate(next_state.payload)
            if state.state_version != snapshot.state_version + 1:
                raise ValueError("V2_CONTEXT_PLANNED_STATE_VERSION_MISMATCH")
            value["state"] = next_state.model_dump(mode="json")
            value["state_version"] = state.state_version
            # A language-only context continuation creates a new TaskVersion
            # without a V2 logical plan.  The previous version's plan must not
            # remain eligible for a later turn: RawTurnPlanner correctly
            # rejects a plan whose version/identity differs from the active
            # task, which would otherwise block an unrelated complete NEW_TASK.
            for task_id, raw_plan in tuple(value["plans"].items()):
                plan = AuthorizedLogicalPlan.model_validate(raw_plan["payload"])
                task = state.tasks.get(task_id)
                active = (
                    next(
                        (
                            item for item in task.versions
                            if item.version == task.active_version
                        ),
                        None,
                    )
                    if task is not None
                    else None
                )
                if (
                    active is None
                    or plan.task_version != active.version
                    or active.plan_id != plan.plan_id
                ):
                    del value["plans"][task_id]
        elif plan_state is not None or pending_state is not None:
            raise ValueError("V2_CONTEXT_ARTIFACTS_REQUIRE_STATE")
        if plan_state is not None:
            plan = AuthorizedLogicalPlan.model_validate(plan_state.payload)
            value["plans"][plan.task_id] = plan_state.model_dump(mode="json")
        if next_state is not None:
            value["pending"] = (
                pending_state.model_dump(mode="json")
                if pending_state is not None
                else None
            )
        value["revision"] = snapshot.revision + 1
        value["messages"][chat.message_id] = {
            "request_fingerprint": request_fingerprint,
            "status": "RUNNING",
            "bridge_route": bridge_route,
            "catalog_provenance": deepcopy(catalog_provenance),
            "response": None,
        }
        encoded = self._encoded(value)
        guard = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "request_fingerprint": request_fingerprint,
                "state_key": snapshot.key,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = await self.redis.eval(
            self._RESERVE,
            2,
            snapshot.key,
            self._guard_key(snapshot.key, chat.message_id),
            snapshot.revision,
            encoded,
            self.ttl_seconds,
            guard,
            self.idempotency_ttl_seconds,
        )
        if int(result) == -1:
            raise ValueError("V2_CONTEXT_MESSAGE_ALREADY_RESERVED")
        if int(result) != 1:
            raise StateTransitionError("V2 context state changed concurrently")
        return self._snapshot(
            snapshot.key, value, self.identity(chat, trusted)
        )

    async def complete(
        self,
        snapshot: ContextStateSnapshot,
        *,
        chat: ChatRequest,
        trusted: TrustedIdentity,
        request_fingerprint: str,
        response: AgentResponse,
        v1_execution_called: bool,
        final_state: ScopedArtifact | None = None,
    ) -> ContextStateSnapshot:
        record = snapshot.message(chat.message_id)
        if (
            record is None
            or record.get("status") != "RUNNING"
            or record.get("request_fingerprint") != request_fingerprint
        ):
            raise ValueError("V2_CONTEXT_MESSAGE_RESERVATION_MISMATCH")
        value = deepcopy(snapshot.envelope)
        if final_state is not None:
            if final_state.kind != "CONVERSATION":
                raise ValueError("V2_CONTEXT_FINAL_STATE_KIND_INVALID")
            state = ConversationState.model_validate(final_state.payload)
            if state.state_version != snapshot.state_version:
                raise ValueError("V2_CONTEXT_FINAL_STATE_VERSION_MISMATCH")
            current = snapshot.state
            if current is None or final_state.context != current.context:
                raise ValueError("V2_CONTEXT_FINAL_STATE_SCOPE_MISMATCH")
            value["state"] = final_state.model_dump(mode="json")
            value["state_version"] = state.state_version
        value["revision"] = snapshot.revision + 1
        value["messages"][chat.message_id] = {
            **record,
            "status": "SUCCEEDED",
            "execution_stage": (
                "V1_EXECUTION_RESPONSE_SAVED"
                if v1_execution_called
                else "V2_CONTEXT_RESPONSE_SAVED"
            ),
            "v1_execution_called": v1_execution_called,
            "response": response.model_dump(mode="json"),
        }
        encoded = self._encoded(value)
        result = await self.redis.eval(
            self._CAS,
            1,
            snapshot.key,
            snapshot.revision,
            encoded,
            self.ttl_seconds,
        )
        if int(result) != 1:
            raise StateTransitionError("V2 context state changed concurrently")
        return self._snapshot(
            snapshot.key, value, self.identity(chat, trusted)
        )

    async def aclose(self) -> None:
        await self.redis.aclose()
