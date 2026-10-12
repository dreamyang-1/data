from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from app.adapters import build_mock_adapters
from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ChatRequest,
    ContextMode,
    CurrentTurnFacts,
    DependencyConstraint,
    MetricRef,
    PlannerExtraction,
    PrimaryIntent,
    SlotProvenance,
    SlotSource,
    TimeRange,
    TurnAdmissionDecision,
    TurnRelation,
    TrustedIdentity,
)
from app.intent.classifier import RuleBasedIntentClassifier
from app.services.intent_asl_contract import (
    build_intent_asl_contract,
    validate_intent_asl_contract_completeness,
    validate_intent_asl_contract_definition,
)
from app.services.orchestrator import DataAnalysisOrchestrator, _INTERNAL_ASSUMPTIONS
from app.services.turn_admission import TurnAdmissionGate
from app.stores import InMemorySessionStore


HOSPITAL = "\u533b\u9662"
PRODUCT = "\u5546\u54c1"
DEALER = "\u7ecf\u9500\u5546"
HOSPITAL_NAME = "\u533b\u9662\u540d\u79f0"
PRODUCT_NAME = "\u5546\u54c1\u540d\u79f0"
DEALER_NAME = "\u7ecf\u9500\u5546\u540d\u79f0"
PRODUCT_SPEC = "\u89c4\u683c\u578b\u53f7"
SALESPERSON_NAME = "\u9500\u552e\u5458\u59d3\u540d"
HOSPITAL_VALUE = "\u590d\u65e6\u5927\u5b66\u9644\u5c5e\u534e\u5c71\u533b\u9662"
SALESPERSON_VALUE = "\u533b\u9662\u4f9b\u5e94\u94fe\u4e1a\u52a1\u4e13\u5458"


CASES = [
    ("\u6839\u636e\u4e0a\u8ff0\u4efb\u52a1\u8fd4\u56de\u7684\u9500\u552e\u5458\uff0c\u67e5\u8be22025\u5e74\u8be5\u9500\u552e\u5458\u5728\u590d\u65e6\u5927\u5b66\u9644\u5c5e\u534e\u5c71\u533b\u9662\u9500\u552e\u7684\u4ea7\u54c1\u660e\u7ec6\u6e05\u5355\u3002", HOSPITAL, PRODUCT, PRODUCT_NAME,
     HOSPITAL, HOSPITAL_NAME, HOSPITAL_VALUE, True),
    ("\u6839\u636e\u4e0a\u8ff0\u4efb\u52a1\u8fd4\u56de\u7684\u9500\u552e\u5458\uff0c\u67e5\u8be2\u5176\u8d1f\u8d23\u7684\u7ecf\u9500\u5546\u540d\u5355\u3002", DEALER, DEALER, DEALER_NAME,
     "\u9500\u552e\u5458", SALESPERSON_NAME, SALESPERSON_VALUE, False),
    ("\u6839\u636e\u4e0a\u8ff0\u4efb\u52a1\u8fd4\u56de\u7684\u9500\u552e\u5458\uff0c\u67e5\u8be2\u5176\u5408\u4f5c\u533b\u9662\u540d\u5355\u3002", HOSPITAL, HOSPITAL, HOSPITAL_NAME,
     "\u9500\u552e\u5458", SALESPERSON_NAME, SALESPERSON_VALUE, False),
    ("\u6839\u636e\u4e0a\u8ff0\u4efb\u52a1\u8fd4\u56de\u7684\u9500\u552e\u5458\uff0c\u67e5\u8be2\u5176\u9500\u552e\u4ea7\u54c1\u7684\u89c4\u683c\u578b\u53f7\u3002", PRODUCT, PRODUCT, PRODUCT_SPEC,
     "\u9500\u552e\u5458", SALESPERSON_NAME, SALESPERSON_VALUE, False),
    ("\u67e5\u8be22025\u5e74\u8be5\u533b\u9662\u9500\u552e\u4ea7\u54c1\u7684\u89c4\u683c\u578b\u53f7\u3002", None, PRODUCT, PRODUCT_SPEC,
     HOSPITAL, HOSPITAL_NAME, HOSPITAL_VALUE, True),
]


