from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import (
    AtomicTask,
    ChatRequest,
    PrimaryIntent,
    TaskPlan,
    TrustedIdentity,
)
from app.domain.semantic_decision import (
    SemanticDecision,
    SemanticDecisionField,
    SemanticDecisionFilter,
    SemanticDecisionSource,
    SemanticDecisionTask,
    SemanticDecisionTimeRange,
    SemanticScopeProof,
)
from app.intent import RuleBasedIntentClassifier
from app.observability.call_timing import RequestTimingTracker, timing_scope
from app.planning import PlannerOutcome
from app.services import DataAnalysisOrchestrator
from app.services.question_rewriter import QuestionRewriter
from app.services.semantic_decision import (
    canonical_request_from_semantic_decision,
    semantic_decision_for_task_plan,
)
from app.stores import InMemorySessionStore


IDENTITY = TrustedIdentity(tenant_id="tenant", user_id="user")


def _chat(*, question: str = "分析2026年销售额趋势") -> ChatRequest:
    return ChatRequest(
        conversation_id="semantic-contract-conversation",
        message_id="semantic-contract-message",
        question=question,
        application_id="app",
        semantic_model_id=81,
        business_domain_ids=[205],
    )


def _proof(chat: ChatRequest) -> SemanticScopeProof:
    scope = chat.authorized_semantic_scope
    return SemanticScopeProof(
        authorized_scope=scope,
        authorized_scope_fingerprint=scope.fingerprint(),
        catalog_version="catalog-v1",
        semantic_model_version="catalog-v1",
    )


def _accepted_decision(chat: ChatRequest) -> SemanticDecision:
    metric = SemanticDecisionField(
        catalog_type="METRIC",
        canonical_id="metric-sales",
        canonical_code="sales_amount",
        display_name="销售额",
        semantic_model_id="81",
        catalog_version="catalog-v1",
        business_domain_ids=("205",),
        resolution_source="PINNED_CATALOG",
    )
    zone = ZoneInfo("Asia/Shanghai")
    task = SemanticDecisionTask(
        task_id="task-1",
        question=chat.question,
        intent=PrimaryIntent.TREND_ANALYSIS,
        intent_confidence=1.0,
        payload_type="TIME_SERIES",
        metrics=(metric,),
        time_range=SemanticDecisionTimeRange(
            start=datetime(2026, 1, 1, tzinfo=zone),
            end_exclusive=datetime(2027, 1, 1, tzinfo=zone),
            grain="MONTH",
            source="USER_EXPLICIT",
        ),
        semantic_complete=True,
    )
    return SemanticDecision(
        source=SemanticDecisionSource.V2_AUTHORIZED_PLAN,
        status="ACCEPTED",
        message_id=chat.message_id,
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        conversation_state="NEW_TASK",
        original_question=chat.question,
        completed_question=chat.question,
        tasks=(task,),
        scope_proof=_proof(chat),
    )


def _fallback_decision(chat: ChatRequest, reason: str) -> SemanticDecision:
    return SemanticDecision(
        source=SemanticDecisionSource.V1_SEMANTIC_FALLBACK,
        status="REQUIRES_V1_FALLBACK",
        message_id=chat.message_id,
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        conversation_state="NEW_TASK",
        original_question=chat.question,
        completed_question=chat.question,
        tasks=(SemanticDecisionTask(
            task_id="task-1",
            question=chat.question,
            semantic_complete=False,
            adaptation_reason=reason,
        ),),
        scope_proof=_proof(chat),
        fallback_reason=reason,
    )


def test_semantic_decision_is_private_and_cannot_be_supplied_by_transport():
    chat = _chat()
    chat._semantic_decision = _accepted_decision(chat)

    assert "_semantic_decision" not in chat.model_dump(mode="json")
    with pytest.raises(ValueError):
        ChatRequest.model_validate({
            **chat.model_dump(mode="json"),
            "_semantic_decision": _accepted_decision(chat).model_dump(mode="json"),
        })


def test_authorized_contract_materializes_complete_v1_request_without_model():
    chat = _chat()
    decision = _accepted_decision(chat)

    request, reason = canonical_request_from_semantic_decision(
        decision,
        chat=chat,
        identity=IDENTITY,
        rules=RuleBasedIntentClassifier(),
    )

    assert reason is None
    assert request is not None
    assert request.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert request.intent_source == "V2_SEMANTIC_DECISION"
    assert request.metrics[0].metric_id == "81:sales_amount"
    assert request.metrics[0].canonical_name == "销售额"
    assert request.time_range.start.isoformat() == "2026-01-01"
    assert request.time_range.end_exclusive.isoformat() == "2027-01-01"
    assert "DEFAULT_TIME_GRANULARITY=month" in request.assumptions
    assert request.authorized_semantic_scope == chat.authorized_semantic_scope
    assert request.missing_slots == []


