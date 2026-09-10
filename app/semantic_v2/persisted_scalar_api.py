"""Opt-in persisted scalar V2 adapter for isolated API validation.

The production application does not construct this handler.  A caller must
inject an isolated Redis namespace, a pinned semantic planner and a bounded
read-only transport.  The public ChatRequest/AgentResponse and SSE envelopes
remain unchanged.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
import json
import math
import re
from typing import Awaitable, Callable, Literal
from uuid import uuid4

from app.domain.models import (
    AgentResponse,
    ChatRequest,
    EvidenceItem,
    PrimaryIntent,
    ReliabilityReport,
    TrustedIdentity,
)
from app.stores import MessageIdReuseConflictError

from . import models as m
from .authorized_contract import (
    AuthorizedScopeContext,
    ScopedArtifact,
    contract_digest,
    scoped_artifact_material,
)
from .isolated_execution import (
    IsolatedExecutionReceipt,
    PreparedExecution,
    TypedExecutionRequest,
    _state_with_attempt,
    _validate_response,
    preparation_digest,
    proof,
    require,
)
from .state_machine import ConversationState, StateTransitionError


EXACT_RESULT_ENCODING = "typed-scalar-result-v1"
PERSISTED_SESSION_SCHEMA = "isolated-scalar-session-v1"
DEPLOYMENT_SESSION_SCHEMA = "limited-scalar-session-v1"
ISOLATED_PREFIX_PATTERN = re.compile(
    r"^[A-Za-z0-9:_-]+:isolated:round5-11:[A-Za-z0-9][A-Za-z0-9_-]{7,127}$"
)
DEPLOYMENT_PREFIX_PATTERN = re.compile(
    r"^[A-Za-z0-9:_-]+:v2-limited-scalar:[A-Za-z0-9][A-Za-z0-9_-]{2,63}$"
)


def _encode_value(value):
    if value is None:
        return {"type": "NULL", "value": None}
    if type(value) is int:
        return {"type": "INTEGER", "value": str(value)}
    if isinstance(value, Decimal):
        require(value.is_finite(), "EXACT_RESULT_NON_FINITE_DECIMAL")
        return {"type": "DECIMAL", "value": str(value)}
    if type(value) is float:
        require(math.isfinite(value), "EXACT_RESULT_NON_FINITE_FLOAT")
        return {"type": "APPROXIMATE_FLOAT", "value": repr(value)}
    if isinstance(value, datetime):
        return {"type": "DATETIME", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "DATE", "value": value.isoformat()}
    if isinstance(value, time):
        return {"type": "TIME", "value": value.isoformat()}
    if type(value) is str:
        return {"type": "TEXT", "value": value}
    raise ValueError("EXACT_RESULT_VALUE_TYPE_UNSUPPORTED")


def _decode_value(value):
    require(isinstance(value, dict) and set(value) == {"type", "value"},
            "EXACT_RESULT_ENCODING_INVALID")
    kind, raw = value["type"], value["value"]
    if kind == "NULL":
        require(raw is None, "EXACT_RESULT_ENCODING_INVALID")
        return None
    require(isinstance(raw, str), "EXACT_RESULT_ENCODING_INVALID")
    if kind == "INTEGER":
        return int(raw)
    if kind == "DECIMAL":
        result = Decimal(raw)
        require(result.is_finite(), "EXACT_RESULT_NON_FINITE_DECIMAL")
        return result
    if kind == "APPROXIMATE_FLOAT":
        result = float(raw)
        require(math.isfinite(result), "EXACT_RESULT_NON_FINITE_FLOAT")
        return result
    if kind == "DATETIME":
        return datetime.fromisoformat(raw)
    if kind == "DATE":
        return date.fromisoformat(raw)
    if kind == "TIME":
        return time.fromisoformat(raw)
    if kind == "TEXT":
        return raw
    raise ValueError("EXACT_RESULT_ENCODING_INVALID")


def exact_result_payload(result: dict) -> dict:
    """Encode a validated result without Decimal -> float conversion."""
    columns = result.get("columns")
    rows = result.get("data")
    require(
        isinstance(columns, list)
        and all(isinstance(column, str) and column for column in columns)
        and len(columns) == len(set(columns))
        and isinstance(rows, list)
        and all(isinstance(row, dict) and list(row) == columns for row in rows),
        "EXACT_RESULT_SHAPE_INVALID",
    )
    return {
        "encoding_version": EXACT_RESULT_ENCODING,
        "columns": list(columns),
        "rows": [
            {column: _encode_value(row[column]) for column in columns}
            for row in rows
        ],
        "row_count": len(rows),
        "snapshot_id": result.get("snapshot_id"),
        "data_as_of": result.get("data_as_of"),
        "quality_status": result.get("quality_status"),
    }


def seal_exact_result(context: AuthorizedScopeContext, result: dict) -> ScopedArtifact:
    payload = exact_result_payload(result)
    return ScopedArtifact(
        kind="RESULT_ARTIFACT",
        context=context,
        payload=payload,
        payload_digest=contract_digest(payload),
    )


def restore_exact_result(artifact: ScopedArtifact, context: AuthorizedScopeContext) -> dict:
    artifact = ScopedArtifact.model_validate(artifact.model_dump(mode="json"))
    require(artifact.kind == "RESULT_ARTIFACT" and artifact.context == context,
            "EXACT_RESULT_SCOPE_MISMATCH")
    payload = artifact.payload
    require(
        isinstance(payload, dict)
        and payload.get("encoding_version") == EXACT_RESULT_ENCODING
        and type(payload.get("row_count")) is int
        and isinstance(payload.get("columns"), list)
        and isinstance(payload.get("rows"), list)
        and payload["row_count"] == len(payload["rows"]),
        "EXACT_RESULT_ENCODING_INVALID",
    )
    columns = list(payload["columns"])
    rows = []
    for encoded in payload["rows"]:
        require(isinstance(encoded, dict) and list(encoded) == columns,
                "EXACT_RESULT_ENCODING_INVALID")
        rows.append({column: _decode_value(encoded[column]) for column in columns})
    return {
        "columns": columns,
        "data": rows,
        "row_count": len(rows),
        "snapshot_id": payload.get("snapshot_id"),
        "data_as_of": payload.get("data_as_of"),
        "quality_status": payload.get("quality_status"),
    }


@dataclass(frozen=True)
class PersistedScalarPlan:
    next_state: ScopedArtifact
    plan_state: ScopedArtifact
    prepared: PreparedExecution


@dataclass(frozen=True)
class PersistedSessionSnapshot:
    key: str
    state_version: int
    envelope: dict
    state: ScopedArtifact | None
    plans: tuple[ScopedArtifact, ...]

    def message(self, message_id: str):
        return self.envelope.get("messages", {}).get(message_id)

    @property
    def revision(self) -> int:
        return int(self.envelope.get("revision", self.state_version))


def _seal_state(template: ScopedArtifact, state: ConversationState) -> ScopedArtifact:
    payload = state.model_dump(mode="json")
    material = scoped_artifact_material(payload, template.source_value_bindings)
    return ScopedArtifact(
        kind="CONVERSATION",
        context=template.context,
        payload=payload,
        source_value_bindings=template.source_value_bindings,
        payload_digest=contract_digest(material),
    )


class RedisScalarSessionStore:
    """CAS-protected V2 state in an isolated or stable deployment namespace."""

    _CAS = """
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

    _STABLE_CAS = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
