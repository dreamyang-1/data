"""SPRINT-20260917-C-SURFACE-PLANNER-BYPASS 原始契约的后续修正。

历史上 V2 surface 直通证明自足单任务时允许跳过 ``v1.task_decomposition``
模型调用。任务规划边界改造后，任务边界、任务意图、结构化参数全部由拆分
模型一次产出，而 surface 直通兜底决策不携带任务边界（复合问题同样只是
单个兜底任务），跳过拆分器会导致多问题不再拆分、参数提取行消失。
因此新契约：除 V2 ACCEPTED 授权计划（semantic_decision_ready）外，所有
新问统一走拆分器；V1 意图模型行为不变，规划节点保持可见。
"""

import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import ChatRequest, TrustedIdentity
from app.domain.semantic_decision import (
    SemanticDecision,
    SemanticDecisionSource,
    SemanticDecisionTask,
    SemanticScopeProof,
)
from app.intent import RuleBasedIntentClassifier
from app.observability.call_timing import RequestTimingTracker, timing_scope
from app.planning import MultiQuestionPlanner
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import progress_scope
from app.services.question_rewriter import QuestionRewriter
from app.stores import InMemorySessionStore

IDENTITY = TrustedIdentity(tenant_id="t1", user_id="u1")
QUESTION = "查询空心纤维血液透析器产品合作的经销商名单。"


class _CountingClassifier(RuleBasedIntentClassifier):
    """Async classify counts V1 intent-model invocations exactly once.

    ``rules`` is deliberately not self-referential: the orchestrator's
    deterministic preflight (``_classify_with_rules``) must build its own
    rule classifier and never touch this counter.
    """

    def __init__(self):
        super().__init__()
        self.model_calls = 0

    async def classify(self, question, identity, conversation_id, **kwargs):
        self.model_calls += 1
        return super().classify(question, identity, conversation_id, **kwargs)


class _CountingPlanner(MultiQuestionPlanner):
    def __init__(self, settings):
        super().__init__(settings)
        self.calls = 0

    async def plan(self, question):
        self.calls += 1
        return await super().plan(question)


def _orchestrator():
    settings = Settings(
        _env_file=None,
        env="test",
        adapter_mode="mock",
        intent_model_enabled=True,
        multi_question_enabled=True,
        multi_question_model_enabled=False,
        analysis_synthesis_enabled=False,
    )
    classifier = _CountingClassifier()
    planner = _CountingPlanner(settings)
    orchestrator = DataAnalysisOrchestrator(
        settings=settings,
        classifier=classifier,
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        task_planner=planner,
        question_rewriter=QuestionRewriter(None),
    )
    return orchestrator, classifier, planner


def _chat(question=QUESTION, *, message_id="m1", conversation_id="conv"):
    return ChatRequest(
        application_id="app",
        conversation_id=conversation_id,
        message_id=message_id,
        question=question,
        semantic_model_id=81,
        business_domain_ids=[205],
        database_id=58,
        knowledge_base_names=["business-kb"],
        department="ORG_ADMIN",
        history=[],
    )


def _surface_decision(chat, *, conversation_state="NEW_TASK", task_count=1):
    scope = chat.authorized_semantic_scope
    return SemanticDecision(
        source=SemanticDecisionSource.V1_SEMANTIC_FALLBACK,
        status="REQUIRES_V1_FALLBACK",
        message_id=chat.message_id,
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        conversation_state=conversation_state,
        original_question=chat.question,
        completed_question=chat.question,
        tasks=tuple(
            SemanticDecisionTask(
                task_id=f"task-{index}",
                question=chat.question,
                semantic_complete=False,
                adaptation_reason="V2_CONTEXT_ONLY_REQUIRES_V1_SEMANTICS",
            )
            for index in range(task_count)
        ),
        scope_proof=SemanticScopeProof(
            authorized_scope=scope,
            authorized_scope_fingerprint=scope.fingerprint(),
        ),
        fallback_reason="V2_CONTEXT_ONLY_REQUIRES_V1_SEMANTICS",
    )


@pytest.mark.asyncio
async def test_surface_fallback_decision_still_runs_planner():
    orchestrator, classifier, planner = _orchestrator()
    chat = _chat()
    chat._completed_question_execution = True
    chat._semantic_decision = _surface_decision(chat)

    progress_events: list[tuple[str, str]] = []

    async def capture_progress(event):
        progress_events.append((event["stage"], event.get("status")))

    tracker = RequestTimingTracker(runtime_mode="V2_CONTEXT_V1_EXECUTION")
    with progress_scope(capture_progress), timing_scope(tracker):
        response = await orchestrator.handle(chat, IDENTITY)

    assert response.status == "COMPLETED"
    # 兜底决策不带任务边界，拆分器必须照常运行（拆分/意图/参数由它产出）。
    assert planner.calls == 1
    operations = [item.operation for item in tracker.finish(response.status).operations]
    assert "v1.task_decomposition" in operations
    # The V1 intent model still runs exactly once.
    assert operations.count("v1.intent_recognition") == 1
    assert classifier.model_calls == 1
    # The user-facing planning node stays visible.
    planning_states = [status for stage, status in progress_events
                       if stage == "TASK_PLANNING"]
    assert planning_states == ["RUNNING", "COMPLETED"]


@pytest.mark.asyncio
async def test_compound_question_without_surface_evidence_still_plans():
    orchestrator, classifier, planner = _orchestrator()
    chat = _chat(question="查询经销商名单并统计销售额。")

    response = await orchestrator.handle(chat, IDENTITY)

    assert response.status == "COMPLETED"
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_context_followup_decision_keeps_original_planning():
    orchestrator, classifier, planner = _orchestrator()
    chat = _chat()
    chat._completed_question_execution = True
    chat._semantic_decision = _surface_decision(
        chat, conversation_state="CONTEXT_RESOLVED"
    )

    response = await orchestrator.handle(chat, IDENTITY)

    assert response.status == "COMPLETED"
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_multi_task_surface_evidence_never_skips_planning():
    orchestrator, classifier, planner = _orchestrator()
    chat = _chat()
    chat._completed_question_execution = True
    chat._semantic_decision = _surface_decision(chat, task_count=2)

    response = await orchestrator.handle(chat, IDENTITY)

    assert response.status == "COMPLETED"
    assert planner.calls == 1
