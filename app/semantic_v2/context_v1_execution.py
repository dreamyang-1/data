"""Opt-in V2 context resolution followed by the existing V1 workflow.

V2 owns only relation, TaskState and completed-question resolution here.  The
existing V1 workflow remains the sole business query execution path.  This
module never creates an ASL, lowers SQL, or calls a database transport.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field
from redis.asyncio import Redis

from app.config import Settings
from app.domain.models import (
    AgentResponse,
    AnalysisProcessStep,
    ChatRequest,
    PrimaryIntent,
    TrustedIdentity,
)
from app.services.progress import emit_progress
from app.stores import MessageIdReuseConflictError

from .authorized_contract import AuthorizedScopeContext, ScopedArtifact, contract_digest
from .catalog_bridge import ScopedPlanSession
from .completed_question import CompletedQuestionDisplay, build_completed_question_display
from .context_proposal import ContextProposalFailure
from .explicit_time import normalize_range
from .limited_scalar_runtime import (
    OAGNET_RUNTIME_FILES,
    _import_runtime_module,
    _open_live_read_only_catalog,
    _prepend_runtime_roots,
    source_bundle_digest,
)
from .pending_recognition import RecognizedClarification
from .pipeline import CurrentTurnParser, CurrentTurnSemanticParse
from .persisted_scalar_api import RedisScalarSessionStore
from .pipeline import AuthorizedLogicalPlan
from .recognition import RawTurnPlanner, RecognizedStandaloneNewTask
from .recognition_client import RecognitionFailure, RecognitionModelClient
from .recognition_repairs import repair_model_parse
from . import models as semantic_models
from .state_machine import ConversationState, StateTransitionError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextV1ExternalDependencies:
    publication: Any
    model: Any
    redis: Any
    module_origins: dict[str, str] = field(default_factory=dict)


class ExecutionAnchorTimeContext(semantic_models.StrictModel):
    """A current-turn calendar value applied to an opaque execution base."""

    surface: str = Field(min_length=1, max_length=1000)
    applied_text: str = Field(min_length=1, max_length=100)
    start: AwareDatetime
    end_exclusive: AwareDatetime
    operation: Literal["SET", "REPLACE"]
    source: Literal["CURRENT_EXPLICIT_TIME"] = "CURRENT_EXPLICIT_TIME"


class ExecutionBackedContextAnchor(semantic_models.StrictModel):
    """Minimal context continuity after a successful V1-only execution.

    The opaque question remains authoritative for unstructured clauses. V1
    output only proves that the exact execution question ran successfully; it
    never becomes a replacement semantic truth source.
    """

    schema_version: Literal["v1-execution-anchor-v1"] = "v1-execution-anchor-v1"
    anchor_id: semantic_models.Identifier
    parent_anchor_id: semantic_models.Identifier | None = None
    source_message_id: semantic_models.Identifier
    provenance: Literal["V1_EXECUTION_ANCHOR"] = "V1_EXECUTION_ANCHOR"
    completeness: Literal["PARTIAL"] = "PARTIAL"
    evidence_mode: Literal["EXECUTION_BACKED"] = "EXECUTION_BACKED"
    original_question: str = Field(min_length=1, max_length=10000)
    opaque_base_question: str = Field(min_length=1, max_length=10000)
    actual_execution_question: str = Field(min_length=1, max_length=10000)
    semantic_model_id: int = Field(strict=True, gt=0)
    requested_business_domain_ids: tuple[int, ...] = ()
    resolved_business_domain_ids: tuple[int, ...] = Field(min_length=1)
    scope_fingerprint: semantic_models.Identifier
    state_version: int = Field(strict=True, ge=1)
    revision: int = Field(strict=True, ge=1)
    time_context: ExecutionAnchorTimeContext | None = None
    confirmed_semantics: dict[str, list[str]] = Field(default_factory=dict)
    current_turn_evidence_digest: semantic_models.Identifier
    v1_response_request_id: semantic_models.Identifier
    execution_evidence_refs: tuple[semantic_models.Identifier, ...] = Field(min_length=1)
    created_at: AwareDatetime


@dataclass(frozen=True)
class ExecutionAnchorUpdate:
    previous: ExecutionBackedContextAnchor | None
    current_parse: CurrentTurnSemanticParse
    time_context: ExecutionAnchorTimeContext | None
    resolved_business_domain_ids: tuple[int, ...]


@dataclass(frozen=True)
class ResolvedContextTurn:
    completed_question: str | None
    next_state: ScopedArtifact | None
    plan_state: ScopedArtifact | None
    pending_state: ScopedArtifact | None = None
    clarification_question: str | None = None
    clarification_trace: Any = None
    display: CompletedQuestionDisplay | None = None
    bridge_route: str = "V2_RESOLVED_COMPLETED_QUESTION"
    anchor_update: ExecutionAnchorUpdate | None = None


def _standalone_execution_display(
    question: str,
    display: CompletedQuestionDisplay,
) -> CompletedQuestionDisplay:
    """Keep a resolved standalone request byte-for-byte compatible with V1."""
    if display.relation != "NEW_TASK":
        return display
    material = display.model_dump(mode="python", exclude={"display_digest"})
    material["completed_question"] = question
    updated = CompletedQuestionDisplay(
        **material,
        display_digest=contract_digest(material),
    )
    if len(updated.public_message) > 500:
        raise ValueError("V2_COMPLETED_QUESTION_DISPLAY_TOO_LONG")
    return updated


def _completed_question_step(resolved: ResolvedContextTurn) -> AnalysisProcessStep | None:
    if not resolved.completed_question:
        return None
    summary = (
        resolved.display.public_message
        if resolved.display is not None
        else "补全后的完整问题：" + resolved.completed_question
    )
    if len(summary) > 500:
        return None
    return AnalysisProcessStep(
        stage="QUESTION_REWRITE",
        status="COMPLETED",
        title="补全后的完整问题",
        summary=summary,
    )


def _attach_completed_question(
    response: AgentResponse,
    resolved: ResolvedContextTurn,
) -> AgentResponse:
    """Expose exactly the question already sent to V1 in the original response."""
    step = _completed_question_step(resolved)
    if step is None:
        return response
    steps = list(response.analysis_process)
    index = next(
        (position for position, item in enumerate(steps)
         if item.stage == "QUESTION_REWRITE"),
        None,
    )
    if index is None:
        steps.insert(0, step)
    else:
        steps[index] = step
    response.analysis_process = steps[:20]
    return response


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _validated_time_context(
    raw_parse: CurrentTurnSemanticParse,
    *,
    question: str,
    message_id: str,
    now: datetime,
    require_followup: bool,
) -> tuple[CurrentTurnSemanticParse, ExecutionAnchorTimeContext]:
    """Accept one explicit current time edit without interpreting opaque text."""

    parsed = CurrentTurnSemanticParse.model_validate(
        raw_parse.model_dump(mode="json")
    )
    parsed, _repairs = repair_model_parse(
        parsed,
        text=question,
        turn_id=message_id,
    )
    validated = CurrentTurnParser.parse(
        text=question,
        turn_id=message_id,
        text_ref=message_id,
        parsed=parsed,
    )
    time_slot_ids = set(validated.explicit_slot_mentions.get("time_spec", []))
    time_ids = set(validated.temporal_expressions) & time_slot_ids
    markers = [
        marker for marker in validated.operation_markers
        if marker.slot_name == "time_spec" and marker.mention_id in time_ids
    ]
    other_slots = set(validated.explicit_slot_mentions) - {"time_spec"}
    other_markers = [
        marker for marker in validated.operation_markers
        if marker.slot_name != "time_spec"
    ]
    if len(time_ids) != 1 or len(markers) != 1:
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_EDIT_UNSUPPORTED")
    if require_followup and (
        other_slots
        or other_markers
        or any(mention.mention_id not in time_ids for mention in validated.mentions)
        or validated.topic_shift_signals
    ):
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_EDIT_UNSUPPORTED")
    marker = markers[0]
    operation = _enum_text(marker.operation_hint)
    if operation not in ({"REPLACE"} if require_followup else {"SET", "REPLACE"}):
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_TIME_OPERATION_REQUIRED")
    if require_followup:
        acts = {_enum_text(value) for value in validated.dialogue_act_candidates}
        dependency_evidence = bool(
            validated.reference_signals
            or validated.followup_signals
            or acts & {"CONTINUE", "MODIFY", "REPLACE"}
        )
        if not dependency_evidence:
            raise RecognitionFailure("V2_EXECUTION_ANCHOR_REFERENCE_REQUIRED")
    mention = next(
        item for item in validated.mentions if item.mention_id in time_ids
    )
    roles = {_enum_text(value) for value in mention.candidate_roles}
    if not roles & {"TIME_RANGE", "TIME_FIELD"}:
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_TIME_EVIDENCE_REQUIRED")
    normalized = normalize_range(mention.surface, now)
    local_start = normalized.start.astimezone(now.tzinfo)
    local_end = normalized.end_exclusive.astimezone(now.tzinfo)
    if (
        local_start.month == local_start.day == 1
        and local_end.month == local_end.day == 1
        and local_end.year == local_start.year + 1
    ):
        applied_text = f"{local_start.year}年"
    else:
        applied_text = mention.surface
    return parsed, ExecutionAnchorTimeContext(
        surface=mention.surface,
        applied_text=applied_text,
        start=normalized.start,
        end_exclusive=normalized.end_exclusive,
        operation=operation,
    )


def _initial_anchor_time_context(
    raw_parse: CurrentTurnSemanticParse,
    *,
    question: str,
    message_id: str,
    now: datetime,
) -> ExecutionAnchorTimeContext | None:
    if not raw_parse.temporal_expressions:
        return None
    try:
        _parsed, context = _validated_time_context(
            raw_parse,
            question=question,
            message_id=message_id,
            now=now,
            require_followup=False,
        )
    except (RecognitionFailure, ValueError):
        # A successful V1 execution still proves the opaque base. Uncertain V2
        # slot hypotheses are retained only by digest and never promoted.
        return None
    return context


def _apply_time_to_opaque_question(
    anchor: ExecutionBackedContextAnchor,
    time_context: ExecutionAnchorTimeContext,
) -> str:
    question = anchor.actual_execution_question
    prior = anchor.time_context
    if prior is not None:
        for old in (prior.applied_text, prior.surface):
            if old and old in question:
                return question.replace(old, time_context.applied_text, 1)
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_PRIOR_TIME_NOT_LOCATABLE")
    if question.startswith("查询"):
        return "查询" + time_context.applied_text + question[len("查询"):]
    return time_context.applied_text + question


def _advance_opaque_anchor_state(
    state: ScopedArtifact,
    *,
    message_id: str,
    context: AuthorizedScopeContext,
) -> ScopedArtifact:
    current = ConversationState.model_validate(state.payload)
    if current.active_topic_id is not None:
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_NOT_CURRENT")
    data = current.model_dump(mode="python")
    data["state_version"] = current.state_version + 1
    data["recent_turn_ids"] = [
        *current.recent_turn_ids,
        message_id,
    ][-100:]
    updated = ConversationState.model_validate(data)
    payload = updated.model_dump(mode="json")
    return ScopedArtifact(
        kind="CONVERSATION",
        context=context,
        payload=payload,
        payload_digest=contract_digest(payload),
    )


def _resolve_execution_anchor_followup(
    chat: ChatRequest,
    state: ScopedArtifact,
    anchor: ExecutionBackedContextAnchor,
    raw_parse: CurrentTurnSemanticParse,
    context: AuthorizedScopeContext,
    now: datetime,
) -> ResolvedContextTurn:
    if (
        anchor.scope_fingerprint != context.fingerprint()
        or anchor.semantic_model_id != chat.semantic_model_id
        or anchor.requested_business_domain_ids != tuple(chat.business_domain_ids)
    ):
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_SCOPE_MISMATCH")
    parsed, time_context = _validated_time_context(
        raw_parse,
        question=chat.question,
        message_id=chat.message_id,
        now=now,
        require_followup=True,
    )
    completed = _apply_time_to_opaque_question(anchor, time_context)
    next_state = _advance_opaque_anchor_state(
        state,
        message_id=chat.message_id,
        context=context,
    )
    semantic_fingerprint = contract_digest({
        "anchor_id": anchor.anchor_id,
        "operation": time_context.operation,
        "slot": "time_spec",
        "time": time_context.model_dump(mode="json"),
        "completed_question": completed,
    })
    display_material = {
        "source": "V1_EXECUTION_ANCHOR_PARTIAL",
        "message_id": chat.message_id,
        "task_id": anchor.anchor_id,
        "task_version": anchor.revision + 1,
        "plan_id": "execution-anchor:" + semantic_fingerprint[:24],
        "semantic_fingerprint": semantic_fingerprint,
        "relation": "MODIFY",
        "understanding": "基于上一轮成功执行的问题，仅替换当前明确指定的时间。",
        "completed_question": completed,
    }
    display = CompletedQuestionDisplay(
        **display_material,
        display_digest=contract_digest(display_material),
    )
    return ResolvedContextTurn(
        completed_question=completed,
        next_state=next_state,
        plan_state=None,
        display=display,
        bridge_route="V1_EXECUTION_ANCHOR_FOLLOWUP",
        anchor_update=ExecutionAnchorUpdate(
            previous=anchor,
            current_parse=parsed,
            time_context=time_context,
            resolved_business_domain_ids=anchor.resolved_business_domain_ids,
        ),
    )


def _execution_evidence_refs(response: AgentResponse) -> tuple[str, ...]:
    refs = tuple(sorted({
        f"{item.kind}:{item.source_ref}"
        for item in response.evidence
        if item.kind and item.source_ref
    }))
    if not any(item.kind == "QUERY_RESULT" for item in response.evidence):
        return ()
    return refs


def _finalize_execution_anchor(
    update: ExecutionAnchorUpdate,
    *,
    chat: ChatRequest,
    completed_question: str,
    response: AgentResponse,
    context: AuthorizedScopeContext,
    state_version: int,
    now: datetime,
) -> ExecutionBackedContextAnchor | None:
    evidence_refs = _execution_evidence_refs(response)
    if response.status != "COMPLETED" or response.error_code or not evidence_refs:
        return None
    previous = update.previous
    original_question = previous.original_question if previous else chat.question
    opaque_base = previous.opaque_base_question if previous else chat.question
    revision = previous.revision + 1 if previous else 1
    parent_anchor_id = previous.anchor_id if previous else None
    time_context = (
        update.time_context
        if update.time_context is not None
        else _initial_anchor_time_context(
            update.current_parse,
            question=chat.question,
            message_id=chat.message_id,
            now=now,
        )
    )
    material = {
        "parent_anchor_id": parent_anchor_id,
        "source_message_id": chat.message_id,
        "original_question": original_question,
        "opaque_base_question": opaque_base,
        "actual_execution_question": completed_question,
        "semantic_model_id": chat.semantic_model_id,
        "requested_business_domain_ids": list(chat.business_domain_ids),
        "resolved_business_domain_ids": list(update.resolved_business_domain_ids),
        "scope_fingerprint": context.fingerprint(),
        "state_version": state_version,
        "revision": revision,
        "time_context": time_context.model_dump(mode="json") if time_context else None,
        "confirmed_semantics": {
            "time": [time_context.applied_text] if time_context else [],
        },
        "current_turn_evidence_digest": contract_digest(
            update.current_parse.model_dump(mode="json")
        ),
        "v1_response_request_id": str(response.request_id),
        "execution_evidence_refs": list(evidence_refs),
        "created_at": now.isoformat(),
    }
    return ExecutionBackedContextAnchor(
        **material,
        anchor_id="execution-anchor:" + contract_digest(material)[:32],
    )


def validate_context_v1_settings(settings: Settings) -> dict[str, Any]:
    if settings.runtime_mode != "V2_CONTEXT_V1_EXECUTION":
        raise RuntimeError("V2_CONTEXT_V1_RUNTIME_NOT_SELECTED")
    if settings.session_store_mode != "redis" or not settings.effective_redis_url():
        raise RuntimeError("V2_CONTEXT_V1_REDIS_REQUIRED")
    if not settings.intent_model_enabled or not settings.intent_model_api_key:
        raise RuntimeError("V2_CONTEXT_V1_MODEL_REQUIRED")
    if settings.intent_model_enable_thinking is not False:
        raise RuntimeError("V2_CONTEXT_V1_THINKING_MUST_BE_DISABLED")
    if settings.intent_model_max_retries != 0:
        raise RuntimeError("V2_CONTEXT_V1_MODEL_RETRY_MUST_BE_ZERO")
    domains = settings.limited_scalar_business_domain_ids
    if (
        not domains
        or any(type(value) is not int or value <= 0 for value in domains)
        or len(domains) != len(set(domains))
        or len(domains) != 1
    ):
        raise RuntimeError("V2_CONTEXT_V1_EXPLICIT_SCOPE_REQUIRED")
    required = {
        "store_namespace": settings.limited_scalar_store_namespace,
        "deployment_id": settings.limited_scalar_deployment_id,
        "catalog_version": settings.limited_scalar_catalog_version,
        "vector_index_version": settings.limited_scalar_vector_index_version,
        "catalog_target_identity_hash": settings.limited_scalar_catalog_target_identity_hash,
        "oagnet_source_digest": settings.limited_scalar_oagnet_source_digest,
    }
    missing = sorted(name for name, value in required.items() if not str(value).strip())
    if missing:
        raise RuntimeError("V2_CONTEXT_V1_CONFIGURATION_MISSING:" + ",".join(missing))
    if ":v2-limited-scalar:" not in settings.limited_scalar_store_namespace:
        raise RuntimeError("V2_CONTEXT_V1_SOURCE_NAMESPACE_INVALID")
    actual_digest = source_bundle_digest(
        settings.limited_scalar_oagnet_root, OAGNET_RUNTIME_FILES
    )
    if actual_digest != settings.limited_scalar_oagnet_source_digest:
        raise RuntimeError("V2_CONTEXT_V1_OAGNET_SOURCE_DRIFT")
    return {
        "runtime_mode": settings.runtime_mode,
        "scope": {
            "semantic_model_id": settings.limited_scalar_semantic_model_id,
            "business_domain_ids": list(domains),
        },
        "catalog_version": settings.limited_scalar_catalog_version,
        "vector_index_version": settings.limited_scalar_vector_index_version,
        "oagnet_source_digest": actual_digest,
    }


def _resolve_context_v1_request_scope(
    chat: ChatRequest,
    deployment_scope: dict[str, Any],
) -> dict[str, Any]:
    """Resolve ingress scope without changing the caller's selection mode.

    The deployment domain list is the reviewed set currently owned by the
    semantic model. An omitted request list therefore selects MODEL_WIDE and
    resolves to that set; it is not rewritten into an explicit-domain grant.
    Legacy transport metadata such as ``department`` is deliberately absent
    from this decision.
    """
    expected_model = deployment_scope["semantic_model_id"]
    model_domains = list(deployment_scope["business_domain_ids"])
    requested = list(chat.business_domain_ids)
    if (
        chat.semantic_model_id != expected_model
        or len(requested) > 1
        or any(domain not in model_domains for domain in requested)
    ):
        raise ValueError("V2_CONTEXT_V1_SCOPE_PIN_MISMATCH")
    return {
        "semantic_model_id": expected_model,
        "requested_business_domain_ids": requested,
        "resolved_business_domain_ids": requested or model_domains,
        "selection_mode": (
            "EXPLICIT_DOMAINS" if requested else "MODEL_WIDE"
        ),
    }


def _build_context_dependencies(settings: Settings) -> ContextV1ExternalDependencies:
    _prepend_runtime_roots(settings)
    roots = {
        "catalog_publication": settings.limited_scalar_oagnet_root,
        "catalog_registry": settings.limited_scalar_oagnet_root,
        "catalog_store": settings.limited_scalar_oagnet_root,
        "vector_store": settings.limited_scalar_oagnet_root,
    }
    modules = {
        name: _import_runtime_module(name, root) for name, root in roots.items()
    }
    if settings.limited_scalar_catalog_access == "LIVE_READ_ONLY_SNAPSHOT":
        publication = _open_live_read_only_catalog(settings, modules)
    else:
        store = modules["catalog_store"].open_catalog_store(
            initialize=False,
            expected_target_identity_hash=(
                settings.limited_scalar_catalog_target_identity_hash
            ),
        )
        publication = modules["catalog_publication"].CatalogPublication(
            store,
            modules["catalog_registry"].RedisCatalogReleaseRegistry(
                store.catalog_target_identity
            ),
        )
    return ContextV1ExternalDependencies(
        publication=publication,
        model=RecognitionModelClient(settings),
        redis=Redis.from_url(
            settings.effective_redis_url(),
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=35,
        ),
        module_origins={
            name: str(Path(module.__file__).resolve())
            for name, module in modules.items()
        },
    )


class V2ContextV1ExecutionBridge:
    """Resolve one turn with V2, then invoke the original V1 workflow once."""

    def __init__(
        self,
        *,
        store: RedisScalarSessionStore,
        context_resolver: Callable[
            [ChatRequest, TrustedIdentity], AuthorizedScopeContext
        ],
        context_planner: Callable[
            [ChatRequest, TrustedIdentity, ScopedArtifact | None,
             tuple[ScopedArtifact, ...], ScopedArtifact | None],
            Awaitable[ResolvedContextTurn],
        ],
        v1_executor: Callable[
            [ChatRequest, TrustedIdentity], Awaitable[AgentResponse]
        ],
        clock: Callable[[], datetime],
        startup_receipt: dict[str, Any],
        readiness_probe: Callable[[], Awaitable[dict[str, bool]]],
    ):
        self.store = store
        self.context_resolver = context_resolver
        self.context_planner = context_planner
        self.v1_executor = v1_executor
        self.clock = clock
        self.startup_receipt = startup_receipt
        self._readiness_probe = readiness_probe
        self._locks: dict[str, asyncio.Lock] = {}
        self._readiness_lock = asyncio.Lock()
        self._readiness_cache: dict[str, bool] | None = None
        self._readiness_cached_at = 0.0

    @staticmethod
    def _identity(chat: ChatRequest, identity: TrustedIdentity) -> dict[str, str]:
        return {
            "conversation_id": chat.conversation_id,
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "application_id": chat.application_id,
        }

    @staticmethod
    def _fingerprint(chat: ChatRequest, identity: TrustedIdentity) -> str:
        return contract_digest({
            "chat": chat.model_dump(mode="json", exclude={"history"}),
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
        })

    @staticmethod
    def _pending(snapshot, context: AuthorizedScopeContext) -> ScopedArtifact | None:
        if snapshot.state is None:
            return None
        state = ConversationState.model_validate(snapshot.state.payload)
        active = state.pending
        if active is None or active.status != "ACTIVE":
            return None
        for record in reversed(list(snapshot.envelope.get("messages", {}).values())):
            raw = record.get("pending_state") if isinstance(record, dict) else None
            if raw is None:
                continue
            artifact = ScopedArtifact.model_validate(raw)
            if artifact.kind != "PENDING" or artifact.context != context:
                raise ValueError("PERSISTED_PENDING_SCOPE_MISMATCH")
            if isinstance(artifact.payload, dict) and artifact.payload.get("pending_id") == active.pending_id:
                return artifact
        raise ValueError("PERSISTED_PENDING_RESUME_MISSING")

    @staticmethod
    def _execution_anchor(
        snapshot,
        context: AuthorizedScopeContext,
    ) -> ExecutionBackedContextAnchor | None:
        """Return only the newest successful opaque-task anchor.

        A newer fallback NEW_TASK without a successful execution anchor clears
        the candidate. This preserves the existing barrier and prevents a
        short turn from attaching to an older task.
        """

        if snapshot.state is None:
            return None
        state = ConversationState.model_validate(snapshot.state.payload)
        if state.active_topic_id is not None:
            return None
        current: ExecutionBackedContextAnchor | None = None
        records = sorted(
            snapshot.envelope.get("messages", {}).values(),
            key=lambda item: (
                item.get("context_sequence", -1)
                if isinstance(item, dict) else -1
            ),
        )
        for record in records:
            if not isinstance(record, dict):
                continue
            route = record.get("bridge_route")
            raw = record.get("execution_anchor")
            if route == "V1_EXECUTION_FALLBACK_NEW_TASK":
                current = (
                    ExecutionBackedContextAnchor.model_validate(raw)
                    if raw is not None else None
                )
            elif route == "V1_EXECUTION_ANCHOR_FOLLOWUP" and raw is not None:
                candidate = ExecutionBackedContextAnchor.model_validate(raw)
                if current is None or candidate.parent_anchor_id != current.anchor_id:
                    raise ValueError("V1_EXECUTION_ANCHOR_CHAIN_MISMATCH")
                current = candidate
        if current is not None and current.scope_fingerprint != context.fingerprint():
            raise ValueError("V1_EXECUTION_ANCHOR_SCOPE_MISMATCH")
        return current

    @staticmethod
    def _terminal_response(
        chat: ChatRequest,
        *,
        status: str,
        answer: str,
        error_code: str | None = None,
        clarification_trace=None,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=uuid4(),
            conversation_id=chat.conversation_id,
            status=status,
            error_code=error_code,
            intent=PrimaryIntent.METRIC_QUERY,
            intent_source="V2_CONTEXT_ENGINE",
            intent_confidence=1.0,
            answer=answer,
            clarification_questions=(
                [answer] if status == "NEEDS_CLARIFICATION" else []
            ),
            clarification_decision_traces=(
                [clarification_trace] if clarification_trace is not None else []
            ),
            semantic_model_id=chat.semantic_model_id,
            database_id=chat.database_id,
            requested_business_domain_ids=list(chat.business_domain_ids),
            business_domain_selection_mode=(
                "EXPLICIT" if chat.business_domain_ids else "AUTO"
            ),
        )

    async def readiness(self) -> dict[str, bool]:
        now = time.monotonic()
        if self._readiness_cache is not None and now - self._readiness_cached_at < 5:
            return dict(self._readiness_cache)
        async with self._readiness_lock:
            checks = await self._readiness_probe()
            self._readiness_cache = {name: bool(value) for name, value in checks.items()}
            self._readiness_cached_at = time.monotonic()
            return dict(self._readiness_cache)

    async def _cached_or_conflict(self, snapshot, chat, fingerprint):
        prior = snapshot.message(chat.message_id)
        if prior is None:
            return None
        if prior.get("request_fingerprint") != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)
        if prior.get("status") == "SUCCEEDED" and prior.get("response"):
            return AgentResponse.model_validate(prior["response"])
        return self._terminal_response(
            chat,
            status="SAFE_FALLBACK",
            error_code="EXECUTION_OUTCOME_PENDING_REVIEW",
            answer="该请求已被受理，不会重复调用理解模型或查询链。",
        )

    async def check_message_conflict(
        self, chat: ChatRequest, identity: TrustedIdentity
    ) -> None:
        context = self.context_resolver(chat, identity)
        snapshot = await self.store.load(context, self._identity(chat, identity))
        fingerprint = self._fingerprint(chat, identity)
        prior = snapshot.message(chat.message_id)
        if prior is not None and prior.get("request_fingerprint") != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)
        guard = await self.store.idempotency_record(snapshot, chat.message_id)
        if guard is not None and guard.get("request_fingerprint") != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)

    async def handle(
        self, chat: ChatRequest, identity: TrustedIdentity
    ) -> AgentResponse:
        context = self.context_resolver(chat, identity)
        state_identity = self._identity(chat, identity)
        fingerprint = self._fingerprint(chat, identity)
        lock = self._locks.setdefault(
            contract_digest([context.fingerprint(), *state_identity.values()]),
            asyncio.Lock(),
        )
        async with lock:
            snapshot = await self.store.load(context, state_identity)
            cached = await self._cached_or_conflict(snapshot, chat, fingerprint)
            if cached is not None:
                return cached
            guard = await self.store.idempotency_record(snapshot, chat.message_id)
            if guard is not None:
                if guard.get("request_fingerprint") != fingerprint:
                    raise MessageIdReuseConflictError(chat.message_id)
                return self._terminal_response(
                    chat,
                    status="SAFE_FALLBACK",
                    error_code="EXECUTION_SESSION_EXPIRED_REUSE_REJECTED",
                    answer="原会话状态已过期，请使用新的消息标识重新提出完整问题。",
                )
            pending = self._pending(snapshot, context)
            context_chat = chat.model_copy(update={"history": []})
            try:
                resolved = await self.context_planner(
                    context_chat,
                    identity,
                    snapshot.state,
                    snapshot.plans,
                    pending,
                )
            except ContextProposalFailure as exc:
                anchor = self._execution_anchor(snapshot, context)
                raw_parse = getattr(exc, "current_turn_parse", None)
                resolved = None
                anchor_failure = None
                if (
                    anchor is not None
                    and snapshot.state is not None
                    and raw_parse is not None
                    and exc.context_status != "AMBIGUOUS"
                ):
                    try:
                        resolved = _resolve_execution_anchor_followup(
                            context_chat,
                            snapshot.state,
                            anchor,
                            CurrentTurnSemanticParse.model_validate(raw_parse),
                            context,
                            self.clock(),
                        )
                    except (RecognitionFailure, ValueError) as anchor_exc:
                        anchor_failure = anchor_exc
                if resolved is not None:
                    logger.info(
                        "V2 context resolved a constrained execution-backed followup",
                        extra={
                            "message_id": chat.message_id,
                            "anchor_id": anchor.anchor_id if anchor else None,
                            "bridge_route": resolved.bridge_route,
                        },
                    )
                elif anchor is not None and anchor_failure is not None:
                    response = self._terminal_response(
                        chat,
                        status="NEEDS_CLARIFICATION",
                        error_code=str(anchor_failure),
                        answer="当前修改无法在上一轮执行问题上安全补全，请提供完整问题。",
                    )
                elif anchor is not None and exc.context_status != "AMBIGUOUS":
                    response = self._terminal_response(
                        chat,
                        status="SAFE_FALLBACK",
                        error_code="CONTEXT_UNSUPPORTED_FOR_FALLBACK_TASK",
                        answer="上一任务仅由原执行链处理，当前短追问缺少可安全继承的 V2 任务状态。",
                    )
                elif exc.context_status == "AMBIGUOUS":
                    response = self._terminal_response(
                        chat,
                        status="NEEDS_CLARIFICATION",
                        error_code="V2_CONTEXT_AMBIGUOUS",
                        answer="请明确您要继续哪个已有任务。",
                    )
                else:
                    response = self._terminal_response(
                        chat,
                        status="SAFE_FALLBACK",
                        error_code="V2_CONTEXT_UNRESOLVED",
                        answer="当前追问无法安全确定所引用的任务，请补充完整问题。",
                    )
                if resolved is None:
                    return await self._save_context_response(
                        snapshot, chat, identity, context, fingerprint,
                        ResolvedContextTurn(None, None, None), response,
                        v1_execution_called=False,
                    )
            except RecognitionFailure as exc:
                response = self._terminal_response(
                    chat,
                    status="SAFE_FALLBACK",
                    error_code=str(exc),
                    answer="V2 上下文理解未能形成可验证的完整问题。",
                )
                return await self._save_context_response(
                    snapshot, chat, identity, context, fingerprint,
                    ResolvedContextTurn(None, None, None), response,
                    v1_execution_called=False,
                )

            if resolved.clarification_question is not None:
                response = self._terminal_response(
                    chat,
                    status="NEEDS_CLARIFICATION",
                    answer=resolved.clarification_question,
                    clarification_trace=resolved.clarification_trace,
                )
                return await self._save_context_response(
                    snapshot, chat, identity, context, fingerprint,
                    resolved, response, v1_execution_called=False,
                )
            if not resolved.completed_question:
                raise ValueError("V2_COMPLETED_QUESTION_REQUIRED")
            progress = _completed_question_step(resolved)
            if progress is not None:
                await emit_progress(
                    "INTENT_RECOGNITION",
                    "COMPLETED",
                    progress.summary,
                    completed_question_digest=(
                        resolved.display.display_digest
                        if resolved.display is not None else None
                    ),
                )
            running = await self.store.begin_context(
                snapshot,
                planned_state=resolved.next_state,
                plan_state=resolved.plan_state,
                pending_state=resolved.pending_state,
                message_id=chat.message_id,
                request_fingerprint=fingerprint,
                context=context,
                state_identity=state_identity,
                bridge_route=resolved.bridge_route,
                started_at=self.clock(),
            )
            execution_chat = chat.model_copy(update={
                "question": resolved.completed_question,
                "history": [],
            })
            execution_chat._completed_question_execution = True
            try:
                response = await self.v1_executor(execution_chat, identity)
                response = _attach_completed_question(response, resolved)
            except Exception:
                try:
                    await self.store.mark_unknown(
                        running,
                        message_id=chat.message_id,
                        request_fingerprint=fingerprint,
                        reason_code="V1_EXECUTION_OUTCOME_UNKNOWN",
                        context=context,
                        state_identity=state_identity,
                        observed_at=self.clock(),
                    )
                except (StateTransitionError, ValueError):
                    pass
                raise
            execution_anchor = (
                _finalize_execution_anchor(
                    resolved.anchor_update,
                    chat=chat,
                    completed_question=resolved.completed_question,
                    response=response,
                    context=context,
                    state_version=running.state_version,
                    now=self.clock(),
                )
                if resolved.anchor_update is not None else None
            )
            await self.store.finish_context(
                running,
                message_id=chat.message_id,
                request_fingerprint=fingerprint,
                response=response,
                context=context,
                state_identity=state_identity,
                v1_execution_called=True,
                execution_anchor=(
                    execution_anchor.model_dump(mode="json")
                    if execution_anchor is not None else None
                ),
            )
            return response

    async def _save_context_response(
        self,
        snapshot,
        chat,
        identity,
        context,
        fingerprint,
        resolved,
        response,
        *,
        v1_execution_called,
    ):
        running = await self.store.begin_context(
            snapshot,
            planned_state=resolved.next_state,
            plan_state=resolved.plan_state,
            pending_state=resolved.pending_state,
            message_id=chat.message_id,
            request_fingerprint=fingerprint,
            context=context,
            state_identity=self._identity(chat, identity),
            bridge_route=resolved.bridge_route,
            started_at=self.clock(),
        )
        await self.store.finish_context(
            running,
            message_id=chat.message_id,
            request_fingerprint=fingerprint,
            response=response,
            context=context,
            state_identity=self._identity(chat, identity),
            v1_execution_called=v1_execution_called,
        )
        return response

    async def aclose(self):
        await self.store.aclose()


def build_context_v1_execution_handler(
    settings: Settings,
    *,
    v1_workflow,
    external: ContextV1ExternalDependencies | None = None,
) -> V2ContextV1ExecutionBridge:
    receipt = validate_context_v1_settings(settings)
    dependencies = external or _build_context_dependencies(settings)
    expected_scope = receipt["scope"]

    pin = dependencies.publication.pin(
        expected_scope["semantic_model_id"],
        expected_scope["business_domain_ids"],
    )
    try:
        catalog_identity = pin.identity
        if (
            catalog_identity.get("catalog_version") != receipt["catalog_version"]
            or catalog_identity.get("vector_index_version")
                != receipt["vector_index_version"]
            or catalog_identity.get("target_identity_hash")
                != settings.limited_scalar_catalog_target_identity_hash
        ):
            raise RuntimeError("V2_CONTEXT_V1_CATALOG_PIN_MISMATCH")
    finally:
        pin.finish()
    model_wide_domains = list(expected_scope["business_domain_ids"])

    def require_scope(chat: ChatRequest) -> dict[str, Any]:
        return _resolve_context_v1_request_scope(chat, expected_scope)

    def context_resolver(chat: ChatRequest, trusted: TrustedIdentity):
        resolved_scope = require_scope(chat)
        session = ScopedPlanSession(
            chat,
            trusted,
            dependencies.publication,
            resolved_business_domain_ids=resolved_scope[
                "resolved_business_domain_ids"
            ],
        )
        context = session.context
        if (
            context.catalog_pin.catalog_version
                != catalog_identity["catalog_version"]
            or context.catalog_pin.vector_index_version
                != catalog_identity["vector_index_version"]
            or context.catalog_pin.target_identity_hash
                != catalog_identity["target_identity_hash"]
        ):
            raise ValueError("V2_CONTEXT_V1_SCOPE_PIN_MISMATCH")
        session.accept_catalog()
        return context

    engine = RawTurnPlanner(
        dependencies.model,
        dependencies.publication,
        clock=lambda: datetime.now(timezone.utc).astimezone(),
    )

    async def context_planner(chat, trusted, state, plans, pending):
        resolved_scope = require_scope(chat)
        previous = ConversationState.model_validate(state.payload) if state else None
        result = await engine.run(
            chat,
            trusted,
            state=state,
            plans=plans,
            pending=pending,
            allow_standalone_new_task_passthrough=True,
            resolved_business_domain_ids=resolved_scope[
                "resolved_business_domain_ids"
            ],
        )
        if isinstance(result, RecognizedStandaloneNewTask):
            logger.info(
                "V2 context handed standalone new task to V1 execution",
                extra={
                    "message_id": chat.message_id,
                    "bridge_route": result.execution_route,
                    "v2_best_effort_failure": result.fallback_reason,
                },
            )
            return ResolvedContextTurn(
                completed_question=result.completed_question,
                next_state=result.next_state,
                plan_state=None,
                bridge_route=result.execution_route,
                anchor_update=ExecutionAnchorUpdate(
                    previous=None,
                    current_parse=result.parse,
                    time_context=None,
                    resolved_business_domain_ids=tuple(
                        resolved_scope["resolved_business_domain_ids"]
                    ),
                ),
            )
        if isinstance(result, RecognizedClarification):
            return ResolvedContextTurn(
                completed_question=None,
                next_state=result.next_state,
                plan_state=None,
                pending_state=result.pending_state,
                clarification_question=result.question,
                clarification_trace=result.trace,
                bridge_route="V2_USER_AMBIGUITY",
            )
        session = ScopedPlanSession(
            chat,
            trusted,
            dependencies.publication,
            resolved_business_domain_ids=resolved_scope[
                "resolved_business_domain_ids"
            ],
        )
        session.restore(result.next_state, kind="CONVERSATION")
        plan = AuthorizedLogicalPlan.model_validate(
            session.restore(result.plan_state, kind="LAST_REQUEST")
        )
        session.accept_catalog()
        next_state = ConversationState.model_validate(result.next_state.payload)
        display = build_completed_question_display(
            message_id=chat.message_id,
            plan=plan,
            previous_state=previous,
            next_state=next_state,
            context_trace=result.context_trace,
        )
        display = _standalone_execution_display(chat.question, display)
        return ResolvedContextTurn(
            completed_question=display.completed_question,
            next_state=result.next_state,
            plan_state=result.plan_state,
            display=display,
        )

    async def v1_executor(chat: ChatRequest, trusted: TrustedIdentity):
        outcome = await v1_workflow.ainvoke({"chat": chat, "identity": trusted})
        return AgentResponse.model_validate(outcome["response"])

    async def readiness_probe():
        try:
            redis_ready = bool(await dependencies.redis.ping())
        except Exception:
            redis_ready = False

        def verify_catalog():
            current = dependencies.publication.pin(
                expected_scope["semantic_model_id"],
                expected_scope["business_domain_ids"],
            )
            try:
                current_identity = current.identity
                return all(
                    current_identity.get(field) == catalog_identity.get(field)
                    for field in (
                        "catalog_version",
                        "vector_index_version",
                        "target_identity_hash",
                    )
                )
            finally:
                current.finish()

        try:
            catalog_ready = bool(await asyncio.to_thread(verify_catalog))
        except Exception:
            catalog_ready = False
        return {
            "v2_context_v1_redis": redis_ready,
            "v2_context_v1_catalog_pin": catalog_ready,
            "v1_execution_bridge": True,
            "v2_execution_transport_disabled": True,
        }

    namespace = settings.limited_scalar_store_namespace.replace(
        ":v2-limited-scalar:", ":v2-context-v1-execution:", 1
    )
    store = RedisScalarSessionStore(
        dependencies.redis,
        prefix=namespace,
        deployment_id=settings.limited_scalar_deployment_id,
        ttl_seconds=settings.limited_scalar_session_ttl_seconds,
        idempotency_ttl_seconds=settings.limited_scalar_idempotency_ttl_seconds,
        max_messages=settings.limited_scalar_max_messages_per_session,
        max_envelope_bytes=settings.limited_scalar_max_envelope_bytes,
        production_prefix=settings.session_key_prefix,
    )
    startup_receipt = {
        **receipt,
        "catalog_pin_verified": True,
        "v1_execution_bridge_enabled": True,
        "v2_limited_scalar_used_for_execution": False,
        "store_schema": store.schema_version,
        "store_namespace_mode": store.namespace_mode,
        "runtime_module_origins_verified": external is None,
        "scope_contract": {
            "model_wide": {
                "selection_mode": "MODEL_WIDE",
                "requested_business_domain_ids": [],
                "resolved_business_domain_ids": model_wide_domains,
            },
            "explicit": {
                "selection_mode": "EXPLICIT_DOMAINS",
                "requested_business_domain_ids": list(
                    expected_scope["business_domain_ids"]
                ),
                "resolved_business_domain_ids": list(
                    expected_scope["business_domain_ids"]
                ),
            },
        },
    }
    return V2ContextV1ExecutionBridge(
        store=store,
        context_resolver=context_resolver,
        context_planner=context_planner,
        v1_executor=v1_executor,
        clock=lambda: datetime.now(timezone.utc).astimezone(),
        startup_receipt=startup_receipt,
        readiness_probe=readiness_probe,
    )
