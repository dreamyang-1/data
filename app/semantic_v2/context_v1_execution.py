"""Thin V2-context to original-V1-execution bridge.

V2 resolves relation, TaskState and the completed question.  The bridge then
changes only ``question``, clears transport history, marks the request as
pre-resolved context and invokes the original V1 execution path.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Awaitable, Callable
from uuid import uuid4

from redis.asyncio import Redis

from app.config import Settings
from app.domain.models import (
    AgentResponse,
    AnalysisProcessStep,
    CanonicalAnalysisRequest,
    ChatRequest,
    PrimaryIntent,
    ReliabilityReport,
    SemanticFilterBinding,
    TrustedIdentity,
)
from app.services.progress import emit_progress
from app.stores import MessageIdReuseConflictError

from .authorized_contract import (
    ScopedArtifact,
    SourceValueBindingEvidence,
    contract_digest,
)
from .catalog_bridge import ScopedPlanSession
from .completed_question import (
    CompletedQuestionDisplay,
    build_completed_question_display,
)
from .context_proposal import ContextProposalFailure
from .context_question import (
    build_result_availability_question,
    build_context_question,
    canonical_matches_execution,
    has_active_context_question,
    is_contextual_short_edit,
    is_self_contained_execution_question,
    publish_context_task,
    refine_context_question_relation_actor,
    resolve_context_question_followup,
    resolve_context_references,
)
from .context_state_store import ContextStateSnapshot, RedisContextStateStore
from .current_catalog import CurrentAuthorizedCatalog
from .enums import CatalogType
from .models import BoundSemanticRef
from .pending_recognition import RecognizedClarification
from .pipeline import AuthorizedLogicalPlan, collect_bound_refs
from .recognition import RawTurnPlanner, RecognizedStandaloneNewTask
from .recognition_client import RecognitionFailure, RecognitionModelClient
from .state_machine import ConversationState, StateTransitionError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextV1ExternalDependencies:
    catalog: Any
    model: Any
    redis: Any


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
    understanding: str | None = None
    standalone_parse: Any = None
    fallback_reason: str | None = None
    publish_context_from_v1: bool = False
    source_question: str | None = None


def _standalone_execution_display(
    question: str, display: CompletedQuestionDisplay
) -> CompletedQuestionDisplay:
    """A self-contained NEW_TASK reaches V1 with the caller's original text."""
    if display.relation != "NEW_TASK":
        return display
    material = display.model_dump(mode="python", exclude={"display_digest"})
    material["completed_question"] = question
    return CompletedQuestionDisplay(
        **material, display_digest=contract_digest(material)
    )


def _completed_question_step(
    resolved: ResolvedContextTurn,
) -> AnalysisProcessStep | None:
    if not resolved.completed_question:
        return None
    summary = resolved.display.public_message if resolved.display is not None else (
        f"本轮理解：{resolved.understanding}\n补全后的完整问题：{resolved.completed_question}"
        if resolved.understanding
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
    response: AgentResponse, resolved: ResolvedContextTurn
) -> AgentResponse:
    step = _completed_question_step(resolved)
    if step is None:
        return response
    steps = list(response.analysis_process)
    index = next(
        (
            position
            for position, item in enumerate(steps)
            if item.stage == "QUESTION_REWRITE"
        ),
        None,
    )
    if index is None:
        steps.insert(0, step)
    else:
        steps[index] = step
    response.analysis_process = steps[:20]
    return response


def _binding_key(ref: BoundSemanticRef) -> str:
    return contract_digest(ref.model_dump(mode="json"))


def _source_proof_index(
    artifacts: tuple[ScopedArtifact | None, ...],
) -> dict[str, SourceValueBindingEvidence]:
    return {
        _binding_key(proof.ref): proof
        for artifact in artifacts
        if artifact is not None
        for proof in artifact.source_value_bindings
    }