local decoded = cjson.decode(current)
if tonumber(decoded['revision']) ~= tonumber(ARGV[1]) then return 0 end
local ttl = redis.call('TTL', KEYS[1])
if ttl <= 0 then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ttl)
return 1
"""

    _BEGIN_CAS = """
local current = redis.call('GET', KEYS[1])
local version = 0
local ttl = tonumber(ARGV[3])
if current then
  local decoded = cjson.decode(current)
  version = tonumber(decoded['revision'])
  ttl = redis.call('TTL', KEYS[1])
  if ttl <= 0 then return 0 end
end
if version ~= tonumber(ARGV[1]) then return 0 end
local guard = redis.call('GET', KEYS[2])
if guard then return -1 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ttl)
redis.call('SET', KEYS[2], ARGV[4], 'EX', ARGV[5])
return 1
"""

    def __init__(self, redis, *, prefix: str, run_id: str | None = None,
                 deployment_id: str | None = None, ttl_seconds: int,
                 idempotency_ttl_seconds: int | None = None,
                 max_messages: int = 100, max_envelope_bytes: int = 4 * 1024 * 1024,
                 production_prefix: str = "youo:data-analysis:v2"):
        normalized = prefix.rstrip(":")
        isolated = run_id is not None and deployment_id is None
        deployment = deployment_id is not None and run_id is None
        require(isolated or deployment, "V2_STORE_NAMESPACE_MODE_REQUIRED")
        if isolated:
            require(
                bool(ISOLATED_PREFIX_PATTERN.fullmatch(normalized))
                and normalized.startswith(production_prefix.rstrip(":") + ":isolated:round5-11:")
                and normalized.endswith(":" + run_id),
                "ISOLATED_REDIS_NAMESPACE_REQUIRED",
            )
            require(300 <= ttl_seconds <= 86400, "ISOLATED_REDIS_TTL_INVALID")
        else:
            require(
                bool(DEPLOYMENT_PREFIX_PATTERN.fullmatch(normalized))
                and normalized.endswith(":" + deployment_id)
                and ":isolated:" not in normalized
                and normalized != production_prefix.rstrip(":"),
                "DEPLOYMENT_REDIS_NAMESPACE_REQUIRED",
            )
            require(300 <= ttl_seconds <= 604800, "DEPLOYMENT_REDIS_TTL_INVALID")
        self.redis = redis
        self.prefix = normalized
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.namespace_mode = "ISOLATED_TEST" if isolated else "STABLE_DEPLOYMENT"
        self.schema_version = PERSISTED_SESSION_SCHEMA if isolated else DEPLOYMENT_SESSION_SCHEMA
        self.ttl_seconds = ttl_seconds
        self.idempotency_ttl_seconds = idempotency_ttl_seconds or ttl_seconds
        require(self.idempotency_ttl_seconds >= ttl_seconds,
                "V2_IDEMPOTENCY_TTL_TOO_SHORT")
        require(1 <= max_messages <= 1000 and 65536 <= max_envelope_bytes <= 32 * 1024 * 1024,
                "V2_STORE_CAPACITY_INVALID")
        self.max_messages = max_messages
        self.max_envelope_bytes = max_envelope_bytes
        self.created_keys: set[str] = set()

    def _key(self, context: AuthorizedScopeContext, state_identity: dict) -> str:
        material = [
            state_identity[name]
            for name in ("tenant_id", "user_id", "application_id", "conversation_id")
        ]
        material.append(context.fingerprint())
        return f"{self.prefix}:conversation:{contract_digest(material)}"

    def _empty(self, context, state_identity):
        return {
            "schema_version": self.schema_version,
            "state_version": 0,
            "revision": 0,
            "context_fingerprint": context.fingerprint(),
            "state_identity": dict(state_identity),
            "state": None,
            "plans": {},
            "messages": {},
        }

    def _snapshot(self, key, value, context, state_identity):
        require(
            isinstance(value, dict)
            and value.get("schema_version") == self.schema_version
            and value.get("context_fingerprint") == context.fingerprint()
            and value.get("state_identity") == state_identity
            and type(value.get("state_version")) is int,
            "PERSISTED_SESSION_CONTRACT_INVALID",
        )
        if self.namespace_mode == "STABLE_DEPLOYMENT":
            require(type(value.get("revision")) is int and value["revision"] >= 0,
                    "PERSISTED_SESSION_REVISION_INVALID")
        state = None
        if value.get("state") is not None:
            state = ScopedArtifact.model_validate(value["state"])
            require(state.kind == "CONVERSATION" and state.context == context,
                    "PERSISTED_SESSION_SCOPE_MISMATCH")
            restored = ConversationState.model_validate(state.payload)
            require(restored.state_version == value["state_version"],
                    "PERSISTED_SESSION_VERSION_MISMATCH")
            require(all(getattr(restored, name) == expected
                for name, expected in state_identity.items()),
                "PERSISTED_SESSION_IDENTITY_MISMATCH")
        elif value["state_version"] != 0:
            raise ValueError("PERSISTED_SESSION_VERSION_MISMATCH")
        plans = []
        for task_id, raw in value.get("plans", {}).items():
            plan = ScopedArtifact.model_validate(raw)
            require(plan.kind == "LAST_REQUEST" and plan.context == context,
                    "PERSISTED_PLAN_SCOPE_MISMATCH")
            require(isinstance(task_id, str), "PERSISTED_PLAN_CONTRACT_INVALID")
            plans.append(plan)
        for message in value.get("messages", {}).values():
            require(isinstance(message, dict) and message.get("status") in {
                "RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN", "REVIEW_REQUIRED"
            } and isinstance(message.get("request_fingerprint"), str),
                "PERSISTED_MESSAGE_CONTRACT_INVALID")
            if message.get("response") is not None:
                AgentResponse.model_validate(message["response"])
            if message.get("result") is not None:
                restore_exact_result(ScopedArtifact.model_validate(message["result"]), context)
        return PersistedSessionSnapshot(key, value["state_version"], deepcopy(value), state, tuple(plans))

    async def load(self, context: AuthorizedScopeContext, state_identity: dict):
        key = self._key(context, state_identity)
        raw = await self.redis.get(key)
        value = self._empty(context, state_identity) if raw is None else json.loads(raw)
        return self._snapshot(key, value, context, state_identity)

    async def _publish(self, previous: PersistedSessionSnapshot, value: dict, context, state_identity):
        if self.namespace_mode == "STABLE_DEPLOYMENT":
            value["revision"] = previous.revision + 1
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        require(len(value.get("messages", {})) <= self.max_messages,
                "PERSISTED_SESSION_MESSAGE_LIMIT_REACHED")
        require(len(encoded.encode("utf-8")) <= self.max_envelope_bytes,
                "PERSISTED_SESSION_SIZE_LIMIT_REACHED")
        script = self._STABLE_CAS if self.namespace_mode == "STABLE_DEPLOYMENT" else self._CAS
        expected = previous.revision if self.namespace_mode == "STABLE_DEPLOYMENT" else previous.state_version
        result = await self.redis.eval(
            script, 1, previous.key, expected,
            encoded, self.ttl_seconds,
        )
        if int(result) != 1:
            raise StateTransitionError("persisted scalar session changed concurrently")
        self.created_keys.add(previous.key)
        return self._snapshot(previous.key, value, context, state_identity)

    def _guard_key(self, state_key: str, message_id: str) -> str:
        return f"{self.prefix}:message:{contract_digest([state_key, message_id])}"

    async def idempotency_record(self, previous: PersistedSessionSnapshot, message_id: str):
        if self.namespace_mode != "STABLE_DEPLOYMENT":
            return None
        raw = await self.redis.get(self._guard_key(previous.key, message_id))
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError("PERSISTED_MESSAGE_GUARD_INVALID") from None
        require(isinstance(value, dict)
                and value.get("schema_version") == "limited-scalar-message-guard-v1"
                and isinstance(value.get("request_fingerprint"), str),
                "PERSISTED_MESSAGE_GUARD_INVALID")
        return value

    async def begin(self, previous, *, planned_state, plan_state, attempt,
                    message_id, request_fingerprint, context, state_identity,
                    started_at: datetime | None = None):
        require(previous.message(message_id) is None, "PERSISTED_MESSAGE_ALREADY_RESERVED")
        planned_state = ScopedArtifact.model_validate(planned_state.model_dump(mode="json"))
        require(planned_state.context == context and planned_state.kind == "CONVERSATION",
                "PERSISTED_SESSION_SCOPE_MISMATCH")
        planned = ConversationState.model_validate(planned_state.payload)
        require(planned.state_version == previous.state_version + 1,
                "PERSISTED_PLANNED_STATE_VERSION_MISMATCH")
        running = _state_with_attempt(planned, attempt)
        value = deepcopy(previous.envelope)
        value["state_version"] = running.state_version
        if self.namespace_mode == "STABLE_DEPLOYMENT":
            value["revision"] = previous.revision + 1
        value["state"] = _seal_state(planned_state, running).model_dump(mode="json")
        value["plans"][attempt.task_id] = plan_state.model_dump(mode="json")
        value["messages"][message_id] = {
            "request_fingerprint": request_fingerprint,
            "status": "RUNNING",
            "execution_id": attempt.execution_id,
            "execution_stage": "PRE_SUBMISSION_RESERVED",
            "started_at": (started_at or datetime.now(timezone.utc)).isoformat(),
        }
        if self.namespace_mode == "STABLE_DEPLOYMENT":
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
            require(len(value.get("messages", {})) <= self.max_messages,
                    "PERSISTED_SESSION_MESSAGE_LIMIT_REACHED")
            require(len(encoded.encode("utf-8")) <= self.max_envelope_bytes,
                    "PERSISTED_SESSION_SIZE_LIMIT_REACHED")
            guard_key = self._guard_key(previous.key, message_id)
            guard = json.dumps({
                "schema_version": "limited-scalar-message-guard-v1",
                "request_fingerprint": request_fingerprint,
                "state_key": previous.key,
                "reserved_at": value["messages"][message_id]["started_at"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            result = await self.redis.eval(
                self._BEGIN_CAS, 2, previous.key, guard_key,
                previous.revision, encoded, self.ttl_seconds,
                guard, self.idempotency_ttl_seconds,
            )
            if int(result) == -1:
                raise ValueError("PERSISTED_MESSAGE_ALREADY_RESERVED")
            if int(result) != 1:
                raise StateTransitionError("persisted scalar session changed concurrently")
            return self._snapshot(previous.key, value, context, state_identity)
        return await self._publish(previous, value, context, state_identity)

    async def mark_stage(self, previous, *, message_id, request_fingerprint,
                         stage: Literal["SUBMISSION_ATTEMPTED", "RESULT_RETURNED", "RESULT_VALIDATED"],
                         context, state_identity, observed_at: datetime):
        record = previous.message(message_id)
        require(record is not None and record["status"] == "RUNNING"
                and record["request_fingerprint"] == request_fingerprint,
                "PERSISTED_MESSAGE_RESERVATION_MISMATCH")
        value = deepcopy(previous.envelope)
        value["messages"][message_id] = {
            **record,
            "execution_stage": stage,
            "last_observed_at": observed_at.isoformat(),
        }
        return await self._publish(previous, value, context, state_identity)

    async def finish(self, previous, *, terminal_state, message_id,
                     request_fingerprint, response, result, context, state_identity,
                     status: Literal["SUCCEEDED", "FAILED"]):
        record = previous.message(message_id)
        require(record is not None and record["status"] == "RUNNING"
                and record["request_fingerprint"] == request_fingerprint,
                "PERSISTED_MESSAGE_RESERVATION_MISMATCH")
        terminal_state = ScopedArtifact.model_validate(terminal_state.model_dump(mode="json"))
        restored = ConversationState.model_validate(terminal_state.payload)
        require(restored.state_version == previous.state_version + 1,
                "PERSISTED_TERMINAL_STATE_VERSION_MISMATCH")
        value = deepcopy(previous.envelope)
        value["state_version"] = restored.state_version
        value["state"] = terminal_state.model_dump(mode="json")
        value["messages"][message_id] = {
            **record,
            "status": status,
            "execution_stage": "SUCCESS_RECEIPT_SAVED" if status == "SUCCEEDED" else "TERMINAL_FAILURE_SAVED",
            "response": response.model_dump(mode="json"),
            "result": result.model_dump(mode="json") if result is not None else None,
        }
        return await self._publish(previous, value, context, state_identity)

    async def require_operator_review(self, previous, *, message_id, request_fingerprint,
                                      reason: str, operator: str, context, state_identity,
                                      observed_at: datetime):
        require(self.namespace_mode == "STABLE_DEPLOYMENT",
                "DEPLOYMENT_RECOVERY_COMMAND_REQUIRED")
        require(1 <= len(reason.strip()) <= 500 and 1 <= len(operator.strip()) <= 200,
                "RECOVERY_AUDIT_IDENTITY_REQUIRED")
        record = previous.message(message_id)
        require(record is not None and record.get("request_fingerprint") == request_fingerprint
                and record.get("status") in {"RUNNING", "UNKNOWN", "REVIEW_REQUIRED"},
                "RECOVERY_MESSAGE_NOT_ELIGIBLE")
        value = deepcopy(previous.envelope)
        value["messages"][message_id] = {
            **record,
            "status": "REVIEW_REQUIRED",
            "review": {
                "reason": reason.strip(),
                "operator": operator.strip(),
                "recorded_at": observed_at.isoformat(),
            },
        }
        return await self._publish(previous, value, context, state_identity)

    async def mark_unknown(self, previous, *, message_id, request_fingerprint,
                           reason_code: str, context, state_identity,
                           observed_at: datetime):
        record = previous.message(message_id)
        require(record is not None and record.get("status") == "RUNNING"
                and record.get("request_fingerprint") == request_fingerprint,
                "PERSISTED_MESSAGE_RESERVATION_MISMATCH")
        value = deepcopy(previous.envelope)
        value["messages"][message_id] = {
            **record,
            "status": "UNKNOWN",
            "execution_stage": "EXECUTION_OUTCOME_UNKNOWN",
            "unknown_reason": reason_code,
            "last_observed_at": observed_at.isoformat(),
        }
        return await self._publish(previous, value, context, state_identity)

    async def delete_exact(self, keys: list[str]):
        require(set(keys) <= self.created_keys, "ISOLATED_REDIS_DELETE_NOT_OWNED")
        if keys:
            await self.redis.delete(*keys)

    async def aclose(self):
        await self.redis.aclose()


class PersistedScalarApiHandler:
    """Execute one supported scalar turn and atomically publish its API receipt."""

    def __init__(
        self,
        *,
        store: RedisScalarSessionStore,
        context_resolver: Callable[[ChatRequest, TrustedIdentity], AuthorizedScopeContext],
        planner: Callable[[ChatRequest, TrustedIdentity, ScopedArtifact | None,
                           tuple[ScopedArtifact, ...]], Awaitable[PersistedScalarPlan]],
        transport: Callable[[TypedExecutionRequest], Awaitable[dict]],
        clock: Callable[[], datetime],
        running_review_seconds: int = 300,
    ):
        self.store = store
        self.context_resolver = context_resolver
        self.planner = planner
        self.transport = transport
        self.clock = clock
        require(30 <= running_review_seconds <= 86400,
                "RUNNING_REVIEW_WINDOW_INVALID")
        self.running_review_seconds = running_review_seconds

    @staticmethod
    def _identity(chat, identity):
        return {
            "conversation_id": chat.conversation_id,
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "application_id": chat.application_id,
        }

    @staticmethod
    def _fingerprint(chat, identity):
        return contract_digest({
            "chat": chat.model_dump(mode="json"),
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
        })

    def _response(self, chat, *, status, answer, error_code=None, evidence=(), dataset_id=None):
        return AgentResponse(
            request_id=uuid4(),
            conversation_id=chat.conversation_id,
            status=status,
            error_code=error_code,
            intent=PrimaryIntent.METRIC_QUERY,
            intent_source="V2_TYPED_SEMANTIC_RUNTIME",
            answer=answer,
            evidence=list(evidence),
            reliability=ReliabilityReport(
                level="HIGH" if status == "COMPLETED" else "FAIL",
                score=1 if status == "COMPLETED" else 0,
                gates={
                    "query_succeeded": status == "COMPLETED",
                    "query_evidence_preserved": status == "COMPLETED",
                    "safe_termination": status != "COMPLETED",
                },
                warnings=[] if status == "COMPLETED" else [answer],
            ),
            dataset_id=dataset_id,
            semantic_model_id=chat.semantic_model_id,
            database_id=chat.database_id,
            requested_business_domain_ids=list(chat.business_domain_ids),
            business_domain_selection_mode=("EXPLICIT" if chat.business_domain_ids else "AUTO"),
        )

    def _success_response(self, chat, prepared, result, artifact, terminal):
        columns = result["columns"]
        values = result["data"][0]
        rendered = "；".join(f"{column}：{values[column]}" for column in columns)
        evidence = EvidenceItem(
            evidence_id=f"query:{artifact.payload_digest}",
            kind="QUERY_RESULT",
            source_ref=f"data-source:{prepared.sql_receipt['data_source_id']}",
            payload={
                "columns": columns,
                "row_count": 1,
                "returned_row_count": 1,
                "total_row_count_confirmed": True,
                "truncated": False,
                "data_as_of": result.get("data_as_of"),
                "quality_status": result.get("quality_status"),
                "result_fingerprint": artifact.payload_digest,
            },
        )
        return self._response(
            chat,
            status="COMPLETED",
            answer=rendered,
            evidence=[evidence],
            dataset_id=terminal.dataset_id,
        )

    async def handle(self, chat: ChatRequest, identity: TrustedIdentity) -> AgentResponse:
        try:
            context = self.context_resolver(chat, identity)
        except ValueError as exc:
            code = str(exc) if str(exc).startswith(("EXECUTION_", "ASL2_", "V2_")) else "V2_SCALAR_SCOPE_REJECTED"
            return self._response(
                chat, status="SAFE_FALLBACK", error_code=code,
                answer="当前请求不在已配置的限定标量授权范围内。",
            )
        state_identity = self._identity(chat, identity)
        fingerprint = self._fingerprint(chat, identity)
        snapshot = await self.store.load(context, state_identity)
        prior = snapshot.message(chat.message_id)
        if prior is not None:
            if prior["request_fingerprint"] != fingerprint:
                raise MessageIdReuseConflictError(chat.message_id)
            if prior["status"] in {"SUCCEEDED", "FAILED"} and prior.get("response"):
                return AgentResponse.model_validate(prior["response"])
            code = "EXECUTION_OUTCOME_PENDING_REVIEW"
            started = prior.get("started_at")
            if prior.get("status") in {"UNKNOWN", "REVIEW_REQUIRED"}:
                code = "EXECUTION_OUTCOME_REQUIRES_OPERATOR_REVIEW"
            elif isinstance(started, str):
                try:
                    if (self.clock() - datetime.fromisoformat(started)).total_seconds() >= self.running_review_seconds:
                        code = "EXECUTION_OUTCOME_REQUIRES_OPERATOR_REVIEW"
                except (TypeError, ValueError):
                    code = "EXECUTION_OUTCOME_REQUIRES_OPERATOR_REVIEW"
            return self._response(
                chat,
                status="SAFE_FALLBACK",
                error_code=code,
                answer="该请求已有执行记录但尚无可确认终态，请稍后查询执行状态。",
            )
        guard = await self.store.idempotency_record(snapshot, chat.message_id)
        if guard is not None:
            if guard["request_fingerprint"] != fingerprint:
                raise MessageIdReuseConflictError(chat.message_id)
            return self._response(
                chat,
                status="SAFE_FALLBACK",
                error_code="EXECUTION_SESSION_EXPIRED_REUSE_REJECTED",
                answer="原会话回执已过期，但消息仍在幂等保护期内。请新建会话后重新提出完整问题。",
            )
        try:
            planned = await self.planner(chat, identity, snapshot.state, snapshot.plans)
            prepared = planned.prepared
            require(prepared.plan.permission_requirement == context,
                    "EXECUTION_SCOPE_PIN_MISMATCH")
            require(prepared.plan.payload.payload_type == "SCALAR_AGGREGATE",
                    "EXECUTION_PAYLOAD_UNSUPPORTED")
            require(
                prepared.fingerprint == preparation_digest(
                    prepared.plan, prepared.lowering, prepared.sql_receipt,
                    prepared.state_identity,
                ),
                "EXECUTION_PREPARATION_IDENTITY_MISMATCH",
            )
        except ValueError as exc:
            code = str(exc) if str(exc).startswith(("EXECUTION_", "ASL2_", "V2_")) else "V2_SCALAR_PLANNING_FAILED"
            return self._response(chat, status="SAFE_FALLBACK", error_code=code,
                                  answer="当前请求尚不在已验证的 V2 标量执行能力范围内。")

        plan, low, sql = prepared.plan, prepared.lowering, prepared.sql_receipt
        request_id = contract_digest([context.fingerprint(), chat.message_id, prepared.fingerprint])
        parameter = sql.get("sql_parameter_contract") or {}
        execution_request = TypedExecutionRequest(
            request_id, prepared.fingerprint, context, plan.plan_id, plan.task_id,
            plan.task_version, chat.message_id, str(sql["data_source_id"]),
            sql["sql"], sql.get("sql_parameters"), parameter.get("statement_fingerprint"),
            "LIVE_READ_ONLY",
        )
        planned_state = ConversationState.model_validate(planned.next_state.payload)
        attempt = m.ExecutionAttemptRecord(
            execution_id="isolated-live-read-only:" + request_id,
            task_id=plan.task_id,
            task_version=plan.task_version,
            attempt_number=1 + sum(
                item.task_id == plan.task_id and item.task_version == plan.task_version
                for item in planned_state.execution_attempts.values()
            ),
            status="RUNNING",
            started_at=self.clock(),
            execution_backend="SEMANTIC_QUERY",
            snapshot_id="isolated-live-read-only:pending:" + request_id,
            catalog_version=context.catalog_pin.catalog_version,
            vector_index_version=context.catalog_pin.vector_index_version,
            semantic_model_version=plan.snapshot_requirement.semantic_model_version,
            policy_version=plan.version_metadata.policy_version,
            asl_digest=contract_digest(low.asl),
            sql_digest=contract_digest({"sql": execution_request.sql,
                                        "parameters": execution_request.parameters}),
        )
        try:
            running_snapshot = await self.store.begin(
                snapshot,
                planned_state=planned.next_state,
                plan_state=planned.plan_state,
                attempt=attempt,
                message_id=chat.message_id,
                request_fingerprint=fingerprint,
                context=context,
                state_identity=state_identity,
                started_at=attempt.started_at,
            )
        except (StateTransitionError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc) in {
                "PERSISTED_SESSION_MESSAGE_LIMIT_REACHED",
                "PERSISTED_SESSION_SIZE_LIMIT_REACHED",
            }:
                return self._response(
                    chat, status="SAFE_FALLBACK", error_code=str(exc),
                    answer="当前 V2 会话已达到保留上限，请新建会话后继续。",
                )
            if isinstance(exc, ValueError) and str(exc) != "PERSISTED_MESSAGE_ALREADY_RESERVED":
                raise
            current = await self.store.load(context, state_identity)
            record = current.message(chat.message_id)
            if record is not None and record.get("request_fingerprint") == fingerprint:
                return self._response(
                    chat, status="SAFE_FALLBACK",
                    error_code="EXECUTION_OUTCOME_PENDING_REVIEW",
                    answer="该请求已被受理，不会重复提交查询。",
                )
            raise MessageIdReuseConflictError(chat.message_id) from None
        running_artifact = running_snapshot.state
        running = ConversationState.model_validate(running_artifact.payload)
        try:
            if self.store.namespace_mode == "STABLE_DEPLOYMENT":
                running_snapshot = await self.store.mark_stage(
                    running_snapshot, message_id=chat.message_id,
                    request_fingerprint=fingerprint, stage="SUBMISSION_ATTEMPTED",
                    context=context, state_identity=state_identity,
                    observed_at=self.clock(),
                )
            response = await self.transport(execution_request)
            if self.store.namespace_mode == "STABLE_DEPLOYMENT":
                running_snapshot = await self.store.mark_stage(
                    running_snapshot, message_id=chat.message_id,
                    request_fingerprint=fingerprint, stage="RESULT_RETURNED",
                    context=context, state_identity=state_identity,
                    observed_at=self.clock(),
                )
            result, result_proof = _validate_response(execution_request, low, response)
            result_artifact = seal_exact_result(context, result)
            chain = m.ProofChain(
                plan=proof("current_authorized_plan", "current_task_version",
                           evidence=prepared.fingerprint),
                asl=proof("native_typed_lowering", "typed_scalar_features_supported",
                          evidence=low.compilation_fingerprint),
                sql_plan=proof("native_pinned_sql_validation", "scope_pin_projection_parameters",
                               evidence=contract_digest(sql)),
                result=result_proof,
            )
            data = attempt.model_dump(mode="json")
            data.update(
                status="SUCCEEDED",
                completed_at=self.clock(),
                snapshot_id=result["snapshot_id"],
                dataset_id="isolated-live-read-only-dataset:" + request_id,
                proof_chain=chain.model_dump(mode="json"),
            )
            terminal = m.ExecutionAttemptRecord.model_validate(data)
            terminal_state = _state_with_attempt(running, terminal, dataset_id=terminal.dataset_id)
            terminal_artifact = _seal_state(running_artifact, terminal_state)
            api_response = self._success_response(
                chat, prepared, result, result_artifact, terminal
            )
            if self.store.namespace_mode == "STABLE_DEPLOYMENT":
                running_snapshot = await self.store.mark_stage(
                    running_snapshot, message_id=chat.message_id,
                    request_fingerprint=fingerprint, stage="RESULT_VALIDATED",
                    context=context, state_identity=state_identity,
                    observed_at=self.clock(),
                )
            await self.store.finish(
                running_snapshot,
                terminal_state=terminal_artifact,
                message_id=chat.message_id,
                request_fingerprint=fingerprint,
                response=api_response,
                result=result_artifact,
                context=context,
                state_identity=state_identity,
                status="SUCCEEDED",
            )
            return api_response
        except Exception as exc:
            reason = (
                "EXECUTION_TIMEOUT_OUTCOME_UNKNOWN" if isinstance(exc, TimeoutError)
                else "EXECUTION_RECEIPT_STATE_CONFLICT" if isinstance(exc, StateTransitionError)
                else str(exc) if isinstance(exc, ValueError) and str(exc).startswith(("EXECUTION_", "ASL2_"))
                else "EXECUTION_TRANSPORT_FAILURE"
            )
            failed_data = attempt.model_dump(mode="json")
            failed_data.update(
                status="FAILED",
                completed_at=self.clock(),
                error_type=("RESULT_CONTRACT_FAILURE" if reason.startswith(("EXECUTION_RESULT_", "ASL2_RESULT_"))
                            else "EXECUTION_FAILURE"),
            )
            failed = m.ExecutionAttemptRecord.model_validate(failed_data)
            failed_state = _state_with_attempt(running, failed)
            fallback = self._response(
                chat,
                status="SAFE_FALLBACK",
                error_code=reason,
                answer="查询执行未能形成可验证结果，本轮未发布成功数据集。",
            )
            outcome_unknown = (
                self.store.namespace_mode == "STABLE_DEPLOYMENT"
                and (
                    isinstance(exc, (TimeoutError, StateTransitionError))
                    or reason == "EXECUTION_TRANSPORT_FAILURE"
                )
            )
            if outcome_unknown:
                try:
                    await self.store.mark_unknown(
                        running_snapshot,
                        message_id=chat.message_id,
                        request_fingerprint=fingerprint,
                        reason_code=reason,
                        context=context,
                        state_identity=state_identity,
                        observed_at=self.clock(),
                    )
                except (StateTransitionError, ValueError):
                    pass
                return self._response(
                    chat,
                    status="SAFE_FALLBACK",
                    error_code=(
                        "EXECUTION_RECEIPT_STATE_CONFLICT"
                        if isinstance(exc, StateTransitionError)
                        else reason
                    ),
                    answer="查询可能已经提交，但尚无可发布的结果回执，需要运维核对；系统不会自动重复执行。",
                )
            try:
                await self.store.finish(
                    running_snapshot,
                    terminal_state=_seal_state(running_artifact, failed_state),
                    message_id=chat.message_id,
                    request_fingerprint=fingerprint,
                    response=fallback,
                    result=None,
                    context=context,
                    state_identity=state_identity,
                    status="FAILED",
                )
            except StateTransitionError:
                return self._response(
                    chat,
                    status="SAFE_FALLBACK",
                    error_code="EXECUTION_RECEIPT_STATE_CONFLICT",
                    answer="查询可能已经提交，但状态发生并发变化，未发布成功结果。",
                )
            return fallback

    async def check_message_conflict(
        self,
        chat: ChatRequest,
        identity: TrustedIdentity,
    ) -> None:
        """Read-only pre-stream check for an existing V2 idempotency claim."""

        try:
            context = self.context_resolver(chat, identity)
        except ValueError:
            # Scope/capability rejection is rendered by ``handle`` using the
            # existing response contract.  It is not a message-id conflict.
            return
        state_identity = self._identity(chat, identity)
        fingerprint = self._fingerprint(chat, identity)
        snapshot = await self.store.load(context, state_identity)
        prior = snapshot.message(chat.message_id)
        if prior is not None and prior["request_fingerprint"] != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)
        guard = await self.store.idempotency_record(snapshot, chat.message_id)
        if guard is not None and guard["request_fingerprint"] != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)

    async def aclose(self):
        await self.store.aclose()