@pytest.mark.parametrize(
    "question,query_object,owner,field,filter_entity,filter_field,filter_value,has_year",
    CASES,
)
def test_dependent_query_final_shape_replaces_parent_before_asl(
    question, query_object, owner, field, filter_entity, filter_field, filter_value, has_year
):
    current_filter = {
        "entity": filter_entity,
        "field": filter_field,
        "operator": "EQ",
        "value": filter_value,
    }
    explicit_slots = {
        "analysis_type": SlotProvenance(
            value=PrimaryIntent.DETAIL_QUERY.value,
            source=SlotSource.CURRENT_EXPLICIT,
        ),
        "projection": SlotProvenance(
            value=[field], source=SlotSource.CURRENT_EXPLICIT
        ),
        "filters": SlotProvenance(
            value=[current_filter], source=SlotSource.CURRENT_EXPLICIT
        ),
    }
    if query_object is not None:
        explicit_slots["query_object"] = SlotProvenance(
            value=query_object,
            source=SlotSource.CURRENT_EXPLICIT,
        )
    request = CanonicalAnalysisRequest(
        conversation_id="dag-task-2",
        tenant_id="tenant",
        user_id="user",
        original_question="parent ranking; child query: " + question,
        rewritten_question="metric parent_metric; salesperson ranking; top 1",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="parent_metric", metric_id="121:parent_metric")],
        entity="\u9500\u552e\u5458",
        fields=["parent_metric"],
        dimensions=["\u9500\u552e\u5458"],
        filters=[dict(current_filter)],
        operators=[
            AnalysisOperator.AGGREGATE,
            AnalysisOperator.GROUP_BY,
            AnalysisOperator.SORT,
            AnalysisOperator.TOP_N,
        ],
        ranking_limit=1,
        time_range=TimeRange(
            start=date(2026, 1, 1), end_exclusive=date(2027, 1, 1)
        ),
        turn_admission=TurnAdmissionDecision(
            relation=TurnRelation.CURRENT_TOPIC_FOLLOWUP,
            confidence=1,
            context_mode=ContextMode.CURRENT_THREAD,
            current_turn_facts=CurrentTurnFacts(
                raw_query=question,
                explicit_slots=explicit_slots,
            ),
        ),
        dependency_constraints=[
            DependencyConstraint(
                source_task_id="task-1",
                source_dataset_id="source-dataset",
                source_column="salesperson.salesperson_code",
                values=["99111025"],
                value_fingerprint=hashlib.sha256(
                    json.dumps(["99111025"], separators=(",", ":")).encode()
                ).hexdigest(),
            )
        ],
    )
    extraction = PlannerExtraction(
        intent=PrimaryIntent.DETAIL_QUERY,
        structured={
            "\u5b9e\u4f53": ["\u9500\u552e\u8ba2\u5355", owner],
            "\u6307\u6807": [],
            "\u7ef4\u5ea6": [],
            "\u5c55\u793a\u5b57\u6bb5": [{"entity": owner, "field": field}],
            "\u8fc7\u6ee4\u6761\u4ef6": [dict(current_filter)],
            "\u6392\u5e8f": [],
            "\u65f6\u95f4\u7c92\u5ea6": (
                {"unit": "\u5e74", "time_range": "2025\u5e74"}
                if has_year else {"unit": None, "time_range": None}
            ),
            "\u9650\u5236": None,
            "\u662f\u5426\u53bb\u91cd": "\u662f",
        },
    )
    request._planner_extraction = extraction

    token = _INTERNAL_ASSUMPTIONS.set(("DAG_BOUND_STRUCTURED_QUERY",))
    try:
        DataAnalysisOrchestrator._apply_dag_structured_query_shape(request, extraction)
        DataAnalysisOrchestrator._apply_explicit_projection_mode(request, extraction)
    finally:
        _INTERNAL_ASSUMPTIONS.reset(token)

    # Dependency values are attached after rebuilding the child's own shape.
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    contract = build_intent_asl_contract(request)

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == (query_object or owner)
    assert request.fields == [field]
    assert request.metrics == []
    assert request.dimensions == []
    assert request.ranking_limit is None
    assert not {
        AnalysisOperator.AGGREGATE,
        AnalysisOperator.GROUP_BY,
        AnalysisOperator.SORT,
        AnalysisOperator.TOP_N,
    }.intersection(request.operators)
    assert request.rewritten_question and "parent_metric" not in request.rewritten_question
    assert request.original_question == question
    if has_year:
        assert request.time_range == TimeRange(
            start=date(2025, 1, 1), end_exclusive=date(2026, 1, 1)
        )
    assert contract["required_projections"] == [field]
    assert validate_intent_asl_contract_definition(contract) == []
    assert validate_intent_asl_contract_completeness(contract, request) == []
    HttpDataRetrievalAdapter._validate_dependency_constraints(
        {"filters": request.filters}, request
    )
    assert any(
        item.get("field") == "salesperson.salesperson_code"
        and item.get("operator") == "IN"
        and item.get("value") == ["99111025"]
        for item in request.filters
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface_enabled", [False, True])
async def test_real_handle_passes_rebuilt_dependent_shape_to_retrieval(surface_enabled):
    question = (
        "\u6839\u636e\u4e0a\u8ff0\u4efb\u52a1\u8fd4\u56de\u7684\u9500\u552e\u5458\uff0c"
        "\u67e5\u8be22025\u5e74\u8be5\u9500\u552e\u5458\u5728\u590d\u65e6\u5927\u5b66\u9644\u5c5e"
        "\u534e\u5c71\u533b\u9662\u9500\u552e\u7684\u4ea7\u54c1\u660e\u7ec6\u6e05\u5355\u3002"
    )
    hospital_filter = {
        "entity": HOSPITAL,
        "field": HOSPITAL_NAME,
        "op": "=",
        "value": [HOSPITAL_VALUE],
    }
    extraction = PlannerExtraction(
        intent=PrimaryIntent.DETAIL_QUERY,
        structured={
            "\u5b9e\u4f53": ["\u9500\u552e\u8ba2\u5355", PRODUCT],
            "\u6307\u6807": [],
            "\u7ef4\u5ea6": [],
            "\u5c55\u793a\u5b57\u6bb5": [{"entity": PRODUCT, "field": PRODUCT_NAME}],
            "\u8fc7\u6ee4\u6761\u4ef6": [hospital_filter],
            "\u6392\u5e8f": [],
            "\u65f6\u95f4\u7c92\u5ea6": {
                "unit": "\u5e74", "time_range": "2025\u5e74"
            },
            "\u9650\u5236": None,
            "\u662f\u5426\u53bb\u91cd": "\u662f",
        },
    )

    class StaleContextClassifier:
        rules = RuleBasedIntentClassifier()

        def classify(self, current_question, identity, conversation_id):
            request = self.rules.classify(
                current_question, identity, conversation_id
            )
            # Model the observed regression: a late context/model merge restores
            # the preceding ranking shape after the child's own extraction.
            request.primary_intent = PrimaryIntent.METRIC_QUERY
            request.metrics = [MetricRef(input="parent_metric")]
            request.entity = "\u9500\u552e\u5458"
            request.fields = ["parent_metric"]
            request.dimensions = ["\u9500\u552e\u5458"]
            request.operators = [
                AnalysisOperator.AGGREGATE,
                AnalysisOperator.GROUP_BY,
                AnalysisOperator.SORT,
                AnalysisOperator.TOP_N,
            ]
            request.ranking_limit = 1
            request.rewritten_question = "metric parent_metric; rank salesperson; top 1"
            return request

    class CurrentTaskAdmission(TurnAdmissionGate):
        def evaluate(self, **kwargs):
            decision = super().evaluate(**kwargs)
            decision.current_turn_facts.explicit_slots["query_object"] = SlotProvenance(
                value=HOSPITAL,
                source=SlotSource.CURRENT_EXPLICIT,
            )
            return decision

    adapters = build_mock_adapters()
    observed = []
    execute = adapters.retrieval.query
    surface_calls = []
    query_surface = adapters.query.query_surface

    async def capture_surface(*args, **kwargs):
        surface_calls.append((args, kwargs))
        return await query_surface(*args, **kwargs)

    async def capture(request, identity, *, semantic_model_id, business_domain_id):
        observed.append(request.model_copy(deep=True))
        return await execute(
            request,
            identity,
            semantic_model_id=semantic_model_id,
            business_domain_id=business_domain_id,
        )

    adapters.retrieval.query = capture
    adapters.query.query_surface = capture_surface
    service = DataAnalysisOrchestrator(
        settings=Settings(
            _env_file=None,
            env="test",
            adapter_mode="mock",
            surface_asl_execution_enabled=surface_enabled,
            intent_model_enabled=False,
            multi_question_enabled=False,
            analysis_synthesis_enabled=False,
        ),
        classifier=StaleContextClassifier(),
        adapters=adapters,
        sessions=InMemorySessionStore(),
        turn_admission_gate=CurrentTaskAdmission(),
    )
    metric_recovery_calls = []

    async def capture_metric_recovery(request, _chat, _identity):
        metric_recovery_calls.append(request.model_copy(deep=True))
        return False

    service._recover_live_published_metrics = capture_metric_recovery
    chat = ChatRequest(
        semantic_model_id=121,
        application_id="app",
        conversation_id="dag-child-integration",
        message_id="dag-child-integration-1",
        question=question,
        history=[],
        dependency_constraints=[
            DependencyConstraint(
                source_task_id="task-1",
                source_dataset_id="source-dataset",
                source_column="salesperson.salesperson_code",
                values=["99111025"],
                value_fingerprint=hashlib.sha256(
                    json.dumps(["99111025"], separators=(",", ":")).encode()
                ).hexdigest(),
            )
        ],
    )
    chat._completed_question_execution = True
    chat._planner_extraction = extraction

    token = _INTERNAL_ASSUMPTIONS.set(("DAG_BOUND_STRUCTURED_QUERY",))
    try:
        response = await service._handle(
            chat, TrustedIdentity(tenant_id="tenant", user_id="user")
        )
    finally:
        _INTERNAL_ASSUMPTIONS.reset(token)

    assert response.status == "COMPLETED"
    assert len(observed) == 1
    assert surface_calls == []
    sent = observed[0]
    assert sent.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert sent.fields == [PRODUCT_NAME]
    assert sent.metrics == []
    assert sent.dimensions == []
    assert sent.ranking_limit is None
    assert sent.entity == HOSPITAL
    assert sent.original_question == question
    assert "parent_metric" not in (sent.rewritten_question or "")
    assert metric_recovery_calls == []