def _revalidate_context_artifacts(
    chat: ChatRequest,
    identity: TrustedIdentity,
    catalog,
    *,
    state: ScopedArtifact | None,
    plans: tuple[ScopedArtifact, ...],
    pending: ScopedArtifact | None,
    resolved_business_domain_ids=None,
) -> tuple[
    ScopedArtifact | None,
    tuple[ScopedArtifact, ...],
    ScopedArtifact | None,
    dict[str, Any],
]:
    """Revalidate stored bindings against this request's current catalog.

    This reuses the stable conversation state only after every stored binding
    is found in the current request catalog. Missing or changed bindings fail.
    """
    session = ScopedPlanSession(
        chat,
        identity,
        catalog,
        resolved_business_domain_ids=resolved_business_domain_ids,
    )
    provenance = session.context.catalog_pin.model_dump(mode="json")
    artifacts = (state, *plans, pending)
    if state is None:
        session.accept_catalog()
        return None, (), None, provenance
    if all(
        artifact is None or artifact.context == session.context
        for artifact in artifacts
    ):
        session.accept_catalog()
        return state, plans, pending, provenance

    source_proofs = _source_proof_index(artifacts)
    rebound: dict[str, BoundSemanticRef] = {}
    candidates: dict[CatalogType, list[dict[str, Any]]] = {}

    def find_candidate(ref: BoundSemanticRef) -> dict[str, Any]:
        available = candidates.setdefault(
            ref.catalog_type, session.candidates(ref.catalog_type)
        )
        matches = [
            item
            for item in available
            if session._rows[item["candidate_id"]].metadata[
                "catalog_logical_id"
            ]
            == ref.canonical_id
        ]
        if len(matches) != 1:
            raise ValueError("V2_CONTEXT_CURRENT_BINDING_NOT_FOUND")
        return matches[0]

    def rebind_ref(ref: BoundSemanticRef) -> BoundSemanticRef:
        key = _binding_key(ref)
        if key in rebound:
            return rebound[key]
        if ref.resolution_source == "VERIFIED_SOURCE_EXACT_LOOKUP":
            proof = source_proofs.get(key)
            if proof is None:
                raise ValueError("V2_CONTEXT_SOURCE_VALUE_PROOF_MISSING")
            field_candidate = find_candidate(proof.field_ref)
            offered = session.lookup_source_values(
                field_candidate["candidate_id"], proof.canonical_value
            )
            match = next(
                (
                    item
                    for item in offered
                    if item["display_name"] == proof.canonical_value
                ),
                None,
            )
            if match is None:
                raise ValueError("V2_CONTEXT_SOURCE_VALUE_NOT_CURRENT")
            current = session.bind(
                match["candidate_id"],
                ref.semantic_role,
                ref.source_mention_ids,
            )
        else:
            candidate = find_candidate(ref)
            current = session.bind(
                candidate["candidate_id"],
                ref.semantic_role,
                ref.source_mention_ids,
            )
        rebound[key] = current
        return current

    required = {
        "canonical_id",
        "catalog_type",
        "semantic_role",
        "semantic_model_id",
        "catalog_version",
        "business_domain_ids",
    }

    def replace(value):
        if isinstance(value, dict):
            if required <= set(value):
                return rebind_ref(BoundSemanticRef.model_validate(value)).model_dump(
                    mode="json"
                )
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, tuple):
            return [replace(item) for item in value]
        return deepcopy(value)

    rebound_plans: list[AuthorizedLogicalPlan] = []
    for artifact in plans:
        old = AuthorizedLogicalPlan.model_validate(artifact.payload)
        data = replace(old.model_dump(mode="json"))
        payload = data["payload"]
        proofs = session._require_refs(collect_bound_refs(payload))
        data.update(
            permission_requirement=session.context.model_dump(mode="json"),
            snapshot_requirement=session._snapshot.model_dump(mode="json"),
            permission_proofs=[
                proof.model_dump(mode="json") for proof in proofs
            ],
        )
        rebound_plans.append(AuthorizedLogicalPlan.model_validate(data))

    state_payload = replace(state.payload)
    pending_payload = replace(pending.payload) if pending is not None else None
    session.accept_catalog()
    rebound_state = session.seal(kind="CONVERSATION", payload=state_payload)
    rebound_plan_artifacts = tuple(
        session.seal(kind="LAST_REQUEST", payload=plan)
        for plan in rebound_plans
    )
    rebound_pending = (
        session.seal(kind="PENDING", payload=pending_payload)
        if pending_payload is not None
        else None
    )
    return rebound_state, rebound_plan_artifacts, rebound_pending, provenance


