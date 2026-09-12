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
from typing import Any, Awaitable, Callable
from uuid import uuid4

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
from .limited_scalar_runtime import (
    OAGNET_RUNTIME_FILES,
    _import_runtime_module,
    _open_live_read_only_catalog,
    _prepend_runtime_roots,
    source_bundle_digest,
)
from .pending_recognition import RecognizedClarification
from .persisted_scalar_api import RedisScalarSessionStore
from .pipeline import AuthorizedLogicalPlan
from .recognition import RawTurnPlanner, RecognizedStandaloneNewTask
from .recognition_client import RecognitionFailure, RecognitionModelClient
from .state_machine import ConversationState, StateTransitionError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextV1ExternalDependencies:
    publication: Any
    model: Any
    redis: Any
    module_origins: dict[str, str] = field(default_factory=dict)


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
                anchor = any(
                    isinstance(record, dict)
                    and record.get("bridge_route")
                        == "V1_EXECUTION_FALLBACK_NEW_TASK"
                    for record in snapshot.envelope.get("messages", {}).values()
                )
                if anchor and exc.context_status != "AMBIGUOUS":
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
            await self.store.finish_context(
                running,
                message_id=chat.message_id,
                request_fingerprint=fingerprint,
                response=response,
                context=context,
                state_identity=state_identity,
                v1_execution_called=True,
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
    identity = pin.identity
    if (
        identity.get("catalog_version") != receipt["catalog_version"]
        or identity.get("vector_index_version") != receipt["vector_index_version"]
        or identity.get("target_identity_hash")
            != settings.limited_scalar_catalog_target_identity_hash
    ):
        raise RuntimeError("V2_CONTEXT_V1_CATALOG_PIN_MISMATCH")
    pin.finish()

    def require_scope(chat: ChatRequest) -> None:
        if (
            chat.semantic_model_id != expected_scope["semantic_model_id"]
            or list(chat.business_domain_ids)
                != expected_scope["business_domain_ids"]
        ):
            raise ValueError("V2_CONTEXT_V1_SCOPE_PIN_MISMATCH")

    def context_resolver(chat: ChatRequest, trusted: TrustedIdentity):
        require_scope(chat)
        session = ScopedPlanSession(chat, trusted, dependencies.publication)
        context = session.context
        if (
            context.catalog_pin.catalog_version != receipt["catalog_version"]
            or context.catalog_pin.vector_index_version
                != receipt["vector_index_version"]
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
        require_scope(chat)
        previous = ConversationState.model_validate(state.payload) if state else None
        result = await engine.run(
            chat,
            trusted,
            state=state,
            plans=plans,
            pending=pending,
            allow_standalone_new_task_passthrough=True,
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
        session = ScopedPlanSession(chat, trusted, dependencies.publication)
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
                return bool(
                    current_identity.get("catalog_version")
                        == receipt["catalog_version"]
                    and current_identity.get("vector_index_version")
                        == receipt["vector_index_version"]
                    and current_identity.get("target_identity_hash")
                        == settings.limited_scalar_catalog_target_identity_hash
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