def test_scope_mismatch_rejects_contract_and_requests_v1_fallback():
    chat = _chat()
    decision = _accepted_decision(chat)
    changed_scope = chat.model_copy(update={"business_domain_ids": [206]})

    request, reason = canonical_request_from_semantic_decision(
        decision,
        chat=changed_scope,
        identity=IDENTITY,
        rules=RuleBasedIntentClassifier(),
    )

    assert request is None
    assert reason == "SEMANTIC_DECISION_SCOPE_MISMATCH"


def test_authorized_entity_value_filter_keeps_v2_binding_proof_for_v1():
    chat = _chat(question="统计上海市2026年销售额")
    decision = _accepted_decision(chat)
    field = SemanticDecisionField(
        catalog_type="ATTRIBUTE",
        canonical_id="attribute-city-name",
        canonical_code="dim_city.city_name",
        display_name="城市",
        semantic_model_id="81",
        catalog_version="catalog-v1",
        business_domain_ids=("205",),
        resolution_source="PINNED_CATALOG",
    )
    value = SemanticDecisionField(
        catalog_type="ENTITY_VALUE",
        canonical_id="source-value-shanghai",
        canonical_code="source-value-shanghai",
        display_name="上海市",
        semantic_model_id="81",
        catalog_version="catalog-v1",
        business_domain_ids=("205",),
        resolution_source="VERIFIED_SOURCE_EXACT_LOOKUP",
    )
    semantic_filter = SemanticDecisionFilter(
        field=field,
        operator="EQ",
        value="上海市",
        value_type="ENTITY_REF",
        source="USER_EXPLICIT",
        value_refs=(value,),
    )
    task = decision.tasks[0].model_copy(update={
        "question": chat.question,
        "filters": (semantic_filter,),
    })
    decision = decision.model_copy(update={
        "original_question": chat.question,
        "completed_question": chat.question,
        "tasks": (task,),
    })

    request, reason = canonical_request_from_semantic_decision(
        decision,
        chat=chat,
        identity=IDENTITY,
        rules=RuleBasedIntentClassifier(),
    )

    assert reason is None
    assert request.filters == [
        {"field": "城市", "operator": "EQ", "value": "上海市"}
    ]
    assert len(request.semantic_filter_bindings) == 1
    binding = request.semantic_filter_bindings[0]
    assert binding.attribute_code == "dim_city.city_name"
    assert binding.record_id == "source-value-shanghai"
    assert binding.business_domain_id == 205
    assert binding.semantic_model_version == "catalog-v1"


@pytest.mark.asyncio
async def test_orchestrator_skips_v1_intent_model_and_rewriter_for_accepted_contract():
    class ModelTrapClassifier(RuleBasedIntentClassifier):
        def __init__(self):
            self.rules = RuleBasedIntentClassifier()
            self.model_calls = 0

        async def classify(self, *args, **kwargs):
            self.model_calls += 1
            raise AssertionError("accepted semantic contract must skip V1 intent model")

    class RewriteTrap(QuestionRewriter):
        async def rewrite(self, *args, **kwargs):
            raise AssertionError("accepted semantic contract must skip V1 rewrite")

        async def ground_executable_filters(self, *args, **kwargs):
            raise AssertionError(
                "accepted semantic contract must skip V1 filter re-grounding"
            )

    class PlannerTrap:
        async def plan(self, *_args, **_kwargs):
            raise AssertionError(
                "accepted single-task contract must skip V1 task decomposition"
            )

    chat = _chat()
    chat._completed_question_execution = True
    chat._semantic_decision = _accepted_decision(chat)
    classifier = ModelTrapClassifier()
    orchestrator = DataAnalysisOrchestrator(
        settings=Settings(
            _env_file=None,
            env="test",
            adapter_mode="mock",
            intent_model_enabled=True,
            multi_question_enabled=True,
        ),
        classifier=classifier,
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        question_rewriter=RewriteTrap(None),
        task_planner=PlannerTrap(),
    )

    tracker = RequestTimingTracker(runtime_mode="V2_CONTEXT_V1_EXECUTION")
    with timing_scope(tracker):
        response = await orchestrator.handle(chat, IDENTITY)
    trace = tracker.finish(response.status)

    assert response.status in {"COMPLETED", "PARTIAL_SUCCESS"}
    assert response.intent == PrimaryIntent.TREND_ANALYSIS
    assert response.intent_source == "V2_SEMANTIC_DECISION"
    assert classifier.model_calls == 0
    operations = [item.operation for item in trace.operations]
    assert "semantic.contract.validation" in operations
    assert "v1.task_decomposition" not in operations
    assert "v1.intent_recognition" not in operations
    validation = next(
        item for item in trace.operations
        if item.operation == "semantic.contract.validation"
    )
    assert validation.attributes["accepted"] is True