def _plans_for_active_task_versions(
    state: ScopedArtifact | None,
    plans: tuple[ScopedArtifact, ...],
) -> tuple[ScopedArtifact, ...]:
    """Drop obsolete plan snapshots before context discovery.

    Stable conversation state may already contain a context-only TaskVersion
    produced by an older bridge process while Redis still holds the preceding
    V2 plan.  A plan is usable only for the exact active version and plan id it
    was created for.  Ignoring obsolete evidence is safe; treating it as the
    current plan is not.
    """

    if state is None:
        return ()
    current = ConversationState.model_validate(state.payload)
    usable: list[ScopedArtifact] = []
    for artifact in plans:
        plan = AuthorizedLogicalPlan.model_validate(artifact.payload)
        task = current.tasks.get(plan.task_id)
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
            active is not None
            and plan.task_version == active.version
            and active.plan_id == plan.plan_id
        ):
            usable.append(artifact)
    return tuple(usable)


class V2ContextV1ExecutionBridge:
    """Resolve context once, then call the original V1 execution once."""

    uses_v1_ingress = True
    uses_v1_execution = True

    def __init__(
        self,
        *,
        store: RedisContextStateStore,
        catalog: Any,
        model: Any,
        v1_executor: Callable[
            [ChatRequest, TrustedIdentity], Awaitable[AgentResponse]
        ],
        clock: Callable[[], datetime],
        startup_receipt: dict[str, Any],
        v1_context_reader: Callable[
            [ChatRequest, TrustedIdentity],
            Awaitable[CanonicalAnalysisRequest | None],
        ] | None = None,
        v1_context_value_resolver: Callable[
            [ChatRequest, TrustedIdentity, str, str, str | None],
            Awaitable[SemanticFilterBinding | None],
        ] | None = None,
        v1_pending_answer_probe: Callable[
            [ChatRequest, TrustedIdentity], Awaitable[bool]
        ] | None = None,
        v1_pending_executor: Callable[
            [ChatRequest, TrustedIdentity], Awaitable[AgentResponse]
        ] | None = None,
        demo_mode: bool = False,
    ):
        self.store = store
        self.catalog = catalog
        self.model = model
        self.v1_executor = v1_executor
        self.v1_context_reader = v1_context_reader
        self.v1_context_value_resolver = v1_context_value_resolver
        self.v1_pending_answer_probe = v1_pending_answer_probe
        self.v1_pending_executor = v1_pending_executor
        self.clock = clock
        self.startup_receipt = dict(startup_receipt)
        self.demo_mode = demo_mode
        self._locks: dict[str, asyncio.Lock] = {}

    async def _retry_for_result_availability(
        self,
        *,
        chat: ChatRequest,
        identity: TrustedIdentity,
        resolved: ResolvedContextTurn,
        response: AgentResponse,
        prior_state: ScopedArtifact | None = None,
    ) -> AgentResponse:
        if (
            not self.demo_mode
            or response.status == "COMPLETED"
            or response.error_code is None
        ):
            return response
        failed_capability = (
            getattr(response, "_upstream_error_code", None)
            or response.error_code
        )
        fallback_question = build_result_availability_question(
            resolved.next_state,
            failed_capability=failed_capability,
            completed_question=resolved.completed_question,
            prior_state_artifact=(
                prior_state
                if resolved.bridge_route.startswith("V2_CONTEXT_")
                else None
            ),
        )
        if not fallback_question or fallback_question == resolved.completed_question:
            return response
        retry_id = "v2-result-retry-" + contract_digest({
            "conversation_id": chat.conversation_id,
            "message_id": chat.message_id,
            "question": fallback_question,
        })[:24]
        retry_chat = chat.model_copy(
            deep=True,
            update={
                "message_id": retry_id,
                "question": fallback_question,
                "history": [],
            },
        )
        retry_chat._completed_question_execution = True
        retried = await self.v1_executor(retry_chat, identity)
        if not (
            retried.status == "COMPLETED"
            and retried.error_code is None
            and any(item.kind == "QUERY_RESULT" for item in retried.evidence)
        ):
            return response
        logger.warning(
            "demo read-only result availability retry succeeded: "
            "conversation_id=%s message_id=%s failed_capability=%s "
            "completed_question_digest=%s fallback_question_digest=%s",
            chat.conversation_id,
            chat.message_id,
            failed_capability,
            contract_digest(resolved.completed_question),
            contract_digest(fallback_question),
        )
        retried._demo_result_availability_retry = {
            "failed_capability": failed_capability,
            "fallback_question_digest": contract_digest(fallback_question),
        }
        return retried

    @staticmethod
    def _response(
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
            semantic_model_id=chat.semantic_model_id,
            database_id=chat.database_id,
            requested_business_domain_ids=list(chat.business_domain_ids),
            business_domain_selection_mode=(
                "EXPLICIT" if chat.business_domain_ids else "AUTO"
            ),
            clarification_questions=(
                [answer] if status == "NEEDS_CLARIFICATION" else []
            ),
            clarification_decision_traces=(
                [clarification_trace] if clarification_trace is not None else []
            ),
            reliability=ReliabilityReport(
                level="FAIL",
                score=0,
                gates={"context_resolved": False, "safe_termination": True},
                warnings=[answer],
            ),
        )

    async def readiness(self) -> dict[str, bool]:
        try:
            redis_ready = bool(await self.store.redis.ping())
        except Exception:
            redis_ready = False
        return {
            "v2_context_state_store": redis_ready,
            "v2_context_current_catalog_on_request": True,
            "v1_execution_bridge": True,
        }

    async def check_message_conflict(
        self, chat: ChatRequest, identity: TrustedIdentity
    ) -> None:
        snapshot = await self.store.load(chat, identity)
        fingerprint = self.store.request_fingerprint(chat, identity)
        prior = snapshot.message(chat.message_id)
        if prior is not None and prior["request_fingerprint"] != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)
        guard = await self.store.idempotency_record(snapshot, chat.message_id)
        if guard is not None and guard["request_fingerprint"] != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)

    async def _request_catalog(self, chat: ChatRequest):
        factory = getattr(self.catalog, "for_request", None)
        if callable(factory):
            return await asyncio.to_thread(
                factory,
                chat.semantic_model_id,
                tuple(chat.business_domain_ids),
            )
        return self.catalog

    async def _resolve(
        self,
        chat: ChatRequest,
        identity: TrustedIdentity,
        snapshot: ContextStateSnapshot,
        catalog,
    ) -> tuple[ResolvedContextTurn, dict[str, Any]]:
        current_plans = _plans_for_active_task_versions(
            snapshot.state, snapshot.plans
        )
        resolved_business_domain_ids = getattr(
            catalog, "resolved_business_domain_ids", None
        )
        state, plans, pending, provenance = _revalidate_context_artifacts(
            chat,
            identity,
            catalog,
            state=snapshot.state,
            plans=current_plans,
            pending=snapshot.pending,
            resolved_business_domain_ids=resolved_business_domain_ids,
        )
        previous = (
            ConversationState.model_validate(state.payload)
            if state is not None
            else None
        )
        async def resolve_value(
            surface: str,
            expected_family: str,
            preferred_attribute_code: str | None,
        ) -> SemanticFilterBinding | None:
            if self.v1_context_value_resolver is None:
                return None
            return await self.v1_context_value_resolver(
                chat,
                identity,
                surface,
                expected_family,
                preferred_attribute_code,
            )

        context_resolution = await resolve_context_question_followup(
            chat=chat,
            identity=identity,
            state_artifact=state,
            catalog=catalog,
            resolved_business_domain_ids=resolved_business_domain_ids,
            now=self.clock(),
            resolve_value=resolve_value,
        )
        if context_resolution is not None:
            return (
                ResolvedContextTurn(
                    completed_question=context_resolution.completed_question,
                    next_state=context_resolution.next_state,
                    plan_state=None,
                    bridge_route="V2_CONTEXT_QUESTION_COMPLETED",
                    understanding=context_resolution.understanding,
                ),
                provenance,
            )
        reference_resolution = resolve_context_references(
            chat=chat,
            identity=identity,
            state_artifact=state,
            catalog=catalog,
            resolved_business_domain_ids=resolved_business_domain_ids,
            now=self.clock(),
        )
        if reference_resolution is not None:
            if reference_resolution.clarification_question is not None:
                return (
                    ResolvedContextTurn(
                        completed_question=None,
                        next_state=None,
                        plan_state=None,
                        clarification_question=(
                            reference_resolution.clarification_question
                        ),
                        bridge_route="V2_CONTEXT_REFERENCE_AMBIGUOUS",
                    ),
                    provenance,
                )
            return (
                ResolvedContextTurn(
                    completed_question=reference_resolution.completed_question,
                    next_state=reference_resolution.next_state,
                    plan_state=None,
                    bridge_route="V2_CONTEXT_REFERENCE_COMPLETED",
                    understanding=reference_resolution.understanding,
                    publish_context_from_v1=False,
                    source_question=chat.question,
                ),
                provenance,
            )
        if has_active_context_question(state) and is_contextual_short_edit(
            chat.question
        ):
            # A specific structural follow-up such as “最低的省份呢” also
            # matches the broad ``X呢`` surface grammar. It must reach the
            # reference resolver above before unresolved entity replacement is
            # classified as ambiguous.
            return (
                ResolvedContextTurn(
                    completed_question=None,
                    next_state=None,
                    plan_state=None,
                    clarification_question=(
                        "当前替换值无法唯一对应上一任务中的可编辑条件，请补充要替换的条件。"
                    ),
                    bridge_route="V2_CONTEXT_QUESTION_AMBIGUOUS",
                ),
                provenance,
            )
        engine = RawTurnPlanner(
            self.model, self.catalog if catalog is None else catalog, clock=self.clock
        )
        try:
            result = await engine.run(
                chat,
                identity,
                state=state,
                plans=plans,
                pending=pending,
                allow_standalone_new_task_passthrough=True,
                resolved_business_domain_ids=resolved_business_domain_ids,
            )
        except (RecognitionFailure, ValueError) as exc:
            if not (
                is_self_contained_execution_question(chat.question)
                and RawTurnPlanner._standalone_fallback_allows(exc)
            ):
                raise
            fallback_session = ScopedPlanSession(
                chat,
                identity,
                catalog,
                resolved_business_domain_ids=resolved_business_domain_ids,
            )
            current = (
                ConversationState.model_validate(
                    fallback_session.restore(
                        state, kind="CONVERSATION", defer_source_values=True
                    )
                )
                if state is not None
                else ConversationState(
                    conversation_id=chat.conversation_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    application_id=chat.application_id,
                    state_version=0,
                )
            )
            barrier = RawTurnPlanner._standalone_new_task_barrier(
                current, chat.message_id
            )
            fallback_session.accept_catalog()
            logger.info(
                "V2 complete current question falling back to original V1 execution",
                extra={
                    "message_id": chat.message_id,
                    "fallback_reason": str(exc),
                },
            )
            return (
                ResolvedContextTurn(
                    completed_question=chat.question,
                    next_state=fallback_session.seal(
                        kind="CONVERSATION", payload=barrier
                    ),
                    plan_state=None,
                    bridge_route="V1_EXECUTION_FALLBACK_NEW_TASK",
                    fallback_reason=str(exc),
                    publish_context_from_v1=True,
                    source_question=chat.question,
                ),
                provenance,
            )
        if isinstance(result, RecognizedStandaloneNewTask):
            return (
                ResolvedContextTurn(
                    completed_question=result.completed_question,
                    next_state=result.next_state,
                    plan_state=None,
                    bridge_route=result.execution_route,
                    standalone_parse=result.parse,
                    fallback_reason=result.fallback_reason,
                    publish_context_from_v1=True,
                    source_question=chat.question,
                ),
                provenance,
            )
        if isinstance(result, RecognizedClarification):
            return (
                ResolvedContextTurn(
                    completed_question=None,
                    next_state=result.next_state,
                    plan_state=None,
                    pending_state=result.pending_state,
                    clarification_question=result.question,
                    clarification_trace=result.trace,
                    bridge_route="V2_USER_AMBIGUITY",
                ),
                provenance,
            )
        plan = AuthorizedLogicalPlan.model_validate(result.plan_state.payload)
        next_state = ConversationState.model_validate(result.next_state.payload)
        display = build_completed_question_display(
            message_id=chat.message_id,
            plan=plan,
            previous_state=previous,
            next_state=next_state,
            context_trace=result.context_trace,
        )
        display = _standalone_execution_display(chat.question, display)
        return (
            ResolvedContextTurn(
                completed_question=display.completed_question,
                next_state=result.next_state,
                plan_state=result.plan_state,
                display=display,
            ),
            provenance,
        )

    async def _save_without_v1(
        self,
        snapshot: ContextStateSnapshot,
        *,
        chat: ChatRequest,
        identity: TrustedIdentity,
        fingerprint: str,
        resolved: ResolvedContextTurn,
        response: AgentResponse,
        provenance: dict[str, Any] | None,
    ) -> AgentResponse:
        running = await self.store.reserve(
            snapshot,
            chat=chat,
            trusted=identity,
            request_fingerprint=fingerprint,
            next_state=resolved.next_state,
            plan_state=resolved.plan_state,
            pending_state=resolved.pending_state,
            bridge_route=resolved.bridge_route,
            catalog_provenance=provenance,
        )
        await self.store.complete(
            running,
            chat=chat,
            trusted=identity,
            request_fingerprint=fingerprint,
            response=response,
            v1_execution_called=False,
        )
        return response

    async def handle(
        self, chat: ChatRequest, identity: TrustedIdentity
    ) -> AgentResponse:
        lock_key = self.store.key(chat, identity)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            snapshot = await self.store.load(chat, identity)
            fingerprint = self.store.request_fingerprint(chat, identity)
            prior = snapshot.message(chat.message_id)
            if prior is not None:
                if prior["request_fingerprint"] != fingerprint:
                    raise MessageIdReuseConflictError(chat.message_id)
                if prior.get("response") is not None:
                    return AgentResponse.model_validate(prior["response"])
                return self._response(
                    chat,
                    status="SAFE_FALLBACK",
                    error_code="V2_CONTEXT_REQUEST_IN_PROGRESS",
                    answer="当前请求仍在处理中，请稍后查看结果。",
                )
            guard = await self.store.idempotency_record(
                snapshot, chat.message_id
            )
            if guard is not None:
                if guard["request_fingerprint"] != fingerprint:
                    raise MessageIdReuseConflictError(chat.message_id)
                return self._response(
                    chat,
                    status="SAFE_FALLBACK",
                    error_code="V2_CONTEXT_SESSION_EXPIRED",
                    answer="原会话状态已过期，请使用新的消息标识重新提出完整问题。",
                )

            if (
                self.v1_pending_answer_probe is not None
                and self.v1_pending_executor is not None
                and await self.v1_pending_answer_probe(chat, identity)
            ):
                running = await self.store.reserve(
                    snapshot,
                    chat=chat,
                    trusted=identity,
                    request_fingerprint=fingerprint,
                    next_state=None,
                    plan_state=None,
                    pending_state=None,
                    bridge_route="V1_PENDING_CLARIFICATION_CONTINUATION",
                    catalog_provenance=None,
                )
                response = await self.v1_pending_executor(
                    chat.model_copy(deep=True, update={"history": []}),
                    identity,
                )
                await self.store.complete(
                    running,
                    chat=chat,
                    trusted=identity,
                    request_fingerprint=fingerprint,
                    response=response,
                    v1_execution_called=True,
                )
                return response

            context_chat = chat.model_copy(deep=True, update={"history": []})
            await emit_progress(
                "INTENT_RECOGNITION",
                "RUNNING",
                "正在理解当前问题，并核对本轮与会话上下文的关系。",
                progress_phase="V2_CONTEXT_START",
            )
            catalog = await self._request_catalog(context_chat)
            provenance = None
            try:
                resolved, provenance = await self._resolve(
                    context_chat, identity, snapshot, catalog
                )
            except ContextProposalFailure as exc:
                ambiguous = exc.context_status == "AMBIGUOUS"
                response = self._response(
                    chat,
                    status="NEEDS_CLARIFICATION" if ambiguous else "SAFE_FALLBACK",
                    error_code=(
                        "V2_CONTEXT_AMBIGUOUS"
                        if ambiguous
                        else "V2_CONTEXT_UNRESOLVED"
                    ),
                    answer=(
                        "请明确您要继续哪一个已有任务。"
                        if ambiguous
                        else "当前追问无法安全确定所引用的任务，请补充完整问题。"
                    ),
                )
                return await self._save_without_v1(
                    snapshot,
                    chat=chat,
                    identity=identity,
                    fingerprint=fingerprint,
                    resolved=ResolvedContextTurn(None, None, None),
                    response=response,
                    provenance=provenance,
                )
            except (RecognitionFailure, ValueError) as exc:
                reason = str(exc)
                binding_failure = reason.startswith("V2_CONTEXT_")
                response = self._response(
                    chat,
                    status=(
                        "NEEDS_CLARIFICATION" if binding_failure else "SAFE_FALLBACK"
                    ),
                    error_code=(
                        "V2_CONTEXT_BINDING_REVALIDATION_REQUIRED"
                        if binding_failure
                        else reason
                    ),
                    answer=(
                        "当前语义目录已变化，请补充完整问题重新确认。"
                        if binding_failure
                        else "V2 上下文理解未能形成可验证的完整问题。"
                    ),
                )
                return await self._save_without_v1(
                    snapshot,
                    chat=chat,
                    identity=identity,
                    fingerprint=fingerprint,
                    resolved=ResolvedContextTurn(None, None, None),
                    response=response,
                    provenance=provenance,
                )

            if resolved.clarification_question is not None:
                response = self._response(
                    chat,
                    status="NEEDS_CLARIFICATION",
                    answer=resolved.clarification_question,
                    clarification_trace=resolved.clarification_trace,
                )
                return await self._save_without_v1(
                    snapshot,
                    chat=chat,
                    identity=identity,
                    fingerprint=fingerprint,
                    resolved=resolved,
                    response=response,
                    provenance=provenance,
                )
            if not resolved.completed_question:
                raise ValueError("V2_COMPLETED_QUESTION_REQUIRED")
            step = _completed_question_step(resolved)
            if step is not None:
                await emit_progress(
                    "QUESTION_REWRITE", "COMPLETED", step.summary
                )
            running = await self.store.reserve(
                snapshot,
                chat=chat,
                trusted=identity,
                request_fingerprint=fingerprint,
                next_state=resolved.next_state,
                plan_state=resolved.plan_state,
                pending_state=resolved.pending_state,
                bridge_route=resolved.bridge_route,
                catalog_provenance=provenance,
            )
            execution_chat = chat.model_copy(
                deep=True,
                update={"question": resolved.completed_question, "history": []},
            )
            execution_chat._completed_question_execution = True
            response = await self.v1_executor(execution_chat, identity)
            response = await self._retry_for_result_availability(
                chat=execution_chat,
                identity=identity,
                resolved=resolved,
                response=response,
                prior_state=snapshot.state,
            )
            response = _attach_completed_question(response, resolved)
            final_state = None
            if (
                (
                    resolved.publish_context_from_v1
                    or resolved.bridge_route == "V1_EXECUTION_FALLBACK_NEW_TASK"
                )
                and resolved.next_state is not None
                and response.status == "COMPLETED"
                and response.error_code is None
                and any(item.kind == "QUERY_RESULT" for item in response.evidence)
            ):
                v1_request = None
                if self.v1_context_reader is not None:
                    candidate = await self.v1_context_reader(
                        execution_chat, identity
                    )
                    if canonical_matches_execution(
                        candidate,
                        chat=execution_chat,
                        identity=identity,
                        response=response,
                    ):
                        v1_request = candidate
                    else:
                        logger.warning(
                            "V1 context evidence did not match completed execution request",
                            extra={"message_id": chat.message_id},
                        )
                frame = build_context_question(
                    chat=execution_chat,
                    parse=resolved.standalone_parse,
                    v1_request=v1_request,
                    catalog_version=(provenance or {}).get("catalog_version"),
                    original_question=(
                        resolved.source_question or execution_chat.question
                    ),
                )
                if self.v1_context_value_resolver is not None:
                    async def resolve_frame_value(
                        surface: str,
                        expected_family: str,
                        preferred_attribute_code: str | None,
                    ) -> SemanticFilterBinding | None:
                        return await self.v1_context_value_resolver(
                            execution_chat,
                            identity,
                            surface,
                            expected_family,
                            preferred_attribute_code,
                        )

                    frame = await refine_context_question_relation_actor(
                        frame, resolve_value=resolve_frame_value
                    )
                final_state = publish_context_task(
                    resolved.next_state,
                    chat=chat,
                    frame=frame,
                    created_at=self.clock(),
                )
                logger.info(
                    "V2 context task published after successful V1 query",
                    extra={
                        "message_id": chat.message_id,
                        "context_task_completeness": frame.completeness,
                        "v1_semantic_evidence": v1_request is not None,
                    },
                )
            await self.store.complete(
                running,
                chat=chat,
                trusted=identity,
                request_fingerprint=fingerprint,
                response=response,
                v1_execution_called=True,
                final_state=final_state,
            )
            return response

    async def aclose(self) -> None:
        await self.store.aclose()


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
    return {
        "runtime_mode": settings.runtime_mode,
        "conversation_identity": (
            "schema+tenant+user+application+conversation+semantic_model+"
            "requested_authorization_scope"
        ),
        "catalog_policy": "CURRENT_AUTHORIZED_PER_REQUEST",
    }


