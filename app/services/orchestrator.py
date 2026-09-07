from __future__ import annotations

import inspect
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.adapters.base import AdapterBundle, AdapterError
import httpx

from app.analysis import (
    AnalysisEngine,
    AnalysisError,
    AnalysisPlanner,
    QwenAnalysisSynthesizer,
    SynthesisValidationError,
)
from app.analysis.interpretation import AnswerPlanner, InsightInterpretationLayer
from app.services.chat_responder import QwenChatResponder
from app.analysis.contracts import ordered_entity_metric_ranking_request
from app.config import Settings
from app.domain.models import AnalysisOperator, AnalysisProcessStep, AnalysisRequirement, AgentResponse, AtomicTask, CanonicalAnalysisRequest, ChatRequest, ClarificationItem, ContextMode, ConversationControl, DataQueryResult, Dataset, DependencyConstraint, EvidenceItem, ExtensionExecution, GeneratedFile, HistoryMessage, KnowledgeContext, MetricRef, PendingState, PrimaryIntent, ReliabilityReport, SemanticAmbiguity, TaskExecutionResult, TaskPlan, TimeRange, TrustedIdentity, TurnAdmissionDecision, TurnRelation
from app.planning import MultiQuestionPlanner, TaskPlanningError
from app.intent.classifier import (
    RuleBasedIntentClassifier,
    applicable_department_filter_slot,
    applicable_department_relation_types,
    render_execution_question,
    safe_semantic_confirmation,
)
from app.stores import SessionConflictError, SessionStore
from app.stores.events import (
    InMemorySessionEventStore,
    SessionEvent,
    SessionEventStore,
    SessionEventType,
)
from app.stores.long_memory import LongTermMemory, LongTermMemoryStore, MemoryScope, MemoryType
from app.services.dataset_followup import (
    is_dataset_operation_followup,
    plan_dataset_followup,
    restore_reference,
    scope_for_request,
)
from app.services.question_rewriter import QuestionRewriter
from app.services.conversation_followup import (
    build_temporal_anchor,
    derive_period_comparison,
    resolve_conversation_temporal_context,
)
from app.services.turn_admission import TurnAdmissionGate
from app.services.history_compaction import compact_history
from app.services.working_memory import recalls_prior_task, select_recalled_task_frame
from app.services.extension_dispatcher import ExtensionDispatcher
from app.services.tool_selector import OptionalToolSelector
from app.services.progress import emit_progress, task_progress_scope
from app.presentation import (
    build_composite_intent_recognition_display_v2,
    build_intent_recognition_display_v2,
    render_composite_intent_recognition_display_v2,
    render_intent_recognition_display_v2,
)
from app.services.relationship_projection import (
    requires_distinct_relationship_projection,
)
from app.skills import DynamicSkillLoader, skill_for_intent
from minio_followup_store import (
    DatasetScope,
    HybridMinioFollowupStore,
    MinioFollowupStore,
)

NO_DATA_INTENTS = {PrimaryIntent.CHAT, PrimaryIntent.CAPABILITY_HELP, PrimaryIntent.OUT_OF_SCOPE}
METADATA_INTENTS = {PrimaryIntent.METRIC_DEFINITION, PrimaryIntent.DATA_LINEAGE}
ANALYSIS_INTENTS = {
    PrimaryIntent.TREND_ANALYSIS, PrimaryIntent.COMPARISON_ANALYSIS,
    PrimaryIntent.COMPOSITION_ANALYSIS, PrimaryIntent.ANOMALY_ANALYSIS,
    PrimaryIntent.ROOT_CAUSE_ANALYSIS, PrimaryIntent.FORECAST_ANALYSIS,
    PrimaryIntent.REPORT_GENERATION, PrimaryIntent.DATA_QUALITY,
}

# Contract failures that can be repaired by regenerating ASL against the same
# published semantic snapshot.  The second attempt receives the exact missing
# fields/filters in ``SEMANTIC_QUERY_RETRY`` and is still fail-closed.
SEMANTIC_QUERY_RETRY_CODES = frozenset({
    "ASL_AMBIGUOUS",
    "ASL_DIMENSION_INVALID",
    "ASL_DETAIL_FIELDS_INCOMPLETE",
    "ASL_REQUIRED_FILTER_MISSING",
    "ASL_REQUIRED_DIMENSION_MISSING",
    "ASL_UNREQUESTED_DIMENSION",
    "SQL_TRANSLATION_AMBIGUOUS",
    "SQL_QUERY_ENTITY_ALIGNMENT_FAILED",
    "SQL_QUERY_FILTER_OPERATOR_FAILED",
    "SQL_RELATIONSHIP_GRAPH_INCOMPLETE",
})
_SALES_RECORD_TIME_ASSUMPTION = "TRANSACTION_TIME_SCOPE=SALES_RECORD"
_INTERNAL_ASSUMPTIONS: ContextVar[tuple[str, ...]] = ContextVar(
    "data_agent_internal_assumptions", default=()
)
_BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
_QUALITY_STATUS_LABELS = {
    "PASS": "通过",
    "FAIL": "不通过",
    "FAILED": "不通过",
    "WARNING": "警告",
    "WARN": "警告",
    "DEGRADED": "降级",
    "LIMITED": "受限",
}


def _business_datetime_text(value: datetime) -> str:
    """Render user-visible timestamps consistently in business local time."""

    localized = value.astimezone(_BUSINESS_TIMEZONE)
    return localized.strftime("%Y-%m-%d %H:%M:%S（北京时间）")


def _quality_status_text(value: str) -> str:
    return _QUALITY_STATUS_LABELS.get(value.strip().upper(), "待确认")


def _requires_deterministic_analysis(request: CanonicalAnalysisRequest) -> bool:
    return (
        request.primary_intent in ANALYSIS_INTENTS
        or AnalysisOperator.TOP_N in request.operators
        or AnalysisOperator.BOTTOM_N in request.operators
        or ordered_entity_metric_ranking_request(request)
    )

logger = logging.getLogger(__name__)


def _compact_trace_value(value: Any, limit: int) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    if len(rendered) <= limit:
        return rendered
    return f"{rendered[:limit]}…（已省略 {len(rendered) - limit} 字符）"


def select_data_execution_question(
    request: CanonicalAnalysisRequest,
    decision: TurnAdmissionDecision,
    raw_question: str,
) -> tuple[str, str, str]:
    """Keep complete standalone wording while executing contextual turns canonically.

    A canonical rendering is useful only after context was intentionally merged.
    Replacing a complete standalone utterance with a partial structured rendering
    can silently discard product or negative-filter requirements that downstream
    semantic parsing could otherwise understand from the raw text.
    """

    canonical = render_execution_question(request)
    if (
        decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
        and decision.context_mode == ContextMode.NONE
    ):
        normalized_raw, _ = QuestionRewriter._normalize_polite_word_order(
            raw_question
        )
        normalized_raw, _ = (
            QuestionRewriter._normalize_grouped_calculation_wording(
                normalized_raw
            )
        )
        safe_normalized_standalone = any(
            isinstance(event, dict)
            and str(event.get("kind") or "") in {
                "POLITE_WORD_ORDER",
                "GROUPED_CALCULATION_WORDING",
            }
            for event in request.rewrite_events
        )
        if normalized_raw != raw_question:
            return normalized_raw, canonical, "NORMALIZED_STANDALONE"
        if safe_normalized_standalone and request.rewritten_question:
            return (
                request.rewritten_question,
                canonical,
                "NORMALIZED_STANDALONE",
            )
        return raw_question, canonical, "RAW_STANDALONE"
    return canonical, canonical, "CANONICAL_CONTEXTUAL"


class ExplicitDatasetUnavailableError(RuntimeError):
    """The caller selected a dataset that cannot be safely reused."""


class DependencyConstraintError(RuntimeError):
    """A predecessor result cannot be safely compiled into a downstream filter."""

class DataAnalysisOrchestrator:
    def __init__(
        self,
        settings: Settings,
        classifier: object,
        adapters: AdapterBundle,
        sessions: SessionStore,
        memories: LongTermMemoryStore | None = None,
        analysis_engine: AnalysisEngine | None = None,
        analysis_synthesizer: QwenAnalysisSynthesizer | None = None,
        dataset_store: MinioFollowupStore | HybridMinioFollowupStore | None = None,
        question_rewriter: QuestionRewriter | None = None,
        task_planner: MultiQuestionPlanner | None = None,
        report_exporter: Any | None = None,
        file_importer: Any | None = None,
        extension_dispatcher: ExtensionDispatcher | None = None,
        chat_responder: QwenChatResponder | None = None,
        event_store: SessionEventStore | None = None,
        turn_admission_gate: TurnAdmissionGate | None = None,
    ) -> None:
        self.settings = settings
        self.classifier = classifier
        self.adapters = adapters
        self.sessions = sessions
        self.memories = memories
        self.analysis_engine = analysis_engine or AnalysisEngine()
        self.analysis_planner = AnalysisPlanner()
        self.insight_interpreter = InsightInterpretationLayer()
        self.answer_planner = AnswerPlanner()
        self.dataset_store = dataset_store
        self.question_rewriter = question_rewriter
        self.task_planner = task_planner
        self.report_exporter = report_exporter
        self.file_importer = file_importer
        self.event_store = event_store or InMemorySessionEventStore()
        self.turn_admission_gate = turn_admission_gate or TurnAdmissionGate()
        self.extension_dispatcher = extension_dispatcher or ExtensionDispatcher(
            settings=settings,
            tool_selector=OptionalToolSelector(settings),
            skill_loader=(
                DynamicSkillLoader(
                    settings.skills_root,
                    max_bytes=settings.skill_max_bytes,
                )
                if settings.dynamic_skills_enabled
                else None
            )
        )
        self.analysis_synthesizer = analysis_synthesizer or (
            QwenAnalysisSynthesizer(settings)
            if settings.analysis_synthesis_enabled and settings.env != "test"
            else None
        )
        self.chat_responder = chat_responder or (
            QwenChatResponder(settings)
            if settings.chat_model_enabled
            and settings.env != "test"
            and settings.intent_model_api_key is not None
            else None
        )

        # Running requests are process-local because asyncio tasks cannot be
        # transferred between service instances.  The scope includes the
        # trusted identity so one user can never cancel another user's query
        # even when conversation ids happen to be equal.
        self._running_lock = asyncio.Lock()
        self._running_requests: dict[
            tuple[str, str, str, str],
            dict[asyncio.Task[AgentResponse], asyncio.Event],
        ] = {}

    async def _append_session_event(
        self,
        *,
        chat: ChatRequest,
        identity: TrustedIdentity,
        event_type: SessionEventType,
        payload: dict[str, Any],
        trace_id: str,
    ) -> None:
        try:
            await self.event_store.append(SessionEvent(
                session_id=chat.conversation_id,
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                application_id=chat.application_id,
                message_id=chat.message_id,
                event_type=event_type,
                trace_id=trace_id,
                payload=payload,
            ))
        except Exception as exc:  # pragma: no cover - backend-specific failure
            # Observability is deliberately fail-open: losing an audit event is
            # reportable, but must never discard a valid business response.
            logger.warning(
                "session event append failed: conversation_id=%s message_id=%s "
                "event_type=%s error=%s",
                chat.conversation_id,
                chat.message_id,
                event_type.value,
                exc,
            )

    async def handle(self, chat: ChatRequest, identity: TrustedIdentity) -> AgentResponse:
        scope = self._running_scope(chat, identity)
        if self._is_cancel_command(chat.question):
            cancelled_count = await self._cancel_running(scope)
            pending = await self.sessions.get_pending(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
            )
            cancelled_intent = (
                pending.request.primary_intent
                if pending is not None
                else self._intent_from_history(chat, identity)
            )
            await self.sessions.clear_pending(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
            )
            await self.sessions.clear_dag_pending(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
            )
            return self._cancel_command_response(
                chat, cancelled_count, cancelled_intent
            )

        cancelled_by_user = asyncio.Event()
        execution = asyncio.create_task(self._handle_request(chat, identity))
        async with self._running_lock:
            self._running_requests.setdefault(scope, {})[execution] = cancelled_by_user
        try:
            return await execution
        except asyncio.CancelledError:
            # A cancellation command from the same trusted conversation should
            # become a normal terminal response.  Transport disconnects and
            # service shutdown cancellation must still propagate unchanged.
            if not cancelled_by_user.is_set():
                execution.cancel()
                raise
            return self._running_cancelled_response(chat)
        finally:
            async with self._running_lock:
                running = self._running_requests.get(scope)
                if running is not None:
                    running.pop(execution, None)
                    if not running:
                        self._running_requests.pop(scope, None)

    async def _handle_request(
        self, chat: ChatRequest, identity: TrustedIdentity
    ) -> AgentResponse:
        await emit_progress(
            "REQUEST_VALIDATION", "COMPLETED", "请求身份、应用范围和消息参数校验完成。"
        )
        request_fingerprint = self._request_fingerprint(chat, identity)
        cached = await self.sessions.get_response(
            identity.tenant_id,
            identity.user_id,
            chat.application_id,
            chat.conversation_id,
            chat.message_id,
            request_fingerprint,
        )
        if cached:
            await emit_progress(
                "RESPONSE_CACHE", "COMPLETED", "命中相同消息的幂等结果缓存。"
            )
            cached.semantic_model_id = chat.semantic_model_id
            cached.requested_business_domain_ids = list(chat.business_domain_ids)
            cached.business_domain_selection_mode = (
                "EXPLICIT" if chat.business_domain_ids else "AUTO"
            )
            return cached
        repeat_fingerprint = (
            self._repeat_query_fingerprint(chat, identity)
            if (
                not chat._bypass_repeat_query_cache
                and self._is_repeat_cache_candidate(chat)
            )
            else None
        )
        repeat_message_id = (
            f"semantic-repeat:{repeat_fingerprint}" if repeat_fingerprint else None
        )
        if repeat_fingerprint and repeat_message_id:
            repeated = await self.sessions.get_response(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
                repeat_message_id,
                repeat_fingerprint,
            )
            if repeated is not None:
                await emit_progress(
                    "RESPONSE_CACHE",
                    "COMPLETED",
                    "检测到当前问题与本会话已完成查询相同，直接复用已有结果和附件。",
                )
                repeated.request_id = uuid4()
                return repeated
        owner_token = str(uuid4())
        claim = getattr(self.sessions, "claim_message_execution", None)
        release = getattr(self.sessions, "release_message_execution", None)
        claimed = True
        if callable(claim):
            claimed = await claim(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id, chat.message_id, request_fingerprint,
                owner_token, ttl_seconds=max(1, int(self.settings.request_timeout_seconds) + 5),
            )
            if not claimed:
                claimed = await self._wait_for_cached_response_or_claim(
                    chat, identity, request_fingerprint, owner_token
                )

                if not claimed:
                    raise TimeoutError("timed out waiting for identical in-flight request")
                cached = await self.sessions.get_response(
                    identity.tenant_id, identity.user_id, chat.application_id,
                    chat.conversation_id, chat.message_id, request_fingerprint,
                )
                if cached is not None:
                    return cached
        try:
            # Recheck after acquiring the lease: the previous owner may have
            # stored its response immediately before releasing the lease.
            cached = await self.sessions.get_response(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id, chat.message_id, request_fingerprint,
            )
            if cached is not None:
                return cached
            if chat._is_regeneration_execution:
                # A refresh replaces the active turn in the same conversation.
                # Clear only mutable continuation state after acquiring the
                # message lease; exact retries returned from cache above never
                # disturb state produced by the first execution.
                await self.sessions.clear_pending(
                    identity.tenant_id,
                    identity.user_id,
                    chat.application_id,
                    chat.conversation_id,
                )
                await self.sessions.clear_dag_pending(
                    identity.tenant_id,
                    identity.user_id,
                    chat.application_id,
                    chat.conversation_id,
                )
            response: AgentResponse
            raw_request = self._classify_with_rules(
                chat.question, identity, chat.conversation_id
            )
            independent_chat = raw_request.primary_intent == PrimaryIntent.CHAT
            dag_pending = (
                None
                if chat._is_regeneration_execution
                else await self.sessions.get_dag_pending(
                    identity.tenant_id,
                    identity.user_id,
                    chat.application_id,
                    chat.conversation_id,
                )
            )
            if independent_chat:
                # A standalone social/lifestyle turn is never a DAG answer or
                # a multi-question analytical task, even when it contains a
                # conjunction such as “草莓和可乐”. Drop stale analytical DAG
                # state before dispatching it to the controlled chat path.
                if dag_pending is not None:
                    await self.sessions.clear_dag_pending(
                        identity.tenant_id,
                        identity.user_id,
                        chat.application_id,
                        chat.conversation_id,
                    )
                response = await self._handle(chat, identity)
            elif dag_pending is not None:
                response = await self._resume_task_plan(chat, identity, dag_pending)
            elif chat.dag_resume_token is not None:
                response = self._dag_resume_error(
                    chat,
                    "多任务追问状态已过期或已完成，请重新提交完整问题。",
                )
            else:
                plan = None
                if self.settings.multi_question_enabled and self.task_planner is not None:
                    await emit_progress(
                        "TASK_PLANNING",
                        "RUNNING",
                        "### ◉ 规划与执行\n正在判断是否需要拆分多个分析任务。",
                    )
                    try:
                        plan = await self.task_planner.plan(chat.question)
                    except TaskPlanningError as exc:
                        logger.warning("multi-question plan rejected: %s", exc)
                    if plan is not None:
                        composite_view = (
                            build_composite_intent_recognition_display_v2(
                                chat.question,
                                plan,
                            )
                        )
                        await emit_progress(
                            "INTENT_RECOGNITION",
                            "COMPLETED",
                            render_composite_intent_recognition_display_v2(
                                composite_view
                            ),
                            intent="COMPOSITE_QUERY",
                            confidence=1.0,
                            display_model="CompositeIntentRecognitionDisplayV2",
                            display_version="V2",
                            presentation_scenario="ANALYTIC",
                            is_composite=True,
                            task_count=len(plan.tasks),
                        )
                    await emit_progress(
                        "TASK_PLANNING",
                        "COMPLETED",
                        (
                            "拆分判断完成。"
                            + (
                                "已拆分为以下任务：\n"
                                + "\n".join(
                                    f"{index}. {task.question}"
                                    for index, task in enumerate(plan.tasks, 1)
                                )
                                if plan is not None
                                else (
                                    "当前问题无需拆分，按单任务执行。\n"
                                    f"子任务1：{chat.question}"
                                )
                            )
                            + "\n规划调用：语义解析 → ASL 查询规划 → 只读 SQL → 结果校验"
                        ),
                        task_count=len(plan.tasks) if plan is not None else 1,
                    )
                response = (
                    await self._handle_task_plan(chat, identity, plan)
                    if plan is not None
                    else await self._handle(chat, identity)
                )
            # Echo the effective routing contract on every outcome, including
            # clarification and safe-fallback responses which are not terminalized
            # through _finish_terminal.
            response.semantic_model_id = chat.semantic_model_id
            response.database_id = chat.database_id
            response.requested_business_domain_ids = list(chat.business_domain_ids)
            response.business_domain_selection_mode = (
                "EXPLICIT" if chat.business_domain_ids else "AUTO"
            )
            selected_skill = skill_for_intent(response.intent)
            used_skills = [selected_skill] if selected_skill else []
            used_skills.extend(
                item.name
                for item in response.extension_executions
                if item.status == "COMPLETED"
            )
            response.skills_used = list(dict.fromkeys(used_skills))
            stored = await self.sessions.put_response(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
                chat.message_id,
                request_fingerprint,
                response,
            )
            if (
                repeat_fingerprint
                and repeat_message_id
                and response.status in {"COMPLETED", "PARTIAL_SUCCESS"}
            ):
                await self.sessions.put_response(
                    identity.tenant_id,
                    identity.user_id,
                    chat.application_id,
                    chat.conversation_id,
                    repeat_message_id,
                    repeat_fingerprint,
                    response,
                )
            return stored
        finally:
            if claimed and callable(release):
                await release(
                    identity.tenant_id, identity.user_id, chat.application_id,
                    chat.conversation_id, chat.message_id, owner_token,
                )

    @staticmethod
    def _running_scope(
        chat: ChatRequest, identity: TrustedIdentity
    ) -> tuple[str, str, str, str]:
        return (
            identity.tenant_id,
            identity.user_id,
            chat.application_id,
            chat.conversation_id,
        )

    @staticmethod
    def _is_cancel_command(question: str) -> bool:
        compact = "".join(question.casefold().split())
        return compact in {"取消", "停止", "停下", "不用了", "算了"} or any(
            term in compact
            for term in ("取消查询", "取消任务", "停止查询", "停止分析")
        )

    async def _cancel_running(self, scope: tuple[str, str, str, str]) -> int:
        async with self._running_lock:
            running = list(self._running_requests.get(scope, {}).items())
            for task, cancelled_by_user in running:
                cancelled_by_user.set()
                task.cancel()
        return len(running)

    @staticmethod
    def _running_cancelled_response(chat: ChatRequest) -> AgentResponse:
        return AgentResponse(
            request_id=uuid4(),
            conversation_id=chat.conversation_id,
            status="CANCELLED",
            intent=PrimaryIntent.OUT_OF_SCOPE,
            intent_source="CONVERSATION_CONTROL",
            intent_confidence=1.0,
            answer="任务已根据本会话的取消指令停止。",
            semantic_model_id=chat.semantic_model_id,
            database_id=chat.database_id,
            requested_business_domain_ids=list(chat.business_domain_ids),
            business_domain_selection_mode=(
                "EXPLICIT" if chat.business_domain_ids else "AUTO"
            ),
            reliability=ReliabilityReport(
                level="HIGH",
                score=1.0,
                gates={"running_request_cancelled": True},
            ),
        )

    @staticmethod
    def _cancel_command_response(
        chat: ChatRequest,
        cancelled_count: int,
        cancelled_intent: PrimaryIntent,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=uuid4(),
            conversation_id=chat.conversation_id,
            status="CANCELLED",
            intent=cancelled_intent,
            intent_source="CONVERSATION_CONTROL",
            intent_confidence=1.0,
            answer=(
                "已停止当前正在执行的查询或分析任务。"
                if cancelled_count
                else "当前没有正在执行的任务，已清除本会话待补充状态。"
            ),
            semantic_model_id=chat.semantic_model_id,
            database_id=chat.database_id,
            requested_business_domain_ids=list(chat.business_domain_ids),
            business_domain_selection_mode=(
                "EXPLICIT" if chat.business_domain_ids else "AUTO"
            ),
            reliability=ReliabilityReport(
                level="HIGH",
                score=1.0,
                gates={
                    "running_request_cancelled": cancelled_count > 0,
                },
            ),
        )

    def _intent_from_history(
        self, chat: ChatRequest, identity: TrustedIdentity
    ) -> PrimaryIntent:
        for message in reversed(chat.history):
            if message.role == "user" and not self._is_cancel_command(message.content):
                return self.classifier.classify(
                    message.content, identity, chat.conversation_id
                ).primary_intent
        return PrimaryIntent.OUT_OF_SCOPE

    async def _wait_for_cached_response_or_claim(
        self,
        chat: ChatRequest,
        identity: TrustedIdentity,
        request_fingerprint: str,
        owner_token: str,
    ) -> bool:
        """Wait for the winning request, taking over only if its lease vanishes."""
        deadline = time.monotonic() + max(1.0, self.settings.request_timeout_seconds)
        claim = getattr(self.sessions, "claim_message_execution")
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            cached = await self.sessions.get_response(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id, chat.message_id, request_fingerprint,
            )
            if cached is not None:
                return True
            if await claim(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id, chat.message_id, request_fingerprint,
                owner_token, ttl_seconds=max(1, int(self.settings.request_timeout_seconds) + 5),
            ):
                return True
        return False

    @staticmethod
    def _dag_scope_fingerprint(chat: ChatRequest) -> str:
        payload = {
            "semantic_model_id": chat.semantic_model_id,
            "database_id": chat.database_id,
            "business_domain_ids": sorted(chat.business_domain_ids),
            "knowledge_base_names": sorted(chat.knowledge_base_names),
            "dataset_id": chat.dataset_id,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _dag_resume_error(chat: ChatRequest, message: str) -> AgentResponse:
        return AgentResponse(
            request_id=uuid4(),
            conversation_id=chat.conversation_id,
            status="SAFE_FALLBACK",
            intent=PrimaryIntent.OUT_OF_SCOPE,
            intent_source="TASK_DAG",
            intent_confidence=0.0,
            answer=message,
            reliability=ReliabilityReport(
                level="FAIL",
                score=0.0,
                gates={"dag_resume": False},
                warnings=[message],
            ),
        )

    async def _resume_task_plan(
        self,
        chat: ChatRequest,
        identity: TrustedIdentity,
        state: dict[str, Any],
    ) -> AgentResponse:
        """Resume a persisted multi-task clarification without replanning.

        The opaque token is bound to tenant/user/application/conversation by the
        session key, and the stored scope fingerprint prevents a reply from
        silently changing semantic model, business-domain, knowledge-base or
        dataset scope halfway through execution.
        """
        try:
            token_hash = str(state["resume_token_hash"])
            root_message_id = str(state["root_message_id"])
            plan = TaskPlan.model_validate(state["task_plan"])
            awaiting = [str(value) for value in state["awaiting_task_ids"]]
            state_version = int(state["state_version"])
            stored_scope = str(state["scope_fingerprint"])
        except (KeyError, TypeError, ValueError):
            logger.error("invalid root DAG pending envelope")
            await self.sessions.clear_dag_pending(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id,
            )
            return self._dag_resume_error(chat, "多任务追问状态损坏，请重新提交完整问题。")

        supplied_token = chat.dag_resume_token or ""
        supplied_hash = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
        # The pending state is already isolated by trusted tenant/user/app and
        # conversation keys, then bound to the immutable semantic/data scope
        # below.  A normal chat client therefore needs no internal token. Keep
        # validating a token when an older/advanced client supplies one so an
        # explicit stale or forged token can never resume the state.
        if supplied_token and not hmac.compare_digest(token_hash, supplied_hash):
            return self._dag_resume_error(
                chat,
                "提交的多任务恢复令牌与当前受信会话状态不匹配，请基于最新响应继续。",
            )
        if not hmac.compare_digest(stored_scope, self._dag_scope_fingerprint(chat)):
            return self._dag_resume_error(
                chat,
                "本轮语义模型、业务域、知识库或数据集范围与原任务不一致；请保持原范围后重试。",
            )

        normalized = chat.question.replace(" ", "")
        if normalized in {"取消", "取消任务", "取消本次任务", "不用了", "算了", "停止", "停止分析"}:
            for conversation_id in state.get("child_conversations", {}).values():
                if isinstance(conversation_id, str) and conversation_id:
                    await self.sessions.clear_pending(
                        identity.tenant_id, identity.user_id, chat.application_id,
                        conversation_id,
                    )
            await self.sessions.clear_dag_pending(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id, expected_version=state_version,
            )
            await self.sessions.delete_dag_checkpoint(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id, root_message_id,
            )
            return AgentResponse(
                request_id=uuid4(), conversation_id=chat.conversation_id,
                status="COMPLETED", intent=PrimaryIntent.CHAT,
                intent_source="TASK_DAG", intent_confidence=1.0,
                answer="已取消本次多任务分析。",
                reliability=ReliabilityReport(
                    level="HIGH", score=1.0, gates={"dag_cancelled": True}
                ),
            )

        answers = dict(chat.task_answers)
        unknown = sorted(set(answers) - set(awaiting))
        if unknown:
            return self._dag_resume_error(
                chat,
                "task_answers 包含当前不待补充的任务：" + "、".join(unknown),
            )
        shared = state.get("shared_clarification")
        shared_time = bool(
            isinstance(shared, dict)
            and shared.get("slot") == "time_range"
            and [str(value) for value in shared.get("task_ids", [])] == awaiting
        )
        if len(awaiting) == 1 and not answers:
            answers[awaiting[0]] = chat.question
        elif len(awaiting) > 1 and not answers and shared_time:
            if RuleBasedIntentClassifier._time_range(chat.question) is not None:
                answers.update({task_id: chat.question for task_id in awaiting})
            else:
                questions = [
                    str(item) for item in state.get("clarification_questions", [])
                ]
                return AgentResponse(
                    request_id=uuid4(), conversation_id=chat.conversation_id,
                    status="NEEDS_CLARIFICATION",
                    intent=PrimaryIntent.REPORT_GENERATION,
                    intent_source="TASK_DAG", intent_confidence=1.0,
                    answer=(
                        "这些报告维度共用一个时间范围，但本轮没有识别出完整、"
                        "有效的起止时间，请补充后再继续。"
                    ),
                    clarification_questions=questions[:1],
                    clarification_items=[ClarificationItem(
                        slot="shared:time_range",
                        title="共享时间范围",
                        question=(questions[0] if questions else "这份报告要分析哪个时间范围？"),
                        options=["本月", "上月", "最近30天", "自定义起止日期"],
                        multi_select=False,
                    )],
                    missing_slots=["shared:time_range"],
                    task_plan=plan,
                    dag_resume_token=supplied_token or None,
                    awaiting_task_ids=awaiting,
                )
        elif len(awaiting) > 1 and not answers:
            questions = state.get("clarification_questions", [])
            return AgentResponse(
                request_id=uuid4(), conversation_id=chat.conversation_id,
                status="NEEDS_CLARIFICATION", intent=PrimaryIntent.OUT_OF_SCOPE,
                intent_source="TASK_DAG", intent_confidence=1.0,
                answer=(
                    "有多个子任务同时需要补充信息。为避免把答案填错任务，请在 task_answers "
                    "中按 task_id 分别提交。"
                ),
                clarification_questions=[str(item) for item in questions][:5],
                remaining_question_count=max(0, len(questions) - 5),
                task_plan=plan,
                dag_resume_token=supplied_token,
                awaiting_task_ids=awaiting,
            )
        if not answers:
            return self._dag_resume_error(chat, "没有收到可用于恢复子任务的补充答案。")
        lease_owner = str(uuid4())
        lease_message_id = f"__dag-resume-v{state_version}"
        claimed = await self.sessions.claim_message_execution(
            identity.tenant_id, identity.user_id, chat.application_id,
            chat.conversation_id, lease_message_id, str(state_version),
            lease_owner,
            ttl_seconds=max(1, int(self.settings.request_timeout_seconds) + 5),
        )
        if not claimed:
            return self._dag_resume_error(
                chat,
                "该多任务补充信息正在由另一条请求处理，请等待最新响应后再继续。",
            )
        try:
            latest = await self.sessions.get_dag_pending(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id,
            )
            if latest is None or int(latest.get("state_version", -1)) != state_version:
                return self._dag_resume_error(
                    chat,
                    "多任务状态已被更新，请基于最新响应继续。",
                )
            return await self._handle_task_plan(
                chat,
                identity,
                plan,
                root_message_id=root_message_id,
                task_answers=answers,
                dag_pending=state,
            )
        finally:
            await self.sessions.release_message_execution(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id, lease_message_id, lease_owner,
            )

    @staticmethod
    def _dag_requires_new_entity(
        question: str, dependencies: list[AgentResponse | Exception | None]
    ) -> bool:
        """Do not reuse a predecessor dataset for an entity it does not contain.

        A dependent instruction such as "根据商品分类筛选供应商" is an
        enrichment/join query, not a local filter over the product-category rows.
        Reusing the predecessor conversation or dataset would silently repeat the
        first result and could incorrectly mark it HIGH reliability.
        """
        entity_markers = ("供应商", "经销商", "商品", "门店", "客户", "订单")
        requested = {marker for marker in entity_markers if marker in question}
        if not requested:
            return False
        completed = [
            dependency for dependency in dependencies
            if isinstance(dependency, AgentResponse)
        ]
        if not completed:
            return False
        evidence_text = " ".join(dependency.answer for dependency in completed)
        return any(marker not in evidence_text for marker in requested)

    @staticmethod
    def _dag_references_dependency_result(question: str) -> bool:
        compact = re.sub(r"\s+", "", question).lower()
        return bool(re.search(
            r"(?:根据|基于|按照|依照|按)(?:上述|上一步|前述|前面|这些|该|其)?"
            r".{0,24}(?:结果|范围|名单|类别|分类|科室|地区|对象|筛选|过滤|推荐|查找|匹配)",
            compact,
        )) or any(marker in compact for marker in (
            "从上述结果", "从上一步结果", "在这些结果中", "再从其中", "用上一步",
        ))

    @staticmethod
    def _dependency_anchor_text(question: str) -> str:
        compact = re.sub(r"\s+", "", question).lower()
        match = re.search(
            r"(?:根据|基于|按照|依照|按)(.+?)(?:筛选|过滤|推荐|查找|查询|匹配|分析|统计)",
            compact,
        )
        return match.group(1) if match else compact

    @staticmethod
    def _dependency_column_family(text: str) -> str | None:
        normalized = text.lower()
        families = {
            "department": ("科室", "部门", "department", "dept"),
            "category": ("分类", "类别", "品类", "category", "class"),
            "region": ("地区", "区域", "省份", "城市", "省", "市", "region", "province", "city"),
            "product": ("商品", "产品", "货品", "product", "goods", "sku"),
            "supplier": ("供应商", "经销商", "supplier", "dealer", "vendor"),
            "hospital": ("医院", "hospital"),
            "customer": ("客户", "会员", "customer", "member"),
            "store": ("门店", "店铺", "store", "shop"),
            "brand": ("品牌", "brand"),
        }
        matches = [
            family for family, aliases in families.items()
            if any(alias in normalized for alias in aliases)
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _is_stable_dependency_key(column: str) -> bool:
        """Return whether a projected column is suitable for a cross-entity join.

        Display names are useful in the answer but are not stable join keys.  A
        DAG branch that feeds another entity therefore prefers an explicitly
        projected ``id``/``code`` column from the same semantic family.
        """
        field = column.strip().lower().rsplit(".", 1)[-1]
        return bool(re.search(r"(?:^|_)(?:id|code)$", field))

    @classmethod
    def _dependency_key_families(
        cls,
        source_task_id: str,
        plan: TaskPlan,
    ) -> tuple[str, ...]:
        families: list[str] = []
        for candidate in plan.tasks:
            if (
                source_task_id not in candidate.depends_on
                or not cls._dag_references_dependency_result(candidate.question)
            ):
                continue
            family = cls._dependency_column_family(
                cls._dependency_anchor_text(candidate.question)
            )
            if family and family not in families:
                families.append(family)
        return tuple(families)

    @classmethod
    def _with_dependency_join_keys(
        cls,
        question: str,
        source_task_id: str,
        plan: TaskPlan,
    ) -> str:
        """Ask a predecessor query to project stable keys needed downstream.

        The family names are selected from the fixed internal vocabulary above,
        never copied from an arbitrary user fragment.  This keeps the prompt
        bounded while making immutable predecessor data usable as an exact,
        code-based downstream constraint.
        """
        families = cls._dependency_key_families(source_task_id, plan)
        if not families:
            return question
        family_list = ", ".join(families)
        return (
            f"{question}\n"
            "Internal DAG join requirement: in addition to requested fields, "
            "return both the stable unique ID/code field and display-name field "
            f"for each downstream dependency dimension family: {family_list}. "
            "The ID/code is required for an exact downstream join. "
            "Do not infer unrelated metrics."
        )

    @classmethod
    def _select_dependency_column(
        cls,
        question: str,
        columns: tuple[str, ...],
        rows: tuple[dict[str, Any], ...],
    ) -> str:
        anchor = cls._dependency_anchor_text(question)
        anchor_family = cls._dependency_column_family(anchor)
        if anchor_family:
            family_matches = [
                column for column in columns
                if cls._dependency_column_family(column) == anchor_family
            ]
            if len(family_matches) == 1:
                return family_matches[0]
            stable_matches = [
                column for column in family_matches
                if cls._is_stable_dependency_key(column)
            ]
            if len(stable_matches) == 1:
                return stable_matches[0]

        direct_matches = [column for column in columns if column.lower() in anchor]
        if len(direct_matches) == 1:
            return direct_matches[0]

        eligible: list[str] = []
        for column in columns:
            lowered = column.lower()
            if re.search(r"(?:^|[._])(?:id|code)$|(?:编号|编码|_id|_code)$", lowered):
                continue
            values = [row.get(column) for row in rows if row.get(column) is not None]
            if not values or all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in values
            ):
                continue
            eligible.append(column)
        if len(eligible) == 1:
            return eligible[0]
        raise DependencyConstraintError("DEPENDENCY_FILTER_COLUMN_AMBIGUOUS")

    @staticmethod
    def _normalize_dependency_value(value: Any) -> str | int | float | bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not (float("-inf") < value < float("inf")):
                raise DependencyConstraintError("DEPENDENCY_FILTER_VALUE_INVALID")
            return value
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, str):
            if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
                raise DependencyConstraintError("DEPENDENCY_FILTER_VALUE_INVALID")
            return value
        raise DependencyConstraintError("DEPENDENCY_FILTER_VALUE_TYPE_UNSUPPORTED")

    async def _compile_dependency_constraints(
        self,
        task: AtomicTask,
        chat: ChatRequest,
        identity: TrustedIdentity,
        responses: dict[str, AgentResponse | Exception],
        conversation_by_task: dict[str, str],
    ) -> list[DependencyConstraint]:
        if self.dataset_store is None:
            raise DependencyConstraintError("DEPENDENCY_DATASET_STORE_UNAVAILABLE")
        constraints: list[DependencyConstraint] = []
        for dependency_id in task.depends_on:
            dependency = responses.get(dependency_id)
            if not isinstance(dependency, AgentResponse) or not dependency.dataset_id:
                raise DependencyConstraintError("DEPENDENCY_SOURCE_DATASET_MISSING")
            source_conversation = conversation_by_task.get(dependency_id)
            if not source_conversation:
                raise DependencyConstraintError("DEPENDENCY_SOURCE_SCOPE_MISSING")
            raw_items = await self.sessions.get_recent_dataset_references(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                source_conversation,
                limit=self.settings.dataset_recent_limit,
            )
            raw = next(
                (item for item in raw_items if item.get("dataset_id") == dependency.dataset_id),
                None,
            )
            if raw is None:
                raise DependencyConstraintError("DEPENDENCY_SOURCE_REFERENCE_MISSING")
            reference = restore_reference(raw)
            expected_scope = DatasetScope(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                source_conversation,
            )
            loaded = await asyncio.to_thread(
                self.dataset_store.load_dataset,
                reference,
                current_scope=expected_scope,
            )
            rows = tuple(dict(row) for row in loaded.rows)
            if reference.row_count != len(rows) or not rows or len(rows) > 1000:
                raise DependencyConstraintError("DEPENDENCY_SOURCE_DATASET_INCOMPLETE_OR_TOO_LARGE")
            column = self._select_dependency_column(task.question, reference.columns, rows)
            values: list[str | int | float | bool] = []
            seen: set[str] = set()
            for row in rows:
                raw_value = row.get(column)
                if raw_value is None:
                    continue
                value = self._normalize_dependency_value(raw_value)
                key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if key not in seen:
                    seen.add(key)
                    values.append(value)
            if not values:
                raise DependencyConstraintError("DEPENDENCY_FILTER_VALUES_EMPTY")
            if len(values) > 50:
                raise DependencyConstraintError("DEPENDENCY_FILTER_VALUES_EXCEED_LIMIT")
            canonical = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
            if len(canonical) > 3000:
                raise DependencyConstraintError("DEPENDENCY_FILTER_VALUES_EXCEED_SIZE_LIMIT")
            constraints.append(DependencyConstraint(
                source_task_id=dependency_id,
                source_dataset_id=dependency.dataset_id,
                source_column=column,
                values=values,
                value_fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            ))
        return constraints

    @staticmethod
    def _dependency_constraint_fallback(
        chat: ChatRequest,
        task: AtomicTask,
        code: str,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=uuid4(),
            conversation_id=chat.conversation_id,
            status="SAFE_FALLBACK",
            intent=PrimaryIntent.DETAIL_QUERY,
            intent_source="TASK_DAG_DEPENDENCY_GUARD",
            intent_confidence=1.0,
            answer=(
                "上游结果无法安全编译为下游查询的精确过滤条件，本子任务未执行；"
                "请明确要沿用的字段或缩小上游结果范围。"
            ),
            evidence=[EvidenceItem(
                evidence_id=f"dependency-constraint-rejected:{task.task_id}",
                kind="DEPENDENCY_CONSTRAINT_REJECTED",
                source_ref="task-dag",
                payload={"task_id": task.task_id, "code": code, "sql_executed": False},
            )],
            reliability=ReliabilityReport(
                level="FAIL",
                score=0.0,
                gates={"dependency_constraint_compiled": False, "sql_not_executed": True},
                warnings=["未在缺少受控依赖过滤的情况下执行全表查询。"],
            ),
        )

    @staticmethod
    def _attach_dependency_constraint_evidence(
        response: AgentResponse,
        constraints: list[DependencyConstraint],
    ) -> AgentResponse:
        evidence = list(response.evidence)
        evidence.extend(
            EvidenceItem(
                evidence_id=f"dependency-constraint:{item.source_task_id}",
                kind="DEPENDENCY_CONSTRAINT",
                source_ref=item.source_dataset_id,
                payload={
                    "source_task_id": item.source_task_id,
                    "source_column": item.source_column,
                    "values": item.values,
                    "value_count": len(item.values),
                    "value_fingerprint": item.value_fingerprint,
                    "scope_verified": True,
                    "asl_filter_verified_before_sql": True,
                },
            )
            for item in constraints
        )
        reliability = response.reliability
        if reliability is not None:
            reliability = reliability.model_copy(update={
                "gates": {**reliability.gates, "dependency_constraint_enforced": True},
            })
        return response.model_copy(update={"evidence": evidence, "reliability": reliability})

    async def _handle_task_plan(
        self,
        chat: ChatRequest,
        identity: TrustedIdentity,
        plan: TaskPlan,
        *,
        root_message_id: str | None = None,
        task_answers: dict[str, str] | None = None,
        dag_pending: dict[str, Any] | None = None,
    ) -> AgentResponse:
        """Execute validated DAG layers; independent tasks run concurrently."""
        if self.task_planner is None:
            raise RuntimeError("task planner is not configured")
        execution_plan, aliases = self.task_planner.deduplicate(plan)
        layers = self.task_planner.execution_layers(execution_plan)
        responses: dict[str, AgentResponse | Exception] = {}
        conversation_by_task: dict[str, str] = {}
        root_message_id = root_message_id or chat.message_id
        task_answers = task_answers or {}
        root_token = hashlib.sha256(
            f"{chat.conversation_id}:{root_message_id}".encode("utf-8")
        ).hexdigest()[:16]
        plan_fingerprint = hashlib.sha256(
            plan.model_dump_json().encode("utf-8")
        ).hexdigest()
        task_index_by_id = {
            task.task_id: index for index, task in enumerate(plan.tasks)
        }
        checkpoint = await self.sessions.get_dag_checkpoint(
            identity.tenant_id, identity.user_id, chat.application_id,
            chat.conversation_id, root_message_id,
        )
        if checkpoint and checkpoint.get("plan_fingerprint") == plan_fingerprint:
            for task_id, raw in checkpoint.get("completed", {}).items():
                try:
                    responses[task_id] = AgentResponse.model_validate(raw)
                    conversation_by_task[task_id] = str(
                        checkpoint.get("conversations", {}).get(task_id, "")
                    )
                except (TypeError, ValueError):
                    logger.warning("discarding invalid DAG task checkpoint: %s", task_id)
        # A task that received an explicit answer must re-enter its child
        # conversation so the existing PendingState can merge that answer.
        for answered_task_id in task_answers:
            responses.pop(answered_task_id, None)
        checkpoint_lock = asyncio.Lock()

        async def save_checkpoint() -> None:
            async with checkpoint_lock:
                completed = {
                    task_id: value.model_dump(mode="json")
                    for task_id, value in responses.items()
                    if isinstance(value, AgentResponse)
                }
                await self.sessions.put_dag_checkpoint(
                    identity.tenant_id, identity.user_id, chat.application_id,
                    chat.conversation_id, root_message_id,
                    {
                        "schema_version": "1.0",
                        "plan_fingerprint": plan_fingerprint,
                        "completed": completed,
                        "conversations": conversation_by_task,
                    },
                )

        async def run(task: AtomicTask) -> tuple[str, AgentResponse | Exception]:
            restored = responses.get(task.task_id)
            if isinstance(restored, AgentResponse):
                return task.task_id, restored
            dependency_responses = [
                responses.get(task_id) for task_id in task.depends_on
            ]
            failed_dependency = next(
                (
                    value for value in dependency_responses
                    if not isinstance(value, AgentResponse)
                    or value.status not in {"COMPLETED", "PARTIAL_SUCCESS"}
                ),
                None,
            )
            if failed_dependency is not None:
                return task.task_id, RuntimeError("DEPENDENCY_NOT_COMPLETED")
            contextual_root_task = (
                not task.depends_on
                and bool(re.match(r"\s*(?:再|那|那么|另外|还|加上|改成|换成)", task.question))
            )
            requires_new_entity = self._dag_requires_new_entity(
                task.question, dependency_responses
            )
            dependency_constraints: list[DependencyConstraint] = []
            if (
                requires_new_entity
                and task.depends_on
                and self._dag_references_dependency_result(task.question)
            ):
                try:
                    dependency_constraints = await self._compile_dependency_constraints(
                        task, chat, identity, responses, conversation_by_task
                    )
                except DependencyConstraintError as exc:
                    value = self._dependency_constraint_fallback(chat, task, str(exc))
                    responses[task.task_id] = value
                    await save_checkpoint()
                    return task.task_id, value
            conversation_id = (
                f"dag-{root_token}-{task.task_id}"
                if requires_new_entity
                else
                conversation_by_task[task.depends_on[0]]
                if task.depends_on
                else chat.conversation_id
                if contextual_root_task
                else f"dag-{root_token}-{task.task_id}"
            )
            conversation_by_task[task.task_id] = conversation_id
            # Internal task history is contextual evidence, not a source of
            # ordering timestamps. Remove timestamps consistently so ChatRequest
            # cannot receive a mixed timestamped/untimestamped history.
            history = [
                item.model_copy(update={"created_at": None}) for item in chat.history
            ]
            for dependency_id in task.depends_on:
                dependency = responses[dependency_id]
                if isinstance(dependency, AgentResponse):
                    dependency_task = next(
                        item for item in plan.tasks if item.task_id == dependency_id
                    )
                    history.extend([
                        HistoryMessage(role="user", content=dependency_task.question),
                        HistoryMessage(role="assistant", content=dependency.answer),
                    ])
            history = compact_history(history)
            child = ChatRequest(
                conversation_id=conversation_id,
                message_id=(
                    f"dag-{root_token}-{task.task_id}"
                    if task.task_id not in task_answers
                    else "dag-{}-{}-{}".format(
                        root_token,
                        task.task_id,
                        hashlib.sha256(chat.message_id.encode("utf-8")).hexdigest()[:12],
                    )
                ),
                question=(
                    task_answers[task.task_id]
                    if task.task_id in task_answers
                    else self._with_dependency_join_keys(
                        task.question, task.task_id, plan
                    )
                ),
                application_id=chat.application_id,
                semantic_model_id=chat.semantic_model_id,
                database_id=chat.database_id,
                business_domain_id=chat.business_domain_id,
                business_domain_ids=chat.business_domain_ids,
                knowledge_base_names=chat.knowledge_base_names,
                history=history,
                use_longterm_memory=chat.use_longterm_memory,
                tools=chat.tools,
                skills=chat.skills,
                mcp=chat.mcp,
                web_search=chat.web_search,
                temp_file_paths=chat.temp_file_paths,
                department=chat.department,
                dependency_constraints=dependency_constraints,
                dataset_id=next(
                    (
                        dependency.dataset_id
                        for dependency_id in reversed(task.depends_on)
                        if isinstance(
                            (dependency := responses.get(dependency_id)), AgentResponse
                        )
                        and dependency.dataset_id
                    ),
                    None,
                ) if not requires_new_entity else None,
            )
            try:
                with task_progress_scope(
                    parent_message_id=root_message_id,
                    task_id=task.task_id,
                    task_index=task_index_by_id[task.task_id],
                    task_count=len(plan.tasks),
                    is_child_task=True,
                ):
                    if len(task.depends_on) >= 2 and self._is_join_task(task.question):
                        value = await self._execute_cross_branch_join(
                            task, chat, identity, responses, conversation_by_task
                        )
                    else:
                        assumptions_token = _INTERNAL_ASSUMPTIONS.set(
                            self._report_task_internal_assumptions(plan, task)
                        )
                        try:
                            value = await self._handle(child, identity)
                        finally:
                            _INTERNAL_ASSUMPTIONS.reset(assumptions_token)
                        if dependency_constraints and value.status in {"COMPLETED", "PARTIAL_SUCCESS"}:
                            if not any(item.kind == "QUERY_RESULT" for item in value.evidence):
                                value = self._dependency_constraint_fallback(
                                    chat, task, "DEPENDENCY_QUERY_EVIDENCE_MISSING"
                                )
                            else:
                                value = self._attach_dependency_constraint_evidence(
                                    value, dependency_constraints
                                )
                responses[task.task_id] = value
                await save_checkpoint()
                return task.task_id, value
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # isolate one failed branch from siblings
                logger.exception("atomic task failed: %s", task.task_id)
                return task.task_id, exc

        for layer in layers:
            layer_results = await asyncio.gather(*(run(task) for task in layer))
            responses.update(layer_results)

        for alias_id, canonical_id in aliases.items():
            responses[alias_id] = responses[canonical_id]
            conversation_by_task[alias_id] = conversation_by_task.get(canonical_id, "")

        task_results: list[TaskExecutionResult] = []
        evidence: list[EvidenceItem] = []
        chart_specs = []
        clarification_questions: list[str] = []
        clarification_items: list[ClarificationItem] = []
        missing_slots: list[str] = []
        completed_count = 0
        dataset_id: str | None = None
        dataset_ids: list[str] = []
        answer_parts: list[str] = []
        scores: list[float] = []
        all_high = True
        for task in plan.tasks:
            value = responses[task.task_id]
            if isinstance(value, AgentResponse):
                if value.status in {"COMPLETED", "PARTIAL_SUCCESS"}:
                    completed_count += 1
                if value.dataset_id:
                    dataset_id = value.dataset_id
                    if value.dataset_id not in dataset_ids:
                        dataset_ids.append(value.dataset_id)
                if value.reliability:
                    scores.append(value.reliability.score)
                    all_high = all_high and value.reliability.level == "HIGH"
                else:
                    all_high = False
                prefixed_ids: list[str] = []
                for item in value.evidence:
                    evidence_id = f"{task.task_id}:{item.evidence_id}"
                    prefixed_ids.append(evidence_id)
                    evidence.append(item.model_copy(update={"evidence_id": evidence_id}))
                chart_specs.extend(value.chart_specs)
                clarification_questions.extend(
                    f"[{task.task_id}] {question}"
                    for question in value.clarification_questions
                )
                clarification_items.extend(
                    item.model_copy(update={"slot": f"{task.task_id}:{item.slot}"})
                    for item in value.clarification_items
                )
                missing_slots.extend(
                    f"{task.task_id}:{slot}" for slot in value.missing_slots
                )
                task_results.append(TaskExecutionResult(
                    task_id=task.task_id,
                    question=task.question,
                    status=value.status,
                    intent=value.intent,
                    answer=value.answer,
                    dataset_id=value.dataset_id,
                    result_file_url=value.result_file_url,
                    reliability=value.reliability,
                    chart_specs=value.chart_specs,
                    evidence_ids=prefixed_ids,
                ))
                answer_parts.append(f"### ◉ {task.question}\n{value.answer}")
            else:
                task_results.append(TaskExecutionResult(
                    task_id=task.task_id,
                    question=task.question,
                    status="SKIPPED" if str(value) == "DEPENDENCY_NOT_COMPLETED" else "FAILED",
                    answer=(
                        "依赖任务尚未完成，本任务未执行。"
                        if str(value) == "DEPENDENCY_NOT_COMPLETED"
                        else "该子任务执行失败，其他独立任务不受影响。"
                    ),
                ))
                answer_parts.append(
                    f"### ◉ {task.question}\n{task_results[-1].answer}"
                )
                all_high = False

        fully_completed_count = sum(
            result.status == "COMPLETED" for result in task_results
        )
        shared_clarification = self._shared_report_time_clarification(
            plan, responses
        )
        if shared_clarification is not None:
            first_response = responses[shared_clarification["task_ids"][0]]
            assert isinstance(first_response, AgentResponse)
            base_question = next(
                iter(first_response.clarification_questions),
                "这份报告要分析哪个时间范围？",
            )
            clarification_questions = [
                f"这 {len(plan.tasks)} 个分析维度共用同一时间范围。{base_question}"
            ]
            first_item = next(
                (
                    item for item in first_response.clarification_items
                    if item.slot == "time_range"
                ),
                None,
            )
            clarification_items = [
                (
                    first_item.model_copy(update={
                        "slot": "shared:time_range",
                        "title": "共享时间范围",
                        "question": clarification_questions[0],
                    })
                    if first_item is not None
                    else ClarificationItem(
                        slot="shared:time_range",
                        title="共享时间范围",
                        question=clarification_questions[0],
                        options=["本月", "上月", "最近30天", "自定义起止日期"],
                        multi_select=False,
                    )
                )
            ]
            missing_slots = ["shared:time_range"]
        if plan.final_deliverable == "COMBINED_REPORT":
            report_sections = []
            for task in plan.tasks:
                value = responses.get(task.task_id)
                if isinstance(value, AgentResponse):
                    report_sections.append({
                        "task_id": task.task_id,
                        "question": task.question,
                        "status": value.status,
                        "dataset_id": value.dataset_id,
                        "result_file_url": value.result_file_url,
                        "evidence_ids": [
                            f"{task.task_id}:{item.evidence_id}"
                            for item in value.evidence
                        ],
                        "query_proofs": [
                            item.payload
                            for item in value.evidence
                            if item.kind == "QUERY_RESULT"
                        ],
                    })
                else:
                    report_sections.append({
                        "task_id": task.task_id,
                        "question": task.question,
                        "status": "FAILED",
                        "dataset_id": None,
                        "result_file_url": None,
                        "evidence_ids": [],
                        "query_proofs": [],
                    })
            evidence.append(EvidenceItem(
                evidence_id=f"report-manifest:{root_message_id}",
                kind="REPORT_DATASET_MANIFEST",
                source_ref="task-dag",
                payload={
                    "section_count": len(report_sections),
                    "available_section_count": completed_count,
                    "fully_completed_section_count": fully_completed_count,
                    "dataset_ids": dataset_ids,
                    "sections": report_sections,
                },
            ))

        all_fully_completed = all(
            isinstance(responses.get(task.task_id), AgentResponse)
            and responses[task.task_id].status == "COMPLETED"
            for task in plan.tasks
        )
        report_has_truncated_section = (
            plan.final_deliverable == "COMBINED_REPORT"
            and any(
                item.kind == "QUERY_RESULT"
                and bool(item.payload.get("truncated"))
                for value in responses.values()
                if isinstance(value, AgentResponse)
                for item in value.evidence
            )
        )
        if clarification_questions:
            # Even when sibling tasks completed, this response still requires a
            # user action. Frontends can rely on status instead of inspecting
            # every child result for a hidden clarification.
            status = "NEEDS_CLARIFICATION"
        elif (
            completed_count == len(plan.tasks)
            and all_fully_completed
            and not report_has_truncated_section
        ):
            status = "COMPLETED"
        elif completed_count > 0:
            status = "PARTIAL_SUCCESS"
        else:
            status = "SAFE_FALLBACK"
        score = sum(scores) / len(plan.tasks) if scores else 0.0
        reliability = ReliabilityReport(
            level=("HIGH" if status == "COMPLETED" and all_high else "LIMITED" if completed_count else "FAIL"),
            score=score,
            gates={result.task_id: result.status == "COMPLETED" for result in task_results},
            warnings=(
                [] if status == "COMPLETED" and all_high
                else ["多任务中存在未完成、降级或需要补充信息的子任务"]
            ),
        )
        awaiting_task_ids = [
            task.task_id
            for task in plan.tasks
            if isinstance(responses.get(task.task_id), AgentResponse)
            and responses[task.task_id].status == "NEEDS_CLARIFICATION"
        ]
        resume_token: str | None = None
        if awaiting_task_ids:
            resume_token = chat.dag_resume_token or secrets.token_urlsafe(32)
            expected_version = int(dag_pending.get("state_version", 0)) if dag_pending else 0
            pending_state = {
                "schema_version": "1.0",
                "state_version": expected_version + 1,
                "resume_token_hash": hashlib.sha256(
                    resume_token.encode("utf-8")
                ).hexdigest(),
                "scope_fingerprint": self._dag_scope_fingerprint(chat),
                "root_message_id": root_message_id,
                "plan_fingerprint": plan_fingerprint,
                "task_plan": plan.model_dump(mode="json"),
                "child_conversations": conversation_by_task,
                "awaiting_task_ids": awaiting_task_ids,
                "clarification_questions": clarification_questions,
                "shared_clarification": shared_clarification,
            }
            try:
                await self.sessions.put_dag_pending(
                    identity.tenant_id, identity.user_id, chat.application_id,
                    chat.conversation_id, pending_state,
                    expected_version=expected_version,
                )
            except SessionConflictError:
                return self._dag_resume_error(
                    chat,
                    "多任务追问状态已被另一条消息更新，请基于最新响应继续。",
                )

        combined_answer = await self._composite_task_answer(
            chat=chat,
            identity=identity,
            plan=plan,
            task_results=task_results,
            conversation_by_task=conversation_by_task,
        )
        if shared_clarification is not None:
            combined_answer = (
                "## 综合分析报告\n"
                f"已识别 {len(plan.tasks)} 个独立分析维度。"
                "它们共同等待同一个时间范围，尚未执行查询。\n\n"
                + clarification_questions[0]
            )
        elif plan.final_deliverable == "COMBINED_REPORT":
            combined_answer = (
                "## 综合分析报告\n"
                f"已按 {len(plan.tasks)} 个独立分析维度执行并逐项保留数据证据；"
                f"完整完成 {fully_completed_count} 项，另有 "
                f"{completed_count - fully_completed_count} 项为部分结果。\n\n"
                + combined_answer
            )
        final_response = AgentResponse(
            request_id=uuid4(),
            conversation_id=chat.conversation_id,
            status=status,
            intent=(
                PrimaryIntent.REPORT_GENERATION
                if plan.final_deliverable == "COMBINED_REPORT"
                else task_results[0].intent or PrimaryIntent.OUT_OF_SCOPE
            ),
            intent_source="TASK_DAG",
            intent_confidence=min(
                (
                    value.intent_confidence for value in responses.values()
                    if isinstance(value, AgentResponse)
                ),
                default=0.6,
            ),
            execution_shape="COMPOSITE",
            task_intents=[
                result.intent
                for result in task_results
                if result.intent is not None
            ],
            answer=combined_answer,
            clarification_questions=clarification_questions[:5],
            clarification_items=clarification_items[:5],
            remaining_question_count=max(0, len(clarification_questions) - 5),
            missing_slots=missing_slots,
            evidence=evidence,
            reliability=reliability,
            dataset_id=(
                dataset_ids[0]
                if plan.final_deliverable == "COMBINED_REPORT" and len(dataset_ids) == 1
                else None
                if plan.final_deliverable == "COMBINED_REPORT"
                else dataset_id
            ),
            dataset_ids=dataset_ids,
            chart_specs=chart_specs[:10],
            task_plan=plan,
            task_results=task_results,
            dag_resume_token=resume_token,
            awaiting_task_ids=awaiting_task_ids,
            analysis_process=[AnalysisProcessStep(
                stage="UNDERSTANDING",
                status="COMPLETED",
                title="拆分并执行多个数据任务",
                summary=(
                    f"已拆分为{len(plan.tasks)}个原子任务，按照"
                    f"{len(layers)}层依赖DAG执行；完成{completed_count}个。"
                ),
            )],
        )
        if plan.final_deliverable == "COMBINED_REPORT" and not awaiting_task_ids:
            await self._attach_composite_report(
                final_response,
                chat=chat,
                identity=identity,
                plan=plan,
                responses=responses,
                conversation_by_task=conversation_by_task,
            )
        if not awaiting_task_ids:
            await self._persist_dag_root_context(
                chat=chat,
                identity=identity,
                plan=plan,
                responses=responses,
                conversation_by_task=conversation_by_task,
                root_message_id=root_message_id,
            )
            if dag_pending is not None:
                await self.sessions.clear_dag_pending(
                    identity.tenant_id, identity.user_id, chat.application_id,
                    chat.conversation_id,
                    expected_version=int(dag_pending.get("state_version", 0)),
                )
            await self.sessions.delete_dag_checkpoint(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id, root_message_id,
            )
        return final_response

    async def _persist_dag_root_context(
        self,
        *,
        chat: ChatRequest,
        identity: TrustedIdentity,
        plan: TaskPlan,
        responses: dict[str, AgentResponse | Exception],
        conversation_by_task: dict[str, str],
        root_message_id: str,
    ) -> None:
        """Keep verified DAG branch frames available to root-conversation follow-ups.

        Execution checkpoints are disposable recovery state.  Conversational
        focus is not: a later ``只看次要科室`` must still be able to replace a
        qualifier while retaining the branch's product/specification binding.
        """

        root_frames: list[CanonicalAnalysisRequest] = []
        for task in plan.tasks:
            response = responses.get(task.task_id)
            child_conversation = conversation_by_task.get(task.task_id)
            if (
                not isinstance(response, AgentResponse)
                or response.status not in {"COMPLETED", "PARTIAL_SUCCESS"}
                or not child_conversation
            ):
                continue
            child_request = await self.sessions.get_last_request(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                child_conversation,
            )
            if child_request is None:
                continue
            root_request = child_request.model_copy(
                deep=True,
                update={
                    "request_id": uuid4(),
                    "conversation_id": chat.conversation_id,
                    "original_question": task.question,
                    "analysis_thread_id": f"dag-thread-{root_message_id}",
                },
            )
            root_request.assumptions = list(dict.fromkeys([
                *root_request.assumptions,
                f"DAG_ROOT_CONTEXT_BRANCH={task.task_id}",
            ]))
            root_frames.append(root_request)

        for frame in root_frames:
            await self.sessions.put_task_frame(frame)
        if root_frames:
            # The final branch is the default focus; recent_task_frames retains
            # every sibling so explicit historical/branch references can select
            # another one without preserving the execution checkpoint.
            await self.sessions.put_last_request(root_frames[-1])

    @staticmethod
    def _shared_report_time_clarification(
        plan: TaskPlan,
        responses: dict[str, AgentResponse | Exception],
    ) -> dict[str, Any] | None:
        """Return one safe shared slot only when every report branch agrees.

        Shared propagation is deliberately limited to an explicit report DAG
        whose every branch is blocked solely by ``time_range``.  Entity,
        metric, field and semantic ambiguity answers can have branch-specific
        meanings and therefore continue to require task_answers.
        """
        if plan.final_deliverable != "COMBINED_REPORT" or len(plan.tasks) < 2:
            return None
        task_ids: list[str] = []
        for task in plan.tasks:
            value = responses.get(task.task_id)
            if (
                not isinstance(value, AgentResponse)
                or value.status != "NEEDS_CLARIFICATION"
                or value.missing_slots != ["time_range"]
            ):
                return None
            task_ids.append(task.task_id)
        return {"slot": "time_range", "task_ids": task_ids}

    @staticmethod
    def _report_task_internal_assumptions(
        plan: TaskPlan, task: AtomicTask
    ) -> tuple[str, ...]:
        """Bind relationship-detail report branches to the report's sales facts.

        A combined sales report applies one time range to its trend and
        relationship facets.  Coverage/cooperation child questions no longer
        contain the word ``销售`` after deterministic splitting, so the binding
        must be carried as trusted internal structure instead of guessed from a
        loose keyword.  Other reports and employee/master-data date questions
        receive no such assumption.
        """
        if plan.final_deliverable != "COMBINED_REPORT":
            return ()
        has_sales_fact_facet = any(
            re.search(r"销售(?:额|量|订单|记录|数据|趋势|走势|变化)", item.question)
            for item in plan.tasks
        )
        relationship_detail = bool(
            re.search(
                r"(?:医院|经销商|供应商|客户|门店|渠道).{0,12}"
                r"(?:覆盖|合作|数据|明细|名单|清单|数量|数)",
                task.question,
            )
            or re.search(
                r"(?:覆盖|合作).{0,8}"
                r"(?:医院|经销商|供应商|客户|门店|渠道)",
                task.question,
            )
        )
        return (
            (_SALES_RECORD_TIME_ASSUMPTION,)
            if has_sales_fact_facet and relationship_detail
            else ()
        )

    @staticmethod
    def _is_join_task(question: str) -> bool:
        return any(word in question for word in ("合并", "关联", "联表", "Join", "join", "结合上述", "结合前面"))

    async def _execute_cross_branch_join(
        self,
        task: AtomicTask,
        chat: ChatRequest,
        identity: TrustedIdentity,
        responses: dict[str, AgentResponse | Exception],
        conversation_by_task: dict[str, str],
    ) -> AgentResponse:
        if self.dataset_store is None:
            raise RuntimeError("DATASET_STORE_UNAVAILABLE")
        references = []
        for dependency_id in task.depends_on:
            dependency = responses.get(dependency_id)
            if not isinstance(dependency, AgentResponse) or not dependency.dataset_id:
                raise RuntimeError("JOIN_SOURCE_DATASET_MISSING")
            raw_items = await self.sessions.get_recent_dataset_references(
                identity.tenant_id, identity.user_id, chat.application_id,
                conversation_by_task[dependency_id], limit=self.settings.dataset_recent_limit,
            )
            raw = next(
                (item for item in raw_items if item.get("dataset_id") == dependency.dataset_id),
                None,
            )
            if raw is None:
                raise RuntimeError("JOIN_SOURCE_REFERENCE_MISSING")
            references.append(restore_reference(raw))
        shared = set(references[0].columns)
        for reference in references[1:]:
            shared &= set(reference.columns)
        candidates = sorted(
            column for column in shared
            if column.lower().endswith("_id") or column.endswith(("编码", "编号", "日期", "月份"))
        )
        mentioned = [column for column in candidates if column in task.question]
        # Never guess a same-named key: e.g. two columns called “日期” may mean
        # order date and registration date. The user/planner must name the key.
        join_keys = mentioned
        if not join_keys:
            raise RuntimeError("JOIN_KEY_AMBIGUOUS_OR_MISSING")
        target_scope = DatasetScope(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            application_id=chat.application_id,
            conversation_id=chat.conversation_id,
        )
        result = await asyncio.to_thread(
            self.dataset_store.join_datasets,
            references,
            current_scope=target_scope,
            join_keys=join_keys,
            ttl_seconds=self.settings.dataset_ttl_seconds,
        )
        await self.sessions.put_dataset_reference(
            result.reference.to_dict(), recent_limit=self.settings.dataset_recent_limit
        )
        preview = list(result.preview_rows)
        join_audit = dict(result.reference.transformation_log[-1])
        match_rates = [
            float(item.get("left_match_rate", 0.0))
            for item in join_audit.get("steps", [])
        ]
        minimum_match_rate = min(match_rates, default=1.0)
        snapshot_skew = float(join_audit.get("snapshot_skew_seconds", 0.0))
        limited = minimum_match_rate < 0.8 or snapshot_skew > 86_400
        warnings = []
        if minimum_match_rate < 0.8:
            warnings.append("关联匹配率低于80%，请核对关联键和两侧数据范围。")
        if snapshot_skew > 86_400:
            warnings.append("来源数据快照时间相差超过24小时，联合结果时效性受限。")
        return AgentResponse(
            request_id=uuid4(), conversation_id=chat.conversation_id,
            status="COMPLETED", intent=PrimaryIntent.DETAIL_QUERY,
            intent_source="TASK_DAG_JOIN", intent_confidence=1.0,
            answer=(f"已按{join_keys[0]}合并{len(references)}个分支数据集，"
                    f"得到{result.reference.row_count}行数据。"),
            dataset_id=result.reference.dataset_id,
            reliability=ReliabilityReport(
                level="LIMITED" if limited else "HIGH",
                score=0.75 if limited else 1.0,
                gates={
                    "scope": True, "join_key": True, "cardinality": True,
                    "match_coverage": minimum_match_rate >= 0.8,
                    "snapshot_alignment": snapshot_skew <= 86_400,
                },
                warnings=warnings,
            ),
            evidence=[EvidenceItem(
                evidence_id="join-result", kind="ANALYSIS_RESULT",
                source_ref=result.reference.dataset_id, payload={
                    "join_key": join_keys[0], "row_count": result.reference.row_count,
                    "join_keys": join_keys,
                    "join_audit": join_audit,
                    "preview": preview[:20],
                },
            )],
        )

    @staticmethod
    def _request_fingerprint(chat: ChatRequest, identity: TrustedIdentity) -> str:
        """Hash every effective input using a deterministic JSON representation."""
        payload = {
            "chat": chat.model_dump(mode="json"),
            # Roles are trusted request context and may affect authorization or
            # data visibility even though they are not part of the JSON body.
            "roles": sorted(set(identity.roles)),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_repeat_cache_candidate(chat: ChatRequest) -> bool:
        """Cache only self-contained business requests, never elliptical follow-ups."""
        compact = re.sub(r"\s+", "", chat.question).strip("，,。.!！?？")
        if chat.dataset_id is not None:
            return False
        if not re.match(r"^(?:请|帮我|给我)?(?:查询|统计|分析|生成|列出|查找)", compact):
            return False
        return not bool(re.match(
            r"^(?:那|那么|再|其中|这些|这个|它|他们|上述|刚才)", compact
        ))

    @staticmethod
    def _repeat_query_fingerprint(
        chat: ChatRequest, identity: TrustedIdentity
    ) -> str:
        normalized_question = re.sub(
            r"[，,。.!！?？;；\s]+", "", chat.question
        ).lower()
        payload = {
            "question": normalized_question,
            "semantic_model_id": chat.semantic_model_id,
            "database_id": chat.database_id,
            "business_domain_ids": sorted(chat.business_domain_ids),
            "knowledge_base_names": sorted(chat.knowledge_base_names),
            "roles": sorted(set(identity.roles)),
            "use_longterm_memory": chat.use_longterm_memory,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _effective_business_domain_id(chat: ChatRequest) -> int | None:
        """Return a hard domain constraint only when exactly one was selected.

        Oagnet already treats a null business_domain_id as semantic-model-wide
        retrieval and lets ASL resolve one or more relevant domains. Multiple
        requested/allowed domains therefore deliberately use that routing mode;
        a single explicit domain remains a strict backwards-compatible hint.
        """
        if len(chat.business_domain_ids) == 1:
            return chat.business_domain_ids[0]
        return None

    @classmethod
    def _effective_query_business_domain_id(
        cls,
        request: CanonicalAnalysisRequest,
        chat: ChatRequest,
    ) -> int | None:
        """Use a unique vector-resolved domain only in caller AUTO mode."""

        explicit = cls._effective_business_domain_id(chat)
        if explicit is not None:
            return explicit
        if not chat.business_domain_ids and len(request.resolved_business_domain_ids) == 1:
            return request.resolved_business_domain_ids[0]
        return None

    @staticmethod
    def _shift_month_start(value: date, months: int) -> date:
        ordinal = value.year * 12 + value.month - 1 + months
        year, month_index = divmod(ordinal, 12)
        return date(year, month_index + 1, 1)

    @classmethod
    def _watermark_default_trend_range(
        cls,
        request: CanonicalAnalysisRequest,
        dataset: Dataset,
    ) -> TimeRange | None:
        """Return 12 complete source months for a system-default trend range."""

        if (
            request.primary_intent != PrimaryIntent.TREND_ANALYSIS
            or request.time_range is None
            or "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" not in request.assumptions
            or dataset.source_data_as_of is None
        ):
            return None
        watermark = (
            dataset.source_data_as_of.date()
            if isinstance(dataset.source_data_as_of, datetime)
            else dataset.source_data_as_of
        )
        if request.time_range.end_exclusive <= watermark + timedelta(days=1):
            return None
        watermark_month = watermark.replace(day=1)
        next_month = cls._shift_month_start(watermark_month, 1)
        # A month is complete only when the verified source watermark reached
        # its final calendar day. Partial current months are deliberately not
        # mixed into a default trend without disclosure.
        end_exclusive = (
            next_month
            if watermark == next_month - timedelta(days=1)
            else watermark_month
        )
        start = cls._shift_month_start(end_exclusive, -12)
        candidate = TimeRange(
            start=start,
            end_exclusive=end_exclusive,
            timezone=request.time_range.timezone,
        )
        if candidate == request.time_range:
            return None
        return candidate

    async def _requery_system_default_trend_at_watermark(
        self,
        request: CanonicalAnalysisRequest,
        query_result: DataQueryResult,
        chat: ChatRequest,
        identity: TrustedIdentity,
    ) -> DataQueryResult:
        """Replan once when wall-clock defaults extend beyond business data."""

        reanchored = self._watermark_default_trend_range(
            request, query_result.dataset
        )
        if reanchored is None:
            return query_result
        retry_request = request.model_copy(deep=True, update={
            "request_id": uuid4(),
            "time_range": reanchored,
            "temporal_anchor": None,
            "resolved_periods": [],
            "asl_template": None,
            "source_dataset_id": None,
            "assumptions": list(dict.fromkeys([
                *(
                    value for value in request.assumptions
                    if value != "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR"
                ),
                "DEFAULT_TIME_RANGE=LATEST_AVAILABLE_12_COMPLETE_MONTHS",
                "TIME_SCOPE_SOURCE=SOURCE_WATERMARK",
                "SOURCE_WATERMARK_REPLAN_ATTEMPTED",
            ])),
        })
        retry_request.rewritten_question = render_execution_question(retry_request)
        await emit_progress(
            "DATA_RETRIEVAL",
            "RUNNING",
            "系统默认时间超过当前业务数据水位，正在按最新12个完整月份受控重查一次。",
            source_data_as_of=query_result.dataset.source_data_as_of.isoformat(),
            reanchored_start=reanchored.start.isoformat(),
            reanchored_end_exclusive=reanchored.end_exclusive.isoformat(),
        )
        try:
            retried = await self.adapters.query.query(
                retry_request,
                identity,
                semantic_model_id=chat.semantic_model_id,
                business_domain_id=self._effective_query_business_domain_id(
                    retry_request, chat
                ),
            )
        except AdapterError as exc:
            logger.warning(
                "source-watermark trend replan failed; preserving first verified result: "
                "request_id=%s code=%s upstream_code=%s",
                request.request_id,
                exc.code,
                exc.upstream_code,
            )
            request.assumptions.append("SOURCE_WATERMARK_REPLAN_FAILED")
            return query_result
        request.time_range = reanchored
        request.temporal_anchor = None
        request.resolved_periods = []
        request.asl_template = None
        request.source_dataset_id = None
        request.rewritten_question = retry_request.rewritten_question
        request.assumptions = list(dict.fromkeys(retry_request.assumptions))
        return retried

    async def _recover_live_published_metrics(
        self,
        request: CanonicalAnalysisRequest,
        chat: ChatRequest,
        identity: TrustedIdentity,
    ) -> bool:
        """Bind a newly published metric before asking the user to name it.

        Static intent vocabulary remains useful for fast classification, but it
        must not become a deployment-time copy of the semantic catalog. The
        live Oagnet discovery contract supplies SQL-verified canonical metrics;
        failure is non-destructive and leaves the existing clarification path.
        """
        rules = getattr(self.classifier, "rules", self.classifier)
        relationship_projection = bool(
            "适用科室" in request.fields
            or any(
                applicable_department_filter_slot(item.get("field"))
                == "applicable_department_relation_type"
                for item in request.filters
            )
            or applicable_department_relation_types(request.original_question)
        )
        if (
            relationship_projection
            or request.turn_relation == TurnRelation.AMBIGUOUS_RELATION
            or "turn_relation" in request.missing_slots
        ):
            # Metric discovery normalizes a measure the user actually named; it
            # must never invent a measure to repair a misclassified relationship
            # projection or an unresolved conversational reference.
            return False
        known_metrics = set(getattr(rules, "_known_metrics", ()))
        unbound_unknown_metric = bool(
            request.metrics
            and any(
                metric.metric_id is None
                and (metric.canonical_name or metric.input) not in known_metrics
                for metric in request.metrics
            )
        )
        if (
            (
                "metric" not in request.missing_slots
                and not unbound_unknown_metric
            )
            or request.primary_intent in NO_DATA_INTENTS
            or chat.semantic_model_id is None
        ):
            return False
        discover = getattr(self.adapters.query, "discover_metrics", None)
        if not callable(discover):
            return False
        try:
            discovery = await discover(
                request,
                identity,
                semantic_model_id=chat.semantic_model_id,
                business_domain_id=self._effective_query_business_domain_id(
                    request, chat
                ),
            )
        except (AdapterError, httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning(
                "live semantic metric discovery degraded: request_id=%s error=%s",
                request.request_id,
                type(exc).__name__,
            )
            return False
        if not discovery.metrics:
            # A user-visible value can be a published entity attribute rather
            # than an aggregate indicator. Try this only for a plain metric
            # lookup after live metric resolution found nothing; analytical
            # intents (trend, comparison, ranking, etc.) must keep their metric
            # contract instead of being silently downgraded to raw rows.
            if (
                request.primary_intent != PrimaryIntent.METRIC_QUERY
                or any(
                    operator != AnalysisOperator.FILTER
                    for operator in request.operators
                )
                or request.ranking_limit is not None
            ):
                return False
            discover_attributes = getattr(
                self.adapters.query, "discover_attribute_details", None
            )
            if not callable(discover_attributes):
                return False
            try:
                attribute_discovery = await discover_attributes(
                    request,
                    identity,
                    semantic_model_id=chat.semantic_model_id,
                    business_domain_id=self._effective_query_business_domain_id(
                        request, chat
                    ),
                )
            except (AdapterError, httpx.HTTPError, ValueError, TypeError) as exc:
                logger.warning(
                    "live semantic attribute discovery degraded: request_id=%s error=%s",
                    request.request_id,
                    type(exc).__name__,
                )
                return False
            if not attribute_discovery.subject or not attribute_discovery.dimensions:
                return False
            if request.semantic_entity_mentions and not attribute_discovery.filters:
                # Do not turn a source-scoped question into an unfiltered table
                # scan when its literal entity values were not proven.
                return False
            request.primary_intent = PrimaryIntent.DETAIL_QUERY
            request.metrics = []
            request.entity = attribute_discovery.subject
            request.fields = list(attribute_discovery.dimensions)
            request.dimensions = []
            request.filters = [dict(item) for item in attribute_discovery.filters]
            request.semantic_entity_mentions = []
            request.rewritten_question = request.original_question
            request.assumptions.extend((
                "QUERY_SHAPE_TRANSFORM=METRIC_TO_PUBLISHED_ATTRIBUTE_DETAIL",
                "ATTRIBUTE_BINDING_SOURCE=LIVE_SEMANTIC_SNAPSHOT",
                (
                    "ATTRIBUTE_DISCOVERY_EVIDENCE="
                    f"{attribute_discovery.evidence_fingerprint or 'UNAVAILABLE'}"
                ),
            ))
            if request.turn_admission is not None:
                explicit_slots = (
                    request.turn_admission.current_turn_facts.explicit_slots
                )
                if "filters" in explicit_slots:
                    explicit_slots["filters"].value = [
                        dict(item) for item in attribute_discovery.filters
                    ]
                if "fields" in explicit_slots:
                    explicit_slots["fields"].value = list(
                        attribute_discovery.dimensions
                    )
            required_missing_slots = getattr(
                rules, "required_missing_slots", None
            )
            if callable(required_missing_slots):
                request.missing_slots = required_missing_slots(request)
            request.assumptions = list(dict.fromkeys(request.assumptions))
            return not request.missing_slots
        request.metrics = [metric.model_copy(deep=True) for metric in discovery.metrics]
        # The same live, source-validated preview that identified an unknown
        # metric also has stronger schema grounding than classifier heuristics.
        # Replace structural guesses as one atomic semantic frame so a city
        # prefix inside an institution name cannot survive as a fake region
        # filter (and analogous future entity shapes update automatically).
        request.entity = discovery.subject
        request.dimensions = list(dict.fromkeys(discovery.dimensions))
        request.filters = [dict(item) for item in discovery.filters]
        # The discovery ASL has already converted every accepted literal into
        # a SQL-verified filter.  Keeping broad model-extracted noun fragments
        # (for example ``各经销商的区域``) would make final ASL validation treat
        # them as additional unresolved entity values and reject an otherwise
        # complete canonical plan.
        request.semantic_entity_mentions = []
        request.rewritten_question = request.original_question
        if request.turn_admission is not None:
            explicit_slots = request.turn_admission.current_turn_facts.explicit_slots
            for slot_name, value in (
                ("entity", discovery.subject),
                ("dimensions", list(discovery.dimensions)),
                ("filters", [dict(item) for item in discovery.filters]),
            ):
                slot = explicit_slots.get(slot_name)
                if slot is not None:
                    slot.value = value
        request.assumptions.extend((
            "METRIC_BINDING_SOURCE=LIVE_SQL_VERIFIED_SEMANTIC_SNAPSHOT",
            "QUERY_FRAME_SOURCE=LIVE_SQL_VERIFIED_SEMANTIC_SNAPSHOT",
            f"METRIC_DISCOVERY_EVIDENCE={discovery.evidence_fingerprint or 'UNAVAILABLE'}",
        ))
        if (
            discovery.time_independent_snapshot
            and "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in request.assumptions
        ):
            request.time_range = None
            request.assumptions = [
                value for value in request.assumptions
                if value != "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR"
            ]
            request.assumptions.extend((
                "TIME_SCOPE=ALL_TIME",
                "TIME_SCOPE_SOURCE=LIVE_SEMANTIC_SNAPSHOT_METRIC",
            ))
        required_missing_slots = getattr(rules, "required_missing_slots", None)
        if callable(required_missing_slots):
            request.missing_slots = required_missing_slots(request)
        request.assumptions = list(dict.fromkeys(request.assumptions))
        return "metric" not in request.missing_slots

    @staticmethod
    def _pending_scope_matches(request: CanonicalAnalysisRequest, chat: ChatRequest) -> bool:
        return (
            request.semantic_model_id == chat.semantic_model_id
            and request.database_id == chat.database_id
            and sorted(request.business_domain_ids) == sorted(chat.business_domain_ids)
            and sorted(request.knowledge_base_names) == sorted(chat.knowledge_base_names)
            and (
                chat.dataset_id is None
                or request.source_dataset_id == chat.dataset_id
            )
        )

    async def _handle(self, chat: ChatRequest, identity: TrustedIdentity) -> AgentResponse:
        preserve_merged_question = False
        recalled_task_frame = False
        admission_question, _ = QuestionRewriter._normalize_polite_word_order(
            chat.question
        )
        admission_question, _ = (
            QuestionRewriter._normalize_grouped_calculation_wording(
                admission_question
            )
        )
        await emit_progress(
            "CONTEXT_RESTORE", "RUNNING", "正在恢复当前会话的短期上下文和待补充状态。"
        )
        pending = (
            None
            if chat._is_regeneration_execution
            else await self.sessions.get_pending(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
            )
        )
        # Recognize an unmistakable standalone chat turn before applying any
        # business task frame. Otherwise a previous data query can rewrite a
        # later lifestyle question back into the old product/dealer task.
        raw_rule_request = self._classify_with_rules(
            admission_question, identity, chat.conversation_id
        )
        independent_chat = raw_rule_request.primary_intent == PrimaryIntent.CHAT
        standalone_complete_business = bool(
            pending is None
            and raw_rule_request.conversation_control == ConversationControl.NEW_REQUEST
            and not raw_rule_request.missing_slots
            and re.match(
                r"^(?:请|帮我|给我)?(?:"
                r"(?:按(?:日|天|周|月|季度|季|年)(?:查询|统计|汇总|分析|展示|显示))|"
                r"(?:查询|统计|列出|展示|显示|查看)"
                r")",
                re.sub(r"\s+", "", admission_question),
            )
            and raw_rule_request.primary_intent in {
                PrimaryIntent.METRIC_QUERY, PrimaryIntent.DETAIL_QUERY,
            }
        )
        deterministic_business_fast_path = bool(
            standalone_complete_business and not self.settings.intent_model_enabled
        )
        if deterministic_business_fast_path:
            # This path deliberately skips the model only when the utterance is
            # an explicit, self-contained query with all required slots.  The
            # generic rule default (0.60) is not a calibrated score for this
            # deterministic gate and would incorrectly look uncertain in UI.
            raw_rule_request.intent_source = "DETERMINISTIC_RULE_FAST_PATH"
            raw_rule_request.intent_confidence = 0.95
        if independent_chat and pending is not None:
            await self.sessions.clear_pending(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
                expected_version=pending.state_version,
            )
            pending = None
        if pending is not None and not self._pending_scope_matches(pending.request, chat):
            # A clarification belongs to the semantic/data scope in which it was
            # created. Never merge it into a request after the caller switches
            # model, domains, KB binding or explicit dataset.
            await self.sessions.clear_pending(
                identity.tenant_id, identity.user_id, chat.application_id,
                chat.conversation_id, expected_version=pending.state_version,
            )
            pending = None
        previous_for_rewrite = (
            None
            if standalone_complete_business or chat._is_regeneration_execution
            else pending.request if pending else await self.sessions.get_task_frame(
                identity.tenant_id, identity.user_id, chat.application_id, chat.conversation_id
            )
        )
        if (
            not chat._is_regeneration_execution
            and pending is None
            and recalls_prior_task(chat.question)
        ):
            recall = getattr(self.sessions, "get_recent_task_frames", None)
            recalled_frames = (
                await recall(
                    identity.tenant_id,
                    identity.user_id,
                    chat.application_id,
                    chat.conversation_id,
                    limit=12,
                )
                if callable(recall) else []
            )
            recalled = select_recalled_task_frame(chat.question, recalled_frames)
            if recalled is not None and self._pending_scope_matches(recalled, chat):
                previous_for_rewrite = recalled
                recalled_task_frame = True
        if independent_chat:
            previous_for_rewrite = None
        if previous_for_rewrite is not None and not self._pending_scope_matches(
            previous_for_rewrite, chat
        ):
            previous_for_rewrite = None
        if (
            not chat._is_regeneration_execution
            and not independent_chat
            and previous_for_rewrite is None
            and pending is None
        ):
            previous_for_rewrite = await self.sessions.get_last_request(
                identity.tenant_id, identity.user_id, chat.application_id, chat.conversation_id
            )
        elif (
            not chat._is_regeneration_execution
            and pending is None
            and not independent_chat
        ):
            # A task frame is provisional: it is written before ASL/SQL runs and
            # may contain an entity or slot interpretation that execution later
            # rejected.  Once a verified request exists, it is the only safe base
            # for an elliptical follow-up.  The provisional frame remains useful
            # only for a cold conversation whose very first upstream execution
            # failed, preserving the existing recovery behaviour in that case.
            completed = await self.sessions.get_last_request(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
            )
            if (
                completed is not None
                and completed.asl_template is not None
                and self._pending_scope_matches(completed, chat)
            ):
                previous_for_rewrite = completed.model_copy(deep=True)
                if "VERIFIED_EXECUTION_FRAME_SELECTED" not in (
                    previous_for_rewrite.assumptions
                ):
                    previous_for_rewrite.assumptions.append(
                        "VERIFIED_EXECUTION_FRAME_SELECTED"
                    )
        turn_decision = self.turn_admission_gate.evaluate(
            question=admission_question,
            current=raw_rule_request,
            previous=previous_for_rewrite,
            message_id=chat.message_id,
            pending=pending is not None,
        )
        turn_decision.selected_thread_id = (
            f"thread-{uuid4()}"
            if turn_decision.create_new_analysis_thread
            or previous_for_rewrite is None
            else previous_for_rewrite.analysis_thread_id
            or f"thread-{previous_for_rewrite.request_id}"
        )
        turn_decision.selected_episode_id = (
            str(previous_for_rewrite.request_id)
            if turn_decision.inherit_business_context
            and previous_for_rewrite is not None
            else None
        )
        if turn_decision.relation == TurnRelation.STANDALONE_NEW_TOPIC:
            # The raw-turn gate runs before contextual rewriting.  Once it has
            # proved this is a complete new topic, old task state must not be
            # supplied to the rewriter or the later merge path.
            previous_for_rewrite = None
        await self._append_session_event(
            chat=chat,
            identity=identity,
            event_type=SessionEventType.TURN_ADMISSION,
            trace_id=str(raw_rule_request.request_id),
            payload={
                "turn_relation": turn_decision.relation.value,
                "context_mode": turn_decision.context_mode.value,
                "self_contained": turn_decision.current_turn_facts.is_self_contained,
                "context_dependency": turn_decision.context_dependent,
                "core_subject_changed": turn_decision.core_subject_changed,
                "reference_signals": turn_decision.current_turn_facts.reference_signals,
                "followup_signals": turn_decision.current_turn_facts.followup_signals,
                "omitted_slots": turn_decision.current_turn_facts.omitted_slots,
                "temporal_reference": turn_decision.current_turn_facts.temporal_references,
                "inheritance_allowed": turn_decision.inherit_business_context,
                "inheritance_slots": turn_decision.inheritance_slots,
                "protected_slots": turn_decision.protected_slots,
                "cleared_slots": turn_decision.cleared_slots,
                "previous_thread": turn_decision.previous_thread_id,
                "selected_thread": turn_decision.selected_thread_id,
                "new_thread_created": turn_decision.create_new_analysis_thread,
                "reason_codes": turn_decision.reason_codes,
            },
        )
        await emit_progress(
            "CONTEXT_RESTORE",
            "COMPLETED",
            "会话上下文恢复完成。" if previous_for_rewrite is not None else "当前为新会话任务。",
        )
        rewrite = None
        classification_question = chat.question
        await emit_progress(
            "QUESTION_REWRITE", "RUNNING", "正在结合上下文和实体别名规范化问题。"
        )
        if (
            self.question_rewriter is not None
            and not independent_chat
            and not deterministic_business_fast_path
        ):
            rewrite = await self.question_rewriter.rewrite(
                chat.question,
                previous=previous_for_rewrite,
                semantic_model_id=chat.semantic_model_id,
                business_domain_id=self._effective_business_domain_id(chat),
                business_domain_ids=list(chat.business_domain_ids),
                force_context=turn_decision.inherit_business_context,
            )
            classification_question = rewrite.rewritten_question
        await emit_progress(
            "QUESTION_REWRITE",
            "DEGRADED" if rewrite is not None and rewrite.degraded else "COMPLETED",
            (
                "实体规范化服务不可用，已安全保留原问题。"
                if rewrite is not None and rewrite.degraded
                else "问题规范化完成。"
            ),
        )
        await emit_progress(
            "INTENT_RECOGNITION", "RUNNING", "正在识别查询意图和关键分析参数。"
        )
        model_entity_mentions: list[str] = []
        filter_semantic_ambiguities: list[SemanticAmbiguity] = []
        if pending:
            explicit_replacement_task = bool(re.search(
                r"(?:算了|不想问|不问了|不用了|换个问题|新问题|新任务)"
                r".{0,40}(?:查询|统计|分析|解释|生成|列出|预测)",
                chat.question,
            ))
            deterministic_slot_reply = self._is_deterministic_pending_reply(
                chat.question, pending.request
            )
            clarification_answer = (
                chat.question if deterministic_slot_reply else classification_question
            )
            incoming = (
                self._classify_with_rules(
                    clarification_answer, identity, chat.conversation_id
                )
                if deterministic_slot_reply
                else await self._classify(
                    classification_question, identity, chat.conversation_id
                )
            )
            if (
                deterministic_slot_reply
                and pending.request.missing_slots == ["comparison_objects"]
            ):
                # A bare list of company names naturally classifies as CHAT in
                # isolation. It is nevertheless a clarification response in
                # this state, including when the submitted count is wrong and
                # the same prompt must remain active.
                incoming.conversation_control = (
                    ConversationControl.CLARIFICATION_RESPONSE
                )
            merged = self.classifier.merge_clarification(
                pending.request.model_copy(deep=True), clarification_answer
            )
            resolved_slots = set(pending.request.missing_slots) - set(merged.missing_slots)
            if explicit_replacement_task or self._should_replace_pending(
                pending.request, incoming, resolved_slots, raw_question=chat.question
            ):
                # A clear new task must not be forced into an unrelated pending
                # clarification.  Delete the old state before creating a possible
                # new pending state so its CAS starts from version zero.
                await self.sessions.clear_pending(
                    identity.tenant_id,
                    identity.user_id,
                    chat.application_id,
                    chat.conversation_id,
                    expected_version=pending.state_version,
                )
                # The contextual rewrite was produced with the stale pending
                # task. Once the raw message is a new task, classify that raw
                # message again so no old slots leak into the new request.
                request = (
                    self._classify_with_rules(
                        chat.question, identity, chat.conversation_id
                    )
                    if explicit_replacement_task
                    else await self._classify(
                        chat.question, identity, chat.conversation_id
                    )
                )
                rounds = 1
            else:
                request = self._preserve_pending_execution_contract(
                    pending.request,
                    merged,
                    clarification_answer=clarification_answer,
                    incoming=incoming,
                )
                preserve_merged_question = True
                request.pending_state_version = pending.state_version
                rounds = pending.clarification_rounds + 1
            if deterministic_slot_reply:
                request.assumptions.append("DETERMINISTIC_SLOT_FAST_PATH")
        else:
            request = (
                raw_rule_request
                if independent_chat or deterministic_business_fast_path
                else await self._classify(
                    classification_question, identity, chat.conversation_id
                )
            )
            current_request = request.model_copy(deep=True)
            self.turn_admission_gate.reconcile_model_relation(
                decision=turn_decision,
                current=current_request,
                previous=previous_for_rewrite,
            )
            self.turn_admission_gate.promote_model_entity_replacement(
                decision=turn_decision,
                current=current_request,
                previous=previous_for_rewrite,
                raw_question=chat.question,
            )
            model_entity_mentions = list(
                current_request.semantic_entity_mentions
            )
            rounds = 1
            # The raw-turn admission decision is authoritative.  Legacy
            # history recovery used to treat any analysis beginning with
            # "按..." as contextual and could therefore re-merge the latest
            # history item even after the gate had proved that this was a
            # complete, standalone topic.  That produced hybrid requests such
            # as a current product filter plus the previous turn's entity and
            # dimensions.
            history_recovery = (
                self._history_recovery_candidate(chat)
                if turn_decision.inherit_business_context
                else None
            )
            contextual_analysis = (
                turn_decision.inherit_business_context
                and self._is_contextual_analysis_follow_up(request)
            )
            if (
                turn_decision.inherit_business_context
                and (
                    request.conversation_control in {
                        ConversationControl.FOLLOW_UP,
                        ConversationControl.CORRECTION,
                    }
                    or (rewrite is not None and rewrite.context_applied)
                    or history_recovery is not None
                    or contextual_analysis
                )
            ):
                previous = previous_for_rewrite
                if previous is None:
                    previous_question = (
                        history_recovery[0]
                        if history_recovery is not None
                        else self._latest_previous_user_question(chat)
                    )
                    if previous_question:
                        classified_previous = self.classifier.classify(
                            previous_question, identity, chat.conversation_id
                        )
                        previous = (
                            await classified_previous
                            if inspect.isawaitable(classified_previous)
                            else classified_previous
                        )
                        previous.application_id = chat.application_id
                if previous and (
                    request.conversation_control in {
                        ConversationControl.FOLLOW_UP,
                        ConversationControl.CORRECTION,
                    }
                    or (rewrite is not None and rewrite.context_applied)
                    or history_recovery is not None
                    or previous.missing_slots
                    or contextual_analysis
                ):
                    merged = self.classifier.merge_clarification(
                        previous.model_copy(deep=True), chat.question
                    )
                    resolved_slots = set(previous.missing_slots) - set(
                        merged.missing_slots
                    )
                    history_is_new_task = (
                        (
                            history_recovery is not None
                            or bool(re.search(
                                r"(?:算了|不想问|不问了|换个问题|新问题|新任务|重新开始)"
                                r".{0,30}(?:查询|统计|分析|解释|生成|列出)",
                                chat.question,
                            ))
                        )
                        and self._should_replace_pending(
                            previous, current_request, resolved_slots
                        )
                    )
                    if not history_is_new_task:
                        request = merged
                        preserve_merged_question = True
                        request.request_id = uuid4()
                        request.intent_source = current_request.intent_source
                        request.intent_confidence = current_request.intent_confidence
                        request.intent_candidates = list(
                            current_request.intent_candidates
                        )
                        request.assumptions = list(dict.fromkeys([
                            *request.assumptions,
                            *current_request.assumptions,
                        ]))
                        # CANCEL/CORRECTION are decisions from the current user
                        # message and must survive history cold recovery.
                        if request.conversation_control not in {
                            ConversationControl.CANCEL,
                            ConversationControl.CORRECTION,
                        }:
                            request.conversation_control = (
                                ConversationControl.FOLLOW_UP
                                if history_recovery is None
                                else ConversationControl.CLARIFICATION_RESPONSE
                            )
                        if (
                            not re.search(
                                r"(?:展示|显示|返回|查看)前?\d{1,5}条(?:数据|结果)?",
                                re.sub(r"\s+", "", chat.question),
                            )
                            and (
                                current_request.primary_intent in ANALYSIS_INTENTS
                                or _requires_deterministic_analysis(current_request)
                            )
                            and (
                            contextual_analysis
                            or current_request.primary_intent != previous.primary_intent
                            )
                        ):
                            request.primary_intent = current_request.primary_intent
                            request.secondary_intents = current_request.secondary_intents
                            request.operators = current_request.operators
                            # An analytical follow-up can change the execution
                            # shape, not merely the intent label.  Carry the
                            # slots extracted from the context-enriched current
                            # utterance as one atomic contract; otherwise a
                            # detail -> ranked metric transition retains the
                            # old fields and immediately asks for metric and
                            # dimension again.
                            if current_request.metrics:
                                request.metrics = [
                                    item.model_copy(deep=True)
                                    for item in current_request.metrics
                                ]
                            if current_request.dimensions:
                                request.dimensions = list(current_request.dimensions)
                            if (
                                previous.entity
                                and re.search(
                                    r"(?:其中|这些|哪个|哪一个|谁)?.{0,20}"
                                    r"(?:最高|最低|最大|最小)",
                                    chat.question,
                                )
                            ):
                                # Dimensions mentioned inside inherited filter
                                # JSON (for example 经销商名称) are eligibility
                                # constraints, not ranking groups.  The object
                                # listed by the preceding detail query is the
                                # sole grouping dimension for “其中最高的是哪个”.
                                request.dimensions = [previous.entity]
                            if current_request.entity:
                                request.entity = current_request.entity
                            if current_request.ranking_limit is not None:
                                request.ranking_limit = current_request.ranking_limit
                            if current_request.primary_intent != PrimaryIntent.DETAIL_QUERY:
                                request.fields = []
                            rules = getattr(self.classifier, "rules", self.classifier)
                            required_missing_slots = getattr(
                                rules, "required_missing_slots", None
                            )
                            if callable(required_missing_slots):
                                request.missing_slots = required_missing_slots(request)
                        request.rewritten_question = render_execution_question(request)

        if rewrite is not None:
            model_completion_applied = (
                "MODEL_QUESTION_COMPLETION_APPLIED" in request.assumptions
            )
            if not preserve_merged_question:
                request.original_question = rewrite.original_question
                if not model_completion_applied:
                    request.rewritten_question = rewrite.rewritten_question
            request.rewrite_context_applied = rewrite.context_applied
            request.rewrite_degraded = rewrite.degraded
            request.rewrite_events = [event.__dict__ for event in rewrite.events]
            if rewrite.semantic_model_version:
                request.semantic_model_version = rewrite.semantic_model_version
            if model_completion_applied:
                request.rewrite_events.append({
                    "original": rewrite.rewritten_question,
                    "canonical": request.rewritten_question or rewrite.rewritten_question,
                    "kind": "MODEL_QUESTION_COMPLETION",
                    "confidence": request.intent_confidence,
                    "attribute_code": None,
                })
            if (
                rewrite.context_applied
                and QuestionRewriter.is_deterministic_time_update(chat.question)
                and previous_for_rewrite is not None
                and previous_for_rewrite.semantic_model_id == chat.semantic_model_id
                and previous_for_rewrite.database_id == chat.database_id
                and sorted(previous_for_rewrite.business_domain_ids)
                == sorted(chat.business_domain_ids)
            ):
                request.assumptions.append("DETERMINISTIC_TIME_FAST_PATH")

        # Do not make verified-query reuse depend on whether the contextual
        # rewriter happened to label a short utterance as context_applied.  The
        # deterministic recognizer itself is deliberately strict and only
        # accepts a closed-form time replacement.  Carry the last verified ASL
        # explicitly so every consecutive time refinement keeps its grounded
        # entity joins and filters.
        if (
            QuestionRewriter.is_deterministic_time_update(chat.question)
            and previous_for_rewrite is not None
            and previous_for_rewrite.asl_template is not None
            and previous_for_rewrite.semantic_model_id == chat.semantic_model_id
            and previous_for_rewrite.database_id == chat.database_id
            and sorted(previous_for_rewrite.business_domain_ids)
            == sorted(chat.business_domain_ids)
        ):
            request.asl_template = copy.deepcopy(previous_for_rewrite.asl_template)
            request.assumptions.append("DETERMINISTIC_TIME_FAST_PATH")

        if rewrite is not None and rewrite.semantic_matches:
            # The entity-attribute endpoint is scoped to the current semantic
            # model/domain.  Use its latest dimension labels for both the raw
            # provenance frame and the executable request before current-turn
            # slot protection runs. Accepted vector hits also make their stored
            # entity value authoritative over the model-extracted surface form.
            QuestionRewriter.ground_request_dimensions(
                raw_rule_request, rewrite.semantic_matches
            )
            QuestionRewriter.ground_request_dimensions(
                request, rewrite.semantic_matches
            )
            if "SEMANTIC_DIMENSIONS_GROUNDED_FROM_CURRENT_MODEL" in (
                raw_rule_request.assumptions
            ):
                self.turn_admission_gate.rebind_current_semantic_shape(
                    turn_decision, raw_rule_request
                )

        admission_base = request
        if (
            pending is None
            and turn_decision.inherit_business_context
            and previous_for_rewrite is not None
        ):
            # Context inheritance is a structured state restore followed by a
            # current-turn delta.  ``merge_clarification`` predates this gate
            # and can legitimately produce an empty metric/filter set for an
            # elliptical comparison; do not let that lossy intermediate state
            # become authoritative.
            admission_base = previous_for_rewrite.model_copy(
                deep=True,
                update={
                    "request_id": request.request_id,
                    "original_question": request.original_question,
                    "rewritten_question": request.rewritten_question,
                    "rewrite_events": list(request.rewrite_events),
                    "rewrite_context_applied": request.rewrite_context_applied,
                    "rewrite_degraded": request.rewrite_degraded,
                    "conversation_control": ConversationControl.FOLLOW_UP,
                    "intent_source": request.intent_source,
                    "intent_confidence": request.intent_confidence,
                    "intent_candidates": list(request.intent_candidates),
                    "assumptions": list(dict.fromkeys([
                        *previous_for_rewrite.assumptions,
                        *request.assumptions,
                    ])),
                    "missing_slots": [],
                },
            )
        request = self.turn_admission_gate.apply_explicit_slot_protection(
            admission_base,
            raw_rule_request,
            turn_decision,
        )
        # A relationship qualifier replacement (for example ``主要科室`` ->
        # ``次要科室``) changes an executable ASL filter even though the
        # subject entity and projection stay the same.  The admission base is
        # intentionally cloned from the last completed request, so it also
        # carries that request's verified ASL.  Reusing the template here
        # would send both the stale relation_type=1 and the current
        # relation_type=2 to the semantic planner; the SQL alignment gate then
        # correctly rejects the result as STALE_CONTEXT_CONFLICT.  Force a
        # fresh plan whenever the current turn explicitly supplies this
        # semantic relationship slot.
        current_relation_filters = [
            item
            for item in raw_rule_request.filters
            if applicable_department_filter_slot(item.get("field"))
            == "applicable_department_relation_type"
        ]
        if current_relation_filters:
            request.asl_template = None
            request.source_dataset_id = None
            request.assumptions = list(dict.fromkeys([
                *request.assumptions,
                "RELATIONSHIP_QUALIFIER_REPLAN_REQUIRED",
            ]))
        # Downstream sanitizers need admission provenance to distinguish a
        # confirmed inherited entity value from a stale value that leaked into
        # a new topic.  Attach the decision before those sanitizers run; the
        # finalized context snapshot is still refreshed later in this method.
        request.turn_admission = turn_decision
        relationship_count_shape = next(
            (
                value for value in raw_rule_request.assumptions
                if value.startswith("RELATIONSHIP_COUNT_PROJECTION=")
            ),
            None,
        )
        if relationship_count_shape is not None:
            # “一共有多少家经销商” changes a relationship list into one
            # distinct-count measure. Preserve the verified subject filters,
            # but make the current result shape authoritative; otherwise the
            # inherited DETAIL_QUERY executes an unfiltered full list and the
            # renderer merely counts those rows.
            request.primary_intent = PrimaryIntent.METRIC_QUERY
            request.secondary_intents = []
            request.metrics = [
                metric.model_copy(deep=True)
                for metric in raw_rule_request.metrics
            ]
            request.entity = None
            request.fields = []
            counted_role = relationship_count_shape.split("=", 1)[1]
            request.dimensions = [
                value for value in request.dimensions
                if value != counted_role
            ]
            count_context = previous_for_rewrite
            if (
                count_context is None
                and not re.search(
                    r"切换话题|换个话题|另一个问题|重新开始|不看(?:之前|上面)",
                    chat.question,
                )
            ):
                count_context = await self.sessions.get_last_request(
                    identity.tenant_id,
                    identity.user_id,
                    chat.application_id,
                    chat.conversation_id,
                )
            if (
                count_context is not None
                and self._pending_scope_matches(count_context, chat)
            ):
                preserved_filters = [
                    dict(item) for item in count_context.filters
                ]
                for current_filter in raw_rule_request.filters:
                    current_field = str(current_filter.get("field") or "")
                    current_family = self.turn_admission_gate._semantic_field_family(
                        current_field
                    )
                    preserved_filters = [
                        item for item in preserved_filters
                        if (
                            str(item.get("field") or "") != current_field
                            and self.turn_admission_gate._semantic_field_family(
                                str(item.get("field") or "")
                            ) != current_family
                        )
                    ]
                    preserved_filters.append(dict(current_filter))
                request.filters = preserved_filters
                request.semantic_entity_mentions = list(dict.fromkeys([
                    *count_context.semantic_entity_mentions,
                    *raw_rule_request.semantic_entity_mentions,
                ]))
                request.assumptions = list(dict.fromkeys([
                    *request.assumptions,
                    *(
                        value for value in count_context.assumptions
                        if value in {"SET_RELATIONSHIP_PROJECTION"}
                        or value.startswith((
                            "TRANSACTION_TIME_SCOPE=", "ACTIVE_DEFINITION=",
                            "GEOGRAPHIC_ROLE=", "TIME_SCOPE=",
                        ))
                    ),
                ]))
            request.operators = list(raw_rule_request.operators)
            request.asl_template = None
            request.source_dataset_id = None
            if relationship_count_shape not in request.assumptions:
                request.assumptions.append(relationship_count_shape)
        relationship_projection_followup = bool(
            "SET_RELATIONSHIP_PROJECTION" in raw_rule_request.assumptions
            and not raw_rule_request.filters
            and not raw_rule_request.semantic_entity_mentions
            and not re.search(
                r"切换话题|换个话题|另一个问题|重新开始|不看(?:之前|上面)",
                chat.question,
            )
        )
        relationship_scope_context = previous_for_rewrite
        if relationship_projection_followup and relationship_scope_context is None:
            relationship_scope_context = await self.sessions.get_last_request(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
            )
            if (
                relationship_scope_context is not None
                and not self._pending_scope_matches(relationship_scope_context, chat)
            ):
                relationship_scope_context = None
        if (
            relationship_projection_followup
            and relationship_scope_context is not None
        ):
            # “合作的经销商有哪些/主要向哪些医院供货” supplies a new
            # relationship endpoint but intentionally omits the already-known
            # subject.  Restore only the verified subject constraints; the
            # current endpoint/projection remains authoritative and old result
            # columns can never be replayed as the new answer.
            request.primary_intent = PrimaryIntent.DETAIL_QUERY
            request.secondary_intents = []
            request.metrics = []
            request.entity = raw_rule_request.entity
            request.fields = list(raw_rule_request.fields)
            request.dimensions = list(raw_rule_request.dimensions)
            request.filters = [
                dict(item) for item in relationship_scope_context.filters
            ]
            request.semantic_entity_mentions = list(
                relationship_scope_context.semantic_entity_mentions
            )
            request.time_range = (
                relationship_scope_context.time_range.model_copy(deep=True)
                if relationship_scope_context.time_range is not None
                else None
            )
            request.operators = list(raw_rule_request.operators)
            request.asl_template = None
            request.source_dataset_id = None
            request.query_resolution_type = "FOLLOWUP_ANALYSIS"
            request.execution_mode = "QUERY_DATABASE"
            request.assumptions = list(dict.fromkeys([
                *request.assumptions,
                *raw_rule_request.assumptions,
                "RELATIONSHIP_FOLLOWUP_SCOPE_INHERITED",
            ]))
        metric_only_followup = bool(
            raw_rule_request.metrics
            and not raw_rule_request.filters
            and not raw_rule_request.semantic_entity_mentions
            and not re.search(
                r"切换话题|换个话题|另一个问题|重新开始|不看(?:之前|上面)",
                chat.question,
            )
            and re.fullmatch(
                r"(?:那|那么|再看|再查|再统计|然后)?"
                r"(?:它的|该对象的|这个对象的|上述对象的)?"
                r"(?:含税销售总额|销售总额|销售额|销售总数量|销售数量|销售量|"
                r"订单笔数|订单数|订单量|合作次数|合作时长|医院数量|经销商数量)"
                r"(?:是|为)?(?:多少|多少个|多少家)?(?:呢|呀|吗)?[。！!？?]?",
                re.sub(r"\s+", "", chat.question),
            )
        )
        metric_scope_context = previous_for_rewrite
        if metric_only_followup and metric_scope_context is None:
            metric_scope_context = await self.sessions.get_last_request(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
            )
            if (
                metric_scope_context is not None
                and not self._pending_scope_matches(metric_scope_context, chat)
            ):
                metric_scope_context = None
        if metric_only_followup and metric_scope_context is not None:
            # A measure-only utterance is a delta over the last verified
            # business scope.  The measure is replaced by the current turn,
            # while entity/value filters and the user's explicit time window
            # remain intact.  Without this guard a model may correctly detect
            # “订单笔数” yet classify the short utterance as standalone and
            # silently execute the all-database total.
            request.primary_intent = PrimaryIntent.METRIC_QUERY
            request.secondary_intents = []
            request.metrics = [
                metric.model_copy(deep=True)
                for metric in raw_rule_request.metrics
            ]
            request.fields = []
            request.filters = [
                dict(item) for item in metric_scope_context.filters
            ]
            request.semantic_entity_mentions = list(
                metric_scope_context.semantic_entity_mentions
            )
            request.time_range = (
                metric_scope_context.time_range.model_copy(deep=True)
                if metric_scope_context.time_range is not None
                else None
            )
            if not raw_rule_request.dimensions:
                request.dimensions = (
                    list(metric_scope_context.dimensions)
                    if AnalysisOperator.TIME_BUCKET
                    in metric_scope_context.operators
                    else []
                )
            request.operators = list(raw_rule_request.operators)
            request.asl_template = None
            request.source_dataset_id = None
            request.assumptions = list(dict.fromkeys([
                *request.assumptions,
                *(
                    value for value in metric_scope_context.assumptions
                    if value.startswith((
                        "TIME_SCOPE=", "GEOGRAPHIC_ROLE=", "ACTIVE_DEFINITION=",
                        "TRANSACTION_TIME_SCOPE=", "DEFAULT_TIME_GRANULARITY=",
                    ))
                ),
                "METRIC_ONLY_FOLLOWUP_SCOPE_INHERITED",
            ]))
        if model_entity_mentions:
            # A current explicit subject may replace the previous subject.  A
            # metric/grain/result-reference follow-up may not.  Previously any
            # non-empty model list replaced the inherited list wholesale, so a
            # fake mention such as “那” or “按月看” silently removed the verified
            # product filter from the active query.
            if (
                not turn_decision.inherit_business_context
                or turn_decision.core_subject_changed
            ):
                request.semantic_entity_mentions = model_entity_mentions
            else:
                request.semantic_entity_mentions = list(dict.fromkeys([
                    *request.semantic_entity_mentions,
                    *model_entity_mentions,
                ]))
        # Model enrichment and contextual merging are two independent entity
        # producers.  Re-run the same literal guard after they converge so
        # equivalent spans such as ``透析器`` and ``透析器产品`` cannot both
        # cross the service boundary when the shorter span is already owned by
        # a typed filter.  The semantic layer still performs catalog and source
        # value resolution; this pass only removes redundant query spans.
        rules = getattr(self.classifier, "rules", self.classifier)
        sanitize_mentions = getattr(
            rules, "sanitize_semantic_entity_mentions", None
        )
        if callable(sanitize_mentions):
            sanitize_mentions(request)
        if rewrite is not None and rewrite.semantic_matches:
            # Context restoration and model enrichment above can re-introduce a
            # raw model span after the first grounding pass. Canonicalize once
            # more at the convergence point so the request, ASL input, memory,
            # and debug output all expose only the current vector-catalog value.
            QuestionRewriter.ground_request_dimensions(
                request, rewrite.semantic_matches
            )
            if callable(sanitize_mentions):
                sanitize_mentions(request)
        if self.question_rewriter is not None:
            # Resolve extracted filter literals separately from the whole
            # question.  A small whole-query top-k can otherwise omit an exact
            # entity value (for example 费森尤斯=母厂牌) and leave the model's
            # provisional 商品名称 field in the executable request.
            filter_semantic_ambiguities = (
                await self.question_rewriter.ground_executable_filters(
                    request,
                    semantic_model_id=chat.semantic_model_id,
                    business_domain_id=self._effective_business_domain_id(chat),
                    business_domain_ids=list(chat.business_domain_ids),
                )
            )
            if callable(sanitize_mentions):
                sanitize_mentions(request)
        # A closed-form “按月/季度统计” is a grouped metric table. Structured
        # completion sometimes rewrites it as “分析趋势”, changing both the
        # deliverable and follow-up behavior. Keep the deterministic current
        # turn authoritative for a standalone grouped statistic, and for a
        # grain change whose active task was already a metric table. Explicit
        # 趋势/走势/变化 wording continues through the trend path unchanged.
        raw_grain = next(
            (
                value
                for value in raw_rule_request.assumptions
                if value.startswith("DEFAULT_TIME_GRANULARITY=")
            ),
            None,
        )
        grouped_statistic = bool(
            raw_rule_request.primary_intent == PrimaryIntent.METRIC_QUERY
            and raw_grain is not None
            and not re.search(
                r"趋势|走势|变化|涨跌|上升|下降|增长|波动",
                chat.question,
            )
            and (
                turn_decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
                or previous_for_rewrite is not None
                and previous_for_rewrite.primary_intent == PrimaryIntent.METRIC_QUERY
                and any(
                    value.startswith("DEFAULT_TIME_GRANULARITY=")
                    for value in previous_for_rewrite.assumptions
                )
            )
        )
        if grouped_statistic:
            request.primary_intent = PrimaryIntent.METRIC_QUERY
            request.operators = list(raw_rule_request.operators)
            request.assumptions = [
                value for value in request.assumptions
                if not value.startswith("DEFAULT_TIME_GRANULARITY=")
            ]
            request.assumptions.append(raw_grain)
            # ``按月/季度统计`` is intentionally normalized to a grouped metric
            # table even when structured completion called it a trend. Keep
            # the current-turn provenance snapshot aligned with that audited
            # normalization; otherwise the outbound completeness gate sees a
            # stale TREND_ANALYSIS fact and rejects an otherwise complete
            # METRIC_QUERY contract as EXPLICIT_INTENT_MISSING.
            explicit_slots = (
                turn_decision.current_turn_facts.explicit_slots
            )
            explicit_intent = explicit_slots.get("analysis_type")
            if (
                explicit_intent is not None
                and explicit_intent.value != PrimaryIntent.METRIC_QUERY.value
            ):
                normalized_intent = explicit_intent.model_copy(update={
                    "value": PrimaryIntent.METRIC_QUERY.value,
                })
                explicit_slots["analysis_type"] = normalized_intent
                request.slot_provenance["analysis_type"] = normalized_intent
            request.assumptions.append(
                "QUERY_SHAPE_TRANSFORM=GROUPED_STATISTIC"
            )
        additive_metric_turn = bool(re.search(
            r"(?:再|同时|并)?(?:加上|增加|新增|补充|带上|显示|返回).{0,20}"
            r"(?:指标|金额|销售|数量|笔数|次数|均价|单价|利润|成本|收入)",
            chat.question,
        ))
        if additive_metric_turn and previous_for_rewrite is not None:
            metric_by_name = {
                metric.canonical_name or metric.input: metric.model_copy(deep=True)
                for metric in (
                    *previous_for_rewrite.metrics,
                    *request.metrics,
                    *raw_rule_request.metrics,
                )
            }
            request.metrics = list(metric_by_name.values())
            for operator in previous_for_rewrite.operators:
                if operator in {
                    AnalysisOperator.SORT,
                    AnalysisOperator.TOP_N,
                    AnalysisOperator.BOTTOM_N,
                } and operator not in request.operators:
                    request.operators.append(operator)
            if request.ranking_limit is None:
                request.ranking_limit = previous_for_rewrite.ranking_limit
            for assumption in previous_for_rewrite.assumptions:
                if (
                    assumption.startswith("SORT_DIRECTION=")
                    and assumption not in request.assumptions
                ):
                    request.assumptions.append(assumption)
            # Adding a measure changes the projection, so the preceding ASL and
            # immutable result cannot be reused as if they already contained it.
            request.asl_template = None
            request.source_dataset_id = None
            if previous_for_rewrite.time_range is None:
                request.assumptions.append("TIME_SCOPE=ALL_TIME")
        sort_only_turn = bool(
            raw_rule_request.metrics
            and not raw_rule_request.filters
            and not raw_rule_request.semantic_entity_mentions
            and re.fullmatch(
                r"按(?:整体业务规模|业务规模|含税销售总额|销售总额|销售额|"
                r"订单金额|销售数量|订单笔数)"
                r"(?:从高到低|从低到高|升序|降序)?(?:进行)?排序[。！!？?]?",
                re.sub(r"\s+", "", chat.question),
            )
        )
        sort_scope_context = previous_for_rewrite
        if sort_only_turn and sort_scope_context is None:
            sort_scope_context = await self.sessions.get_last_request(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
            )
            if (
                sort_scope_context is not None
                and not self._pending_scope_matches(sort_scope_context, chat)
            ):
                sort_scope_context = None
        if sort_only_turn and sort_scope_context is not None:
            counted_role = next(
                (
                    value.split("=", 1)[1]
                    for value in sort_scope_context.assumptions
                    if value.startswith("RELATIONSHIP_COUNT_PROJECTION=")
                    and value.partition("=")[2]
                ),
                None,
            )
            grouping = (
                [sort_scope_context.entity]
                if sort_scope_context.entity
                else [counted_role]
                if counted_role
                else [
                    value for value in sort_scope_context.dimensions
                    if value not in {"时间", "日期", "年", "季度", "月", "周", "日"}
                ]
            )
            request.primary_intent = PrimaryIntent.METRIC_QUERY
            request.secondary_intents = []
            request.metrics = [
                metric.model_copy(deep=True)
                for metric in raw_rule_request.metrics
            ]
            request.entity = sort_scope_context.entity or counted_role
            request.fields = []
            request.dimensions = list(dict.fromkeys(value for value in grouping if value))
            request.filters = [dict(item) for item in sort_scope_context.filters]
            request.semantic_entity_mentions = list(
                sort_scope_context.semantic_entity_mentions
            )
            request.time_range = (
                sort_scope_context.time_range.model_copy(deep=True)
                if sort_scope_context.time_range is not None
                else None
            )
            request.operators = list(dict.fromkeys([
                AnalysisOperator.AGGREGATE,
                AnalysisOperator.SORT,
                *raw_rule_request.operators,
            ]))
            request.ranking_limit = raw_rule_request.ranking_limit
            request.asl_template = None
            request.source_dataset_id = None
            direction = (
                "ASC" if re.search(r"从低到高|升序", chat.question) else "DESC"
            )
            request.assumptions = [
                value for value in dict.fromkeys([
                    *sort_scope_context.assumptions,
                    *request.assumptions,
                ])
                if not value.startswith("SORT_DIRECTION=")
            ]
            request.assumptions.extend([
                f"SORT_DIRECTION={direction}",
                "SORT_ONLY_FOLLOWUP_SCOPE_INHERITED",
            ])
        explicit_group_ranking_followup = bool(
            previous_for_rewrite is not None
            and raw_rule_request.primary_intent == PrimaryIntent.METRIC_QUERY
            and raw_rule_request.metrics
            and raw_rule_request.dimensions
            and raw_rule_request.ranking_limit is not None
            and any(
                operator in raw_rule_request.operators
                for operator in (
                    AnalysisOperator.TOP_N,
                    AnalysisOperator.BOTTOM_N,
                    AnalysisOperator.SORT,
                )
            )
            and re.search(
                r"(?:按|分|各|每个).{0,12}(?:统计|汇总|排名|排行)|"
                r"(?:排名|排行)(?:前|后)?[一二三四五六七八九十\d]+",
                re.sub(r"\s+", "", chat.question),
            )
        )
        if explicit_group_ranking_followup:
            # An explicit current grouping changes the result object while an
            # omitted predicate still denotes the active business scope.  For
            # example, after querying one hospital's partners, ``按产品统计销售
            # 额前5`` must keep the hospital filter but replace the dealer
            # projection with product grouping.  Treat the current analytical
            # shape atomically so no prior detail projection can survive.
            request.primary_intent = PrimaryIntent.METRIC_QUERY
            request.secondary_intents = list(raw_rule_request.secondary_intents)
            request.metrics = [
                metric.model_copy(deep=True)
                for metric in raw_rule_request.metrics
            ]
            request.entity = raw_rule_request.entity
            request.fields = []
            request.dimensions = list(raw_rule_request.dimensions)
            request.operators = list(raw_rule_request.operators)
            request.ranking_limit = raw_rule_request.ranking_limit
            if not raw_rule_request.filters:
                request.filters = [
                    dict(item) for item in previous_for_rewrite.filters
                ]
            request.semantic_entity_mentions = list(dict.fromkeys([
                *previous_for_rewrite.semantic_entity_mentions,
                *raw_rule_request.semantic_entity_mentions,
            ]))
            request.time_range = (
                previous_for_rewrite.time_range.model_copy(deep=True)
                if previous_for_rewrite.time_range is not None
                else raw_rule_request.time_range.model_copy(deep=True)
                if raw_rule_request.time_range is not None
                else None
            )
            request.asl_template = None
            request.source_dataset_id = None
            request.query_resolution_type = "FOLLOWUP_ANALYSIS"
            request.execution_mode = "QUERY_DATABASE"
            request.assumptions = list(dict.fromkeys([
                *previous_for_rewrite.assumptions,
                *raw_rule_request.assumptions,
                "EXPLICIT_GROUP_RANKING_SCOPE_INHERITED",
            ]))
        limit_only_turn = bool(re.fullmatch(
            r"(?:改成|改为|换成|只(?:显示|展示|返回|保留)?|展示|显示|返回)?"
            r"(?:前|top)(?:\d{1,5}|[一二三四五六七八九十]{1,3})(?:名|条|个)?"
            r"(?:，?(?:其他|其余)(?:筛选)?条件不变)?[。！!？?]?",
            re.sub(r"\s+", "", chat.question).lower(),
        ))
        limit_scope_context = previous_for_rewrite
        if limit_only_turn and limit_scope_context is None:
            limit_scope_context = await self.sessions.get_last_request(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                chat.conversation_id,
            )
            if (
                limit_scope_context is not None
                and not self._pending_scope_matches(limit_scope_context, chat)
            ):
                limit_scope_context = None
        if limit_only_turn and limit_scope_context is not None:
            promote_unsorted_relationship_list = bool(
                limit_scope_context.primary_intent == PrimaryIntent.DETAIL_QUERY
                and "SET_RELATIONSHIP_PROJECTION" in limit_scope_context.assumptions
                and not any(
                    operator in limit_scope_context.operators
                    for operator in (AnalysisOperator.SORT, AnalysisOperator.TOP_N)
                )
            )
            request.primary_intent = (
                PrimaryIntent.METRIC_QUERY
                if promote_unsorted_relationship_list
                else limit_scope_context.primary_intent
            )
            request.secondary_intents = list(limit_scope_context.secondary_intents)
            request.metrics = (
                [MetricRef(input="含税销售总额")]
                if promote_unsorted_relationship_list
                else [
                    metric.model_copy(deep=True)
                    for metric in limit_scope_context.metrics
                ]
            )
            request.entity = limit_scope_context.entity
            request.fields = (
                [] if promote_unsorted_relationship_list
                else list(limit_scope_context.fields)
            )
            request.dimensions = (
                [limit_scope_context.entity]
                if promote_unsorted_relationship_list and limit_scope_context.entity
                else list(limit_scope_context.dimensions)
            )
            request.filters = [dict(item) for item in limit_scope_context.filters]
            request.semantic_entity_mentions = list(
                limit_scope_context.semantic_entity_mentions
            )
            request.time_range = (
                limit_scope_context.time_range.model_copy(deep=True)
                if limit_scope_context.time_range is not None
                else None
            )
            request.asl_template = (
                None if promote_unsorted_relationship_list
                else copy.deepcopy(limit_scope_context.asl_template)
            )
            request.source_dataset_id = (
                None if promote_unsorted_relationship_list
                else limit_scope_context.source_dataset_id
            )
            request.assumptions = list(dict.fromkeys([
                *limit_scope_context.assumptions,
                *request.assumptions,
            ]))
            if promote_unsorted_relationship_list:
                request.operators = list(dict.fromkeys([
                    AnalysisOperator.AGGREGATE,
                    AnalysisOperator.SORT,
                    AnalysisOperator.TOP_N,
                ]))
                request.assumptions.extend([
                    "SORT_DIRECTION=DESC",
                    "RANKING_DEFAULT_METRIC=含税销售总额",
                ])
        grain_only_turn = bool(re.fullmatch(
            r"(?:改成|改为|换成|还是)?按(?:日|天|周|月|季度|年)"
            r"(?:统计|汇总|分析|看|给我|吧)?"
            r"(?:，?(?:其他|其余)条件不变)?[。！!？?]?",
            re.sub(r"\s+", "", chat.question),
        ))
        if (
            grain_only_turn
            and raw_grain is not None
            and previous_for_rewrite is not None
            and previous_for_rewrite.primary_intent in {
                PrimaryIntent.TREND_ANALYSIS,
                PrimaryIntent.METRIC_QUERY,
            }
            and previous_for_rewrite.asl_template is not None
        ):
            previously_grouped_metric = (
                previous_for_rewrite.primary_intent == PrimaryIntent.METRIC_QUERY
                and any(
                    value.startswith("DEFAULT_TIME_GRANULARITY=")
                    for value in previous_for_rewrite.assumptions
                )
            )
            if previously_grouped_metric:
                request.primary_intent = PrimaryIntent.METRIC_QUERY
            request.metrics = [
                metric.model_copy(deep=True)
                for metric in previous_for_rewrite.metrics
            ]
            request.entity = previous_for_rewrite.entity
            request.fields = list(previous_for_rewrite.fields)
            request.dimensions = list(previous_for_rewrite.dimensions)
            request.filters = [dict(item) for item in previous_for_rewrite.filters]
            request.time_range = (
                previous_for_rewrite.time_range.model_copy(deep=True)
                if previous_for_rewrite.time_range is not None
                else None
            )
            request.asl_template = copy.deepcopy(previous_for_rewrite.asl_template)
            request.source_dataset_id = None
            request.assumptions.append("DETERMINISTIC_GRAIN_FAST_PATH")
        await self._apply_recent_region_set_reference(request, chat, identity)
        rules = getattr(self.classifier, "rules", self.classifier)
        required_missing_slots = getattr(rules, "required_missing_slots", None)
        if callable(required_missing_slots):
            request.missing_slots = required_missing_slots(request)
        rewrite_ambiguities = (
            rewrite.semantic_ambiguities if rewrite is not None else []
        )
        if rewrite_ambiguities or filter_semantic_ambiguities:
            combined_ambiguities: dict[str, SemanticAmbiguity] = {}
            for item in [*rewrite_ambiguities, *filter_semantic_ambiguities]:
                if not item.blocking:
                    continue
                key = item.ambiguity_id or f"{item.type}:{item.phrase}"
                combined_ambiguities[key] = item.model_copy(deep=True)
            request.semantic_ambiguities = list(combined_ambiguities.values())[:5]
            request.ambiguities = [
                item.question for item in request.semantic_ambiguities
            ]
            if request.semantic_ambiguities and "semantic_ambiguity" not in request.missing_slots:
                request.missing_slots.append("semantic_ambiguity")
        if turn_decision.relation == TurnRelation.AMBIGUOUS_RELATION:
            relation_ambiguity = SemanticAmbiguity(
                ambiguity_id=f"turn-relation-{request.request_id}",
                type="turn_relation",
                phrase=chat.question[:200],
                affected_slots=["turn_relation"],
                question="这句话既可以作为上一轮的补充，也可以作为一个新问题。请确认本次意图。",
                candidates=["补充或修改上一轮问题", "作为独立新问题"],
                material_impact="不同选择会决定是否继承上一轮指标、实体、时间和筛选条件",
                blocking=True,
                semantic_model_id=chat.semantic_model_id,
                semantic_model_version=request.semantic_model_version,
            )
            request.semantic_ambiguities = [
                relation_ambiguity,
                *[
                    item for item in request.semantic_ambiguities
                    if item.type != "turn_relation"
                ],
            ][:5]
            request.ambiguities = [
                item.question for item in request.semantic_ambiguities
            ]
            if "turn_relation" not in request.missing_slots:
                request.missing_slots.insert(0, "turn_relation")
            turn_decision.needs_clarification = True
        request.analysis_thread_id = turn_decision.selected_thread_id
        request = resolve_conversation_temporal_context(
            request,
            previous_for_rewrite,
            turn_decision,
            chat.question,
        )
        execution_question, canonical_question, execution_source = (
            select_data_execution_question(request, turn_decision, chat.question)
        )
        request.rewritten_question = execution_question
        request.assumptions.append(f"EXECUTION_QUERY_SOURCE={execution_source}")
        turn_decision.context_after = self.turn_admission_gate.context_snapshot(request)
        turn_decision.context_conflicts = (
            self.turn_admission_gate.validate_context_consistency(
                request, turn_decision
            )
        )
        request.turn_admission = turn_decision
        await self._append_session_event(
            chat=chat,
            identity=identity,
            event_type=SessionEventType.CONTEXT_MERGE,
            trace_id=str(request.request_id),
            payload={
                "turn_relation": turn_decision.relation.value,
                "context_mode": turn_decision.context_mode.value,
                "inheritance_allowed": turn_decision.inherit_business_context,
                "inheritance_slots": turn_decision.inheritance_slots,
                "protected_slots": turn_decision.protected_slots,
                "cleared_slots": turn_decision.cleared_slots,
                "context_before": turn_decision.context_before,
                "context_delta": turn_decision.context_delta,
                "context_after": turn_decision.context_after,
                "context_conflicts": turn_decision.context_conflicts,
                "slot_operations": [
                    item.model_dump(mode="json")
                    for item in turn_decision.slot_operations
                ],
                "new_thread_created": turn_decision.create_new_analysis_thread,
                "inherited_slots": turn_decision.inheritance_slots,
                "temporal_anchor": (
                    request.temporal_anchor.model_dump(mode="json")
                    if request.temporal_anchor else None
                ),
                "resolved_periods": request.resolved_periods,
                "comparison": (
                    request.resolved_comparison.model_dump(mode="json")
                    if request.resolved_comparison else None
                ),
                "canonical_query": canonical_question,
                "execution_query_source": execution_source,
            },
        )

        request.application_id = chat.application_id
        if chat.dataset_id is not None:
            request.assumptions.append("EXPLICIT_SOURCE_DATASET_SELECTION")
        elif request.source_dataset_id is not None:
            request.assumptions.append("INHERITED_CONVERSATION_DATASET")
        request.source_dataset_id = (
            chat.dataset_id
            if chat.dataset_id is not None
            else request.source_dataset_id
            if turn_decision.inherit_business_context
            else None
        )
        request.semantic_model_id = chat.semantic_model_id
        request.database_id = chat.database_id
        request.business_domain_ids = list(chat.business_domain_ids)
        request.business_domain_selection_mode = (
            "EXPLICIT" if chat.business_domain_ids else "AUTO"
        )
        request.dependency_constraints = list(chat.dependency_constraints)
        request.assumptions = list(dict.fromkeys([
            *request.assumptions,
            *_INTERNAL_ASSUMPTIONS.get(),
            *(["STRUCTURED_TASK_RECALL"] if recalled_task_frame else []),
        ]))
        if (
            "LATEST_RESULT_DATASET_NOT_REUSABLE" in request.assumptions
            and re.search(
                r"(?:展示|显示|返回|查看)前?(\d{1,5})条(?:数据|结果)?",
                re.sub(r"\s+", "", chat.question),
            )
        ):
            request.assumptions.append("DETERMINISTIC_LIMIT_FAST_PATH")

        # Knowledge scope is authoritative per request and comes from the platform's
        # current app binding.  Empty means no bound KB and must clear stale history;
        # never inherit an older scope or infer all globally available collections.
        request.knowledge_base_names = list(dict.fromkeys(chat.knowledge_base_names))

        # Normalize newly published or model-extracted metric phrases before
        # rendering intent diagnostics.  The former late-only recovery allowed
        # the UI to announce stale dimensions/clarification even when the same
        # turn subsequently obtained a SQL-verified canonical semantic frame.
        if (
            "metric" in request.missing_slots
            or any(metric.metric_id is None for metric in request.metrics)
        ):
            await self._recover_live_published_metrics(request, chat, identity)

        # Intent diagnostics are visible to end users. Resolve their semantic
        # slots separately so raw LLM/rule candidates can never be presented as
        # if they were current vector-catalog facts. This is display-only and
        # intentionally cannot mutate the executable request.
        if self.question_rewriter is not None:
            await self.question_rewriter.ground_display_slots(request)

        await emit_progress(
            "INTENT_RECOGNITION",
            "COMPLETED",
            self._intent_think_summary(
                request,
                file_status=str(chat._file_inspection.get("status") or "NOT_PROVIDED"),
                file_based=bool(chat._file_inspection.get("file_based")),
            ),
            intent=request.primary_intent.value,
            confidence=round(float(request.intent_confidence), 4),
            display_model="IntentRecognitionDisplayV2",
            display_version="V2",
            presentation_scenario=(
                "CHAT"
                if request.primary_intent == PrimaryIntent.CHAT
                else "CLARIFICATION"
                if request.missing_slots
                else "ANALYTIC"
            ),
            file_status=str(chat._file_inspection.get("status") or "NOT_PROVIDED"),
            file_based=bool(chat._file_inspection.get("file_based")),
        )
        file_inspection = dict(chat._file_inspection)
        await emit_progress(
            "FILE_INSPECTION",
            "COMPLETED",
            self._file_inspection_think_summary(file_inspection),
            file_status=str(file_inspection.get("status") or "NOT_PROVIDED"),
            file_based=bool(file_inspection.get("file_based")),
            file_count=int(file_inspection.get("file_count") or bool(chat.temp_file_paths)),
            row_count=file_inspection.get("row_count"),
            column_count=file_inspection.get("column_count"),
            sheet_count=file_inspection.get("sheet_count"),
        )

        if chat.use_longterm_memory and self.memories is not None:
            await emit_progress(
                "MEMORY_RETRIEVAL", "RUNNING", "正在加载用户确认过的长期记忆。"
            )
            await self._apply_confirmed_memories(request)
            await emit_progress(
                "MEMORY_RETRIEVAL", "COMPLETED", "长期记忆加载完成。"
            )

        if request.conversation_control == ConversationControl.CANCEL:
            await self.sessions.clear_pending(identity.tenant_id, identity.user_id, chat.application_id, chat.conversation_id)
            response = AgentResponse(
                request_id=request.request_id, conversation_id=request.conversation_id,
                status="CANCELLED", intent=request.primary_intent,
                intent_source=request.intent_source,
                intent_confidence=request.intent_confidence,
                answer="已取消当前查询或分析任务。",
                reliability=ReliabilityReport(level="HIGH", score=1, gates={"cancelled_without_execution": True}),
            )
            response.analysis_process = self._build_analysis_process(request, response)
            return response

        # A local dataset follow-up (for example “刚才销售额前1名”) does not
        # need a new time range or group dimension. Try the whitelisted MinIO
        # operation before applying fresh-query completeness requirements.
        query_result: DataQueryResult | None = None
        dataset_id: str | None = None
        if (
            request.conversation_control == ConversationControl.FOLLOW_UP
            or request.source_dataset_id is not None
            or is_dataset_operation_followup(request.original_question)
        ):
            try:
                query_result, dataset_id = await self._try_dataset_followup(request)
                if query_result is not None and dataset_id is not None:
                    # Automatic conversational reuse is just as immutable as an
                    # explicitly selected dataset.  Record the resolved id on
                    # the request so reliability gates do not demand a second
                    # semantic-metric binding for a local limit/sort/select.
                    request.source_dataset_id = dataset_id
                    request.assumptions.append("IMMUTABLE_CONVERSATION_DATASET_REUSED")
            except ExplicitDatasetUnavailableError as exc:
                return await self._finish_terminal(
                    request,
                    self._fallback(request, str(exc)),
                )

        derived_comparison = (
            derive_period_comparison(
                request,
                query_result.dataset.columns,
                query_result.dataset.rows,
            )
            if query_result is not None and request.resolved_comparison is not None
            else None
        )
        if derived_comparison is not None:
            request.execution_mode = "REUSE_PREVIOUS_RESULT"
            request.query_resolution_type = "DERIVED_RESULT_QUERY"
            request.temporal_anchor = build_temporal_anchor(
                request,
                query_result.dataset.columns,
                query_result.dataset.rows,
            )
            await self._append_session_event(
                chat=chat,
                identity=identity,
                event_type=SessionEventType.QUERY_RESOLUTION,
                trace_id=str(request.request_id),
                payload={
                    "raw_query": chat.question,
                    "turn_relation": (
                        request.turn_relation.value
                        if request.turn_relation else None
                    ),
                    "active_thread": request.analysis_thread_id,
                    "active_episode": turn_decision.selected_episode_id,
                    "context_dependency": True,
                    "omitted_slots": turn_decision.current_turn_facts.omitted_slots,
                    "inherited_slots": turn_decision.inheritance_slots,
                    "temporal_reference": (
                        turn_decision.current_turn_facts.temporal_references
                    ),
                    "temporal_anchor": (
                        request.temporal_anchor.model_dump(mode="json")
                        if request.temporal_anchor else None
                    ),
                    "resolved_periods": request.resolved_periods,
                    "comparison_type": request.comparison_type,
                    "comparison": request.resolved_comparison.model_dump(mode="json"),
                    "previous_result_available": True,
                    "result_sufficiency": True,
                    "execution_mode": request.execution_mode,
                    "source_dataset_id": request.source_dataset_id,
                },
            )
            response = AgentResponse(
                request_id=request.request_id,
                conversation_id=request.conversation_id,
                status="COMPLETED",
                intent=request.primary_intent,
                intent_source=request.intent_source,
                intent_confidence=request.intent_confidence,
                answer=derived_comparison.answer(),
                evidence=[EvidenceItem(
                    evidence_id=f"derived-result:{query_result.dataset.snapshot_id}",
                    kind="DERIVED_RESULT_COMPARISON",
                    source_ref=request.source_dataset_id or query_result.data_source_id or "conversation-result",
                    payload={
                        "sql_executed": False,
                        "left_period": derived_comparison.left_period,
                        "right_period": derived_comparison.right_period,
                        "metric": derived_comparison.metric_column,
                        "left_value": str(derived_comparison.left_value),
                        "right_value": str(derived_comparison.right_value),
                        "delta": str(derived_comparison.delta),
                        "change_rate": (
                            str(derived_comparison.change_rate)
                            if derived_comparison.change_rate is not None else None
                        ),
                    },
                )],
                reliability=ReliabilityReport(
                    level="HIGH",
                    score=1.0,
                    gates={
                        "validated_previous_result": True,
                        "periods_uniquely_resolved": True,
                        "deterministic_arithmetic": True,
                        "sql_not_reexecuted": True,
                    },
                ),
                dataset_id=dataset_id,
            )
            await self.sessions.put_task_frame(request)
            await self.sessions.put_last_request(request)
            return await self._finish_terminal(request, response)

        if (
            query_result is None
            and request.knowledge_base_names
            and (
                request.primary_intent == PrimaryIntent.OUT_OF_SCOPE
                or (
                    request.primary_intent == PrimaryIntent.CHAT
                    and self._is_document_question(request.original_question)
                )
            )
        ):
            response = await self._knowledge_document_answer(request, identity)
            return await self._finish_terminal(request, response)

        external_search_mode = self._external_search_mode(request.original_question)
        if query_result is None and external_search_mode == "PURE":
            response = await self._external_only_answer(request, chat)
            if response is not None:
                await self.sessions.put_last_request(request)
                return await self._finish_terminal(request, response)

        if query_result is None and (
            "metric" in request.missing_slots
            or any(metric.metric_id is None for metric in request.metrics)
        ):
            recovered_metric = await self._recover_live_published_metrics(
                request, chat, identity
            )
            if recovered_metric:
                await emit_progress(
                    "COMPLETENESS_CHECK",
                    "RUNNING",
                    "已从当前发布的语义层识别并核验查询字段，正在继续检查执行参数。",
                    metric_ids=[
                        metric.metric_id for metric in request.metrics
                        if metric.metric_id is not None
                    ],
                )

        if request.missing_slots and query_result is None:
            await emit_progress(
                "COMPLETENESS_CHECK",
                "NEEDS_INPUT",
                "关键信息不足，需要用户补充后继续。",
                missing_slots=list(request.missing_slots),
            )
            await emit_progress(
                "CLARIFICATION_EXECUTION",
                "COMPLETED",
                "任务状态 = 参数缺失待补充，命中暂停工具调用规则。\n"
                "工具调用结果：跳过所有工具调用，无工具发起请求。",
                tool_call_skipped=True,
                missing_slots=list(request.missing_slots),
            )
            await emit_progress(
                "CLARIFICATION_RESULT",
                "COMPLETED",
                "返回追问话术，本轮查询任务暂不执行，等待用户补充参数后再继续处理。\n"
                "生成自然语言追问文本，引导用户补充缺失条件。",
                missing_slots=list(request.missing_slots),
            )
            return await self._request_clarification(request, rounds)

        await emit_progress(
            "COMPLETENESS_CHECK", "COMPLETED", "执行所需的关键信息已满足。"
        )

        # Persist the understood task before external execution. This is the
        # short-term working memory used by follow-ups even when ASL/SQL or the
        # business data service fails later in this turn.
        if request.primary_intent not in NO_DATA_INTENTS | METADATA_INTENTS:
            await self.sessions.put_task_frame(request)

        if request.primary_intent in NO_DATA_INTENTS:
            return await self._finish_terminal(
                request, await self._direct(request, chat)
            )
        if request.primary_intent in METADATA_INTENTS:
            response = await self._metadata_answer(
                request, identity, chat.semantic_model_id
            )
            return await self._finish_terminal(request, response)

        if query_result is None:
            await emit_progress(
                "DATA_RETRIEVAL",
                "RUNNING",
                "### ◉ 规划与执行\n"
                "分析链路：智能语义查询器 → 独立 SQL 执行服务 → 数据集 → 分析。\n"
                "正在按顺序执行查询规划与数据读取。",
            )
            try:
                relationship_count_request = self._relationship_count_projection_request(request)
                retrieval_request = relationship_count_request or request
                try:
                    query_result = await self.adapters.query.query(
                        retrieval_request, identity,
                        semantic_model_id=chat.semantic_model_id,
                        business_domain_id=self._effective_query_business_domain_id(
                            retrieval_request, chat
                        ),
                    )
                    if relationship_count_request is not None:
                        query_result = self._relationship_count_projection_result(
                            request, query_result
                        )
                except AdapterError as first_error:
                    retry_code = (
                        first_error.upstream_code
                        if first_error.upstream_code in SEMANTIC_QUERY_RETRY_CODES
                        else first_error.code
                    )
                    if first_error.code == "ANALYSIS_RESULT_CONTRACT_INVALID":
                        await emit_progress(
                            "DATA_RETRIEVAL",
                            "RUNNING",
                            "首次结果不符合分析契约，正在按缺失字段受控重查一次。",
                            error_code=first_error.code,
                        )
                        retry_assumption = (
                            "ANALYSIS_CONTRACT_RETRY:"
                            + json.dumps(
                                first_error.details,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                default=str,
                            )[:4000]
                        )
                    elif retry_code in SEMANTIC_QUERY_RETRY_CODES:
                        await emit_progress(
                            "DATA_RETRIEVAL",
                            "RUNNING",
                            "首次语义规划未稳定对齐，正在基于当前已发布语义层重新召回并规划一次。",
                            error_code=retry_code,
                        )
                        retry_assumption = (
                            "SEMANTIC_QUERY_RETRY:"
                            + json.dumps(
                                first_error.details,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                default=str,
                            )[:4000]
                        )
                    else:
                        raise
                    retry_request = retrieval_request.model_copy(deep=True, update={
                        "request_id": uuid4(),
                        "asl_template": None,
                        "assumptions": [
                            *retrieval_request.assumptions,
                            retry_assumption,
                        ],
                    })
                    query_result = await self.adapters.query.query(
                        retry_request, identity,
                        semantic_model_id=chat.semantic_model_id,
                        business_domain_id=self._effective_query_business_domain_id(
                            retry_request, chat
                        ),
                    )
                    if relationship_count_request is not None:
                        query_result = self._relationship_count_projection_result(
                            request, query_result
                        )
            except AdapterError as exc:
                if exc.code in {"ASL_AMBIGUOUS", "SQL_TRANSLATION_AMBIGUOUS"}:
                    request.semantic_ambiguities = self._semantic_ambiguities(exc)
                    request.ambiguities = self._ambiguity_texts(exc)
                    request.missing_slots = ["semantic_ambiguity"]
                    return await self._request_clarification(request, rounds)
                if exc.code == "ANALYSIS_RESULT_CONTRACT_INVALID":
                    requirements = self._analysis_contract_requirements(exc)
                    response = self._fallback(
                        request,
                        "查询结果在受控重查后仍不满足分析算子的数据契约，本次未生成分析结论。",
                    )
                    response.requirements = requirements
                    response.missing_slots = [item.code for item in requirements]
                    if requirements:
                        response.answer += "\n需要补充或修正：" + "；".join(
                            item.description for item in requirements
                        )
                    return await self._finish_terminal(request, response)
                logger.warning(
                    "data retrieval failed: request_id=%s conversation_id=%s "
                    "stage_code=%s upstream_code=%s status_code=%s retryable=%s details=%s message=%s",
                    request.request_id,
                    request.conversation_id,
                    exc.code,
                    exc.upstream_code,
                    exc.status_code,
                    exc.retryable,
                    exc.details,
                    str(exc),
                )
                await emit_progress(
                    "DATA_RETRIEVAL",
                    "FAILED",
                    "上游数据查询未成功，正在安全结束本次分析。",
                    error_code=exc.code,
                    upstream_code=exc.upstream_code,
                )
                return await self._finish_terminal(
                    request, self._fallback(request, self._dependency_message(exc))
                )

        query_result = await self._requery_system_default_trend_at_watermark(
            request,
            query_result,
            chat,
            identity,
        )
        self._restore_projected_filter_columns(request, query_result.dataset)
        query_result = self._enforce_name_projection_integrity(
            request, query_result
        )
        await emit_progress(
            "DATA_RETRIEVAL",
            "COMPLETED",
            (
                "调度执行完成。\n"
                "数据集输出：\n"
                f"查询字段：{_compact_trace_value(query_result.dataset.columns, 800)}；\n"
                f"返回行数：{query_result.dataset.row_count}；\n"
                f"结果总行数：{query_result.dataset.total_row_count}；\n"
                f"数据质量：{_quality_status_text(query_result.dataset.quality_status)}；\n"
                f"查询快照时间：{_business_datetime_text(query_result.dataset.data_as_of)}；\n"
                f"数据预览：{_compact_trace_value(query_result.dataset.rows[:2], 1200)}。\n"
                f"结果状态：{'结果已截断，完整数据通过结果文件提供。' if query_result.dataset.truncated else '当前结果未截断。'}"
            ),
            row_count=query_result.dataset.row_count,
            truncated=query_result.dataset.truncated,
        )

        self._bind_metrics_from_asl(request, query_result.asl, chat.semantic_model_id)
        if query_result.sql not in {"DATASET_FOLLOWUP_NO_SQL", "UPLOADED_DATASET_NO_SQL"}:
            request.asl_template = query_result.asl
        if query_result.dataset.quality_status.upper() in {
            "FAIL",
            "FAILED",
            "INVALID",
            "ERROR",
        }:
            response = self._fallback(
                request, "上游数据质量校验失败，本次不生成分析结论，请先修复数据后重试。"
            )
            response.result_file_url = query_result.result_file_url
            return await self._finish_terminal(request, response)
        total_row_count = (
            query_result.dataset.total_row_count
            if query_result.dataset.total_row_count is not None
            else query_result.dataset.row_count
        )
        total_row_count_confirmed = (
            not query_result.dataset.truncated
            or total_row_count > query_result.dataset.row_count
        )
        # The SQL service owns large-result materialization. A complete detail
        # file proves export availability, but it does not prove aggregate facts
        # needed by an analysis report. Never call a download-only result a
        # completed analysis.
        if (
            request.primary_intent == PrimaryIntent.REPORT_GENERATION
            and query_result.result_file_url
            and query_result.dataset.truncated
        ):
            evidence = [
                EvidenceItem(
                    evidence_id=f"query:{query_result.dataset.snapshot_id}",
                    kind="QUERY_RESULT",
                    source_ref=f"data-source:{query_result.data_source_id or 'unknown'}",
                    payload={
                        "columns": query_result.dataset.columns,
                        "row_count": total_row_count,
                        "returned_row_count": query_result.dataset.row_count,
                        "total_row_count_confirmed": total_row_count_confirmed,
                        "truncated": True,
                        "complete_result_file": True,
                        "complete_analysis_statistics": False,
                        "data_as_of": query_result.dataset.data_as_of.isoformat(),
                        "quality_status": query_result.dataset.quality_status,
                        "result_fingerprint": query_result.dataset.snapshot_id,
                        **self._source_watermark_payload(
                            request, query_result.dataset
                        ),
                    },
                )
            ]
            evidence.extend(
                self._semantic_metric_evidence(
                    request,
                    query_result,
                    semantic_model_id=chat.semantic_model_id,
                    business_domain_id=self._effective_business_domain_id(chat),
                )
            )
            reliability = ReliabilityReport(
                level="LIMITED",
                score=0.65 if total_row_count_confirmed else 0.5,
                gates={
                    "complete_result_file": True,
                    "total_row_count_confirmed": total_row_count_confirmed,
                    "complete_analysis_statistics": False,
                },
                warnings=[
                    "SQL服务提供了完整明细文件，但未提供可校验的全量聚合统计，"
                    "因此未生成分析结论。"
                ],
            )
            response = AgentResponse(
                request_id=request.request_id,
                conversation_id=request.conversation_id,
                status="PARTIAL_SUCCESS",
                intent=request.primary_intent,
                intent_source=request.intent_source,
                intent_confidence=request.intent_confidence,
                answer=(
                    (
                        f"查询成功，共 {total_row_count} 条结果；"
                        if total_row_count_confirmed
                        else (
                            "查询成功并返回预览，但SQL服务未确认完整结果行数；"
                        )
                    )
                    + "数据量较大，"
                    "SQL服务已生成完整明细文件，请使用回答末尾的附件链接下载。"
                    "当前没有收到基于全量数据计算且可校验的聚合统计，"
                    "所以本次未把前端预览行用于分析，也未生成分析结论。"
                ),
                evidence=evidence,
                reliability=reliability,
                result_file_url=query_result.result_file_url,
            )
            request.assumptions.append("LATEST_RESULT_DATASET_NOT_REUSABLE")
            await self.sessions.put_last_request(request)
            return await self._finish_terminal(request, response)
        if (
            total_row_count > self.settings.data_query_max_rows
            and not query_result.result_file_url
        ):
            response = self._fallback(
                request, "查询结果超过智能体允许处理的最大行数，请缩小时间范围或增加过滤条件。"
            )
            response.result_file_url = query_result.result_file_url
            return await self._finish_terminal(request, response)
        if query_result.dataset.truncated and request.primary_intent in ANALYSIS_INTENTS:
            response = self._fallback(
                request,
                "查询结果已被截断，无法基于不完整数据生成可靠分析结论；可下载完整结果或缩小范围后重试。",
            )
            response.result_file_url = query_result.result_file_url
            return await self._finish_terminal(request, response)
        if (
            dataset_id is None
            and query_result.result_file_url
            and request.primary_intent == PrimaryIntent.DETAIL_QUERY
        ):
            dataset_id = await self._import_query_result_file(
                request, query_result.result_file_url
            )
        if (
            dataset_id is None
            and query_result.dataset.rows
            and not query_result.dataset.truncated
            and not (
                request.primary_intent == PrimaryIntent.METRIC_QUERY
                and query_result.dataset.row_count == 1
                and not request.dimensions
            )
        ):
            dataset_id = await self._persist_query_dataset(request, query_result)
        if dataset_id is None:
            request.assumptions.append("LATEST_RESULT_DATASET_NOT_REUSABLE")
        else:
            request.assumptions = [
                value for value in request.assumptions
                if value != "LATEST_RESULT_DATASET_NOT_REUSABLE"
            ]
        current_analysis_plan = self.analysis_planner.build(request)
        plan_minimum_rows = max(
            (
                rule.minimum_rows
                for rule in current_analysis_plan.sufficiency_rules
            ),
            default=0,
        ) if current_analysis_plan is not None else 0
        minimum_rows = plan_minimum_rows if request.primary_intent in {
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
            PrimaryIntent.COMPOSITION_ANALYSIS,
            PrimaryIntent.ANOMALY_ANALYSIS,
            PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        } else 0
        no_effective_values = bool(query_result.dataset.rows) and all(
            value is None
            for row in query_result.dataset.rows
            for value in row.values()
        )
        if (
            not query_result.dataset.rows and not query_result.result_file_url
        ) or no_effective_values:
            # The SQL/ASL contract is still verified when its result is empty.
            # Preserve it as the latest executable context so a subsequent
            # time refinement keeps entity filters introduced in this turn
            # instead of falling back to an older successful request.
            await self.sessions.put_last_request(request)
            if (
                (not query_result.dataset.rows or no_effective_values)
                and not query_result.result_file_url
                and request.primary_intent in {
                    PrimaryIntent.DETAIL_QUERY,
                    PrimaryIntent.METRIC_QUERY,
                }
            ):
                # An empty detail result is a valid, auditable query outcome.  It
                # must not be reported as an execution failure: follow-up
                # questions frequently narrow a previously successful result to
                # a period or condition that genuinely contains no records.
                evidence = EvidenceItem(
                    evidence_id=f"query:{query_result.dataset.snapshot_id}",
                    kind="QUERY_RESULT",
                    source_ref=f"data-source:{query_result.data_source_id or 'unknown'}",
                    payload={
                        "columns": query_result.dataset.columns,
                        "row_count": 0,
                        "returned_row_count": 0,
                        "total_row_count_confirmed": True,
                        "truncated": False,
                        "data_as_of": query_result.dataset.data_as_of.isoformat(),
                        "quality_status": query_result.dataset.quality_status,
                        "result_fingerprint": query_result.dataset.snapshot_id,
                        **self._source_watermark_payload(request, query_result.dataset),
                    },
                )
                response = AgentResponse(
                    request_id=request.request_id,
                    conversation_id=request.conversation_id,
                    status="COMPLETED",
                    intent=request.primary_intent,
                    intent_source=request.intent_source,
                    intent_confidence=request.intent_confidence,
                    answer=(
                        "查询已完成。在指定时间范围和筛选条件下没有找到匹配的业务记录；"
                        + (
                            "本次返回 0 条结果，这表示结果为空，并非查询执行失败。"
                            if request.primary_intent is PrimaryIntent.DETAIL_QUERY
                            else "当前没有可用于计算该指标的数据；无数据不等同于指标值为 0。"
                        )
                    ),
                    evidence=[evidence],
                    reliability=ReliabilityReport(
                        level="HIGH",
                        score=1,
                        gates={"query_succeeded": True, "empty_result_confirmed": True},
                    ),
                    dataset_id=dataset_id,
                )
                return await self._finish_terminal(request, response)
            scope = "、".join(
                f"{item.get('field')}={item.get('value')}"
                for item in request.filters
                if isinstance(item, dict)
                and item.get("field")
                and item.get("value") not in (None, "", [])
            )
            message = "查询执行成功，但指定条件下没有匹配到有效业务数据。"
            watermark = query_result.dataset.source_data_as_of
            if request.time_range is not None and watermark is not None:
                watermark_day = watermark.date()
                if request.time_range.start > watermark_day:
                    message += (
                        f"本次查询时间从 {request.time_range.start.isoformat()} 开始，"
                        f"但当前业务数据只更新到 {watermark_day.isoformat()}，"
                        "所选区间完全位于数据水位之后。可改查水位日前的时间范围。"
                    )
                elif request.time_range.end_exclusive > watermark_day + timedelta(days=1):
                    message += (
                        f"当前业务数据只更新到 {watermark_day.isoformat()}，"
                        "所选区间在该日期之后的部分尚无数据。"
                    )
            if scope:
                message += f"当前筛选条件：{scope}。"
                if request.semantic_filter_bindings:
                    message += "筛选字段和值已经按当前语义模型的实体属性向量库规范化。"
                else:
                    message += (
                        "本次没有获得可核验的实体属性向量绑定；"
                        "请使用更完整的业务名称重新查询。"
                    )
            response = self._fallback(request, message)
            response.dataset_id = dataset_id
            response.result_file_url = query_result.result_file_url
            return await self._finish_terminal(request, response)
        if query_result.dataset.row_count < minimum_rows:
            # The deterministic analysis method may require more observations,
            # but every returned database row is still valid query evidence.
            # Do not turn "insufficient for a trend/outlier conclusion" into
            # "no result": disclose the rows, retain the immutable dataset for
            # follow-ups, and clearly separate facts from the skipped analysis.
            insufficiency = (
                f"当前返回 {query_result.dataset.row_count} 行有效数据，"
                f"少于{self._intent_label(request.primary_intent)}所需的最少 "
                f"{minimum_rows} 行；已展示真实查询结果，但不据此生成趋势、比较、"
                "异常、归因或预测结论。"
            )
            evidence = [
                EvidenceItem(
                    evidence_id=f"query:{query_result.dataset.snapshot_id}",
                    kind="QUERY_RESULT",
                    source_ref=(
                        f"data-source:{query_result.data_source_id or 'unknown'}"
                    ),
                    payload={
                        "columns": query_result.dataset.columns,
                        "row_count": total_row_count,
                        "returned_row_count": query_result.dataset.row_count,
                        "total_row_count_confirmed": total_row_count_confirmed,
                        "truncated": query_result.dataset.truncated,
                        "data_as_of": query_result.dataset.data_as_of.isoformat(),
                        "quality_status": query_result.dataset.quality_status,
                        "result_fingerprint": query_result.dataset.snapshot_id,
                        "analysis_sufficiency": {
                            "sufficient": False,
                            "required_rows": minimum_rows,
                            "returned_rows": query_result.dataset.row_count,
                        },
                        **self._source_watermark_payload(
                            request, query_result.dataset
                        ),
                    },
                )
            ]
            evidence.extend(self._derived_metric_evidence(request, query_result))
            evidence.extend(self._semantic_metric_evidence(
                request,
                query_result,
                semantic_model_id=chat.semantic_model_id,
                business_domain_id=self._effective_business_domain_id(chat),
            ))
            answer = (
                "查询已成功，以下是数据库实际返回的数据：\n\n"
                + self._analyze(
                    request,
                    query_result.dataset.columns,
                    query_result.dataset.rows,
                    KnowledgeContext(query=request.original_question, documents=[]),
                    result_truncated=query_result.dataset.truncated,
                )
                + f"\n\n数据充足性说明：{insufficiency}"
            )
            source_watermark_note = self._source_watermark_note(
                request, query_result.dataset
            )
            if source_watermark_note:
                answer += f"\n{source_watermark_note}"
            reliability = ReliabilityReport(
                level="LIMITED",
                score=(
                    0.75
                    if query_result.dataset.quality_status.upper() == "PASS"
                    else 0.55
                ),
                gates={
                    "query_succeeded": True,
                    "query_evidence_preserved": True,
                    "analysis_data_sufficient": False,
                    "analysis_conclusion_withheld": True,
                },
                warnings=[insufficiency],
            )
            await emit_progress(
                "RELIABILITY_CHECK",
                "COMPLETED",
                "查询结果已通过数据证据校验；分析样本不足，已保留并展示实际数据，"
                "同时跳过不可靠的分析结论。",
                reliability_level=reliability.level,
                reliability_score=reliability.score,
            )
            response = AgentResponse(
                request_id=request.request_id,
                conversation_id=request.conversation_id,
                status="PARTIAL_SUCCESS",
                intent=request.primary_intent,
                intent_source=request.intent_source,
                intent_confidence=request.intent_confidence,
                answer=answer,
                evidence=evidence,
                reliability=reliability,
                dataset_id=dataset_id,
                result_file_url=query_result.result_file_url,
            )
            return await self._finish_terminal(
                request,
                response,
            )
        evidence: list[EvidenceItem] = [
            EvidenceItem(
                evidence_id=f"query:{query_result.dataset.snapshot_id}",
                kind="QUERY_RESULT",
                source_ref=f"data-source:{query_result.data_source_id or 'unknown'}",
                payload={
                    "columns": query_result.dataset.columns,
                    "row_count": total_row_count,
                    "returned_row_count": query_result.dataset.row_count,
                    "total_row_count_confirmed": total_row_count_confirmed,
                    "truncated": query_result.dataset.truncated,
                    "data_as_of": query_result.dataset.data_as_of.isoformat(),
                    "quality_status": query_result.dataset.quality_status,
                    "result_fingerprint": query_result.dataset.snapshot_id,
                    **self._source_watermark_payload(request, query_result.dataset),
                    **(
                        {
                            "presentation": {
                                "mode": "UNIQUE_RELATIONSHIP_PROJECTION",
                                "original_relationship_row_count": query_result.dataset.row_count,
                                "unique_combination_count": len(unique_projection_rows),
                            }
                        }
                        if (
                            not query_result.dataset.truncated
                            and (
                                unique_projection_rows := self._relationship_projection_rows(
                                    request,
                                    query_result.dataset.columns,
                                    query_result.dataset.rows,
                                )
                            )
                            is not None
                        )
                        else {}
                    ),
                },
            )
        ]
        evidence.extend(
            self._derived_metric_evidence(request, query_result)
        )
        evidence.extend(
            self._semantic_metric_evidence(
                request,
                query_result,
                semantic_model_id=chat.semantic_model_id,
                business_domain_id=self._effective_business_domain_id(chat),
            )
        )
        enrichment_executions: list[ExtensionExecution] = []
        external_supplement = ""
        external_records: list[dict[str, Any]] = []
        ranking_labels: list[str] = []
        covered_ranking_labels: list[str] = []
        if request.confirmed_memory_ids:
            evidence.append(
                EvidenceItem(
                    evidence_id=f"long-memory:{request.request_id}",
                    kind="CONFIRMED_LONG_TERM_MEMORY",
                    source_ref="agent_long_term_memory",
                    payload={"memory_ids": request.confirmed_memory_ids},
                )
            )
        knowledge_context = KnowledgeContext(query=request.original_question)
        # An empty request scope means the application is not bound to a
        # knowledge base. Never broaden it with process-wide defaults here.
        knowledge_scope = list(request.knowledge_base_names)
        if _requires_deterministic_analysis(request) and knowledge_scope:
            await emit_progress(
                "KNOWLEDGE_RETRIEVAL", "RUNNING", "正在检索与分析相关的业务文档证据。"
            )
            try:
                knowledge_context = await self.adapters.knowledge.retrieve_analysis_context(
                    request, query_result.dataset, identity
                )
            except AdapterError as exc:
                logger.warning(
                    "optional analysis knowledge unavailable (%s); continuing "
                    "with deterministic data evidence",
                    exc.code,
                )
            if knowledge_context.documents:
                evidence.append(EvidenceItem(
                    evidence_id=f"analysis-knowledge:{request.request_id}",
                    kind="ANALYSIS_KNOWLEDGE",
                    source_ref=self.settings.knowledge_base_search_path,
                    payload={"hit_count": len(knowledge_context.documents), "sources": [
                        {"source": d.source, "block_id": d.block_id, "kb_name": d.kb_name, "score": d.score}
                        for d in knowledge_context.documents
                    ]},
                ))
            await emit_progress(
                "KNOWLEDGE_RETRIEVAL",
                "COMPLETED",
                "业务文档证据检索完成。",
                document_count=len(knowledge_context.documents),
            )

        analysis_output = None
        synthesized_answer: str | None = None
        if _requires_deterministic_analysis(request):
            await emit_progress(
                "DETERMINISTIC_ANALYSIS", "RUNNING", "正在使用确定性算法计算分析结果。"
            )
            try:
                analysis_output = (
                    self.analysis_engine.analyze_ranking(
                        request,
                        query_result.dataset.columns,
                        query_result.dataset.rows,
                        knowledge_context,
                    )
                    if ordered_entity_metric_ranking_request(request)
                    or AnalysisOperator.TOP_N in request.operators
                    or AnalysisOperator.BOTTOM_N in request.operators
                    else self.analysis_engine.analyze(
                        request,
                        query_result.dataset.columns,
                        query_result.dataset.rows,
                        knowledge_context,
                    )
                )
            except AnalysisError as exc:
                fallback = self._fallback(
                    request, f"数据不足以支持可靠分析：{exc}。"
                )
                fallback.evidence = evidence
                fallback.dataset_id = dataset_id
                fallback.requirements = list(exc.requirements)
                fallback.missing_slots = [item.code for item in exc.requirements]
                fallback.extension_executions = enrichment_executions
                self._attach_external_enrichment(
                    fallback,
                    request_id=str(request.request_id),
                    supplement=external_supplement,
                    records=external_records,
                )
                if exc.requirements:
                    fallback.answer += "\n需要补充或处理：" + "；".join(
                        f"{item.description} 建议：{item.action}"
                        for item in exc.requirements
                    )
                return await self._finish_terminal(
                    request,
                    fallback,
                )
            structured_analysis = self.insight_interpreter.interpret(
                request, analysis_output
            )
            answer_plan = self.answer_planner.plan(structured_analysis)
            governed_facts = dict(analysis_output.facts)
            governed_facts["structured_analysis_result"] = (
                structured_analysis.model_dump(mode="json")
            )
            governed_facts["answer_plan"] = answer_plan.model_dump(mode="json")
            analysis_output = replace(
                analysis_output,
                answer=answer_plan.render(),
                facts=governed_facts,
            )
            await emit_progress(
                "DETERMINISTIC_ANALYSIS",
                "COMPLETED",
                "确定性计算、业务解释和答案规划已完成，等待结果可靠性校验。",
                method=analysis_output.method,
            )
            evidence.append(
                EvidenceItem(
                    evidence_id=f"analysis:{request.request_id}",
                    kind="ANALYSIS_RESULT",
                    source_ref=f"deterministic:{analysis_output.method}",
                    payload={
                        "method": analysis_output.method,
                        "facts": analysis_output.facts,
                        "warnings": analysis_output.warnings,
                    },
                )
            )
            if self.analysis_synthesizer is not None:
                await emit_progress(
                    "ANSWER_SYNTHESIS", "RUNNING", "正在将已验证的分析事实整理成回答。"
                )
                try:
                    synthesized_answer, synthesis = (
                        await self.analysis_synthesizer.synthesize(
                            request, analysis_output, evidence
                        )
                    )
                    evidence.append(
                        EvidenceItem(
                            evidence_id=f"analysis-synthesis:{request.request_id}",
                            kind="ANSWER_SYNTHESIS",
                            source_ref=(
                                f"qwen:{self.settings.analysis_synthesis_model_name}"
                            ),
                            payload={
                                "model": self.settings.analysis_synthesis_model_name,
                                "claim_count": len(synthesis.claims),
                                "claims": [
                                    claim.model_dump(mode="json")
                                    for claim in synthesis.claims
                                ],
                            },
                        )
                    )
                except (
                    httpx.HTTPError,
                    KeyError,
                    RuntimeError,
                    ValueError,
                    SynthesisValidationError,
                ) as exc:
                    logger.warning(
                        "analysis synthesis unavailable or rejected; using "
                        "deterministic answer: %s",
                        exc,
                    )
                    analysis_evidence = next(
                        item for item in evidence if item.kind == "ANALYSIS_RESULT"
                    )
                    warnings = analysis_evidence.payload.setdefault(
                        "presentation_warnings", []
                    )
                    warning = (
                        "Qwen分析总结未通过可用性或证据校验，"
                        "已返回确定性分析结果"
                    )
                    if warning not in warnings:
                        warnings.append(warning)
                await emit_progress(
                    "ANSWER_SYNTHESIS",
                    "COMPLETED" if synthesized_answer is not None else "DEGRADED",
                    (
                        "分析结论整理完成。"
                        if synthesized_answer is not None
                        else "模型总结不可用，已使用确定性分析结论。"
                    ),
                )

        reliability = self._reliability(request, evidence, query_result.dataset.quality_status)
        await emit_progress(
            "RELIABILITY_CHECK",
            "COMPLETED" if reliability.level != "FAIL" else "FAILED",
            (
                "### ◉ 结果研判与应答\n"
                f"校验结论：{reliability.level}（{reliability.score:.2f}）。\n"
                f"数据质量：{query_result.dataset.quality_status}；证据数量：{len(evidence)}；"
                f"告警数量：{len(reliability.warnings)}。\n"
                + ("结果通过可靠性门禁。" if reliability.level != "FAIL" else "结果未通过可靠性门禁，不输出未经验证的数值。")
            ),
            reliability_level=reliability.level,
            reliability_score=round(float(reliability.score), 4),
        )
        await emit_progress(
            "INSIGHT_ANALYSIS",
            "COMPLETED" if reliability.level != "FAIL" else "SKIPPED",
            (
                "### ◉ 结果研判与应答\n"
                f"分析意图：{self._intent_label(request.primary_intent)}。"
                + (
                    "已完成确定性计算、结构化解释和答案优先级筛选。\n"
                    f"核心判断：{analysis_output.facts.get('structured_analysis_result', {}).get('headline', analysis_output.answer)}\n"
                    if analysis_output is not None
                    else "当前意图采用结构化查询结果展示，不额外生成推断性洞察。\n"
                )
                + "说明：展示的是可审计的方法和事实摘要，不包含模型内部隐藏推理。"
            ),
        )
        if reliability.level == "FAIL":
            response = self._fallback(
                    request, "结果未通过数据、指标与证据一致性校验，本次不返回数值。"
                )
            response.dataset_id = dataset_id
            return await self._finish_terminal(
                request,
                response,
            )
        if external_search_mode == "ENRICH":
            await emit_progress(
                "EXTERNAL_SEARCH",
                "RUNNING",
                "正在按数据库确定性排名的实体检索公开信息。",
            )
            (
                enrichment_executions,
                external_supplement,
                external_records,
                ranking_labels,
                covered_ranking_labels,
            ) = await self._enrich_ranked_entities(
                chat=chat,
                request=request,
                analysis_output=analysis_output,
            )
            complete_coverage = bool(ranking_labels) and (
                covered_ranking_labels == ranking_labels
            )
            await emit_progress(
                "EXTERNAL_SEARCH",
                "COMPLETED" if complete_coverage else "DEGRADED",
                (
                    "排名实体的公开信息检索完成。"
                    if complete_coverage
                    else "部分排名实体未返回能明确对应其名称的公开信息。"
                ),
                ranking_labels=ranking_labels,
                covered_ranking_labels=covered_ranking_labels,
            )
        structured_table_methods = {
            "validated_top_n_ranking",
            "validated_ordered_metric_ranking",
            "entity_period_decline_ranking",
        }
        answer = (
            (
                analysis_output.answer
                if (
                    analysis_output.method in structured_table_methods
                )
                else (synthesized_answer or analysis_output.answer)
            )
            if analysis_output is not None
            else (
                (
                    (
                        f"查询成功，共命中 {total_row_count} 条结果；"
                        if total_row_count_confirmed
                        else "查询成功，但SQL服务未确认完整结果行数；"
                    )
                    + f"本次接口返回 {query_result.dataset.row_count} 条预览，"
                    "以下内容不是全量清单。\n"
                    + self._analyze(
                        request,
                        query_result.dataset.columns,
                        query_result.dataset.rows,
                        knowledge_context,
                        result_truncated=True,
                    )
                    + (
                        "\n完整结果请使用回答末尾的附件链接下载。"
                        if query_result.result_file_url
                        else "\n本次未收到完整结果文件；请缩小查询范围或继续分页查询。"
                    )
                )
                if query_result.dataset.truncated
                else self._analyze(
                    request,
                    query_result.dataset.columns,
                    query_result.dataset.rows,
                    knowledge_context,
                    result_truncated=query_result.dataset.truncated,
                )
            )
        )
        if analysis_output is not None and analysis_output.warnings:
            answer += "\n注意事项：" + "；".join(analysis_output.warnings) + "。"
        unavailable_fields = [
            value.split("=", 1)[1]
            for value in request.assumptions
            if value.startswith("UNAVAILABLE_REQUESTED_FIELD=") and "=" in value
        ]
        if unavailable_fields:
            answer += (
                "\n字段说明：当前语义模型未配置“"
                + "、".join(dict.fromkeys(unavailable_fields))
                + "”，已返回其余可执行指标；未使用其他字段代替该口径。"
            )
        activity_definition_note = self._activity_definition_note(request)
        if activity_definition_note:
            answer += f"\n{activity_definition_note}"
        source_watermark_note = self._source_watermark_note(
            request, query_result.dataset
        )
        if source_watermark_note:
            answer += f"\n{source_watermark_note}"
        incomplete_result = bool(
            query_result.dataset.truncated and not query_result.result_file_url
        )
        if incomplete_result:
            reliability = ReliabilityReport(
                level="LIMITED",
                score=min(reliability.score, 0.65),
                gates={
                    **reliability.gates,
                    "complete_result_available": False,
                    "preview_disclosed": True,
                },
                warnings=[
                    *reliability.warnings,
                    "SQL服务仅返回结果预览，且未提供完整结果文件。",
                ],
            )
        response = AgentResponse(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            status="PARTIAL_SUCCESS" if incomplete_result else "COMPLETED",
            intent=request.primary_intent,
            intent_source=request.intent_source,
            intent_confidence=request.intent_confidence,
            answer=answer,
            evidence=evidence,
            reliability=reliability,
            dataset_id=dataset_id,
            result_file_url=query_result.result_file_url,
            chart_specs=(
                analysis_output.facts.get("chart_specs", [])
                if analysis_output is not None
                else []
            ),
        )
        if external_search_mode == "ENRICH":
            response.extension_executions = enrichment_executions
        else:
            response.extension_executions = await self.extension_dispatcher.execute(
                chat=chat,
                intent=request.primary_intent.value,
                builtin_skill=skill_for_intent(request.primary_intent),
                payload={
                    "question": request.original_question,
                    "intent": request.primary_intent.value,
                    "semantic_model_id": request.semantic_model_id,
                    "business_domain_ids": request.business_domain_ids,
                    "application_id": request.application_id,
                    "conversation_id": request.conversation_id,
                    "dataset_id": dataset_id,
                    "columns": query_result.dataset.columns,
                    "rows": query_result.dataset.rows[:200],
                    "row_count": total_row_count,
                    "returned_row_count_for_extension": min(
                        query_result.dataset.row_count, 200
                    ),
                    "truncated_for_extension": (
                        query_result.dataset.truncated
                        or query_result.dataset.row_count > 200
                    ),
                    "complete_dataset_available_to_extension": (
                        not query_result.dataset.truncated
                        and query_result.dataset.row_count <= 200
                    ),
                    "deterministic_answer": answer,
                    "count": 5,
                    "summary": True,
                },
            )
            external_supplement, external_records = self._external_search_material(
                response.extension_executions,
            )
        self._attach_external_enrichment(
            response,
            request_id=str(request.request_id),
            supplement=external_supplement,
            records=external_records,
            ranking_labels=ranking_labels,
            covered_ranking_labels=covered_ranking_labels,
        )
        failed_extensions = [
            item for item in response.extension_executions if item.status != "COMPLETED"
        ]
        if failed_extensions:
            response.answer += "\n部分扩展工具未成功执行，核心查询与确定性分析结果不受影响。"
        if request.primary_intent == PrimaryIntent.REPORT_GENERATION:
            await self._attach_requested_report(
                response, request=request, identity=identity, dataset_id=dataset_id
            )
        request.temporal_anchor = build_temporal_anchor(
            request,
            query_result.dataset.columns,
            query_result.dataset.rows,
        )
        if (
            request.primary_intent == PrimaryIntent.TREND_ANALYSIS
            and not request.metrics
            and request.temporal_anchor is not None
        ):
            metric_candidates = [
                str(column)
                for column in query_result.dataset.columns
                if not any(
                    marker in str(column).casefold()
                    for marker in (
                        "日期", "时间", "月份", "period", "month", "year",
                    )
                )
            ]
            if len(metric_candidates) == 1:
                # A successful, validated trend result is authoritative enough
                # to name its sole value column for a later elliptical
                # comparison, even when the initial rule parse used a generic
                # phrase such as “销售趋势”.
                request.metrics = [MetricRef(input=metric_candidates[0])]
        if dataset_id is not None:
            # Successful episodes point at their immutable result artifact.
            # Follow-ups must never infer the active result from an older or
            # already-sliced preview when this exact reference is available.
            request.source_dataset_id = dataset_id
        await self.sessions.put_last_request(request)
        return await self._finish_terminal(request, response)

    async def _composite_task_answer(
        self,
        *,
        chat: ChatRequest,
        identity: TrustedIdentity,
        plan: TaskPlan,
        task_results: list[TaskExecutionResult],
        conversation_by_task: dict[str, str],
    ) -> str:
        """Assemble DAG results from verified datasets, never nested Markdown.

        Homogeneous, bounded datasets are rendered as one table with a branch
        column. Heterogeneous, unavailable, large, partial, or failed results
        remain independent sections. This keeps formatting deterministic while
        preserving every branch's original status and explanation.
        """

        fallback = self._task_result_summary_table(task_results)
        if (
            self.dataset_store is None
            or not task_results
            or any(result.status != "COMPLETED" for result in task_results)
            or any(not result.dataset_id for result in task_results)
        ):
            return fallback

        datasets: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
        try:
            for task, result in zip(plan.tasks, task_results, strict=True):
                child_conversation = conversation_by_task.get(task.task_id)
                if not child_conversation or not result.dataset_id:
                    return fallback
                raw_items = await self.sessions.get_recent_dataset_references(
                    identity.tenant_id,
                    identity.user_id,
                    chat.application_id,
                    child_conversation,
                    limit=self.settings.dataset_recent_limit,
                )
                raw = next(
                    (
                        item for item in raw_items
                        if item.get("dataset_id") == result.dataset_id
                    ),
                    None,
                )
                if raw is None:
                    return fallback
                reference = restore_reference(raw)
                # Final chat tables are bounded. Large datasets retain the
                # child's preview and downloadable dataset reference instead of
                # being fully materialized merely for presentation.
                if reference.row_count > 200:
                    return fallback
                loaded = await asyncio.to_thread(
                    self.dataset_store.load_dataset,
                    reference,
                    current_scope=reference.scope,
                )
                datasets[task.task_id] = (
                    list(loaded.reference.columns),
                    [dict(row) for row in loaded.rows],
                )
        except Exception as exc:
            logger.warning("composite result presentation fallback: %s", exc)
            return fallback

        return self._render_homogeneous_task_datasets(
            chat.question,
            plan,
            task_results,
            datasets,
        ) or fallback

    @staticmethod
    def _task_facet_label(question: str) -> tuple[str, str]:
        compact = re.sub(r"\s+", "", question)
        if "主要适用科室" in compact or "主科室" in compact:
            return "适用类型", "主要适用"
        if "次要适用科室" in compact or "次要科室" in compact or "次科室" in compact:
            return "适用类型", "次要适用"
        for label in ("正常", "异常", "新增", "存量", "线上", "线下", "有效", "无效"):
            if label in compact:
                return "查询分类", label
        return "查询分支", question

    @classmethod
    def _render_homogeneous_task_datasets(
        cls,
        root_question: str,
        plan: TaskPlan,
        task_results: list[TaskExecutionResult],
        datasets: dict[str, tuple[list[str], list[dict[str, Any]]]],
    ) -> str | None:
        if len(datasets) != len(task_results) or len(plan.tasks) != len(task_results):
            return None
        schemas = [tuple(datasets[task.task_id][0]) for task in plan.tasks]
        if not schemas:
            return None
        # SQL engines and semantic projections may return the same logical
        # columns in a different order for sibling branches.  Column order is
        # presentation metadata, not a schema incompatibility: compare the
        # normalized column identities while retaining one deterministic order
        # for the merged table.  Length is checked separately so duplicate
        # column names can never be hidden by the set comparison.
        canonical_schema = schemas[0]
        canonical_columns = set(canonical_schema)
        if any(
            len(schema) != len(canonical_schema)
            or set(schema) != canonical_columns
            for schema in schemas[1:]
        ):
            return None
        facet_pairs = [cls._task_facet_label(task.question) for task in plan.tasks]
        facet_columns = {column for column, _ in facet_pairs}
        facet_column = facet_columns.pop() if len(facet_columns) == 1 else "查询分支"
        columns = list(canonical_schema)
        if facet_column == "适用类型":
            # For applicability queries, keep the business subject first and
            # the requested department last, regardless of the raw SQL order.
            # This yields a stable ``商品名称 / 适用类型 / 适用科室`` table.
            columns.sort(
                key=lambda column: (
                    0 if "商品" in cls._display_column_name(column) else
                    2 if "科室" in cls._display_column_name(column) else
                    1
                )
            )
        display_columns = [
            *columns[:-1], facet_column, *columns[-1:]
        ] if columns else [facet_column]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for task, (_, label) in zip(plan.tasks, facet_pairs, strict=True):
            _, task_rows = datasets[task.task_id]
            for source_row in task_rows:
                row = dict(source_row)
                row[facet_column] = label
                fingerprint = json.dumps(
                    [row.get(column) for column in display_columns],
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                rows.append(row)

        total = sum(len(datasets[task.task_id][1]) for task in plan.tasks)
        lines = [
            f"已完成 {len(plan.tasks)} 个独立查询，共返回 {total} 条明细。",
        ]
        if facet_column == "适用类型":
            normalized = root_question.translate(str.maketrans({
                "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
                "\u2014": "-", "\u2212": "-", "\ufe58": "-", "\ufe63": "-",
                "\uff0d": "-",
            }))
            identifier = re.search(
                r"(?<![0-9A-Za-z])(?=[0-9A-Za-z-]{3,64}(?![0-9A-Za-z-]))"
                r"(?=[0-9A-Za-z-]*[A-Za-z])[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+",
                normalized,
            )
            if identifier is not None:
                lines.append(
                    f"查询条件：规格型号 = `{identifier.group(0)}`；"
                    "“商品名称”列为该规格型号对应的规范商品名称。"
                )
        lines.extend(["", cls._markdown_result_table(display_columns, rows)])
        return "\n".join(lines)

    @staticmethod
    def _requested_report_format(question: str) -> str:
        compact = "".join(question.lower().split())
        if "pdf" in compact:
            return "pdf"
        if any(marker in compact for marker in ("word", "docx", "文档")):
            return "docx"
        return "xlsx"

    async def _attach_composite_report(
        self,
        response: AgentResponse,
        *,
        chat: ChatRequest,
        identity: TrustedIdentity,
        plan: TaskPlan,
        responses: dict[str, AgentResponse | Exception],
        conversation_by_task: dict[str, str],
    ) -> None:
        """Export every complete report dataset as a separate auditable section."""
        if self.report_exporter is None:
            response.answer += "\n未生成下载文件：当前环境尚未启用MinIO报表存储。"
            return
        export_many = getattr(self.report_exporter, "export_many", None)
        if not callable(export_many):
            response.answer += "\n未生成下载文件：当前报表组件不支持复合数据集导出。"
            return
        sections: list[tuple[str, Any]] = []
        seen_dataset_ids: set[str] = set()
        for task in plan.tasks:
            value = responses.get(task.task_id)
            if (
                not isinstance(value, AgentResponse)
                or not value.dataset_id
                or value.dataset_id in seen_dataset_ids
            ):
                continue
            raw_items = await self.sessions.get_recent_dataset_references(
                identity.tenant_id,
                identity.user_id,
                chat.application_id,
                conversation_by_task.get(task.task_id, chat.conversation_id),
                limit=self.settings.dataset_recent_limit,
            )
            raw = next(
                (
                    item for item in raw_items
                    if item.get("dataset_id") == value.dataset_id
                ),
                None,
            )
            if raw is None:
                continue
            sections.append((task.question, restore_reference(raw)))
            seen_dataset_ids.add(value.dataset_id)
        if len(sections) < 2:
            response.answer += (
                "\n未生成综合下载文件：至少需要两个已完整落盘的独立结果集；"
                "未完成章节及仅有上游下载文件的章节已在正文中单独标明。"
            )
            return
        file_format = self._requested_report_format(chat.question)
        root_scope = DatasetScope(
            identity.tenant_id,
            identity.user_id,
            chat.application_id,
            chat.conversation_id,
        )
        try:
            result = await asyncio.to_thread(
                export_many,
                sections,
                scope=root_scope,
                file_format=file_format,
                title="综合数据分析报告",
            )
            report_reference = result.pop("report_reference")
            try:
                await self.sessions.put_report_reference(report_reference)
            except Exception:
                await asyncio.to_thread(
                    self.report_exporter.delete_object, result["object_name"]
                )
                raise
        except Exception as exc:
            logger.warning("composite report generation failed: %s", exc)
            response.answer += "\n分析正文已生成，但综合下载文件生成失败，请稍后重试。"
            return
        exported_dataset_ids = [reference.dataset_id for _, reference in sections]
        response.files.append(GeneratedFile(
            file_id=result["report_id"],
            dataset_id=None,
            dataset_ids=exported_dataset_ids,
            format=result["format"],
            object_name=result["object_name"],
            download_url=result["download_url"],
            byte_size=result["byte_size"],
            expires_at=result["object_expires_at"],
        ))
        response.answer += (
            f"\n已生成包含 {len(sections)} 个独立数据章节的{file_format.upper()}文件，"
            "可通过 files[0].download_url 下载。"
        )

    async def _attach_requested_report(
        self,
        response: AgentResponse,
        *,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        dataset_id: str | None,
    ) -> None:
        """Create a report only when the chat intent explicitly requests one."""
        if dataset_id is None:
            response.answer += "\n未生成下载文件：本轮没有可导出的数据集。"
            return
        if self.report_exporter is None:
            response.answer += "\n未生成下载文件：当前环境尚未启用MinIO报表存储。"
            return
        references = await self.sessions.get_recent_dataset_references(
            identity.tenant_id,
            identity.user_id,
            request.application_id,
            request.conversation_id,
            limit=self.settings.dataset_recent_limit,
        )
        raw_reference = next(
            (item for item in references if item.get("dataset_id") == dataset_id), None
        )
        if raw_reference is None:
            response.answer += "\n未生成下载文件：数据集引用已过期，请重新查询后再试。"
            return
        file_format = self._requested_report_format(request.original_question)
        try:
            result = await asyncio.to_thread(
                self.report_exporter.export,
                restore_reference(raw_reference),
                scope=DatasetScope(
                    identity.tenant_id,
                    identity.user_id,
                    request.application_id,
                    request.conversation_id,
                ),
                file_format=file_format,
                title="数据分析报告",
            )
            report_reference = result.pop("report_reference")
            try:
                await self.sessions.put_report_reference(report_reference)
            except Exception:
                await asyncio.to_thread(
                    self.report_exporter.delete_object, result["object_name"]
                )
                raise
        except Exception as exc:
            logger.warning("chat report generation failed: %s", exc)
            response.answer += "\n分析已完成，但下载文件生成失败，请稍后重试。"
            return
        response.files.append(
            GeneratedFile(
                file_id=result["report_id"],
                dataset_id=dataset_id,
                dataset_ids=[dataset_id],
                format=result["format"],
                object_name=result["object_name"],
                download_url=result["download_url"],
                byte_size=result["byte_size"],
                expires_at=result["object_expires_at"],
            )
        )
        response.answer += f"\n已生成{file_format.upper()}文件，可通过返回的 files[0].download_url 下载。"

    async def _knowledge_document_answer(
        self, request: CanonicalAnalysisRequest, identity: TrustedIdentity
    ) -> AgentResponse:
        """Answer document questions only from retrieved, scoped KB evidence."""
        empty_dataset = Dataset(
            columns=[],
            rows=[],
            row_count=0,
            snapshot_id=f"knowledge-only:{request.request_id}",
            data_as_of=datetime.now(timezone.utc),
            quality_status="PASS",
        )

        try:
            context = await self.adapters.knowledge.retrieve_analysis_context(
                request, empty_dataset, identity
            )
        except AdapterError as exc:
            return self._fallback(request, self._dependency_message(exc))
        if not context.documents:
            return self._fallback(
                request,
                "当前绑定的知识库中没有检索到能够回答该问题的内容。请确认文件已完成解析和向量入库，或换一种问法。",
            )
        documents = context.documents[:3]
        evidence = [
            EvidenceItem(
                evidence_id=f"document:{request.request_id}:{index}",
                kind="KNOWLEDGE_DOCUMENT",
                source_ref=document.source or document.kb_name or "knowledge-base",
                payload={
                    "content": document.content,
                    "source": document.source,
                    "kb_name": document.kb_name,
                    "block_id": document.block_id,
                    "normalized_relevance": document.normalized_relevance,
                },
            )
            for index, document in enumerate(documents, 1)
        ]
        excerpts = []
        for index, document in enumerate(documents, 1):
            content = " ".join(document.content.split())
            excerpts.append(f"[{index}] {content[:800]}")
        sources = "；".join(
            f"[{index}] {document.source or document.kb_name or '知识库文档'}"
            for index, document in enumerate(documents, 1)
        )
        answer = "以下是当前绑定文档中与问题相关的检索片段（尚未等同于综合结论）：\n" + "\n".join(excerpts)
        answer += "\n来源：" + sources
        return AgentResponse(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            status="COMPLETED",
            intent=request.primary_intent,
            intent_source=request.intent_source,
            intent_confidence=request.intent_confidence,
            answer=answer,
            evidence=evidence,
            reliability=ReliabilityReport(
                level="LIMITED",
                score=0.7,
                gates={
                    "knowledge_scope_bound": True,
                    "document_evidence_found": True,
                    "grounded_claim_synthesis": False,
                    "relevance_score_available": all(
                        document.normalized_relevance is not None
                        for document in documents
                    ),
                },
                warnings=["当前返回的是带来源的检索片段，未执行逐条主张验证。"],
            ),
        )

    @staticmethod
    def _is_document_question(question: str) -> bool:
        compact = "".join(question.split()).lower()
        return any(
            marker in compact
            for marker in (
                "文档", "文件", "附件", "知识库", "上传", "材料", "资料",
                "pdf", "word", "docx", "ppt", "xlsx", "excel",
            )
        )

    @staticmethod
    def _operation_limit(operation: dict[str, Any] | None) -> int | None:
        if not isinstance(operation, dict):
            return None
        operation_type = str(operation.get("type") or "").lower()
        if operation_type in {"limit", "sort_limit"}:
            count = operation.get("count")
            return count if isinstance(count, int) and count > 0 else None
        if operation_type == "pipeline":
            limits = [
                value
                for item in operation.get("operations", [])
                if isinstance(item, dict)
                and (value := DataAnalysisOrchestrator._operation_limit(item))
                is not None
            ]
            return limits[-1] if limits else None
        return None

    @staticmethod
    def _dataset_ordering_proof(reference: Any) -> dict[str, Any] | None:
        """Return trusted ordering provenance for rank-preserving follow-ups.

        A row position is meaningful only when the source query or an audited
        dataset transformation established an order.  Merely receiving rows in
        a particular sequence is not proof that “第一名” or “前三名” denotes a
        business ranking.
        """

        raw_log = (
            reference.get("transformation_log", ())
            if isinstance(reference, dict)
            else getattr(reference, "transformation_log", ())
        )
        for item in reversed(tuple(raw_log or ())):
            if not isinstance(item, dict):
                continue
            operation_type = str(item.get("type") or "").lower()
            if operation_type in {"sort", "sort_limit"} and item.get("field"):
                return dict(item)
            if operation_type == "query_provenance" and (
                item.get("ranked") or item.get("ordered_by")
            ):
                return dict(item)
            if operation_type == "pipeline":
                for operation in reversed(item.get("operations") or []):
                    if (
                        isinstance(operation, dict)
                        and str(operation.get("type") or "").lower()
                        in {"sort", "sort_limit"}
                        and operation.get("field")
                    ):
                        return dict(operation)
        return None

    @staticmethod
    def _expandable_dataset_reference(
        selected: dict[str, Any],
        references: list[dict[str, Any]],
        target_count: int,
    ) -> dict[str, Any] | None:
        """Find the nearest stored ancestor large enough for an expanded slice."""

        by_id = {
            str(item.get("dataset_id")): item
            for item in references
            if item.get("dataset_id")
        }
        queue = list(selected.get("parent_dataset_ids") or [])
        visited: set[str] = set()
        while queue:
            dataset_id = str(queue.pop(0))
            if dataset_id in visited:
                continue
            visited.add(dataset_id)
            candidate = by_id.get(dataset_id)
            if candidate is None:
                continue
            row_count = candidate.get("row_count")
            if isinstance(row_count, int) and row_count >= target_count:
                return candidate
            queue.extend(candidate.get("parent_dataset_ids") or [])
        return None

    async def _try_dataset_followup(
        self, request: CanonicalAnalysisRequest
    ) -> tuple[DataQueryResult | None, str | None]:
        if request.query_resolution_type in {
            "FOLLOWUP_ANALYSIS",
            "DIRECT_DATA_QUERY",
        }:
            # “为什么下降” needs decomposition, while a single explicit-period
            # replacement needs a newly scoped value.  Prior trend rows remain
            # temporal/semantic anchors but are not silently substituted for a
            # fresh database query. Closed-form two-period arithmetic uses the
            # separate DERIVED_RESULT_QUERY path below.
            request.execution_mode = "QUERY_DATABASE"
            return None, None
        if self.dataset_store is None:
            if request.source_dataset_id is not None:
                raise ExplicitDatasetUnavailableError(
                    "指定的数据集当前不可用，请重新选择文件或重新执行原始查询。"
                )
            return None, None
        if (
            request.conversation_control != ConversationControl.FOLLOW_UP
            and request.source_dataset_id is None
            and not is_dataset_operation_followup(request.original_question)
        ):
            return None, None
        if (
            request.source_dataset_id is None
            and "LATEST_RESULT_DATASET_NOT_REUSABLE" in request.assumptions
        ):
            # The immediately preceding query was download-only/truncated or
            # could not be persisted.  Older references in this conversation
            # are not valid substitutes for pronouns such as “刚才结果” or
            # “展示前20条”.  Skip historical dataset reuse so the inherited
            # semantic request is executed again with the new limit/filter.
            return None, None
        references = await self.sessions.get_recent_dataset_references(
            request.tenant_id,
            request.user_id,
            request.application_id,
            request.conversation_id,
            limit=self.settings.dataset_recent_limit,
        )
        if not references:
            if request.source_dataset_id is not None:
                raise ExplicitDatasetUnavailableError(
                    "指定的数据集不存在、已过期或不属于当前会话，请重新选择。"
                )
            return None, None
        try:
            selected = (
                next(
                    (
                        item
                        for item in references
                        if item.get("dataset_id") == request.source_dataset_id
                    ),
                    None,
                )
                if request.source_dataset_id
                else next(
                    (
                        item for item in references
                        if self._dataset_reference_matches_scope(item, request)
                    ),
                    None,
                )
            )
            if selected is None:
                logger.warning("requested dataset is not available in the current scope")
                if request.source_dataset_id is not None:
                    raise ExplicitDatasetUnavailableError(
                        "指定的数据集不存在、已过期或不属于当前会话，请重新选择。"
                    )
                return None, None
            source_reference = restore_reference(selected)
            scope = scope_for_request(request)
            loaded = await asyncio.to_thread(
                self.dataset_store.load_dataset,
                source_reference,
                current_scope=scope,
            )
            operation_question = (
                request.original_question.rsplit("补充：", 1)[-1].strip()
                if "补充：" in request.original_question
                else request.original_question
            )
            operation = plan_dataset_followup(
                operation_question,
                loaded.reference.columns,
                loaded.rows,
                ordering_proof=self._dataset_ordering_proof(loaded.reference),
            )
            requested_limit = self._operation_limit(operation)
            if (
                requested_limit is not None
                and requested_limit > loaded.reference.row_count
                and loaded.reference.parent_dataset_ids
            ):
                expanded = self._expandable_dataset_reference(
                    selected,
                    references,
                    requested_limit,
                )
                if expanded is not None:
                    source_reference = restore_reference(expanded)
                    loaded = await asyncio.to_thread(
                        self.dataset_store.load_dataset,
                        source_reference,
                        current_scope=scope,
                    )
                    operation = plan_dataset_followup(
                        operation_question,
                        loaded.reference.columns,
                        loaded.rows,
                        ordering_proof=self._dataset_ordering_proof(
                            loaded.reference
                        ),
                    )
                    request.assumptions.append(
                        "TOP_N_EXPANDED_FROM_BASE_RESULT"
                    )
            if operation is None:
                self._bind_result_entity_reference(
                    request,
                    operation_question,
                    loaded.reference.columns,
                    loaded.rows,
                    ordering_proof=self._dataset_ordering_proof(
                        loaded.reference
                    ),
                )
                explicit_dataset_selection = (
                    "EXPLICIT_SOURCE_DATASET_SELECTION" in request.assumptions
                )
                if (
                    not explicit_dataset_selection
                    and request.primary_intent != PrimaryIntent.REPORT_GENERATION
                    and request.resolved_comparison is None
                ):
                    # An automatically inherited result is an optimization,
                    # never the authority for a semantically changed request.
                    # If no whitelisted local operation can answer the turn,
                    # execute the merged canonical request against the source.
                    request.source_dataset_id = None
                    request.execution_mode = "QUERY_DATABASE"
                    request.assumptions.append(
                        "INHERITED_DATASET_INSUFFICIENT_REQUERY"
                    )
                    return None, None
                # File delivery is a follow-up over the latest immutable result,
                # not a reason to execute the original SQL again.
                if (
                    request.source_dataset_id is None
                    and request.primary_intent != PrimaryIntent.REPORT_GENERATION
                ):
                    return None, None
                visible_columns = [
                    column for column in loaded.reference.columns
                    if not str(column).startswith("_")
                ]
                rows = [
                    {column: row.get(column) for column in visible_columns}
                    for row in loaded.rows[: self.settings.data_query_max_rows]
                ]
                dataset = Dataset(
                    columns=visible_columns,
                    rows=rows,
                    row_count=len(rows),
                    snapshot_id=loaded.reference.dataset_id,
                    data_as_of=datetime.fromisoformat(loaded.reference.data_as_of),
                    quality_status="PASS",
                    truncated=loaded.reference.row_count > len(rows),
                )
                request.execution_mode = "REUSE_PREVIOUS_RESULT"
                return (
                    DataQueryResult(
                        asl={
                            "version": "uploaded-dataset/1",
                            "source_dataset_id": loaded.reference.dataset_id,
                        },
                        sql="UPLOADED_DATASET_NO_SQL",
                        dataset=dataset,
                        data_source_id=f"minio:{loaded.reference.dataset_id}",
                    ),
                    loaded.reference.dataset_id,
                )
            visible_source_columns = [
                column for column in loaded.reference.columns
                if not str(column).startswith("_")
            ]
            if operation.get("type") == "limit":
                # A pure limit over an immutable result is a presentation
                # projection, not a new ranking analysis. Preserve every
                # verified source column and its existing order; otherwise a
                # multi-metric TOP-N follow-up can silently drop its primary
                # ranking measure during a second analysis pass.
                request.primary_intent = PrimaryIntent.DETAIL_QUERY
                request.metrics = []
                request.entity = request.entity or visible_source_columns[0]
                request.fields = list(visible_source_columns)
                request.operators = [
                    AnalysisOperator.FILTER,
                    AnalysisOperator.RENDER_TABLE,
                ]
            result = await asyncio.to_thread(
                self.dataset_store.execute_followup,
                source_reference,
                current_scope=scope,
                operation=operation,
                ttl_seconds=self.settings.dataset_ttl_seconds,
                preview_rows=self.settings.data_query_max_rows,
            )
            request.execution_mode = "REUSE_PREVIOUS_RESULT"
            await self.sessions.put_dataset_reference(
                result.reference.to_dict(), recent_limit=self.settings.dataset_recent_limit
            )
            visible_columns = [
                column for column in result.reference.columns
                if not str(column).startswith("_")
            ]
            rows = [
                {column: row.get(column) for column in visible_columns}
                for row in result.preview_rows
            ]
            dataset = Dataset(
                columns=visible_columns,
                rows=rows,
                row_count=len(rows),
                snapshot_id=result.reference.dataset_id,
                data_as_of=datetime.fromisoformat(result.reference.data_as_of),
                quality_status="PASS",
                truncated=result.reference.row_count > len(rows),
            )
            return (
                DataQueryResult(
                    asl={
                        "version": "dataset-followup/1",
                        "source_dataset_id": source_reference.dataset_id,
                        "operation": operation,
                    },
                    sql="DATASET_FOLLOWUP_NO_SQL",
                    dataset=dataset,
                    data_source_id=f"minio:{result.reference.dataset_id}",
                ),
                result.reference.dataset_id,
            )
        except ExplicitDatasetUnavailableError:
            raise
        except Exception as exc:
            if request.source_dataset_id is not None:
                logger.warning("explicit dataset follow-up failed closed: %s", exc)
                raise ExplicitDatasetUnavailableError(
                    "指定的数据集无法安全读取或计算，请重新导入文件或重新执行原始查询。"
                ) from exc
            # Historical reuse is an optimization. Any stale/corrupt/ambiguous
            # dataset falls back to a fresh semantic query instead of failing chat.
            logger.warning("dataset follow-up unavailable; regenerating query: %s", exc)
            return None, None

    @classmethod
    def _bind_result_entity_reference(
        cls,
        request: CanonicalAnalysisRequest,
        question: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        *,
        ordering_proof: dict[str, Any] | None = None,
    ) -> None:
        """Bind typed references to verified rows from the preceding result.

        Row order is used for ordinals only when the persisted dataset carries
        ordering provenance.  Set references such as “这些科室” do not require
        order, but still bind from structured result columns rather than from
        assistant prose.  Punctuation-only inherited values are rejected.
        """

        def valid_value(value: Any) -> bool:
            if value is None:
                return False
            text = str(value).strip()
            return bool(text and re.search(r"[\w\u4e00-\u9fff]", text))

        cleaned_filters: list[dict[str, Any]] = []
        removed_invalid = False
        for item in request.filters:
            if (
                isinstance(item, dict)
                and str(item.get("operator") or "").upper()
                in {"IS_NULL", "IS_NOT_NULL"}
            ):
                cleaned_filters.append(item)
                continue
            value = item.get("value") if isinstance(item, dict) else None
            if isinstance(value, list):
                values = [candidate for candidate in value if valid_value(candidate)]
                if not values:
                    removed_invalid = True
                    continue
                cleaned_filters.append({**item, "value": values})
            elif valid_value(value):
                cleaned_filters.append(item)
            else:
                removed_invalid = True
        request.filters = cleaned_filters
        if removed_invalid:
            request.assumptions.append("INVALID_INHERITED_FILTER_VALUE_DROPPED")

        compact = re.sub(r"\s+", "", question)
        role_specs = {
            "经销商": (
                "经销商名称",
                {"经销商", "经销商名称", "dealer", "dealername"},
            ),
            "供应商": (
                "供应商名称",
                {"供应商", "供应商名称", "supplier", "suppliername"},
            ),
            "医院": (
                "医院名称",
                {"医院", "医院名称", "hospital", "hospitalname"},
            ),
            "厂家": (
                "厂家名称",
                {"厂家", "厂家名称", "厂商", "厂商名称", "manufacturer", "manufacturername"},
            ),
            "科室": (
                "科室名称",
                {"科室", "科室名称", "适用科室", "主科室", "maindepartment"},
            ),
            "商品": (
                "商品名称",
                {"商品", "产品", "商品名称", "产品名称", "product", "productname"},
            ),
        }

        def normalized_column(value: str) -> str:
            return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())

        def column_for_role(role: str) -> str | None:
            _, aliases = role_specs[role]
            normalized_aliases = {normalized_column(value) for value in aliases}
            candidates = [
                column for column in columns
                if normalized_column(str(column)) in normalized_aliases
            ]
            return candidates[0] if len(candidates) == 1 else None

        def values_for_role(role: str, count: int | None = None) -> list[str]:
            column = column_for_role(role)
            if column is None:
                return []
            source_rows = rows if count is None else rows[:count]
            return list(dict.fromkeys(
                str(row[column]).strip()
                for row in source_rows
                if valid_value(row.get(column))
            ))

        def bind_values(role: str, values: list[str]) -> None:
            field, aliases = role_specs[role]
            request.filters = [
                item for item in request.filters
                if normalized_column(str(item.get("field") or ""))
                not in {normalized_column(value) for value in {*aliases, field}}
            ]
            request.filters.append({
                "field": field,
                "operator": "EQ" if len(values) == 1 else "IN",
                "value": values[0] if len(values) == 1 else values,
            })
            request.assumptions.append(f"RESULT_ENTITY_REFERENCE={field}")

        digits = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
            "十": 10,
        }

        def ordinal_number(token: str) -> int | None:
            if token.isdigit():
                return int(token)
            if token in digits:
                return digits[token]
            if token.startswith("十") and len(token) == 2:
                tail = digits.get(token[1:])
                return 10 + tail if tail is not None else None
            if len(token) == 2 and token.endswith("十"):
                head = digits.get(token[:1])
                return head * 10 if head is not None else None
            if len(token) == 3 and token[1] == "十":
                head = digits.get(token[:1])
                tail = digits.get(token[2:])
                return head * 10 + tail if head and tail is not None else None
            return None

        ordinal_index: int | None = None
        ordinal_match = re.search(
            r"(?:第|排名第?|排第?)(?P<count>\d{1,3}|[一二两三四五六七八九十]{1,3})(?:名|个)?",
            compact,
        )
        if ordinal_match is not None:
            ordinal_index = ordinal_number(ordinal_match.group("count"))
        elif re.search(r"(?:第一名|排名第一|排第一|第一个)", compact):
            ordinal_index = 1

        leading_count: int | None = None
        leading_match = re.search(
            r"前(?P<count>\d{1,3}|[一二两三四五六七八九十]{1,3})名",
            compact,
        )
        if leading_match is not None:
            leading_count = ordinal_number(leading_match.group("count"))

        target_match = re.search(
            r"哪些(?P<target>厂家|医院|经销商|供应商|科室)", compact
        )
        target_role = target_match.group("target") if target_match is not None else None
        mentioned_roles = [
            role
            for role in ("经销商", "供应商", "医院", "厂家", "科室", "商品")
            if role in compact and role != target_role
        ]
        source_role = mentioned_roles[0] if len(mentioned_roles) == 1 else None
        if source_role is None:
            # Natural result references commonly omit the role: “第一名的销售额”
            # and “那第二名呢”. Infer it only when the verified result exposes a
            # single recognized identity column; never guess between two roles.
            result_roles = [
                role for role in role_specs
                if role != target_role and column_for_role(role) is not None
            ]
            if len(result_roles) == 1:
                source_role = result_roles[0]

        if (
            (ordinal_index is not None or leading_count is not None)
            and source_role is not None
        ):
            if ordering_proof is None:
                request.assumptions.append("UNVERIFIED_RESULT_ORDINAL_NOT_BOUND")
                return

            available = values_for_role(source_role)
            if ordinal_index is not None:
                values = (
                    [available[ordinal_index - 1]]
                    if 0 < ordinal_index <= len(available)
                    else []
                )
                expected_count = 1
            else:
                values = available[:leading_count]
                expected_count = leading_count or 0
            if len(values) != expected_count:
                return
            bind_values(source_role, values)
            if ordinal_index is not None:
                request.assumptions.append(
                    f"RESULT_ORDINAL_REFERENCE={ordinal_index}"
                )
            if "销售差异" in compact and not request.metrics:
                request.primary_intent = PrimaryIntent.COMPARISON_ANALYSIS
                request.comparison_type = "对象间比较"
                request.metrics = [MetricRef(input="含税销售总额")]
                if source_role not in request.dimensions:
                    request.dimensions.append(source_role)
                request.assumptions.append(
                    "SALES_DIFFERENCE_DEFAULT_METRIC=含税销售总额"
                )
            if target_match is not None:
                target = target_match.group("target")
                request.primary_intent = PrimaryIntent.DETAIL_QUERY
                request.entity = target
                request.fields = [f"{target}名称"]
                request.dimensions = [target]
                request.metrics = []
            return

        if re.search(r"(?:这些|上述|前述)科室", compact):
            department_values = values_for_role("科室")
            if department_values:
                bind_values("科室", department_values)
                if "含税销售总额" in compact and not request.metrics:
                    request.metrics = [MetricRef(input="含税销售总额")]
                return

        if not re.search(
            r"(?:它|该产品|这个产品).{0,16}(?:卖给|销售给)(?:了)?哪些医院",
            compact,
        ):
            return
        values = values_for_role("商品")
        if len(values) != 1:
            return
        bind_values("商品", values)

    async def _apply_recent_region_set_reference(
        self,
        request: CanonicalAnalysisRequest,
        chat: ChatRequest,
        identity: TrustedIdentity,
    ) -> None:
        """Resolve “这两个省” from the two most recent explicit region turns.

        The reference denotes discourse selections, not every row in the latest
        grouped result. Task frames are newest-first and retain those explicit
        selections even when each intermediate result contains only one row.
        """

        compact = re.sub(r"\s+", "", chat.question)
        if not re.search(
            r"这(?:两|2)个省份?.{0,12}(?:加起来|合计|总共|一共)", compact
        ):
            return
        recall = getattr(self.sessions, "get_recent_task_frames", None)
        if not callable(recall):
            return
        frames = await recall(
            identity.tenant_id,
            identity.user_id,
            chat.application_id,
            chat.conversation_id,
            limit=8,
        )
        selections: list[tuple[str, str]] = []
        for frame in frames:
            frame_regions = [
                item for item in frame.filters
                if isinstance(item, dict)
                and self.turn_admission_gate._semantic_field_family(
                    str(item.get("field") or "")
                ) == "region"
                and str(item.get("operator") or "EQ").upper() in {"EQ", "="}
                and isinstance(item.get("value"), (str, int, float))
            ]
            if len(frame_regions) != 1:
                continue
            field = str(frame_regions[0].get("field") or "")
            value = str(frame_regions[0].get("value") or "").strip()
            if value and all(existing[1] != value for existing in selections):
                selections.append((field, value))
            if len(selections) == 2:
                break
        if len(selections) != 2:
            return
        field = selections[0][0]
        values = [value for _, value in reversed(selections)]
        request.filters = [
            item for item in request.filters
            if self.turn_admission_gate._semantic_field_family(
                str(item.get("field") or "")
            ) != "region"
        ]
        request.filters.append({"field": field, "operator": "IN", "value": values})
        request.dimensions = [
            value for value in request.dimensions
            if self.turn_admission_gate._semantic_field_family(value) != "region"
        ]
        request.primary_intent = PrimaryIntent.METRIC_QUERY
        request.operators = [
            operator for operator in request.operators
            if operator not in {
                AnalysisOperator.GROUP_BY,
                AnalysisOperator.TOP_N,
                AnalysisOperator.BOTTOM_N,
                AnalysisOperator.SORT,
            }
        ]
        if AnalysisOperator.AGGREGATE not in request.operators:
            request.operators.append(AnalysisOperator.AGGREGATE)
        request.asl_template = None
        request.source_dataset_id = None
        request.assumptions.append("RESULT_SET_REFERENCE=RECENT_TWO_REGIONS")

    @staticmethod
    def _dataset_reference_matches_scope(
        reference: dict[str, Any], request: CanonicalAnalysisRequest
    ) -> bool:
        """Gate automatic history reuse by its semantic provenance.

        Explicit dataset_id selection is handled separately and remains useful
        for uploaded files. Automatic reuse of database-derived snapshots must
        never cross semantic-model or explicit business-domain scope. Legacy
        database references without provenance deliberately fail closed.
        """
        source_type = str(reference.get("source_type") or "")
        model_id = reference.get("semantic_model_id")
        if source_type == "UPLOADED_SPREADSHEET":
            return True
        if model_id is None or request.semantic_model_id is None:
            return False
        try:
            if int(model_id) != request.semantic_model_id:
                return False
            if request.semantic_model_version:
                provenance = next((
                    item
                    for item in reversed(reference.get("transformation_log", []))
                    if isinstance(item, dict)
                    and item.get("type") == "query_provenance"
                ), None)
                reference_version = (
                    str(provenance.get("semantic_model_version") or "").strip()
                    if provenance is not None
                    else ""
                )
                if reference_version != str(request.semantic_model_version):
                    return False
            reference_domains = sorted(
                int(item) for item in reference.get("business_domain_ids", [])
            )
        except (TypeError, ValueError):
            return False
        if request.business_domain_ids:
            return reference_domains == sorted(request.business_domain_ids)
        return True

    async def _persist_query_dataset(
        self, request: CanonicalAnalysisRequest, query_result: DataQueryResult
    ) -> str | None:
        if self.dataset_store is None:
            return None
        try:
            metric_names = [
                metric.metric_id or metric.canonical_name or metric.input
                for metric in request.metrics
            ]
            ranked = any(
                operator in request.operators
                for operator in (
                    AnalysisOperator.TOP_N,
                    AnalysisOperator.BOTTOM_N,
                    AnalysisOperator.SORT,
                )
            )
            query_fingerprint = hashlib.sha256(json.dumps({
                "semantic_model_id": request.semantic_model_id,
                "semantic_model_version": request.semantic_model_version,
                "intent": request.primary_intent.value,
                "metrics": metric_names,
                "dimensions": request.dimensions,
                "filters": request.filters,
                "ranking_limit": request.ranking_limit,
                "operators": [operator.value for operator in request.operators],
            }, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            provenance = {
                "type": "query_provenance",
                "query_fingerprint": query_fingerprint,
                "semantic_model_version": request.semantic_model_version,
                "ranked": ranked,
                "ordered_by": metric_names if ranked else [],
                "descending": AnalysisOperator.BOTTOM_N not in request.operators,
                "ranking_limit": request.ranking_limit,
            }
            reference = await asyncio.to_thread(
                self.dataset_store.save_dataset,
                scope=scope_for_request(request),
                columns=query_result.dataset.columns,
                rows=query_result.dataset.rows,
                snapshot_id=query_result.dataset.snapshot_id,
                data_as_of=query_result.dataset.data_as_of,
                source_type="DATABASE_QUERY",
                source_ref=(query_result.data_source_id or str(request.request_id)),
                semantic_model_id=request.semantic_model_id,
                business_domain_ids=request.business_domain_ids,
                metric_ids=[
                    metric.metric_id or metric.canonical_name or metric.input
                    for metric in request.metrics
                ],
                transformation_log=(provenance,),
                ttl_seconds=self.settings.dataset_ttl_seconds,
            )
            await self.sessions.put_dataset_reference(
                reference.to_dict(), recent_limit=self.settings.dataset_recent_limit
            )
            return reference.dataset_id
        except Exception as exc:
            # The query result is still usable for the current response. Degrade
            # only cross-turn reuse and never discard a valid business answer.
            logger.warning("failed to persist query dataset for follow-up: %s", exc)
            return None

    async def _import_query_result_file(
        self, request: CanonicalAnalysisRequest, result_file_url: str
    ) -> str | None:
        """Materialize a trusted SQL-export spreadsheet for table follow-ups.

        Large SQL results may contain no inline preview at all.  Importing the
        generated XLSX into the immutable conversational dataset store lets
        later requests such as “展示20条结果” read the actual table instead of
        re-running the semantic query with a presentation limit.
        """
        if self.file_importer is None:
            return None
        try:
            parsed = urlparse(result_file_url)
            path_parts = [unquote(item) for item in parsed.path.split("/") if item]
            if len(path_parts) < 2 or path_parts[0] != self.file_importer.bucket:
                logger.warning("query result file is outside the configured MinIO bucket")
                return None
            object_name = "/".join(path_parts[1:])
            reference, _ = await self.file_importer.import_object(
                object_name=object_name,
                scope=scope_for_request(request),
                source_type="DATABASE_QUERY_EXPORT",
                semantic_model_id=request.semantic_model_id,
                business_domain_ids=request.business_domain_ids,
            )
            return reference.dataset_id
        except Exception as exc:
            logger.warning("failed to import SQL result file for follow-up: %s", exc)
            return None

    async def _classify(
        self, question: str, identity: TrustedIdentity, conversation_id: str
    ) -> CanonicalAnalysisRequest:
        classified = self.classifier.classify(question, identity, conversation_id)
        return await classified if inspect.isawaitable(classified) else classified

    def _classify_with_rules(
        self, question: str, identity: TrustedIdentity, conversation_id: str
    ) -> CanonicalAnalysisRequest:
        # Hybrid production classifiers expose their deterministic baseline as
        # ``rules``. Test/custom classifiers may not; never invoke such a
        # classifier synchronously merely to obtain the preflight fast path.
        rules = getattr(self.classifier, "rules", None)
        if rules is None or not isinstance(rules, RuleBasedIntentClassifier):
            rules = RuleBasedIntentClassifier()
        classified = rules.classify(question, identity, conversation_id)
        if inspect.isawaitable(classified):
            raise RuntimeError("deterministic intent rules must be synchronous")
        return classified

    @staticmethod
    def _is_deterministic_pending_reply(
        question: str, pending: CanonicalAnalysisRequest
    ) -> bool:
        missing_slots = pending.missing_slots
        if len(missing_slots) != 1:
            return False
        slot = missing_slots[0]
        compact = re.sub(r"\s+", "", question).strip("，,。.!！?？")
        if slot == "time_range":
            return bool(
                QuestionRewriter.is_deterministic_time_update(question)
                and RuleBasedIntentClassifier._time_range(question) is not None
            )
        if slot == "metric":
            return bool(re.fullmatch(QuestionRewriter._EXPLICIT_METRIC_PATTERN, compact))
        if slot == "dimension":
            return bool(re.fullmatch(
                r"(?:按)?(?:区域|地区|门店|渠道|客户|商品|供应商|科室)(?:拆分|分析|看)?",
                compact,
            ))
        if slot == "comparison_type":
            return compact in {"同比", "环比", "目标值", "对象间比较"}
        if slot == "turn_relation":
            return compact in {
                "补充", "继续", "上一轮", "修改",
                "补充上一轮", "修改上一轮", "继续上一轮", "追问上一轮",
                "补充或修改上一轮问题", "作为独立新问题", "独立新问题",
                "新问题", "作为新问题",
            }
        if slot == "comparison_objects":
            confirmation = safe_semantic_confirmation(question)
            if confirmation is None:
                return False
            return not any(
                compact.startswith(marker)
                for marker in (
                    "取消", "停止", "算了", "换个问题", "新问题",
                    "查询", "分析", "统计", "生成", "推荐", "为什么", "怎么",
                )
            )
        if slot == "semantic_ambiguity":
            # The semantic service has already established the task intent. A
            # short noun/value supplied to its follow-up (for example
            # ``产品名称是…``) must be merged with that stored request instead of
            # being sent through the generic intent model, where an isolated
            # business name can legitimately look like CHAT. Keep this gate
            # narrow: unsafe/long instructions and sentence-shaped new tasks
            # still use normal intent classification and interruption logic.
            confirmation = safe_semantic_confirmation(question)
            if confirmation is None:
                return False
            if compact in {
                "取消", "停止", "算了", "不用了", "你好", "您好", "谢谢",
                "再见", "换个问题", "换一个问题", "新问题",
            }:
                return True
            normalized_candidates = {
                re.sub(r"\s+", "", candidate).strip("，,。.!！?？")
                for ambiguity in pending.semantic_ambiguities
                for candidate in ambiguity.candidates
                if candidate.strip()
            }
            if any(
                compact == candidate
                or compact.endswith(candidate)
                or candidate.endswith(compact)
                for candidate in normalized_candidates
            ):
                return True
            answer_markers = (
                "产品名称是", "商品名称是", "产品编码是", "商品编码是",
                "指标是", "口径是", "维度是", "对象是", "主体是",
                "地区是", "时间是", "选择", "选用", "采用", "按",
            )
            if any(marker in compact for marker in answer_markers):
                return True
            new_task_markers = (
                "查询", "分析", "统计", "生成", "推荐", "对比", "比较",
                "列出", "找出", "查看", "告诉我", "为什么", "怎么",
                "如何", "哪里", "哪些", "多少", "是否",
            )
            if any(marker in compact for marker in new_task_markers):
                return False
            # A compact noun/code with no task verb is a normal free-form answer
            # to a subject/filter ambiguity. The 100-character safety ceiling is
            # enforced by safe_semantic_confirmation above.
            return bool(compact)
        return False

    async def _finish_terminal(
        self, request: CanonicalAnalysisRequest, response: AgentResponse
    ) -> AgentResponse:
        """Clear clarification state only after execution reaches a terminal response.

        In particular, a resolved local slot can still reveal an ASL/SQL semantic
        ambiguity.  Keeping the previous pending version alive until retrieval
        completes lets that second clarification advance with the existing CAS
        version instead of trying to recreate state from version zero.
        """
        if response.analysis_plan is None:
            response.analysis_plan = self.analysis_planner.build(request)
        if not response.analysis_process:
            response.analysis_process = self._build_analysis_process(request, response)
        response.semantic_model_id = request.semantic_model_id
        response.database_id = request.database_id
        response.requested_business_domain_ids = list(request.business_domain_ids)
        response.business_domain_selection_mode = request.business_domain_selection_mode
        response.answer = self._sanitize_user_visible_answer(response.answer)
        self._attach_query_result_file(response)
        self._append_download_links(response)
        # A fresh terminal request has no pending state to clear. If this request
        # consumed a clarification state, delete only that exact version; a newer
        # concurrent message must survive.
        if request.pending_state_version is not None:
            await self.sessions.clear_pending(
                request.tenant_id,
                request.user_id,
                request.application_id,
                request.conversation_id,
                expected_version=request.pending_state_version,
            )
        return response

    @staticmethod
    def _sanitize_user_visible_answer(answer: str) -> str:
        """Replace known physical identifiers with governed business labels."""
        replacements = {
            "sales_order.created_date": "销售记录日期",
            "created_date": "销售记录日期",
            "sales_order.order_key": "订单号",
            "product.category_id": "商品分类",
            "product.product_name": "商品名称",
            "dealer.dealer_name": "经销商名称",
            "hospital.hospital_name": "医院名称",
            "manufacturer.manufacturer_name": "厂家名称",
            "sales_order.amount_with_tax": "含税销售总额",
        }
        sanitized = answer
        for physical, business in replacements.items():
            sanitized = re.sub(
                re.escape(physical), business, sanitized, flags=re.IGNORECASE
            )
        return sanitized

    @classmethod
    def _sanitize_clarification_text(cls, text: str) -> str:
        """Convert upstream diagnostics into a bounded business question."""

        raw = str(text or "").strip()
        if not raw:
            return "请确认本次查询希望采用的业务口径。"
        compact = re.sub(r"\s+", "", raw).casefold()
        if "metrics必须为空" in compact or "未指定具体聚合指标" in compact:
            return "请确认本次需要查看明细名单，还是按销售额、数量等指标汇总？"
        if re.search(
            r"(?:sql_query_|asl_|dependency_|schema_|internal|constraint|traceback)",
            compact,
            re.I,
        ):
            return "当前业务口径未能唯一匹配，请确认要查询的业务对象或筛选范围。"
        sanitized = cls._sanitize_user_visible_answer(raw)
        sanitized = re.sub(
            r"(?:根据|按照)?(?:内部)?约束[^。；;]*[。；;]?",
            "",
            sanitized,
            flags=re.I,
        ).strip()
        return sanitized or "请确认本次查询希望采用的业务口径。"

    @staticmethod
    def _append_download_links(response: AgentResponse) -> None:
        """Put real file URLs in the rendered answer as well as structured fields.

        Some platform clients render only streamed answer text and do not inspect
        the final ``complete.files`` payload.  A placeholder such as
        ``result_file_url`` is therefore not actionable.  Keep the structured
        attachment contract, and also provide a Markdown link as a compatible
        presentation fallback.
        """
        links = [
            item for item in response.files
            if item.download_url and item.download_url not in response.answer
        ]
        if not links:
            return
        rendered = []
        for index, item in enumerate(links, 1):
            label = (
                "下载完整查询结果"
                if len(links) == 1
                else f"下载附件 {index}"
            )
            rendered.append(f"[{label}（{item.format.upper()}）]({item.download_url})")
        response.answer = response.answer.rstrip() + "\n\n附件：" + "；".join(rendered)

    def _attach_query_result_file(self, response: AgentResponse) -> None:
        """Expose the SQL service's large-result URL through the UI file contract."""
        url = response.result_file_url
        if not url or any(item.download_url == url for item in response.files):
            return
        object_name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        suffix = object_name.rsplit(".", 1)[-1].lower() if "." in object_name else ""
        if suffix not in {"xlsx", "docx", "pdf"}:
            return
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        response.files.append(GeneratedFile(
            file_id=f"sql-result-{digest}",
            dataset_id=response.dataset_id,
            dataset_ids=list(response.dataset_ids),
            format=suffix,
            object_name=object_name,
            download_url=url,
            byte_size=0,
            expires_at=(
                datetime.now(timezone.utc)
                + timedelta(seconds=self.settings.report_object_ttl_seconds)
            ).isoformat(),
        ))

    @staticmethod
    def _should_replace_pending(
        pending: CanonicalAnalysisRequest,
        incoming: CanonicalAnalysisRequest,
        resolved_slots: set[str],
        *,
        raw_question: str | None = None,
    ) -> bool:
        """Tell an actual new task from a short answer to the active clarification.

        Short replies such as ``本月`` or ``销售额`` normally resolve a pending
        slot and must stay in the current task.  Explicit chat/help/safety requests,
        a strongly signalled different intent, or a complete self-contained task
        replace the stale pending task instead.
        """
        normalized_text = (raw_question or incoming.original_question).replace(" ", "")
        if any(
            marker in normalized_text
            for marker in (
                "换个问题",
                "换一个问题",
                "新问题",
                "重新查询",
                "重新问",
                "另外查询",
                "另外问",
                "不问这个",
            )
        ):
            return True
        if incoming.conversation_control in {
            ConversationControl.CANCEL,
            ConversationControl.CORRECTION,
            ConversationControl.FOLLOW_UP,
            ConversationControl.CLARIFICATION_RESPONSE,
        }:
            return False
        if incoming.primary_intent in NO_DATA_INTENTS:
            return True

        # A time clarification may contain the eligibility definition needed
        # by the original task, for example “按某日期区间内有销售记录的经销商
        # 筛选”.  Classified in isolation that sentence resembles a detail
        # query, but it is still a scoped answer when it resolved the pending
        # time slot.  Only an explicit new-task verb may replace the task here.
        if (
            "time_range" in resolved_slots
            and incoming.time_range is not None
            and not re.match(
                r"^(?:请|帮我)?(?:查询|查找|查看|列出|找出|统计|分析|"
                r"对比|比较|生成|推荐|预测)",
                normalized_text,
            )
        ):
            return False

        text = normalized_text
        explicit_signals = {
            PrimaryIntent.DETAIL_QUERY: ("明细", "名单", "列表", "逐笔", "记录"),
            PrimaryIntent.TREND_ANALYSIS: ("趋势", "走势", "历史变化"),
            PrimaryIntent.COMPARISON_ANALYSIS: ("同比", "环比", "对比", "比较", "增长率"),
            PrimaryIntent.COMPOSITION_ANALYSIS: ("占比", "构成", "份额"),
            PrimaryIntent.ANOMALY_ANALYSIS: ("异常", "突增", "突降", "不正常"),
            PrimaryIntent.ROOT_CAUSE_ANALYSIS: ("归因", "原因", "为什么"),
            PrimaryIntent.FORECAST_ANALYSIS: ("预测", "预估", "预计", "未来"),
            PrimaryIntent.REPORT_GENERATION: (
                "报表", "报告", "月报", "周报", "导出", "生成文件", "下载",
                "excel", "xlsx", "pdf", "word", "docx",
            ),
            PrimaryIntent.METRIC_DEFINITION: ("口径", "定义", "怎么算", "公式"),
            PrimaryIntent.DATA_LINEAGE: ("血缘", "来源表", "来自哪", "哪个字段"),
            PrimaryIntent.DATA_QUALITY: ("数据质量", "对账", "没更新", "延迟"),
        }
        has_explicit_intent = any(
            signal in text for signal in explicit_signals.get(incoming.primary_intent, ())
        )
        if incoming.primary_intent != pending.primary_intent and has_explicit_intent:
            return True

        complete_self_contained = not incoming.missing_slots and bool(
            incoming.metrics
            or incoming.entity
            or incoming.time_range
            or incoming.primary_intent in METADATA_INTENTS
        )
        explicit_new_task = bool(re.match(
            r"^(?:请|麻烦|帮我|给我|请帮我)?"
            r"(?:查询|查找|查看|列出|找出|统计|分析|对比|比较|生成|推荐|预测)",
            normalized_text,
        ))
        # A complete sentence beginning with a task verb is a new request even
        # when a value such as “最近一年” incidentally fills an old missing slot.
        return complete_self_contained and (not resolved_slots or explicit_new_task)

    @classmethod
    def _preserve_pending_execution_contract(
        cls,
        pending: CanonicalAnalysisRequest,
        merged: CanonicalAnalysisRequest,
        *,
        clarification_answer: str = "",
        incoming: CanonicalAnalysisRequest | None = None,
    ) -> CanonicalAnalysisRequest:
        """Treat a clarification as a slot patch, not a fresh execution plan.

        The pending request is already the user-confirmed task.  A normal
        clarification can fill its declared missing slots, while only an
        explicit correction may rewrite the intent, operators, ranking metric
        order or other confirmed slots.
        """
        if merged.conversation_control in {
            ConversationControl.CANCEL,
            ConversationControl.CORRECTION,
        }:
            return merged

        parsed_patch = merged.model_copy(deep=True)
        missing = set(pending.missing_slots)
        if "turn_relation" in missing:
            compact_answer = re.sub(r"\s+", "", clarification_answer)
            followup_selected = any(
                marker in compact_answer
                for marker in ("上一轮", "继续", "补充", "修改", "追问")
            ) and not any(
                marker in compact_answer for marker in ("独立", "新问题")
            )
            relation_request = pending.model_copy(deep=True)
            relation_request.missing_slots = [
                slot for slot in relation_request.missing_slots
                if slot != "turn_relation"
            ]
            relation_request.semantic_ambiguities = [
                item for item in relation_request.semantic_ambiguities
                if item.type != "turn_relation"
            ]
            relation_request.ambiguities = [
                item.question for item in relation_request.semantic_ambiguities
            ]
            if followup_selected:
                before = (
                    relation_request.turn_admission.context_before
                    if relation_request.turn_admission is not None else {}
                )
                if not relation_request.metrics:
                    relation_request.metrics = [
                        MetricRef(input=str(value))
                        for value in before.get("metrics") or []
                        if str(value).strip()
                    ]
                relation_request.entity = relation_request.entity or before.get("entity")
                if not relation_request.semantic_entity_mentions:
                    relation_request.semantic_entity_mentions = list(
                        before.get("semantic_entity_mentions") or []
                    )
                if not relation_request.fields:
                    relation_request.fields = list(before.get("fields") or [])
                if not relation_request.dimensions:
                    relation_request.dimensions = list(before.get("dimensions") or [])
                if not relation_request.filters:
                    relation_request.filters = [
                        dict(item) for item in before.get("filters") or []
                        if isinstance(item, dict)
                    ]
                if relation_request.time_range is None and before.get("time_range"):
                    relation_request.time_range = TimeRange.model_validate(
                        before["time_range"]
                    )
                relation_request.comparison_type = (
                    relation_request.comparison_type or before.get("comparison")
                )
                relation_request.ranking_limit = (
                    relation_request.ranking_limit or before.get("top_n")
                )
                raw_query = (
                    relation_request.turn_admission.current_turn_facts.raw_query
                    if relation_request.turn_admission is not None
                    else relation_request.original_question
                )
                relation_types = applicable_department_relation_types(raw_query)
                if relation_types:
                    relation_request.primary_intent = PrimaryIntent.DETAIL_QUERY
                    relation_request.metrics = []
                    relation_request.entity = relation_request.entity or "产品"
                    relation_request.fields = list(dict.fromkeys([
                        *relation_request.fields,
                        "商品名称",
                        "适用科室",
                    ]))
                    relation_request.filters = [
                        item for item in relation_request.filters
                        if applicable_department_filter_slot(item.get("field"))
                        != "applicable_department_relation_type"
                    ]
                    relation_request.filters.append({
                        "field": "适用科室类型",
                        "operator": "EQ" if len(relation_types) == 1 else "IN",
                        "value": (
                            relation_types[0]
                            if len(relation_types) == 1 else relation_types
                        ),
                    })
                    relation_request.time_range = None
                    relation_request.temporal_anchor = None
                    relation_request.asl_template = None
                    relation_request.source_dataset_id = None
                    relation_request.assumptions = [
                        value for value in relation_request.assumptions
                        if value != "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR"
                        and not value.startswith(
                            "APPLICABLE_DEPARTMENT_RELATION_TYPE="
                        )
                    ]
                    relation_request.assumptions.extend((
                        "TIME_SCOPE=ALL_TIME",
                        "APPLICABLE_DEPARTMENT_RELATION_TYPE="
                        + (
                            "PRIMARY"
                            if relation_types == [1]
                            else "SECONDARY"
                            if relation_types == [2]
                            else "BOTH"
                        ),
                        "APPLICABLE_DEPARTMENT_RELATION_REPLAN_REQUIRED",
                    ))
                relation_request.turn_relation = TurnRelation.CURRENT_TOPIC_FOLLOWUP
                relation_request.context_mode = ContextMode.CURRENT_THREAD
                relation_request.assumptions.append(
                    "TURN_RELATION_CLARIFIED_AS_CURRENT_TOPIC"
                )
            else:
                relation_request.turn_relation = TurnRelation.STANDALONE_NEW_TOPIC
                relation_request.context_mode = ContextMode.NONE
                relation_request.asl_template = None
                relation_request.source_dataset_id = None
                relation_request.assumptions.append(
                    "TURN_RELATION_CLARIFIED_AS_STANDALONE"
                )
            if relation_request.turn_admission is not None:
                relation_request.turn_admission.relation = relation_request.turn_relation
                relation_request.turn_admission.context_mode = relation_request.context_mode
                relation_request.turn_admission.needs_clarification = False
            relation_request.rewritten_question = render_execution_question(
                relation_request
            )
            return relation_request
        semantic_clarification = "semantic_ambiguity" in missing

        merged.primary_intent = pending.primary_intent
        merged.secondary_intents = list(pending.secondary_intents)
        merged.intent_source = pending.intent_source
        merged.intent_confidence = pending.intent_confidence
        merged.intent_candidates = list(pending.intent_candidates)
        merged.risk_level = pending.risk_level
        merged.operators = list(pending.operators)
        merged.ranking_limit = pending.ranking_limit
        if (
            merged.ranking_limit is not None
            and AnalysisOperator.SORT in merged.operators
            and AnalysisOperator.TOP_N not in merged.operators
            and AnalysisOperator.BOTTOM_N not in merged.operators
        ):
            # A numeric ranking contract remains a top-N request even when a
            # lexical clarification parse only retained generic SORT/TABLE
            # operators.  Losing TOP_N here can turn a bounded ranking into an
            # unbounded result after the user merely supplies a missing slot.
            merged.operators.append(AnalysisOperator.TOP_N)

        if "metric" not in missing and not semantic_clarification:
            merged.metrics = [item.model_copy(deep=True) for item in pending.metrics]
        if "entity" not in missing and not semantic_clarification:
            merged.entity = pending.entity
        if "fields" not in missing and not semantic_clarification:
            merged.fields = list(pending.fields)
        if "dimension" not in missing and not semantic_clarification:
            merged.dimensions = list(pending.dimensions)
        if "comparison_type" not in missing and not semantic_clarification:
            merged.comparison_type = pending.comparison_type
        if not semantic_clarification and "comparison_objects" not in missing:
            merged.filters = [dict(item) for item in pending.filters]
        if "forecast_horizon" not in missing:
            merged.forecast_horizon_periods = pending.forecast_horizon_periods
            merged.forecast_granularity = pending.forecast_granularity
        if "forecast_history_range" not in missing:
            merged.forecast_history_provided = pending.forecast_history_provided

        if not semantic_clarification:
            cls._merge_explicit_collection_deltas(
                pending,
                merged,
                parsed_patch,
                incoming,
                clarification_answer,
                missing,
            )

        if (
            merged.time_range is not None
            and cls._has_explicit_sales_fact_event(clarification_answer)
        ):
            merged.assumptions.append(_SALES_RECORD_TIME_ASSUMPTION)

        merged.assumptions = list(dict.fromkeys([
            *pending.assumptions,
            *merged.assumptions,
        ]))
        if not semantic_clarification:
            merged.rewritten_question = render_execution_question(merged)
        return merged

    @staticmethod
    def _has_explicit_sales_fact_event(text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        return bool(re.search(
            r"(?:有|存在|发生|产生|基于|按照|按).{0,8}"
            r"(?:销售记录|销售订单|交易记录|订单记录|成交记录)",
            compact,
        ))

    @classmethod
    def _merge_explicit_collection_deltas(
        cls,
        pending: CanonicalAnalysisRequest,
        target: CanonicalAnalysisRequest,
        parsed_patch: CanonicalAnalysisRequest,
        incoming: CanonicalAnalysisRequest | None,
        answer: str,
        missing: set[str],
    ) -> None:
        """Merge only user-signalled additions to already confirmed set slots."""
        sources = [parsed_patch]
        if incoming is not None:
            sources.append(incoming)

        if "fields" not in missing and cls._explicit_field_projection(answer):
            projected_fields = [
                field
                for source in sources
                for field in source.fields
            ]
            projected_fields.extend(cls._literal_projected_fields(answer))
            if projected_fields:
                target.fields = list(dict.fromkeys(projected_fields))
        elif "fields" not in missing and cls._explicit_field_addition(answer):
            added_fields = [
                field
                for source in sources
                for field in source.fields
                if field not in pending.fields
            ]
            added_fields.extend(cls._literal_added_fields(answer))
            target.fields = list(dict.fromkeys([*pending.fields, *added_fields]))

        if "dimension" not in missing and cls._explicit_dimension_addition(answer):
            added_dimensions = [
                dimension
                for source in sources
                for dimension in source.dimensions
                if dimension not in pending.dimensions
            ]
            target.dimensions = list(dict.fromkeys([
                *pending.dimensions,
                *added_dimensions,
            ]))

        if (
            "comparison_objects" not in missing
            and cls._explicit_filter_addition(answer)
        ):
            candidates = [
                dict(item)
                for source in sources
                for item in source.filters
                if isinstance(item, dict) and item not in pending.filters
            ]
            literal_exclusion = cls._literal_exclusion_filter(pending, answer)
            if literal_exclusion is not None:
                candidates.append(literal_exclusion)
            target.filters = [dict(item) for item in pending.filters]
            fingerprints = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                for item in target.filters
            }
            for item in candidates:
                if not item.get("field") or "value" not in item:
                    continue
                fingerprint = json.dumps(
                    item, ensure_ascii=False, sort_keys=True, default=str
                )
                if fingerprint not in fingerprints:
                    target.filters.append(item)
                    fingerprints.add(fingerprint)

    @staticmethod
    def _explicit_field_projection(answer: str) -> bool:
        compact = re.sub(r"\s+", "", answer)
        return bool(re.search(
            r"(?:只|仅)(?:保留|显示|返回|要|看)(?:字段|列)?",
            compact,
        ))

    @staticmethod
    def _literal_projected_fields(answer: str) -> list[str]:
        """Extract common business columns from an explicit projection turn.

        The structured parser may only retain columns already present in the
        previous result.  Reading the literal turn here ensures that requests
        such as ``只保留医院名称和医院等级`` can enrich and re-query the
        previous table instead of silently dropping the new column.
        """
        compact = re.sub(r"\s+", "", answer)
        known_fields = (
            "医院名称", "医院等级", "经销商名称", "供应商名称",
            "商品名称", "产品名称", "品牌名称", "客户名称",
            "订单号", "交易日期", "订单日期", "金额", "含税销售总额",
            "地址", "城市", "省份", "状态", "门店名称",
        )
        return [field for field in known_fields if field in compact]

    @staticmethod
    def _explicit_field_addition(answer: str) -> bool:
        compact = re.sub(r"\s+", "", answer)
        return bool(re.search(
            r"(?:再|还|并|同时|另外)(?:加上?|添加|附带|显示|返回)"
            r"(?:字段|列)?",
            compact,
        ))

    @staticmethod
    def _literal_added_fields(answer: str) -> list[str]:
        compact = re.sub(r"\s+", "", answer)
        match = re.search(
            r"(?:再|还|并|同时|另外)(?:加上?|添加|附带|显示|返回)"
            r"(?:字段|列)([^，,。；;]{1,100})",
            compact,
        )
        if match is None:
            return []
        return [
            value
            for value in (
                item.strip("的")
                for item in re.split(r"、|,|，|和|与|及", match.group(1))
            )
            if value and len(value) <= 50
        ][:10]

    @staticmethod
    def _explicit_dimension_addition(answer: str) -> bool:
        compact = re.sub(r"\s+", "", answer)
        return bool(re.search(
            r"(?:再|还|并|同时|另外)(?:按|按照).{1,50}"
            r"(?:拆分|分组|统计|分析|查看)",
            compact,
        ))

    @staticmethod
    def _explicit_filter_addition(answer: str) -> bool:
        compact = re.sub(r"\s+", "", answer)
        return bool(re.search(
            r"(?:并|同时|还|再|另外)?"
            r"(?:排除|剔除|不含|不包含|仅保留|只保留|增加筛选|添加筛选)",
            compact,
        ))

    @staticmethod
    def _literal_exclusion_filter(
        pending: CanonicalAnalysisRequest, answer: str
    ) -> dict[str, Any] | None:
        compact = re.sub(r"\s+", "", answer)
        match = re.search(
            r"(?:排除|剔除|不含|不包含)([^，,。；;]{1,80})",
            compact,
        )
        if match is None:
            return None
        value = match.group(1).strip("的")
        if not value or any(ord(char) < 32 for char in value):
            return None
        entity_fields = {
            "经销商": "经销商名称",
            "供应商": "供应商名称",
            "厂家": "厂家名称",
            "制造商": "制造商名称",
            "客户": "客户名称",
            "门店": "门店名称",
            "医院": "医院名称",
            "公司": "公司名称",
        }
        scopes = list(dict.fromkeys([
            *pending.dimensions,
            *([pending.entity] if pending.entity else []),
        ]))
        matched_scopes = [scope for scope in scopes if scope in entity_fields]
        if len(matched_scopes) != 1:
            return None
        return {
            "field": entity_fields[matched_scopes[0]],
            "operator": "NE",
            "value": value,
        }

    async def _apply_confirmed_memories(self, request: CanonicalAnalysisRequest) -> None:
        """Load confirmed preferences after short-context rewriting and before slot checks."""
        scope = MemoryScope(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            application_id=request.application_id,
        )
        try:
            memories = await self.memories.list_active(
                scope, limit=self.settings.long_term_memory_max_items
            )
        except Exception:
            # Preference recall is enrichment. An outage must not destroy an otherwise explicit,
            # safe query, but it is recorded so confidence can be downgraded transparently.
            request.assumptions.append("LONG_TERM_MEMORY_UNAVAILABLE")
            return
        applicable = [
            (item, rendered)
            for item in memories
            if self._confirmed_memory_is_applicable(request, item)
            and (rendered := self._render_confirmed_memory(item)) is not None
        ]
        self._apply_confirmed_memory_slots(request, [item for item, _ in applicable])
        if not applicable:
            return
        request.confirmed_memory_ids = [item.memory_id for item, _ in applicable]
        request.confirmed_preferences = [rendered for _, rendered in applicable]
        base = request.rewritten_question or request.original_question
        request.rewritten_question = (
            f"{base}\n已确认的用户默认偏好（只在本次问题没有明确指定时使用，"
            f"本次明确表达优先）：{json.dumps(request.confirmed_preferences, ensure_ascii=False)}"
        )

    def _apply_confirmed_memory_slots(
        self, request: CanonicalAnalysisRequest, memories: list[LongTermMemory]
    ) -> None:
        """Apply only whitelisted defaults and never override an explicit current value."""
        for memory in memories:
            key, value = memory.memory_key, memory.value
            if key == "default_metric" and not request.metrics:
                metric = value.get("metric")
                if isinstance(metric, str) and 0 < len(metric) <= 100:
                    request.metrics = [MetricRef(input=metric)]
            elif key == "default_dimension" and not request.dimensions:
                dimensions = value.get("dimensions", value.get("dimension"))
                if isinstance(dimensions, str):
                    dimensions = [dimensions]
                if isinstance(dimensions, list) and all(
                    isinstance(item, str) and 0 < len(item) <= 100 for item in dimensions
                ):
                    request.dimensions = dimensions[:10]
            elif key == "default_comparison" and not request.comparison_type:
                comparison = value.get("comparison_type")
                if comparison in {"同比", "环比", "目标值", "对象间比较"}:
                    request.comparison_type = comparison
            elif key in {"default_time_range", "default_time_period"} and (
                not request.time_range
                or "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in request.assumptions
            ):
                period = value.get("time_period")
                rules = getattr(self.classifier, "rules", self.classifier)
                parser = getattr(rules, "_time_range", None)
                if isinstance(period, str) and callable(parser):
                    request.time_range = parser(period)
                    request.assumptions = [
                        item for item in request.assumptions
                        if item != "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR"
                    ]
            elif key == "default_entity" and not request.entity:
                entity = value.get("entity")
                if isinstance(entity, str) and 0 < len(entity) <= 100:
                    request.entity = entity
            elif key == "default_fields" and not request.fields:
                fields = value.get("fields")
                if isinstance(fields, list) and all(
                    isinstance(item, str) and 0 < len(item) <= 100 for item in fields
                ):
                    request.fields = fields[:20]
        rules = getattr(self.classifier, "rules", self.classifier)
        required_missing_slots = getattr(rules, "required_missing_slots", None)
        if callable(required_missing_slots):
            request.missing_slots = required_missing_slots(request)

    @staticmethod
    def _confirmed_memory_is_applicable(
        request: CanonicalAnalysisRequest, memory: LongTermMemory
    ) -> bool:
        """Retrieve only memories that can affect this task without overriding it."""
        key = memory.memory_key
        if key == "default_metric":
            return not request.metrics
        if key in {"default_time_range", "default_time_period"}:
            return not request.time_range or "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in request.assumptions
        if key == "default_dimension":
            return not request.dimensions
        if key == "default_comparison":
            return not request.comparison_type
        if key == "default_entity":
            return not request.entity
        if key == "default_fields":
            return not request.fields
        if key == "metric_alias":
            alias = memory.value.get("alias")
            return isinstance(alias, str) and alias in request.original_question
        # Display/output preferences are safe task-wide defaults. Business
        # context and filters remain prompt-only but are not injected when no
        # bounded, structured value can be rendered.
        return memory.memory_type == MemoryType.DISPLAY_PREFERENCE or key in {
            "default_filter", "business_context", "default_time_grain",
        }

    @staticmethod
    def _render_confirmed_memory(memory: LongTermMemory) -> str | None:
        """Render bounded structured values; never inject the free-form summary into prompts."""
        allowed_keys = {
            "metric", "alias", "canonical_metric", "time_grain", "time_period",
            "dimension", "dimensions", "comparison_type", "unit", "currency",
            "output_format", "filter_field", "filter_operator", "filter_value",
            "term", "meaning", "preference", "entity", "fields",
        }
        safe_value: dict[str, Any] = {}
        for key, value in memory.value.items():
            if key not in allowed_keys:
                continue
            if isinstance(value, (str, int, float, bool)):
                safe_value[key] = value[:200] if isinstance(value, str) else value
            elif isinstance(value, list) and len(value) <= 10 and all(
                isinstance(item, (str, int, float, bool)) for item in value
            ):
                safe_value[key] = [
                    item[:100] if isinstance(item, str) else item for item in value
                ]
        if not safe_value:
            return None
        encoded = json.dumps(safe_value, ensure_ascii=False, separators=(",", ":"))
        return f"{memory.memory_type.value}/{memory.memory_key or 'unnamed'}={encoded}"

    @staticmethod
    def _latest_previous_user_question(chat: ChatRequest) -> str | None:
        return next(
            (
                turn.content
                for turn in reversed(chat.history)
                if turn.role == "user" and turn.content.strip() != chat.question.strip()
            ),
            None,
        )

    @staticmethod
    def _is_contextual_analysis_follow_up(request: CanonicalAnalysisRequest) -> bool:
        if request.primary_intent not in ANALYSIS_INTENTS | {
            PrimaryIntent.METRIC_QUERY, PrimaryIntent.DETAIL_QUERY,
        }:
            return False
        text = request.original_question.replace(" ", "")
        context_signals = (
            "这个", "这些", "该数据", "上述", "刚才", "数据下降", "数据上涨",
            "下降原因", "上涨原因", "为什么下降", "为什么上涨", "接着分析",
            "导出", "生成文件", "下载", "excel", "xlsx", "pdf", "word", "docx",
        )
        modification_follow_up = bool(re.search(
            r"^(?:那|再|也|同时|还是|仍然|继续|回到|恢复|撤销|取消|去掉|清除|"
            r"不要|不看|不按|不是|改成|改为|改查|换成|只看|只留|只保留|只显示|"
            r"显示|展示|返回|查看|从低到高|从高到低|按)[^。！？]{0,60}"
            r"|(?:^|[，,])(?:再)?按[^，,。]{0,30}(?:拆分|分组|排序|展示|显示|查看|统计|给我|看|趋势)?$"
            r"|(?:展示|显示|只看|只留|保留)(?:前|后)?\d+(?:名|个|条|天)?"
            r"|^(?:前|后)\d+(?:名|个|条|天)?$"
            r"|^(?:哪个|哪一个|第一个|最高|最低|差多少|为什么|怎么|这些|它|这个)",
            text,
        ))
        return any(signal in text for signal in context_signals) or modification_follow_up

    @staticmethod
    def _history_recovery_candidate(chat: ChatRequest) -> tuple[str, str] | None:
        """Return the prior user question and assistant clarification, if safely detectable.

        History is a cold-start fallback supplied by the platform after Redis expires.  We only
        merge when an assistant clarification follows a prior user question; arbitrary history is
        never concatenated into a new request.
        """
        clarification_markers = (
            "还需要补充", "请补充", "需要确认", "请确认", "哪个指标", "时间范围",
            "哪些字段", "哪个维度", "同比", "环比",
        )
        prior_user: str | None = None
        for index in range(len(chat.history) - 1, -1, -1):
            turn = chat.history[index]
            if turn.role != "assistant" or not any(
                marker in turn.content for marker in clarification_markers
            ):
                continue
            for earlier in range(index - 1, -1, -1):
                candidate = chat.history[earlier]
                if candidate.role == "user" and candidate.content.strip() != chat.question.strip():
                    prior_user = candidate.content
                    break
            if prior_user:
                return prior_user, turn.content
        return None

    async def _request_clarification(self, request: CanonicalAnalysisRequest, rounds: int) -> AgentResponse:
        if rounds > self.settings.max_clarification_rounds:
            logger.warning(
                "clarification limit exceeded: request_id=%s intent=%s missing_slots=%s rounds=%s",
                request.request_id,
                request.primary_intent.value,
                request.missing_slots,
                rounds,
            )
            await self.sessions.clear_pending(
                request.tenant_id,
                request.user_id,
                request.application_id,
                request.conversation_id,
                expected_version=request.pending_state_version,
            )
            return self._fallback(request, "关键信息多轮补充后仍不完整，请重新描述分析目标。")
        all_questions = self._clarification_questions(request)
        question_limit = (
            1
            if any(
                slot in request.missing_slots
                for slot in ("turn_relation", "semantic_ambiguity")
            )
            else 5
        )
        questions = all_questions[:question_limit]
        clarification_items = self._clarification_items(request)[:question_limit]
        remaining_questions = all_questions[question_limit:]
        try:
            await self.sessions.put_pending(
                PendingState(
                    request=request,
                    clarification_rounds=rounds,
                    state_version=rounds,
                    remaining_questions=remaining_questions,
                ),
                expected_version=rounds - 1,
            )
        except SessionConflictError:
            return self._fallback(request, "会话状态已被另一条消息更新，请基于最新追问重新回答。")
        understood_slots = self._understood_slots(request)
        understood_text = self._understood_text(understood_slots)
        requirements: list[AnalysisRequirement] = []
        if "forecast_horizon" in request.missing_slots:
            requirements.append(AnalysisRequirement(
                code="forecast_horizon",
                category="USER_INPUT",
                description="缺少预测期数或预测时间粒度。",
                action="请说明未来多少期，以及按天、周、月、季度还是年预测。",
            ))
        if "forecast_history_range" in request.missing_slots:
            requirements.append(AnalysisRequirement(
                code="forecast_history_range",
                category="USER_INPUT",
                description="缺少用于训练和回测的历史数据范围。",
                action="请提供历史起止范围或窗口，例如：基于过去12个月。",
            ))
        prefix = f"我已理解：{understood_text}。" if understood_text else ""
        response = AgentResponse(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            status="NEEDS_CLARIFICATION",
            intent=request.primary_intent,
            intent_source=request.intent_source,
            intent_confidence=request.intent_confidence,
            answer=prefix + "还需要补充：" + "；".join(questions),
            clarification_questions=questions,
            clarification_items=clarification_items,
            remaining_question_count=len(remaining_questions),
            missing_slots=request.missing_slots,
            understood_slots=understood_slots,
            clarification_round=rounds,
            requirements=requirements,
        )
        response.analysis_plan = self.analysis_planner.build(request)
        response.analysis_process = self._build_analysis_process(request, response)
        return response

    @staticmethod
    def _intent_label(intent: PrimaryIntent) -> str:
        return {
            PrimaryIntent.CHAT: "闲聊",
            PrimaryIntent.METRIC_QUERY: "指标查询",
            PrimaryIntent.DETAIL_QUERY: "明细查询",
            PrimaryIntent.TREND_ANALYSIS: "趋势分析",
            PrimaryIntent.COMPARISON_ANALYSIS: "对比分析",
            PrimaryIntent.COMPOSITION_ANALYSIS: "占比分析",
            PrimaryIntent.ANOMALY_ANALYSIS: "异常分析",
            PrimaryIntent.ROOT_CAUSE_ANALYSIS: "归因分析",
            PrimaryIntent.FORECAST_ANALYSIS: "预测分析",
            PrimaryIntent.REPORT_GENERATION: "报表生成",
            PrimaryIntent.METRIC_DEFINITION: "解释口径",
            PrimaryIntent.DATA_LINEAGE: "数据血缘",
            PrimaryIntent.DATA_QUALITY: "数据质量",
            PrimaryIntent.CAPABILITY_HELP: "能力说明",
            PrimaryIntent.OUT_OF_SCOPE: "非数据任务",
        }.get(intent, intent.value)

    @staticmethod
    def _turn_relation_label(relation: TurnRelation | None) -> str:
        if relation is None:
            return "未判定"
        return {
            TurnRelation.STANDALONE_NEW_TOPIC: "独立新问题",
            TurnRelation.CURRENT_TOPIC_FOLLOWUP: "当前主题追问",
            TurnRelation.CURRENT_TOPIC_MODIFICATION: "当前主题条件修改",
            TurnRelation.CURRENT_TOPIC_DRILLDOWN: "当前主题下钻",
            TurnRelation.HISTORICAL_TOPIC_RETURN: "返回历史主题",
            TurnRelation.CLARIFICATION_RESPONSE: "澄清回复",
            TurnRelation.CORRECTION: "纠正上一请求",
            TurnRelation.AMBIGUOUS_RELATION: "轮次关系待确认",
        }.get(relation, relation.value)

    @classmethod
    def _intent_think_summary(
        cls,
        request: CanonicalAnalysisRequest,
        *,
        file_status: str = "NOT_PROVIDED",
        file_based: bool = False,
    ) -> str:
        view = build_intent_recognition_display_v2(
            request,
            file_status=file_status,
            file_based=file_based,
        )
        return render_intent_recognition_display_v2(view)

    @staticmethod
    def _file_inspection_think_summary(inspection: dict[str, Any]) -> str:
        status = str(inspection.get("status") or "NOT_PROVIDED")
        if status == "READ_SUCCESS":
            columns = [str(item) for item in inspection.get("columns") or []]
            column_text = "、".join(columns) or "未识别到字段"
            if inspection.get("columns_truncated"):
                column_text += "等"
            sheets = [str(item) for item in inspection.get("sheets") or []]
            sheet_text = "、".join(sheets) or "默认工作表"
            return (
                "### ◉ 问题补全与意图识别\n"
                f"检测到用户上传文件：{inspection.get('file_name') or '未命名文件'}；"
                f"格式={inspection.get('format') or '未知'}。\n"
                + (
                    "文件已成功获取并完成安全解析，内容已注册为本轮可追问数据集。"
                    if inspection.get("file_based")
                    else "文件已成功获取并完成安全解析；当前问题不使用该文件，继续普通业务数据链路。"
                )
                + f"共 {int(inspection.get('sheet_count') or 0)} 个工作表、"
                f"{int(inspection.get('row_count') or 0)} 行数据、"
                f"{int(inspection.get('column_count') or 0)} 个字段。\n"
                f"工作表：{sheet_text}；字段预览：{column_text}。"
            )
        if status == "DATASET_BOUND":
            usage_text = (
                "后续分析将继续校验数据集权限、有效期、完整性和字段可用性。"
                if inspection.get("file_based")
                else "当前问题不使用该数据集，后续按普通业务数据链路处理。"
            )
            return (
                "### ◉ 问题补全与意图识别\n"
                "本轮未重复读取临时文件，已检测到调用方绑定的数据集。"
                f"{usage_text}"
            )
        return (
            "### ◉ 问题补全与意图识别\n"
            "本轮未检测到用户上传的 CSV/XLSX 文件，将使用已授权的业务数据源、"
            "会话数据集或知识范围继续处理。"
        )

    @classmethod
    def _insight_dimension_summary(
        cls, request: CanonicalAnalysisRequest, facts: dict[str, Any]
    ) -> str:
        focus = {
            PrimaryIntent.TREND_ANALYSIS: "趋势方向、变化幅度、峰谷与转折点",
            PrimaryIntent.COMPARISON_ANALYSIS: "对象差异、排序与差值",
            PrimaryIntent.COMPOSITION_ANALYSIS: "结构占比、集中度与主要贡献项",
            PrimaryIntent.ANOMALY_ANALYSIS: "异常点、偏离程度与影响范围",
            PrimaryIntent.ROOT_CAUSE_ANALYSIS: "分解维度、贡献项与候选原因",
            PrimaryIntent.FORECAST_ANALYSIS: "历史充分性、预测区间与不确定性",
            PrimaryIntent.REPORT_GENERATION: "核心指标、趋势、结构和风险提示",
        }.get(request.primary_intent, "指标结果、排序、分组及数据完整性")
        fact_keys = [str(key) for key in list(facts)[:10]]
        return f"{focus}；已生成事实项={fact_keys or ['无额外事实项']}。"

    @staticmethod
    def _build_analysis_process(
        request: CanonicalAnalysisRequest, response: AgentResponse
    ) -> list[AnalysisProcessStep]:
        """Build a bounded evidence-backed trace suitable for end-user display.

        This deliberately reports observable pipeline actions only. It never
        exposes prompts, hidden chain-of-thought, SQL text, raw result rows, or
        credentials.
        """
        rewrite_summary = "未使用历史上下文或实体别名改写。"
        rewrite_status = "COMPLETED"
        if request.rewrite_degraded:
            rewrite_summary = "实体规范化服务不可用或缺少作用域，已安全保留原问题继续处理。"
            rewrite_status = "DEGRADED"
        elif request.rewrite_events or request.rewrite_context_applied:
            rewrite_summary = (
                f"已执行分类前改写：上下文补全={'是' if request.rewrite_context_applied else '否'}；"
                f"实体/别名规范化 {len(request.rewrite_events)} 项。"
            )
        steps = [
            AnalysisProcessStep(
                stage="QUESTION_REWRITE",
                status=rewrite_status,
                title="规范化问题",
                summary=rewrite_summary,
            ),
            AnalysisProcessStep(
                stage="UNDERSTANDING",
                status="COMPLETED",
                title="理解问题",
                summary=(
                    f"已识别用户意图为 {request.primary_intent.value}；"
                    f"识别来源为 {request.intent_source}，置信度 {request.intent_confidence:.2f}。"
                ),
            )
        ]
        if response.analysis_plan is not None:
            steps.append(AnalysisProcessStep(
                stage="ANALYSIS_PLANNING",
                status="COMPLETED",
                title="制定分析计划",
                summary=(
                    f"已确定 {len(response.analysis_plan.methods)} 个分析方法、"
                    f"{len(response.analysis_plan.sufficiency_rules)} 项数据充分性门槛。"
                ),
            ))
        if response.status == "NEEDS_CLARIFICATION":
            missing = "、".join(response.missing_slots) or "关键信息"
            steps.append(
                AnalysisProcessStep(
                    stage="COMPLETENESS_CHECK",
                    status="NEEDS_INPUT",
                    title="检查信息完整性",
                    summary=f"还缺少：{missing}；补充后将继续原任务。",
                )
            )
            return steps

        steps.append(
            AnalysisProcessStep(
                stage="COMPLETENESS_CHECK",
                status="COMPLETED",
                title="检查信息完整性",
                summary="执行当前任务所需的关键信息已通过完整性检查。",
            )
        )
        by_kind: dict[str, list[EvidenceItem]] = {}
        for item in response.evidence:
            by_kind.setdefault(item.kind, []).append(item)

        query_items = by_kind.get("QUERY_RESULT", [])
        if query_items:
            item = query_items[0]
            row_count = item.payload.get("row_count", "未知")
            quality = item.payload.get("quality_status", "UNVERIFIED")
            steps.append(
                AnalysisProcessStep(
                    stage="DATA_QUERY",
                    status="COMPLETED",
                    title="获取数据",
                    summary=f"已取得 {row_count} 行结果；数据质量状态为 {quality}。",
                    evidence_ids=[value.evidence_id for value in query_items],
                )
            )

        metric_items = by_kind.get("SEMANTIC_METRIC_RESOLUTION", [])
        if metric_items:
            verified = sum(item.payload.get("verified") is True for item in metric_items)
            steps.append(
                AnalysisProcessStep(
                    stage="METRIC_VERIFICATION",
                    status="COMPLETED" if verified == len(metric_items) else "DEGRADED",
                    title="核验指标口径",
                    summary=f"已核验 {len(metric_items)} 个指标，其中 {verified} 个通过 Oagnet/语义层校验。",
                    evidence_ids=[item.evidence_id for item in metric_items],
                )
            )

        knowledge_items = by_kind.get("ANALYSIS_KNOWLEDGE", [])
        if knowledge_items:
            hits = sum(int(item.payload.get("hit_count", 0)) for item in knowledge_items)
            steps.append(
                AnalysisProcessStep(
                    stage="KNOWLEDGE_RETRIEVAL",
                    status="COMPLETED",
                    title="检索业务知识",
                    summary=f"检索到 {hits} 条与分析相关的业务知识，用于约束解释范围。",
                    evidence_ids=[item.evidence_id for item in knowledge_items],
                )
            )

        analysis_items = by_kind.get("ANALYSIS_RESULT", [])
        if analysis_items:
            method = analysis_items[0].payload.get("method", "deterministic")
            warning_count = sum(
                len(item.payload.get("warnings", [])) for item in analysis_items
            )
            steps.append(
                AnalysisProcessStep(
                    stage="DETERMINISTIC_ANALYSIS",
                    status="DEGRADED" if warning_count else "COMPLETED",
                    title="执行确定性分析",
                    summary=f"已使用 {method} 方法计算可验证结论；限制提示 {warning_count} 条。",
                    evidence_ids=[item.evidence_id for item in analysis_items],
                )
            )

        synthesis_items = by_kind.get("ANSWER_SYNTHESIS", [])
        if synthesis_items:
            item = synthesis_items[0]
            steps.append(
                AnalysisProcessStep(
                    stage="MODEL_SYNTHESIS",
                    status="COMPLETED",
                    title="生成解释与总结",
                    summary=(
                        f"模型基于已有证据整理了 {item.payload.get('claim_count', 0)} 条结论；"
                        "模型不被允许新增无证据事实。"
                    ),
                    evidence_ids=[value.evidence_id for value in synthesis_items],
                )
            )
        elif analysis_items and any(
            "Qwen" in str(warning)
            for item in analysis_items
            for warning in item.payload.get("presentation_warnings", [])
        ):
            steps.append(
                AnalysisProcessStep(
                    stage="MODEL_SYNTHESIS",
                    status="DEGRADED",
                    title="生成解释与总结",
                    summary="模型总结未通过可用性或证据校验，已退回确定性分析结果。",
                    evidence_ids=[item.evidence_id for item in analysis_items],
                )
            )

        if response.reliability is not None:
            level = response.reliability.level.upper()
            steps.append(
                AnalysisProcessStep(
                    stage="RELIABILITY_CHECK",
                    status=(
                        "COMPLETED" if level == "HIGH" else "DEGRADED" if level == "LIMITED" else "FAILED"
                    ),
                    title="校验结果可靠性",
                    summary=(
                        f"可靠性等级 {response.reliability.level}，得分 "
                        f"{response.reliability.score:.2f}；提示 {len(response.reliability.warnings)} 条。"
                    ),
                    evidence_ids=[item.evidence_id for item in response.evidence][:20],
                )
            )

        if response.status in {"SAFE_FALLBACK", "CANCELLED"}:
            steps.append(
                AnalysisProcessStep(
                    stage="SAFE_TERMINATION",
                    status="DEGRADED" if response.status == "CANCELLED" else "FAILED",
                    title="安全结束",
                    summary=(
                        "用户已取消当前任务，系统未继续执行。"
                        if response.status == "CANCELLED"
                        else "依赖、数据或证据未通过安全门禁，系统未输出未经验证的结论。"
                    ),
                )
            )
        return steps

    async def _metadata_answer(self, request: CanonicalAnalysisRequest, identity: TrustedIdentity, semantic_model_id: int | None) -> AgentResponse:
        try:
            resolved = await self.adapters.semantic.resolve_metrics(request, semantic_model_id)
            if len(resolved) != 1:
                return self._fallback(request, "没有找到唯一、已发布的指标定义。")
            item = await (self.adapters.semantic.definition(resolved[0]) if request.primary_intent == PrimaryIntent.METRIC_DEFINITION else self.adapters.semantic.lineage(resolved[0], identity))
        except AdapterError as exc:
            return self._fallback(request, self._dependency_message(exc))
        if request.primary_intent == PrimaryIntent.DATA_LINEAGE:
            business_lineage = [
                str(value).strip() for value in (item.payload.get("business_lineage") or [])
                if str(value).strip()
            ]
            physical_sources = [
                value for value in (item.payload.get("physical_sources") or [])
                if isinstance(value, dict)
            ]
            if not business_lineage and not physical_sources:
                return self._fallback(request, "血缘服务未返回可验证的业务或物理链路。")
            sections = []
            if business_lineage:
                sections.append("业务血缘：" + " → ".join(business_lineage))
            if physical_sources:
                source_lines = []
                for source in physical_sources[:20]:
                    table = next((str(source.get(key)).strip() for key in (
                        "table", "table_name", "source_table", "dataset"
                    ) if source.get(key)), "未知表")
                    field = next((str(source.get(key)).strip() for key in (
                        "field", "column", "column_name", "source_field"
                    ) if source.get(key)), "")
                    transform = next((str(source.get(key)).strip() for key in (
                        "transformation", "expression", "job", "task"
                    ) if source.get(key)), "")
                    line = table + (f".{field}" if field else "")
                    if transform:
                        line += f"（转换：{transform[:200]}）"
                    source_lines.append(line)
                sections.append("物理来源：" + "；".join(source_lines))
            physical_complete = bool(physical_sources)
            warnings = [] if physical_complete else ["上游仅返回业务血缘，未提供表/字段级物理来源"]
            answer = "\n".join(sections)
            reliability = ReliabilityReport(
                level="HIGH" if physical_complete and business_lineage else "LIMITED",
                score=1.0 if physical_complete and business_lineage else 0.65,
                gates={
                    "metadata_found": True,
                    "business_lineage_present": bool(business_lineage),
                    "physical_lineage_present": physical_complete,
                },
                warnings=warnings,
            )
        else:
            payload = item.payload if isinstance(item.payload, dict) else {}
            metric_name = str(
                payload.get("metric_name")
                or resolved[0].canonical_name
                or resolved[0].input
            ).strip()
            description = str(
                payload.get("definition") or payload.get("description") or ""
            ).strip()
            unit = str(payload.get("unit") or resolved[0].unit or "").strip()
            scenarios = [
                str(value).strip()
                for value in (payload.get("applicable_scenarios") or [])
                if str(value).strip()
            ]
            synonyms = [
                str(value).strip()
                for value in (payload.get("synonyms") or [])
                if str(value).strip()
            ]
            filter_explanations = []
            for value in payload.get("global_filters") or []:
                text = str(value)
                match = re.search(
                    r"filterExplanation['\"]?\s*:\s*['\"]([^'\"]+)", text
                )
                if match:
                    filter_explanations.append(match.group(1).strip())
            sections = [f"指标名称：{metric_name}"]
            if description:
                sections.append(f"统计口径：{description}")
            if unit:
                sections.append(f"单位：{unit}")
            if filter_explanations:
                sections.append("数据范围：" + "；".join(dict.fromkeys(filter_explanations)))
            if scenarios:
                sections.append("适用场景：" + "、".join(scenarios))
            if synonyms:
                sections.append("常用叫法：" + "、".join(synonyms))
            answer = "\n".join(sections)
            reliability = ReliabilityReport(
                level="HIGH", score=1, gates={"metadata_found": True}
            )
        return AgentResponse(request_id=request.request_id, conversation_id=request.conversation_id, status="COMPLETED", intent=request.primary_intent, intent_source=request.intent_source, intent_confidence=request.intent_confidence, answer=answer, evidence=[item], reliability=reliability)

    @staticmethod
    def _bind_metrics_from_asl(request: CanonicalAnalysisRequest, asl: dict[str, Any], semantic_model_id: int | None) -> None:
        metrics = asl.get("metrics") or []
        if not isinstance(metrics, list):
            return
        bound = []
        for item in metrics:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"])
            code = name.split(":", 1)[1] if ":" in name else name
            alias = str(item.get("alias") or code)
            existing = next(
                (
                    metric
                    for metric in request.metrics
                    if metric.metric_id
                    and metric.metric_id.split(":", 1)[-1] == code
                ),
                None,
            )
            metric_id = (
                existing.metric_id
                if existing is not None
                else name
            )
            bound.append(
                MetricRef(
                    input=existing.input if existing is not None else alias,
                    metric_id=metric_id,
                    version=(
                        existing.version
                        if existing is not None
                        else f"semantic-model:{semantic_model_id}"
                    ),
                    canonical_name=(
                        existing.canonical_name
                        if existing is not None and existing.canonical_name
                        else alias
                    ),
                    unit=existing.unit if existing is not None else None,
                )
            )
        if bound:
            request.metrics = bound

    @staticmethod
    def _derived_metric_evidence(
        request: CanonicalAnalysisRequest,
        query_result: DataQueryResult,
    ) -> list[EvidenceItem]:
        """Build metric proof from an audited deterministic query transform."""

        evidence: list[EvidenceItem] = []
        for transform in query_result.execution_transforms:
            if (
                transform.get("type")
                != "RELATIONSHIP_COUNT_TO_DISTINCT_PROJECTION"
                or transform.get("verified") is not True
            ):
                continue
            metric_name = str(transform.get("metric_name") or "").strip()
            metric = next(
                (
                    item
                    for item in request.metrics
                    if metric_name
                    in {
                        str(item.input or "").strip(),
                        str(item.canonical_name or "").strip(),
                    }
                ),
                None,
            )
            if metric is None:
                continue
            if (
                query_result.dataset.columns != [metric_name]
                or query_result.dataset.row_count != 1
                or query_result.dataset.rows != [
                    {metric_name: transform.get("derived_value")}
                ]
            ):
                continue
            evidence.append(
                EvidenceItem(
                    evidence_id=(
                        f"derived-metric:{query_result.dataset.snapshot_id}:"
                        f"{metric_name}"
                    ),
                    kind="DERIVED_METRIC_RESOLUTION",
                    source_ref="audited-query-transform",
                    payload={
                        **transform,
                        "metric_id": metric.metric_id,
                        "canonical_name": metric.canonical_name or metric.input,
                    },
                )
            )
        return evidence

    @staticmethod
    def _semantic_metric_evidence(
        request: CanonicalAnalysisRequest,
        query_result: DataQueryResult,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> list[EvidenceItem]:
        """Reuse the successful ASL resolution as metric verification evidence.

        Oagnet resolves names/aliases inside the requested semantic scope. The SQL
        translator then accepts those canonical metric codes and executes the query.
        Re-querying a document knowledge base would add latency without improving
        metric correctness and would incorrectly couple numeric answers to app KBs.
        """
        asl_metrics = query_result.asl.get("metrics") or []
        if not isinstance(asl_metrics, list):
            return []
        resolved = {
            str(item.get("name")).split(":", 1)[-1]: item
            for item in asl_metrics
            if isinstance(item, dict) and item.get("name")
        }
        evidence: list[EvidenceItem] = []
        for metric in request.metrics:
            metric_code = metric.metric_id.split(":", 1)[-1] if metric.metric_id else None
            if metric_code is None or metric_code not in resolved:
                continue
            item = resolved[metric_code]
            evidence.append(
                EvidenceItem(
                    evidence_id=(
                        f"semantic:{semantic_model_id}:{business_domain_id}:"
                        f"{metric.metric_id}"
                    ),
                    kind="SEMANTIC_METRIC_RESOLUTION",
                    source_ref="oagnet-asl",
                    payload={
                        "metric_id": metric.metric_id,
                        "canonical_name": metric.canonical_name or item.get("alias"),
                        "semantic_model_id": semantic_model_id,
                        "business_domain_id": business_domain_id,
                        "verified": True,
                    },
                )
            )
        return evidence

    @staticmethod
    def _projection_value_key(value: Any) -> Any:
        """Return a type-aware, hashable key without changing the source row."""

        if isinstance(value, dict):
            return (
                "dict",
                tuple(
                    sorted(
                        (
                            str(key),
                            DataAnalysisOrchestrator._projection_value_key(item),
                        )
                        for key, item in value.items()
                    )
                ),
            )
        if isinstance(value, (list, tuple)):
            return (
                type(value).__name__,
                tuple(
                    DataAnalysisOrchestrator._projection_value_key(item)
                    for item in value
                ),
            )
        try:
            hash(value)
        except TypeError:
            return (type(value).__name__, repr(value))
        return (type(value).__name__, value)

    @staticmethod
    def _restore_projected_filter_columns(
        request: CanonicalAnalysisRequest,
        dataset: Dataset,
    ) -> None:
        """Restore requested columns that SQL legitimately optimized away.

        A projected dimension constrained by an equality predicate is a known
        constant.  Some semantic SQL plans omit that dimension from SELECT,
        even when the follow-up explicitly asks to display it.  Reintroducing
        only exact requested EQ-filter columns is deterministic and prevents
        ``只保留医院名称和医院等级`` from silently showing one column.
        """
        if request.primary_intent != PrimaryIntent.DETAIL_QUERY or not dataset.rows:
            return
        aliases = {
            "医院等级": {"hospitallevel", "hospitalgrade", "医院级别"},
            "医院名称": {"hospitalname", "medicalinstitutionname"},
            "经销商名称": {"dealername", "distributorname"},
            "供应商名称": {"suppliername", "vendorname"},
            "商品名称": {"goodsname", "productname", "itemname"},
            "订单号": {"orderid", "orderno", "ordercode", "orderkey"},
            "金额": {"amount", "payamount", "paymentamount", "amountwithtax"},
        }
        normalized_columns = {
            re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", column.lower()): column
            for column in dataset.columns
        }
        # Prefer the requested business label over a semantic/physical alias.
        # This also prevents restoring a second constant column when the
        # upstream projection already supplied the same concept.
        for requested in request.fields:
            if requested in dataset.columns:
                continue
            candidates = aliases.get(requested, set())
            source = next(
                (
                    normalized_columns[token]
                    for token in candidates
                    if token in normalized_columns
                ),
                None,
            )
            if source is None:
                continue
            index = dataset.columns.index(source)
            dataset.columns[index] = requested
            for row in dataset.rows:
                if source in row:
                    row[requested] = row.pop(source)
        missing = [field for field in request.fields if field not in dataset.columns]
        if not missing:
            return
        constants = {
            str(item.get("field")): item.get("value")
            for item in request.filters
            if str(item.get("operator") or "").upper() == "EQ"
            and item.get("field")
            and "value" in item
        }
        restored = [field for field in missing if field in constants]
        if not restored:
            return
        dataset.columns.extend(restored)
        for row in dataset.rows:
            for field in restored:
                row[field] = constants[field]

    @classmethod
    def _enforce_name_projection_integrity(
        cls,
        request: CanonicalAnalysisRequest,
        query_result: DataQueryResult,
    ) -> DataQueryResult:
        """Remove invalid master-name members from complete list results.

        The canonical/ASL constraint is the primary guard.  This deterministic
        result gate protects against stale relationship rows, legacy translators
        and dirty placeholder strings that still reach a complete result.  A
        truncated result cannot be repaired locally because unseen/file rows
        may contain the same defect, so it is marked failed instead of silently
        claiming a complete clean list.
        """

        if request.primary_intent != PrimaryIntent.DETAIL_QUERY:
            return query_result
        required_fields = list(dict.fromkeys(
            value.split("=", 1)[1].strip()
            for value in request.assumptions
            if value.startswith("REQUIRED_NAME_NON_NULL=")
            and value.split("=", 1)[1].strip()
        ))
        if not required_fields or not query_result.dataset.rows:
            return query_result

        aliases = {
            "医院名称": {"医院", "hospital", "hospitalname", "medicalinstitutionname"},
            "经销商名称": {"经销商", "dealer", "dealername", "distributorname"},
            "供应商名称": {"供应商", "supplier", "suppliername", "vendorname"},
            "厂家名称": {"厂家", "厂商", "manufacturer", "manufacturername", "makername"},
            "制造商名称": {"制造商", "manufacturer", "manufacturername", "makername"},
            "商品名称": {"商品", "产品", "product", "productname", "goodsname", "itemname"},
            "客户名称": {"客户", "customer", "customername", "clientname"},
            "门店名称": {"门店", "store", "storename", "shopname"},
            "科室名称": {"科室", "适用科室", "department", "departmentname", "deptname"},
            "品牌名称": {"品牌", "brand", "brandname"},
            "母品牌": {"母品牌名称", "parentbrand", "parentbrandname"},
        }

        def normalize(value: Any) -> str:
            return re.sub(
                r"[^0-9a-z\u4e00-\u9fff]", "", str(value or "").casefold()
            )

        resolved_columns: list[str] = []
        for field in required_fields:
            tokens = {
                normalize(field),
                *(normalize(value) for value in aliases.get(field, set())),
            }
            matches = [
                column for column in query_result.dataset.columns
                if normalize(column) in tokens
            ]
            if len(matches) == 1:
                resolved_columns.append(matches[0])
        if len(resolved_columns) != len(required_fields):
            return query_result

        invalid_markers = {
            "", "-", "--", "—", "–", "－", "null", "none", "nil", "n/a",
            "na", "未填写", "未知", "无",
        }

        def valid_row(row: dict[str, Any]) -> bool:
            for column in resolved_columns:
                value = row.get(column)
                if value is None or str(value).strip().casefold() in invalid_markers:
                    return False
            return True

        clean_rows = [row for row in query_result.dataset.rows if valid_row(row)]
        removed_count = len(query_result.dataset.rows) - len(clean_rows)
        if removed_count == 0:
            return query_result

        dataset_payload = query_result.dataset.model_dump()
        dataset_payload["rows"] = clean_rows
        dataset_payload["row_count"] = len(clean_rows)
        if query_result.dataset.truncated:
            dataset_payload["quality_status"] = "FAIL"
        else:
            dataset_payload["total_row_count"] = len(clean_rows)
        cleaned_dataset = Dataset.model_validate(dataset_payload)
        transform = {
            "type": "DROP_INVALID_NAME_PROJECTION_ROWS",
            "fields": required_fields,
            "removed_row_count": removed_count,
            "verified_complete_result": not query_result.dataset.truncated,
        }
        assumption = f"INVALID_NAME_ROWS_REMOVED={removed_count}"
        if assumption not in request.assumptions:
            request.assumptions.append(assumption)
        return query_result.model_copy(update={
            "dataset": cleaned_dataset,
            "execution_transforms": [
                *query_result.execution_transforms,
                transform,
            ],
            "result_file_url": (
                None if query_result.dataset.truncated
                else query_result.result_file_url
            ),
        })

    @staticmethod
    def _relationship_projection_rows(
        request: CanonicalAnalysisRequest,
        columns: list[str],
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Deduplicate only a set-shaped relationship projection for display.

        A physical relationship table can legitimately contain several source
        rows that project to the same user-facing pair (for example, two source
        objects related to the same target).  The underlying dataset remains
        untouched.  Transaction/event detail is deliberately excluded because
        identical projected facts can still be separate valid observations.
        """

        master_name_columns = {
            "产品名称", "商品名称", "经销商名称", "供应商名称",
            "医院名称", "客户名称", "门店名称", "品牌名称",
        }
        unambiguous_single_master_projection = (
            len(columns) == 1 and columns[0] in master_name_columns
        )
        if len(rows) < 2 or not (
            unambiguous_single_master_projection
            or requires_distinct_relationship_projection(request)
        ):
            return None

        unique_rows: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for row in rows:
            projection_key = tuple(
                (column, DataAnalysisOrchestrator._projection_value_key(row.get(column)))
                for column in columns
            )
            if projection_key in seen:
                continue
            seen.add(projection_key)
            unique_rows.append(row)
        return unique_rows if len(unique_rows) < len(rows) else None

    @staticmethod
    def _relationship_count_projection_request(
        request: CanonicalAnalysisRequest,
    ) -> CanonicalAnalysisRequest | None:
        """Execute scalar relationship counts from the corresponding name set.

        Fact-table identifiers and displayable master-data names can diverge
        because of null or stale mappings.  Users reasonably expect “名单” and
        “数量” to describe the same set, so scalar relationship counts are
        derived from the exact DISTINCT name projection used by the list query.
        Grouped counts keep their ordinary aggregate execution shape.
        """

        mapping = {
            "已合作医院数": ("医院", "医院名称"),
            "已合作经销商数": ("经销商", "经销商名称"),
            "已合作供应商数": ("供应商", "供应商名称"),
            "已合作客户数": ("客户", "客户名称"),
            "已合作门店数": ("门店", "门店名称"),
        }
        if request.primary_intent != PrimaryIntent.METRIC_QUERY or len(request.metrics) != 1:
            return None
        metric_name = request.metrics[0].canonical_name or request.metrics[0].input
        shape = mapping.get(metric_name) or mapping.get(request.metrics[0].input)
        if shape is None or request.dimensions:
            return None
        entity, field = shape
        question = request.rewritten_question or request.original_question
        question = re.sub(
            rf"(?:已)?合作(?:的)?{entity}(?:数量|数)",
            f"合作{entity}名单",
            question,
        )
        question = re.sub(rf"{entity}数量", f"{entity}名单", question)
        return request.model_copy(deep=True, update={
            "request_id": uuid4(),
            "original_question": question,
            "rewritten_question": question,
            "primary_intent": PrimaryIntent.DETAIL_QUERY,
            "metrics": [],
            "entity": entity,
            "fields": [field],
            "dimensions": [entity],
            "operators": [AnalysisOperator.FILTER, AnalysisOperator.RENDER_TABLE],
            "asl_template": None,
            "execution_contract_transform": (
                "RELATIONSHIP_COUNT_TO_DISTINCT_PROJECTION"
            ),
            "assumptions": [
                *request.assumptions,
                "SET_RELATIONSHIP_PROJECTION",
                "RELATIONSHIP_COUNT_FROM_VISIBLE_NAME_SET",
            ],
        })

    @staticmethod
    def _relationship_count_projection_result(
        request: CanonicalAnalysisRequest,
        result: DataQueryResult,
    ) -> DataQueryResult:
        mapping = {
            "已合作医院数": "医院名称",
            "已合作经销商数": "经销商名称",
            "已合作供应商数": "供应商名称",
            "已合作客户数": "客户名称",
            "已合作门店数": "门店名称",
        }
        metric_name = request.metrics[0].canonical_name or request.metrics[0].input
        expected_column = mapping.get(metric_name) or mapping.get(
            request.metrics[0].input
        )
        if expected_column is None:
            raise AdapterError(
                "RELATIONSHIP_COUNT_PROJECTION_INVALID",
                "relationship count metric is not eligible for projection",
            )
        dataset = result.dataset
        source_total_row_count = (
            dataset.total_row_count
            if dataset.total_row_count is not None
            else dataset.row_count
        )
        source_total_confirmed = (
            not dataset.truncated
            or source_total_row_count > dataset.row_count
        )
        visible_column = (
            expected_column
            if expected_column in dataset.columns
            else dataset.columns[0]
            if len(dataset.columns) == 1
            else None
        )
        if visible_column is None:
            raise AdapterError(
                "RELATIONSHIP_COUNT_PROJECTION_INVALID",
                "relationship projection did not return one identifiable name column",
                details={
                    "expected_column": expected_column,
                    "returned_columns": dataset.columns,
                },
            )
        if dataset.truncated:
            if (
                not source_total_confirmed
                or re.search(r"^\s*SELECT\s+DISTINCT\b", result.sql, re.I) is None
            ):
                raise AdapterError(
                    "RELATIONSHIP_COUNT_SOURCE_INCOMPLETE",
                    "truncated relationship projection has no verified distinct total",
                    details={
                        "returned_row_count": dataset.row_count,
                        "total_row_count": source_total_row_count,
                        "sql_distinct": bool(
                            re.search(r"^\s*SELECT\s+DISTINCT\b", result.sql, re.I)
                        ),
                    },
                )
            count = source_total_row_count
            distinctness_method = "UPSTREAM_DISTINCT_TOTAL"
        else:
            # SQL aggregate/join paths may return one physical row whose
            # projected master-data name is NULL.  The list renderer correctly
            # treats that as no displayable object; the derived count must do
            # the same instead of reporting the physical row count as one.
            visible_values = {
                str(row.get(visible_column)).strip()
                for row in dataset.rows
                if visible_column is not None
                and row.get(visible_column) is not None
                and str(row.get(visible_column)).strip()
            }
            count = len(visible_values)
            distinctness_method = "LOCAL_DISTINCT_VISIBLE_VALUES"
        scalar = Dataset(
            columns=[metric_name],
            rows=[{metric_name: count}],
            snapshot_id=dataset.snapshot_id,
            data_as_of=dataset.data_as_of,
            source_data_as_of=dataset.source_data_as_of,
            source_watermark_field=dataset.source_watermark_field,
            quality_status=dataset.quality_status,
            row_count=1,
            total_row_count=1,
            truncated=False,
        )
        transform = {
            "type": "RELATIONSHIP_COUNT_TO_DISTINCT_PROJECTION",
            "metric_name": metric_name,
            "source_projection": visible_column,
            "source_row_count": dataset.row_count,
            "source_total_row_count": source_total_row_count,
            "source_truncated": dataset.truncated,
            "source_total_confirmed": source_total_confirmed,
            "distinctness_method": distinctness_method,
            "derived_value": count,
            "verified": True,
        }
        return result.model_copy(update={
            "dataset": scalar,
            "execution_transforms": [
                *result.execution_transforms,
                transform,
            ],
        })

    @staticmethod
    def _analyze(
        request: CanonicalAnalysisRequest,
        columns: list[str],
        rows: list[dict[str, Any]],
        knowledge: KnowledgeContext,
        *,
        result_truncated: bool = False,
    ) -> str:
        if not rows:
            return "查询执行成功，但指定条件下没有数据。"
        # Aggregate SQL commonly returns one physical row containing NULL when
        # no source rows match (for example SUM over an empty date range). This
        # is semantically an empty result, not a metric value named "None".
        if all(value is None for row in rows for value in row.values()):
            return "查询执行成功，但指定条件下没有有效数据。"
        if request.primary_intent == PrimaryIntent.DETAIL_QUERY:
            # Keep ordinary complete business lists complete in the answer.
            # The retrieval contract already caps an in-memory dataset at
            # ``data_query_max_rows`` (1000 by default); an older presentation
            # cap silently turned a verified 217-row result into a 20/200-row
            # looking answer.  Truly larger results arrive as truncated/file
            # responses and are handled by the explicit preview branch.
            display_limit = 1000
            unique_rows = (
                None
                if result_truncated
                else DataAnalysisOrchestrator._relationship_projection_rows(
                    request, columns, rows
                )
            )
            if unique_rows is not None:
                answer = (
                    f"查询返回 {len(rows)} 条原始关系记录；"
                    f"按当前投影字段完全相同的组合去重展示后，共 {len(unique_rows)} 个唯一组合。\n\n"
                    f"{DataAnalysisOrchestrator._markdown_result_table(columns, unique_rows[:display_limit])}\n\n"
                )
                if len(unique_rows) > display_limit:
                    answer += (
                        f"> 当前展示前 {display_limit} 个唯一组合，完整结果请使用附件下载。\n\n"
                    )
                answer += f"> 原始数据集及证据行数仍为 {len(rows)}。"
                return answer
            answer = (
                f"共查询到 {len(rows)} 条明细。\n\n"
                f"{DataAnalysisOrchestrator._markdown_result_table(columns, rows[:display_limit])}"
            )
            if len(rows) > display_limit:
                answer += (
                    f"\n\n> 当前展示前 {display_limit} 条，完整结果请使用附件下载。"
                )
            return answer
        if len(rows) == 1:
            return DataAnalysisOrchestrator._markdown_result_table(columns, rows)
        label = {
            PrimaryIntent.TREND_ANALYSIS: "趋势分析数据",
            PrimaryIntent.COMPARISON_ANALYSIS: "对比分析数据",
            PrimaryIntent.COMPOSITION_ANALYSIS: "占比分析数据",
            PrimaryIntent.ANOMALY_ANALYSIS: "异常分析数据",
            PrimaryIntent.ROOT_CAUSE_ANALYSIS: "归因分析基础数据",
            PrimaryIntent.FORECAST_ANALYSIS: "预测模型输入数据",
            PrimaryIntent.REPORT_GENERATION: "报表数据",
        }.get(request.primary_intent, "查询结果")
        display_limit = 200
        answer = (
            f"{label}，共 {len(rows)} 行。\n\n"
            f"{DataAnalysisOrchestrator._markdown_result_table(columns, rows[:display_limit])}"
        )
        if len(rows) > display_limit:
            answer += (
                f"\n\n> 当前展示前 {display_limit} 行，完整结果请使用附件下载。"
            )
        if knowledge.documents:
            references = "；".join(
                f"[{doc.source or doc.kb_name or '知识库'}] {doc.content}"
                for doc in knowledge.documents[:3]
            )
            if request.primary_intent in {PrimaryIntent.ANOMALY_ANALYSIS, PrimaryIntent.ROOT_CAUSE_ANALYSIS}:
                answer += f"\n知识库提供的待验证因素：{references}\n注意：这些是候选解释，只有与查询数据吻合后才能认定为原因。"
            else:
                answer += f"\n相关知识依据：{references}"
        return answer

    @staticmethod
    def _display_column_name(column: str) -> str:
        """Hide semantic/physical prefixes while retaining recognizable labels."""
        raw = str(column)
        aliases = {
            "product_name": "产品名称", "dealer_name": "经销商名称",
            "hospital_name": "医院名称", "customer_name": "客户名称",
            "supplier_name": "供应商名称", "store_name": "门店名称",
            "商品": "商品名称",
        }
        tail = raw.rsplit(".", 1)[-1]
        return aliases.get(tail, tail.replace("_", " "))

    @staticmethod
    def _markdown_cell(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        elif isinstance(value, float):
            text = f"{value:,.4f}".rstrip("0").rstrip(".")
        else:
            text = str(value)
        return text.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")

    @staticmethod
    def _markdown_result_table(columns: list[str], rows: list[dict[str, Any]]) -> str:
        visible_columns = list(dict.fromkeys([
            *columns,
            *(key for row in rows for key in row if key not in columns),
        ]))
        if not visible_columns:
            return "未返回可展示字段。"
        headers = [DataAnalysisOrchestrator._display_column_name(item) for item in visible_columns]
        lines = [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
        ]
        lines.extend(
            "| " + " | ".join(
                DataAnalysisOrchestrator._markdown_cell(row.get(column))
                for column in visible_columns
            ) + " |"
            for row in rows
        )
        return "\n".join(lines)

    @staticmethod
    def _task_result_summary_table(results: list[TaskExecutionResult]) -> str:
        # A child answer may itself contain a Markdown table. Embedding it in
        # an outer table forces pipes to be escaped and newlines to become
        # ``<br>``, leaving raw table syntax visible in the UI. Independent
        # sections are valid for every result shape and preserve child tables.
        sections: list[str] = []
        for index, result in enumerate(results, 1):
            content = (result.answer or "未返回结果。").strip()
            status = "" if result.status == "COMPLETED" else f"（{result.status}）"
            sections.append(
                f"### {index}. {result.question}{status}\n\n{content}"
            )
        return "\n\n".join(sections)

    @classmethod
    def _ambiguity_texts(cls, exc: AdapterError) -> list[str]:
        if isinstance(exc.details, list):
            raw = [
                str(value.get("message") or value.get("question") or value)
                if isinstance(value, dict)
                else str(value)
                for value in exc.details
            ]
        else:
            try:
                value = json.loads(str(exc))
                raw = [
                    str(v.get("message") or v.get("question") or v)
                    if isinstance(v, dict) else str(v)
                    for v in value
                ]
            except (ValueError, TypeError):
                raw = [str(exc)]
        return list(dict.fromkeys(
            cls._sanitize_clarification_text(value) for value in raw
        ))

    @staticmethod
    def _semantic_ambiguities(exc: AdapterError) -> list[SemanticAmbiguity]:
        if not isinstance(exc.details, list):
            return []
        result: list[SemanticAmbiguity] = []
        allowed_types = {
            "turn_relation", "schema_relation", "metric", "dimension", "filter",
            "entity_value", "entity_role", "filter_slot", "operation_intent",
            "time_anchor", "comparison", "data_source", "context",
            "fact_conflict", "rewrite_conflict", "historical_branch", "subject",
        }
        for value in exc.details[:5]:
            if not isinstance(value, dict):
                continue
            question = str(value.get("question") or value.get("message") or "").strip()
            if not question:
                continue
            raw_candidates = value.get("candidates")
            candidates = (
                [str(item).strip() for item in raw_candidates if str(item).strip()]
                if isinstance(raw_candidates, list)
                else []
            )
            ambiguity_type = str(value.get("type") or "unknown")
            if ambiguity_type not in allowed_types:
                ambiguity_type = "unknown"
            try:
                result.append(SemanticAmbiguity(
                    type=ambiguity_type,
                    question=question[:500],
                    candidates=candidates[:10],
                    ambiguity_id=(
                        str(value.get("ambiguity_id"))[:128]
                        if value.get("ambiguity_id") else None
                    ),
                    phrase=(
                        str(value.get("phrase"))[:200]
                        if value.get("phrase") else None
                    ),
                    affected_slots=[
                        str(item)[:200]
                        for item in value.get("affected_slots") or []
                        if str(item).strip()
                    ][:20],
                    candidate_details=[
                        dict(item) for item in value.get("candidate_details") or []
                        if isinstance(item, dict)
                    ][:10],
                    material_impact=(
                        str(value.get("material_impact"))[:300]
                        if value.get("material_impact") else None
                    ),
                    blocking=bool(value.get("blocking", True)),
                    semantic_model_id=(
                        int(value["semantic_model_id"])
                        if value.get("semantic_model_id") else None
                    ),
                    semantic_model_version=(
                        str(value.get("semantic_model_version"))[:128]
                        if value.get("semantic_model_version") else None
                    ),
                ))
            except ValueError:
                continue
        return result

    @classmethod
    def _clarification_questions(
        cls, request: CanonicalAnalysisRequest
    ) -> list[str]:
        prompts = {"turn_relation": "请确认这句话是在补充上一轮，还是一个独立新问题？", "metric": "要查询或分析哪个指标？", "time_range": "要分析哪个时间范围？", "entity": "要查询哪类业务明细？", "fields": "明细中需要哪些字段？", "comparison_type": "希望同比、环比、目标值还是对象间比较？", "comparison_objects": "请提供要对比的具体经销商或供应商名称，并用顿号或逗号分隔。", "dimension": "希望按哪个维度分析？", "product": "要计算哪个具体商品的科室匹配度？", "semantic_ambiguity": "请确认存在歧义的业务口径。"}
        normalized_question = re.sub(r"\s+", "", request.original_question or "")
        if "推荐" in normalized_question or "画像" in normalized_question:
            prompts["metric"] = (
                "请确认推荐排序依据，例如销售额、订单量、信用等级或综合评分。"
            )
        questions: list[str] = []
        for slot in request.missing_slots:
            if slot == "forecast_horizon":
                prompt = "需要预测未来多少期、按天/周/月/季度还是按年？例如：预测未来3个月。"
                if prompt not in questions:
                    questions.append(prompt)
            elif slot == "forecast_history_range":
                prompt = "需要用哪段历史数据建模？请提供历史起止范围或窗口，例如：基于过去12个月。"
                if prompt not in questions:
                    questions.append(prompt)
            elif slot == "semantic_ambiguity" and request.ambiguities:
                questions.extend(
                    text
                    for value in request.ambiguities
                    if (
                        text := cls._sanitize_clarification_text(value)
                    ) not in questions
                )
            else:
                prompt = prompts.get(slot, f"请补充 {slot}。")
                if prompt not in questions:
                    questions.append(prompt)
        return questions

    @classmethod
    def _clarification_items(cls, request: CanonicalAnalysisRequest) -> list[dict[str, Any]]:
        options_by_slot = {
            "turn_relation": ["补充或修改上一轮问题", "作为独立新问题"],
            "time_range": ["今天", "昨天", "本周", "上周", "本月", "上月"],
            "comparison_type": ["同比", "环比", "目标值", "对象间比较"],
            "forecast_horizon": ["未来7天", "未来4周", "未来3个月", "未来4个季度"],
            "forecast_history_range": ["过去3个月", "过去6个月", "过去12个月", "过去24个月"],
        }
        titles = {
            "turn_relation": "对话关系", "metric": "指标", "time_range": "时间", "entity": "业务对象",
            "fields": "字段", "comparison_type": "比较方式", "comparison_objects": "比较对象", "dimension": "分析维度",
            "product": "商品", "semantic_ambiguity": "口径确认",
            "forecast_horizon": "预测范围", "forecast_history_range": "历史范围",
        }
        semantic_titles = {
            "turn_relation": "对话关系", "schema_relation": "数据关系",
            "metric": "指标口径", "dimension": "维度口径", "filter": "筛选口径",
            "entity_value": "实体值", "entity_role": "实体角色",
            "filter_slot": "筛选字段", "operation_intent": "查询方式",
            "time_anchor": "时间口径", "comparison": "比较方式",
            "data_source": "数据来源", "context": "上下文",
            "fact_conflict": "事实冲突", "rewrite_conflict": "改写冲突",
            "historical_branch": "历史任务", "subject": "业务对象",
            "unknown": "口径确认",
        }
        items: list[dict[str, Any]] = []
        for slot in request.missing_slots:
            if slot == "semantic_ambiguity" and request.semantic_ambiguities:
                items.extend({
                    "slot": slot,
                    "title": semantic_titles[ambiguity.type],
                    "question": ambiguity.question,
                    "options": ambiguity.candidates,
                    "option_details": ambiguity.candidate_details,
                    "multi_select": False,
                    "allow_free_text": True,
                } for ambiguity in request.semantic_ambiguities)
                continue
            single_slot_request = request.model_copy(update={"missing_slots": [slot]})
            slot_questions = cls._clarification_questions(single_slot_request)
            question = slot_questions[0] if slot_questions else f"请补充 {slot}。"
            items.append({
                "slot": slot,
                "title": titles.get(slot, "补充信息"),
                "question": question,
                "options": options_by_slot.get(slot, []),
                "multi_select": slot in {"metric", "fields", "dimension"},
                "allow_free_text": True,
            })
        return items

    @staticmethod
    def _understood_slots(request: CanonicalAnalysisRequest) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if request.metrics:
            result["metrics"] = [
                metric.canonical_name or metric.input for metric in request.metrics
            ]
        if request.time_range:
            result["time_range"] = {
                "start": request.time_range.start.isoformat(),
                "end_inclusive": (
                    request.time_range.end_exclusive - timedelta(days=1)
                ).isoformat(),
                "timezone": request.time_range.timezone,
            }
        if request.entity:
            result["entity"] = request.entity
        if request.fields:
            result["fields"] = request.fields
        if request.dimensions:
            result["dimensions"] = request.dimensions
        if request.comparison_type:
            result["comparison_type"] = request.comparison_type
        if request.forecast_horizon_periods and request.forecast_granularity:
            result["forecast_target"] = {
                "periods": request.forecast_horizon_periods,
                "granularity": request.forecast_granularity,
            }
        if request.forecast_history_provided:
            result["forecast_history_provided"] = True
        return result

    @staticmethod
    def _understood_text(slots: dict[str, Any]) -> str:
        parts: list[str] = []
        if metrics := slots.get("metrics"):
            parts.append("指标=" + "、".join(str(value) for value in metrics))
        if time_range := slots.get("time_range"):
            parts.append(
                f"时间范围={time_range['start']} 至 {time_range['end_inclusive']}（含首尾）"
            )
        if entity := slots.get("entity"):
            parts.append(f"明细对象={entity}")
        if fields := slots.get("fields"):
            parts.append("字段=" + "、".join(str(value) for value in fields))
        if dimensions := slots.get("dimensions"):
            parts.append("分析维度=" + "、".join(str(value) for value in dimensions))
        if comparison_type := slots.get("comparison_type"):
            parts.append(f"对比方式={comparison_type}")
        return "；".join(parts)

    @staticmethod
    def _external_search_mode(question: str) -> str | None:
        """Return PURE/ENRICH only for narrowly identifiable public-web needs.

        This is a routing guard, not a second general intent classifier.  It
        keeps business queries on the semantic-data path while allowing public
        facts such as a hospital address to use a caller-provided web capability.
        """
        text = "".join(question.casefold().split())
        explicit_web = any(
            marker in text
            for marker in ("联网", "互联网", "全网", "网上搜索", "公开网络")
        )
        internal_data_markers = (
            "销售", "订单", "经销商", "供应商", "金额", "趋势", "排名",
            "分析", "推荐", "筛选", "合作", "商品", "产品", "品牌", "科室",
            "top", "画像", "业绩", "增长率",
        )
        needs_internal_data = any(marker in text for marker in internal_data_markers)
        if explicit_web:
            return "ENRICH" if needs_internal_data else "PURE"

        public_location = (
            any(marker in text for marker in ("医院", "学校", "机构", "公司"))
            and (
                "地址" in text
                or "位置" in text
                or any(marker in text for marker in ("在哪里", "在哪儿", "在哪", "在那里"))
            )
        )
        if public_location and not needs_internal_data:
            return "PURE"

        if "画像" in text and any(
            marker in text for marker in ("经销商", "供应商", "推荐", "top")
        ):
            return "ENRICH"
        return None

    async def _enrich_ranked_entities(
        self,
        *,
        chat: ChatRequest,
        request: CanonicalAnalysisRequest,
        analysis_output: Any,
    ) -> tuple[
        list[ExtensionExecution],
        str,
        list[dict[str, Any]],
        list[str],
        list[str],
    ]:
        """Search public information only for governed ranking labels.

        Database ranking is the source of truth.  Public search starts only
        after deterministic analysis has produced ``facts.rankings`` and its
        records are supplementary; they can neither create nor reorder ranked
        entities.
        """
        rankings = (
            analysis_output.facts.get("rankings")
            if analysis_output is not None
            and isinstance(getattr(analysis_output, "facts", None), dict)
            else None
        )
        if not isinstance(rankings, list):
            return [], "", [], [], []

        labels: list[str] = []
        for item in rankings:
            if not isinstance(item, dict):
                continue
            label = self._bounded_external_text(item.get("label"), 120)
            if label and label not in labels:
                labels.append(label)
            if len(labels) == 3:
                break
        if not labels:
            return [], "", [], [], []

        queries = [
            self._ranked_entity_search_query(label, request.original_question)
            for label in labels
        ]

        async def search(query: str) -> list[ExtensionExecution]:
            try:
                return await self.extension_dispatcher.execute(
                    chat=chat,
                    intent=request.primary_intent.value,
                    builtin_skill=skill_for_intent(request.primary_intent),
                    payload={
                        # ExtensionDispatcher maps the New_Agent ``query``
                        # argument from ``question`` for HTTP and MCP tools.
                        "question": query,
                        "query": query,
                        "count": 5,
                        "summary": True,
                    },
                    required_capability="WEB_SEARCH",
                )
            except Exception as exc:  # provider/discovery failure is optional
                logger.warning(
                    "ranked entity web enrichment failed safely: %s",
                    type(exc).__name__,
                )
                return [ExtensionExecution(
                    name="web_search_data",
                    kind="HTTP_TOOL",
                    status="FAILED",
                    error="排名实体联网检索失败",
                    error_type="network_service_error",
                )]

        batches = await asyncio.gather(*(search(query) for query in queries))
        executions = [execution for batch in batches for execution in batch]
        grouped_records: list[tuple[str, str, list[dict[str, str]]]] = []
        for label, query, batch in zip(labels, queries, batches):
            _, records = self._external_search_material(
                batch,
                assume_selected_is_web=True,
            )
            matching = [
                record for record in records
                if self._web_record_matches_label(record, label)
            ]
            grouped_records.append((label, query, matching))

        merged: list[dict[str, Any]] = []
        by_key: dict[tuple[str, ...], dict[str, Any]] = {}
        covered: list[str] = []
        for label, query, records in grouped_records:
            if records:
                covered.append(label)
            for record in records:
                url = record.get("url", "").strip()
                key = (
                    ("url", url.casefold())
                    if url
                    else (
                        "content",
                        record.get("title", "").casefold(),
                        record.get("snippet", "").casefold(),
                    )
                )
                existing = by_key.get(key)
                if existing is not None:
                    if label not in existing["matched_labels"]:
                        existing["matched_labels"].append(label)
                    continue
                value: dict[str, Any] = {
                    **record,
                    "entity_label": label,
                    "matched_labels": [label],
                    "search_query": query,
                }
                by_key[key] = value
                merged.append(value)

        if not merged:
            return executions, "", [], labels, covered

        lines: list[str] = []
        for label, _, records in grouped_records:
            if not records:
                lines.append(f"- {label}：未检索到能明确对应该实体的公开网页。")
                continue
            item = records[0]
            title = item.get("title") or "公开网页"
            line = f"- {label}：{title}"
            if snippet := item.get("snippet", ""):
                line += f"；{snippet}"
            if url := item.get("url", ""):
                line += f"\n  来源：{url}"
            lines.append(line)
        return executions, "\n".join(lines), merged[:15], labels, covered

    @classmethod
    def _ranked_entity_search_query(cls, label: str, question: str) -> str:
        compact = cls._bounded_external_text(question, 240)
        focus: list[str] = []
        requested_facets = (
            (("画像",), ("企业画像", "主营业务", "官网")),
            (("联系方式", "联系人", "电话", "邮箱"), ("联系方式", "官网")),
            (("地址", "位置"), ("地址", "官网")),
            (("业务范围", "主营"), ("主营业务",)),
            (("资质", "许可证"), ("资质",)),
            (("规模",), ("企业规模",)),
        )
        for markers, terms in requested_facets:
            if any(marker in compact for marker in markers):
                focus.extend(terms)
        if not focus:
            focus.append(compact)
        focus.append("公开信息")
        normalized_focus = " ".join(dict.fromkeys(term for term in focus if term))
        safe_label = label.replace('"', "").strip()
        return f'"{safe_label}" {normalized_focus}'[:500]

    @staticmethod
    def _web_record_matches_label(record: dict[str, str], label: str) -> bool:
        needle = "".join(label.casefold().split())
        haystack = "".join(
            f"{record.get('title', '')} {record.get('snippet', '')}".casefold().split()
        )
        return bool(needle) and needle in haystack

    @staticmethod
    def _attach_external_enrichment(
        response: AgentResponse,
        *,
        request_id: str,
        supplement: str,
        records: list[dict[str, Any]],
        ranking_labels: list[str] | None = None,
        covered_ranking_labels: list[str] | None = None,
    ) -> None:
        """Attach successful web evidence without changing governed ranking facts."""
        if not supplement:
            return
        response.answer += (
            "\n\n公开信息补充（不改变业务数据库的筛选与排名口径）：\n"
            + supplement
        )
        evidence_id = f"external-search:{request_id}"
        if not any(item.evidence_id == evidence_id for item in response.evidence):
            response.evidence.append(
                EvidenceItem(
                    evidence_id=evidence_id,
                    kind="WEB_SEARCH_RESULT",
                    source_ref="request-provided-web-search",
                    payload={
                        "records": records,
                        **(
                            {
                                "ranking_labels": ranking_labels,
                                "covered_ranking_labels": (
                                    covered_ranking_labels or []
                                ),
                                "ranking_label_coverage": (
                                    len(covered_ranking_labels or [])
                                    / len(ranking_labels)
                                ),
                            }
                            if ranking_labels
                            else {}
                        ),
                    },
                )
            )
        if response.reliability is not None:
            warning = "公开网络信息未经业务数据口径校验，仅作为补充参考。"
            if warning not in response.reliability.warnings:
                response.reliability.warnings.append(warning)
            # Supplementary public information may lower a successful core
            # result, but it must never upgrade a failed safety decision.
            if response.reliability.level != "FAIL":
                response.reliability.level = "LIMITED"

    async def _external_only_answer(
        self,
        request: CanonicalAnalysisRequest,
        chat: ChatRequest,
    ) -> AgentResponse | None:
        await emit_progress(
            "EXTERNAL_SEARCH", "RUNNING", "正在通过请求提供的联网能力检索公开信息。"
        )
        executions = await self.extension_dispatcher.execute(
            chat=chat,
            intent=request.primary_intent.value,
            builtin_skill=skill_for_intent(request.primary_intent),
            payload={
                "question": request.original_question,
                "query": request.original_question,
                "intent": request.primary_intent.value,
                "application_id": request.application_id,
                "conversation_id": request.conversation_id,
                # Public facts need enough independent results to detect a
                # conflicting snippet instead of trusting the provider's first
                # rank blindly.  This remains within the New_Agent tool
                # contract and the provider's bounded result set.
                "count": 10,
                "summary": True,
            },
            required_capability="WEB_SEARCH",
        )
        if not executions:
            await emit_progress(
                "EXTERNAL_SEARCH",
                "DEGRADED",
                "本次请求未提供可用的联网搜索tool、MCP或对应skill绑定。",
            )
            return None

        # External public-fact routing is a read-only detail lookup, even when
        # the general data classifier labelled it outside the business model.
        if request.primary_intent in {
            PrimaryIntent.OUT_OF_SCOPE,
            PrimaryIntent.CHAT,
        }:
            request.primary_intent = PrimaryIntent.DETAIL_QUERY
            request.intent_source = "EXTERNAL_ROUTE"

        answer, records = self._external_search_material(
            executions, assume_selected_is_web=True
        )
        if answer:
            await emit_progress(
                "EXTERNAL_SEARCH", "COMPLETED", "联网公开信息检索完成。"
            )
            return AgentResponse(
                request_id=request.request_id,
                conversation_id=request.conversation_id,
                status="COMPLETED",
                intent=request.primary_intent,
                intent_source=request.intent_source,
                intent_confidence=request.intent_confidence,
                answer="联网检索结果：\n" + answer,
                evidence=[
                    EvidenceItem(
                        evidence_id=f"external-search:{request.request_id}",
                        kind="WEB_SEARCH_RESULT",
                        source_ref="request-provided-web-search",
                        payload={"records": records},
                    )
                ],
                extension_executions=executions,
                reliability=ReliabilityReport(
                    level="LIMITED",
                    score=0.75,
                    gates={
                        "external_search_succeeded": True,
                        "source_attribution_present": any(
                            bool(item.get("url")) for item in records
                        ),
                    },
                    warnings=["公开网络信息未经业务数据口径校验，请以来源页面为准。"],
                ),
            )

        await emit_progress(
            "EXTERNAL_SEARCH", "FAILED", "联网能力未返回可用于回答的公开信息。"
        )
        errors = list(
            dict.fromkeys(
                item.error for item in executions if item.error
            )
        )
        return AgentResponse(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            status="FAILED",
            intent=request.primary_intent,
            intent_source=request.intent_source,
            intent_confidence=request.intent_confidence,
            answer=(
                "联网搜索工具未能返回可用于回答的信息。"
                + ((" " + "；".join(errors)) if errors else "")
            ),
            extension_executions=executions,
            reliability=ReliabilityReport(
                level="FAIL",
                score=0,
                gates={
                    "external_search_succeeded": False,
                    "source_attribution_present": False,
                },
            ),
        )

    @classmethod
    def _external_search_material(
        cls,
        executions: list[ExtensionExecution],
        *,
        assume_selected_is_web: bool = False,
    ) -> tuple[str, list[dict[str, str]]]:
        records: list[dict[str, str]] = []
        for execution in executions:
            if execution.status != "COMPLETED" or execution.output is None:
                continue
            normalized_name = execution.name.casefold()
            if not assume_selected_is_web and not any(
                marker in normalized_name
                for marker in ("web_search", "internet_search", "联网搜索")
            ):
                continue
            records.extend(cls._web_search_records(execution.output))

        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in records:
            key = (item.get("url", ""), item.get("snippet", ""))
            if key in seen or not any(key):
                continue
            seen.add(key)
            unique.append(item)
        if not unique:
            return "", []

        consensus_address = cls._consensus_chinese_address(unique)
        if consensus_address:
            # Search ranking can occasionally put a content-farm result above
            # several mutually consistent sources.  Prefer records supporting
            # the independently repeated address, while preserving provider
            # order within each group and retaining conflicting records in the
            # evidence payload for auditability.
            unique.sort(
                key=lambda item: consensus_address not in item.get("snippet", "")
            )
        unique = unique[:10]

        lines: list[str] = []
        if consensus_address:
            lines.append(f"多个独立公开来源一致指向：{consensus_address}。")
        for item in unique[:3]:
            title = item.get("title") or "公开网页"
            snippet = item.get("snippet", "")
            line = f"- {title}"
            if snippet:
                line += f"：{snippet}"
            if url := item.get("url"):
                line += f"\n  来源：{url}"
            lines.append(line)
        return "\n".join(lines), unique

    @staticmethod
    def _consensus_chinese_address(records: list[dict[str, str]]) -> str:
        """Return one independently repeated Chinese street address.

        A single search snippet is not evidence of consensus.  We count at
        most one vote per URL host and only return a unique winner repeated by
        at least two independent hosts.  Ties deliberately produce no direct
        claim (for example, an institution with several campuses).
        """
        pattern = re.compile(
            r"(?:^|位于|地址(?:为|是|在|位于|：|:)?|"
            r"位置(?:为|是|在|位于|：|:)?)\s*"
            # Chinese city names are at most five Han characters including
            # the 市 suffix.  The bound prevents a sentence prefix such as
            # “医院地址为上海市” from being captured as part of the address.
            r"([\u4e00-\u9fff]{2,5}市"
            r"[\u4e00-\u9fff]{1,8}(?:区|县|旗)"
            r"[\u4e00-\u9fff0-9]{1,20}(?:大道|路|街|道|巷)"
            r"[0-9]{1,6}(?:-[0-9]{1,6})?号)"
        )
        votes: dict[str, set[str]] = {}
        for index, item in enumerate(records):
            snippet = item.get("snippet", "")
            url = item.get("url", "")
            host_match = re.match(r"^https?://([^/:?#]+)", url, re.I)
            source = (
                host_match.group(1).casefold().removeprefix("www.")
                if host_match
                else f"record-{index}"
            )
            for address in set(pattern.findall(snippet)):
                votes.setdefault(address, set()).add(source)
        ranked = sorted(
            ((len(sources), address) for address, sources in votes.items()),
            reverse=True,
        )
        if not ranked or ranked[0][0] < 2:
            return ""
        if len(ranked) > 1 and ranked[1][0] == ranked[0][0]:
            return ""
        return ranked[0][1]

    @classmethod
    def _web_search_records(cls, output: dict[str, Any]) -> list[dict[str, str]]:
        """Normalize Bocha/New_Agent HTTP output and MCP text responses."""
        code = output.get("code")
        if isinstance(code, int) and code != 200:
            return []

        containers: list[Any] = []
        if isinstance(output.get("value"), list):
            containers.append(output["value"])
        for key in ("result", "data"):
            value = output.get(key)
            if isinstance(value, dict):
                web_pages = value.get("webPages")
                if isinstance(web_pages, dict) and isinstance(web_pages.get("value"), list):
                    containers.append(web_pages["value"])
                if isinstance(value.get("value"), list):
                    containers.append(value["value"])
            elif isinstance(value, list):
                containers.append(value)

        content = output.get("result", {}).get("content") if isinstance(
            output.get("result"), dict
        ) else output.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    containers.append([{"snippet": text}])
                else:
                    if isinstance(decoded, dict):
                        containers.extend(
                            [cls._web_search_records(decoded)]
                        )

        direct_answer = next(
            (
                str(output[key]).strip()
                for key in ("answer", "summary", "text")
                if isinstance(output.get(key), str) and str(output[key]).strip()
            ),
            "",
        )
        if direct_answer:
            containers.append([{
                "title": str(output.get("title") or "联网搜索结果"),
                "snippet": direct_answer,
                "url": str(output.get("url") or ""),
            }])

        records: list[dict[str, str]] = []
        for container in containers:
            if not isinstance(container, list):
                continue
            for item in container:
                if not isinstance(item, dict):
                    continue
                title = cls._bounded_external_text(
                    item.get("name") or item.get("title") or item.get("siteName"), 200
                )
                snippet = cls._bounded_external_text(
                    item.get("summary")
                    or item.get("snippet")
                    or item.get("description")
                    or item.get("content")
                    or item.get("text"),
                    700,
                )
                raw_url = str(item.get("url") or item.get("link") or "").strip()
                url = raw_url[:2048] if re.match(r"^https?://[^\s]+$", raw_url, re.I) else ""
                if title or snippet or url:
                    records.append({"title": title, "snippet": snippet, "url": url})
        return records

    @staticmethod
    def _bounded_external_text(value: Any, limit: int) -> str:
        if value is None:
            return ""
        text = re.sub(r"\s+", " ", str(value)).strip()
        return text[:limit]

    @staticmethod
    def _activity_definition_note(request: CanonicalAnalysisRequest) -> str:
        if (
            "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
            not in request.assumptions
        ):
            return ""
        if request.time_range is None:
            return ""
        end_inclusive = request.time_range.end_exclusive - timedelta(days=1)
        return (
            "活跃口径：本次将 "
            f"{request.time_range.start.isoformat()} 至 {end_inclusive.isoformat()} "
            "期间内存在销售记录的对象定义为活跃对象。"
        )

    async def _direct(
        self,
        request: CanonicalAnalysisRequest,
        chat: ChatRequest | None = None,
    ) -> AgentResponse:
        answers = {
            PrimaryIntent.CAPABILITY_HELP: "我可以进行指标、明细、趋势、对比、占比、异常、归因、预测和报表分析。",
            PrimaryIntent.OUT_OF_SCOPE: "该请求超出只读数据分析范围。",
        }
        model_used = False
        if request.primary_intent == PrimaryIntent.CHAT:
            answer = "当然可以呀。你想聊点什么？"
            if self.chat_responder is not None:
                try:
                    answer = await self.chat_responder.respond(
                        request.original_question,
                        history=(
                            [
                                {"role": item.role, "content": item.content}
                                for item in chat.history
                            ]
                            if chat is not None
                            else None
                        ),
                    )
                    model_used = True
                except Exception as exc:
                    logger.warning("controlled chat model unavailable: %s", exc)
        else:
            answer = answers[request.primary_intent]
        return AgentResponse(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            status="COMPLETED",
            intent=request.primary_intent,
            intent_source=request.intent_source,
            intent_confidence=request.intent_confidence,
            answer=answer,
            reliability=ReliabilityReport(
                level="HIGH", score=1,
                gates={
                    "no_data_claim": True,
                    "controlled_chat_model": model_used,
                },
            ),
        )

    @staticmethod
    def _source_watermark_payload(
        request: CanonicalAnalysisRequest, dataset: Dataset
    ) -> dict[str, Any]:
        """Expose trusted business freshness separately from query snapshot time."""
        if dataset.source_data_as_of is None or dataset.source_watermark_field is None:
            return {}
        watermark_day = (
            dataset.source_data_as_of.date()
            if isinstance(dataset.source_data_as_of, datetime)
            else dataset.source_data_as_of
        )
        coverage = "NOT_REQUESTED"
        if request.time_range is not None:
            if request.time_range.start > watermark_day:
                coverage = "OUTSIDE_SOURCE_WATERMARK"
            elif request.time_range.end_exclusive > watermark_day + timedelta(days=1):
                coverage = "PARTIAL_AFTER_SOURCE_WATERMARK"
            else:
                coverage = "COMPLETE_THROUGH_SOURCE_WATERMARK"
        return {
            "source_data_as_of": dataset.source_data_as_of.isoformat(),
            "source_watermark_field": dataset.source_watermark_field,
            "requested_time_coverage": coverage,
        }

    @classmethod
    def _source_watermark_note(
        cls, request: CanonicalAnalysisRequest, dataset: Dataset
    ) -> str:
        payload = cls._source_watermark_payload(request, dataset)
        if not payload:
            return ""
        note = (
            f"数据水位：当前业务数据截至 {payload['source_data_as_of']}"
            "（按销售记录时间统计）。"
            "查询快照时间仅表示本次读取时间，不代表业务数据更新时间。"
        )
        coverage = payload["requested_time_coverage"]
        if coverage == "OUTSIDE_SOURCE_WATERMARK":
            note += "所请求的时间段完全晚于该水位，空结果不能解释为业务没有发生。"
        elif coverage == "PARTIAL_AFTER_SOURCE_WATERMARK":
            note += "所请求范围延伸至该水位之后，水位后的日期未被当前数据覆盖。"
        return note

    @staticmethod
    def _reliability(request: CanonicalAnalysisRequest, evidence: list[EvidenceItem], quality_status: str) -> ReliabilityReport:
        immutable_dataset_followup = request.source_dataset_id is not None
        metric_validation_required = (
            request.primary_intent != PrimaryIntent.DETAIL_QUERY
            and bool(request.metrics)
            and not immutable_dataset_followup
        )
        def derived_metric_verified(metric: MetricRef) -> bool:
            names = {
                str(metric.input or "").strip(),
                str(metric.canonical_name or "").strip(),
            }
            return any(
                item.kind == "DERIVED_METRIC_RESOLUTION"
                and item.payload.get("verified") is True
                and str(item.payload.get("metric_name") or "").strip() in names
                for item in evidence
            )

        gates = {
            "query_succeeded": any(e.kind == "QUERY_RESULT" for e in evidence),
            "metric_bound": not metric_validation_required or all(
                (m.metric_id and m.version) or derived_metric_verified(m)
                for m in request.metrics
            ),
            "semantic_metric_verified": not metric_validation_required or all(
                any(
                    e.kind == "SEMANTIC_METRIC_RESOLUTION"
                    and e.payload.get("metric_id") == m.metric_id
                    and e.payload.get("verified") is True
                    for e in evidence
                )
                or derived_metric_verified(m)
                for m in request.metrics
            ),
        }
        derived_metrics = [
            metric for metric in request.metrics if derived_metric_verified(metric)
        ]
        if derived_metrics:
            gates["derived_metric_contract_verified"] = (
                len(derived_metrics) == len(request.metrics)
            )
        if immutable_dataset_followup:
            gates["immutable_source_dataset_selected"] = True
        if _requires_deterministic_analysis(request):
            gates["analysis_succeeded"] = any(e.kind == "ANALYSIS_RESULT" for e in evidence)
        score = sum(gates.values()) / len(gates)
        warnings = [] if quality_status == "PASS" else [f"上游数据质量状态为 {quality_status}，结论需要复核。"]
        relationship_count_metrics = {
            "已合作医院数", "已合作经销商数", "已合作供应商数",
            "已合作客户数", "已合作门店数",
        }
        source_coverage_required = request.time_range is not None or (
            any(
                assumption in {
                    "TIME_SCOPE=ALL_TIME",
                    "TIME_SCOPE=ALL_AVAILABLE_HISTORY",
                }
                for assumption in request.assumptions
            )
            and any(
                (metric.canonical_name or metric.input)
                in relationship_count_metrics
                for metric in request.metrics
            )
        )
        if source_coverage_required and not immutable_dataset_followup:
            query_evidence = [item for item in evidence if item.kind == "QUERY_RESULT"]
            source_coverages = [
                str(item.payload.get("requested_time_coverage"))
                for item in query_evidence
                if item.payload.get("source_data_as_of")
                and item.payload.get("source_watermark_field")
            ]
            if source_coverages:
                gates["source_watermark_verified"] = True
                if any(
                    value in {
                        "OUTSIDE_SOURCE_WATERMARK",
                        "PARTIAL_AFTER_SOURCE_WATERMARK",
                    }
                    for value in source_coverages
                ):
                    warnings.append(
                        "请求时间范围未被当前业务数据水位完整覆盖，不能据此判断水位后的业务事实。"
                    )
            else:
                warnings.append(
                    "上游未提供可验证的业务数据水位，本次无法确认请求时间范围的数据覆盖完整性。"
                )
        for item in evidence:
            if item.kind == "ANALYSIS_RESULT":
                warnings.extend(
                    str(value) for value in item.payload.get("warnings", []) if value
                )
        if "LONG_TERM_MEMORY_UNAVAILABLE" in request.assumptions:
            warnings.append("长期偏好服务暂时不可用，本次仅依据当前问题和短期会话回答。")
        warnings = list(dict.fromkeys(warnings))
        return ReliabilityReport(level="HIGH" if score == 1 and not warnings else "LIMITED" if score >= 2 / 3 else "FAIL", score=score, gates=gates, warnings=warnings)

    @staticmethod
    def _dependency_message(exc: AdapterError) -> str:
        known = {
            "SEMANTIC_CONTEXT_MISSING": "缺少 semantic_model_id，暂时无法确定使用哪套语义模型。business_domain_id 可不传，由语义模型自动选择业务域。",
            "ASL_GENERATION_FAILED": "自然语言转 ASL 服务暂时不可用。",
            "ASL_ANALYSIS_SHAPE_INVALID": "语义查询没有返回分析所需的分组维度，本次未执行可能产生误导的单值分析。",
            "SQL_TRANSLATION_FAILED": "ASL 转 SQL 服务未能生成可执行查询。",
            "SQL_EXECUTION_FAILED": "SQL 查询执行失败，本次不返回数据。",
            "SQL_TRANSLATION_ENDPOINT_UNAVAILABLE": "SQL服务尚未部署独立翻译接口，请先发布或重启新版SQL Translator。",
            "SQL_EXECUTION_ENDPOINT_UNAVAILABLE": "SQL服务尚未部署独立执行接口，请先发布或重启新版SQL Translator。",
            "ASL_TIME_ANCHOR_INVALID": "当前语义模型缺少有效的时间字段绑定，请联系管理员完善语义配置后重试。",
            "ASL_METRIC_SELECTION_INVALID": "当前问题匹配到的指标口径存在冲突，请联系管理员检查指标名称和别名配置。",
            "ASL_DIMENSION_INVALID": "当前分析维度没有完成有效字段映射，请联系管理员完善语义配置。",
            "ASL_SCOPE_INVALID": "当前语义模型与业务域配置不一致，请联系管理员检查应用绑定关系。",
            "ASL_ENTITY_MENTION_UNRESOLVED": (
                "当前问题中的业务名称无法在最新发布的语义模型和数据目录中唯一匹配。"
                "请确认名称或补充它属于产品、品牌、医院、经销商还是厂家。"
            ),
            "ASL_FILTER_INVALID": (
                "当前筛选条件无法唯一绑定到可执行的语义字段或关系路径，"
                "请检查语义模型中的字段角色和实体关系配置。"
            ),
            "ASL_REQUIRED_FILTER_MISSING": "语义查询未保留当前问题要求的筛选条件，本次未执行可能扩大范围的查询。",
            "ASL_REQUIRED_DIMENSION_MISSING": "语义查询未保留当前问题要求的分组维度，本次未执行不完整查询。",
            "ASL_DETAIL_FIELDS_INCOMPLETE": "语义查询未返回用户明确要求的全部明细字段。",
            "ASL_DETAIL_PROJECTION_MISSING": "当前语义模型无法唯一确定所请求明细字段的投影。",
            "ASL_GROUPING_DIMENSION_MISSING": "当前语义模型无法唯一确定所请求的分组维度。",
            "RELATIONSHIP_COUNT_PROJECTION_INVALID": (
                "合作对象数量查询未返回可识别的名称投影，本次不输出可能错误的数量。"
            ),
            "RELATIONSHIP_COUNT_SOURCE_INCOMPLETE": (
                "合作对象明细结果已截断，且上游未提供经过确认的去重总数，"
                "本次不使用预览行数代替完整数量。"
            ),
        }
        diagnostic_code = exc.upstream_code or exc.code
        if diagnostic_code in known:
            return known[diagnostic_code]
        if exc.status_code in {401, 403}:
            return "上游数据服务拒绝了当前可信身份，本次不返回数据。"
        if exc.status_code == 404:
            return "上游数据服务没有找到请求的业务资源。"
        if exc.status_code == 409:
            return "上游数据状态发生冲突，请刷新后重试。"
        if exc.status_code == 422:
            return "上游数据服务无法按当前条件完成查询，请调整条件后重试。"
        safe_code = diagnostic_code
        return f"上游数据服务暂时不可用（{safe_code}），请稍后重试。"

    @staticmethod
    def _analysis_contract_requirements(
        exc: AdapterError,
    ) -> list[AnalysisRequirement]:
        details = exc.details if isinstance(exc.details, dict) else {}
        violations = details.get("violations")
        if not isinstance(violations, dict):
            return []
        requirements: list[AnalysisRequirement] = []
        for role in violations.get("missing_roles") or []:
            requirements.append(AnalysisRequirement(
                code=f"missing_analysis_column:{role}",
                category="DATA",
                description=f"分析结果缺少语义角色“{role}”对应的数据列",
                action="在语义模型中绑定该字段，并让查询按分析契约返回",
            ))
        for role, columns in (violations.get("ambiguous_roles") or {}).items():
            requirements.append(AnalysisRequirement(
                code=f"ambiguous_analysis_column:{role}",
                category="DATA",
                description=f"语义角色“{role}”同时匹配多列：{'、'.join(map(str, columns))}",
                action="通过字段别名或ASL投影保留唯一对应列",
            ))
        for role in violations.get("invalid_numeric_roles") or []:
            requirements.append(AnalysisRequirement(
                code=f"invalid_analysis_numeric:{role}",
                category="DATA",
                description=f"分析字段“{role}”包含空值或非有限数值",
                action="修复数据类型、空值或非法数值后重试",
            ))
        if violations.get("row_count_error"):
            requirements.append(AnalysisRequirement(
                code="analysis_row_count_invalid",
                category="DATA",
                description=f"分析结果行数不满足要求：{violations['row_count_error']}",
                action="调整分组粒度或查询范围以满足分析样本要求",
            ))
        return requirements[:20]

    @staticmethod
    def _fallback(request: CanonicalAnalysisRequest, reason: str) -> AgentResponse:
        return AgentResponse(request_id=request.request_id, conversation_id=request.conversation_id, status="SAFE_FALLBACK", intent=request.primary_intent, intent_source=request.intent_source, intent_confidence=request.intent_confidence, answer=reason, reliability=ReliabilityReport(level="FAIL", score=0, gates={"safe_termination": True}, warnings=[reason]))