@pytest.mark.asyncio
async def test_orchestrator_explicit_fallback_keeps_v1_intent_model():
    class CountingClassifier(RuleBasedIntentClassifier):
        def __init__(self):
            self.model_calls = 0

        async def classify(self, question, identity, conversation_id):
            self.model_calls += 1
            return super().classify(question, identity, conversation_id)

    chat = _chat(question="统计2026年销售额")
    chat._completed_question_execution = True
    chat._semantic_decision = _fallback_decision(
        chat, "V2_MODEL_DYNAMIC_SCHEMA_VIOLATION"
    )
    classifier = CountingClassifier()
    orchestrator = DataAnalysisOrchestrator(
        settings=Settings(
            _env_file=None,
            env="test",
            adapter_mode="mock",
            intent_model_enabled=True,
            multi_question_enabled=False,
        ),
        classifier=classifier,
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )

    tracker = RequestTimingTracker(runtime_mode="V2_CONTEXT_V1_EXECUTION")
    with timing_scope(tracker):
        response = await orchestrator.handle(chat, IDENTITY)
    trace = tracker.finish(response.status)

    assert response.status in {"COMPLETED", "PARTIAL_SUCCESS"}
    assert classifier.model_calls == 1
    assert response.intent_source != "V2_SEMANTIC_DECISION"
    operations = [item.operation for item in trace.operations]
    assert "semantic.contract.validation" in operations
    assert "v1.intent_recognition" in operations
    validation = next(
        item for item in trace.operations
        if item.operation == "semantic.contract.validation"
    )
    assert validation.attributes["accepted"] is False
    assert validation.attributes["fallback_reason"] == (
        "V2_MODEL_DYNAMIC_SCHEMA_VIOLATION"
    )


@pytest.mark.asyncio
async def test_pre_resolution_contract_cannot_skip_v1_task_decomposition():
    class CountingPlanner:
        def __init__(self):
            self.calls = 0

        async def plan(self, *_args, **_kwargs):
            self.calls += 1
            return PlannerOutcome(plan=None)

    chat = _chat()
    chat._semantic_decision = _accepted_decision(chat)
    planner = CountingPlanner()
    orchestrator = DataAnalysisOrchestrator(
        settings=Settings(
            _env_file=None,
            env="test",
            adapter_mode="mock",
            intent_model_enabled=False,
            multi_question_enabled=True,
        ),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        task_planner=planner,
    )

    tracker = RequestTimingTracker(runtime_mode="V2_CONTEXT_V1_EXECUTION")
    with timing_scope(tracker):
        response = await orchestrator.handle(chat, IDENTITY)
    trace = tracker.finish(response.status)

    assert planner.calls == 1
    validation = next(
        item for item in trace.operations
        if item.operation == "semantic.contract.validation"
    )
    assert validation.attributes["fallback_reason"] == (
        "SEMANTIC_DECISION_PRE_RESOLUTION_REQUIRED"
    )


def test_complete_contract_rejects_unbound_semantic_field():
    chat = _chat()
    with pytest.raises(ValueError, match="cannot contain unbound fields"):
        SemanticDecisionTask(
            task_id="task-1",
            question=chat.question,
            intent=PrimaryIntent.METRIC_QUERY,
            metrics=(SemanticDecisionField(
                catalog_type="METRIC",
                display_name="销售额",
                binding_status="UNBOUND",
            ),),
            semantic_complete=True,
        )


def test_accepted_contract_rejects_field_from_another_model():
    chat = _chat()
    task = _accepted_decision(chat).tasks[0]
    foreign = task.metrics[0].model_copy(update={"semantic_model_id": "82"})
    with pytest.raises(ValueError, match="belongs to another model"):
        SemanticDecision(
            source=SemanticDecisionSource.V2_AUTHORIZED_PLAN,
            status="ACCEPTED",
            message_id=chat.message_id,
            conversation_id=chat.conversation_id,
            application_id=chat.application_id,
            conversation_state="NEW_TASK",
            original_question=chat.question,
            completed_question=chat.question,
            tasks=(task.model_copy(update={"metrics": (foreign,)}),),
            scope_proof=_proof(chat),
        )


def test_multi_task_contract_records_intents_parameters_and_dependencies_as_fallback():
    chat = _chat(question="查询经销商名单，再根据名单查询合作医院")
    chat._semantic_decision = _accepted_decision(chat)
    plan = TaskPlan(
        planner="STRUCTURED_MODEL",
        tasks=[
            AtomicTask(task_id="task-1", question="查询经销商名单"),
            AtomicTask(
                task_id="task-2",
                question="根据经销商名单查询合作医院",
                depends_on=["task-1"],
            ),
        ],
    )
    rules = RuleBasedIntentClassifier()
    preliminary = [
        rules.classify(task.question, IDENTITY, chat.conversation_id)
        for task in plan.tasks
    ]

    decision = semantic_decision_for_task_plan(
        chat=chat,
        plan=plan,
        preliminary_requests=preliminary,
    )

    assert decision.status == "REQUIRES_V1_FALLBACK"
    assert decision.source == SemanticDecisionSource.V1_SEMANTIC_FALLBACK
    assert [task.intent for task in decision.tasks] == [
        request.primary_intent for request in preliminary
    ]
    assert decision.tasks[1].depends_on == ("task-1",)
    assert decision.fallback_reason == (
        "MULTI_TASK_REQUIRES_V1_SEMANTIC_CLASSIFICATION"
    )
    assert not decision.can_skip_v1_intent_model