def build_context_v1_execution_handler(
    settings: Settings,
    *,
    v1_executor: Callable[
        [ChatRequest, TrustedIdentity], Awaitable[AgentResponse]
    ],
    v1_context_reader: Callable[
        [ChatRequest, TrustedIdentity], Awaitable[CanonicalAnalysisRequest | None]
    ] | None = None,
    v1_context_value_resolver: Callable[
        [ChatRequest, TrustedIdentity, str, str, str | None],
        Awaitable[SemanticFilterBinding | None],
    ] | None = None,
    v1_pending_answer_probe: Callable[
        [ChatRequest, TrustedIdentity], Awaitable[bool]
    ] | None = None,
    v1_pending_executor: Callable[
        [ChatRequest, TrustedIdentity], Awaitable[AgentResponse]
    ] | None = None,
    external: ContextV1ExternalDependencies | None = None,
) -> V2ContextV1ExecutionBridge:
    receipt = validate_context_v1_settings(settings)
    dependencies = external or ContextV1ExternalDependencies(
        catalog=CurrentAuthorizedCatalog(settings.context_catalog_root),
        model=RecognitionModelClient(settings),
        redis=Redis.from_url(
            settings.effective_redis_url(),
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=35,
        ),
    )
    store = RedisContextStateStore(
        dependencies.redis,
        prefix=settings.session_key_prefix + ":v2-context-live:v1",
        ttl_seconds=settings.session_ttl_seconds,
        idempotency_ttl_seconds=max(
            settings.session_ttl_seconds, settings.response_cache_ttl_seconds
        ),
    )
    return V2ContextV1ExecutionBridge(
        store=store,
        catalog=dependencies.catalog,
        model=dependencies.model,
        v1_executor=v1_executor,
        v1_context_reader=v1_context_reader,
        v1_context_value_resolver=v1_context_value_resolver,
        v1_pending_answer_probe=v1_pending_answer_probe,
        v1_pending_executor=v1_pending_executor,
        demo_mode=settings.demo_mode,
        clock=lambda: datetime.now(timezone.utc).astimezone(),
        startup_receipt={
            **receipt,
            "store_schema": "v2-context-live-state-v1",
            "v1_execution_bridge_enabled": True,
            "v2_execution_transport_enabled": False,
        },
    )
