"""End-to-end regression: trusted V2 dimension identity must close the loop.

Captures every hop of the failing chain for one grouped query:

1. V2 binding: ``SemanticDecisionField(catalog_type="DIMENSION")`` carries the
   complete catalog identity (canonical_code/display_name/model/domain).
2. ``canonical_request_from_semantic_decision`` materializes the V1 request.
3. ``build_intent_asl_contract`` derives query_object/required_groupings.
4. Oagnet returns the ASL dimension proven by the current recall metadata.
5. ``HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions`` gates.

The production failure kept only the display label in hop 2, so hop 5 could
compare nothing but literal display text against the normalized canonical code
and raised ``ASL_REQUIRED_DIMENSION_MISSING`` even though Oagnet preserved the
dimension.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.domain.models import (
    ChatRequest,
    PrimaryIntent,
    TrustedIdentity,
)
from app.domain.semantic_decision import (
    SemanticDecision,
    SemanticDecisionField,
    SemanticDecisionSource,
    SemanticDecisionTask,
    SemanticDecisionTimeRange,
    SemanticScopeProof,
)
from app.intent import RuleBasedIntentClassifier
from app.services.intent_asl_contract import build_intent_asl_contract
from app.services.semantic_decision import canonical_request_from_semantic_decision


IDENTITY = TrustedIdentity(tenant_id="tenant", user_id="user")
QUESTION = "统计近半年各个产线故障发生次数"


def _chat() -> ChatRequest:
    return ChatRequest(
        conversation_id="dimension-e2e-conversation",
        message_id="dimension-e2e-message",
        question=QUESTION,
        application_id="app",
        semantic_model_id=77,
        business_domain_ids=[300],
    )


def _decision(chat: ChatRequest) -> SemanticDecision:
    metric = SemanticDecisionField(
        catalog_type="METRIC",
        canonical_id="metric-fault-count",
        canonical_code="fault_count",
        display_name="故障发生次数",
        semantic_model_id="77",
        catalog_version="catalog-v1",
        business_domain_ids=("300",),
        resolution_source="PINNED_CATALOG",
    )
    dimension = SemanticDecisionField(
        catalog_type="DIMENSION",
        canonical_id="dim-line",
        canonical_code="line_id",
        display_name="产线编号",
        semantic_model_id="77",
        catalog_version="catalog-v1",
        business_domain_ids=("300",),
        resolution_source="PINNED_CATALOG",
    )
    zone = ZoneInfo("Asia/Shanghai")
    task = SemanticDecisionTask(
        task_id="task-1",
        question=chat.question,
        intent=PrimaryIntent.METRIC_QUERY,
        intent_confidence=1.0,
        payload_type="SCALAR_AGGREGATE",
        metrics=(metric,),
        dimensions=(dimension,),
        time_range=SemanticDecisionTimeRange(
            start=datetime(2026, 3, 16, tzinfo=zone),
            end_exclusive=datetime(2026, 9, 16, tzinfo=zone),
            grain="MONTH",
            source="USER_EXPLICIT",
        ),
        semantic_complete=True,
    )
    scope = chat.authorized_semantic_scope
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
        scope_proof=SemanticScopeProof(
            authorized_scope=scope,
            authorized_scope_fingerprint=scope.fingerprint(),
            catalog_version="catalog-v1",
            semantic_model_version="catalog-v1",
        ),
    )


def _materialize():
    chat = _chat()
    request, reason = canonical_request_from_semantic_decision(
        _decision(chat),
        chat=chat,
        identity=IDENTITY,
        rules=RuleBasedIntentClassifier(),
    )
    assert reason is None, reason
    assert request is not None
    return request


# Oagnet's proven ASL for this query: the dimension name is the canonical
# code recalled from the current metadata and the alias is the canonical
# dim_name written by the provenance completion.
OAGNET_ASL = {
    "dimensions": [{"name": "line_id", "alias": "产线编号"}],
    "metrics": [{"name": "fault_count", "alias": "故障发生次数"}],
}


def test_every_hop_shape_is_captured():
    request = _materialize()
    assert request.dimensions == ["产线编号"]

    contract = build_intent_asl_contract(request)
    assert contract["query_object"] == "产线编号"
    assert contract["required_groupings"] == ["产线编号"]

    assert HttpDataRetrievalAdapter._required_grouped_dimension_roles(
        request
    ) == ["产线编号"]


def test_trusted_dimension_identity_closes_the_gate():
    """Regression: must raise before the fix, pass after it."""
    request = _materialize()
    HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(
        OAGNET_ASL, request
    )


def test_generic_published_dimension_closes_by_identity():
    """Positive case from an arbitrary non-pharma domain."""
    request = _materialize()
    request.dimensions = ["设备运行状态"]
    for binding in getattr(request, "trusted_dimension_bindings", []):
        binding.display_name = "设备运行状态"
        binding.canonical_code = "device_status"
    HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(
        {"dimensions": [{"name": "device_status", "alias": "设备运行状态"}]},
        request,
    )


def test_fabricated_alias_is_rejected():
    request = _materialize()
    with pytest.raises(AdapterError) as error:
        HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(
            {"dimensions": [{"name": "unrelated_code", "alias": "产线编号"}]},
            request,
        )
    assert error.value.code == "ASL_REQUIRED_DIMENSION_MISSING"


def test_unbound_display_name_cannot_close_on_code_alone():
    """Without a trusted V2 binding the display-label path stays Fail Closed."""
    request = _materialize()
    request.trusted_dimension_bindings = []
    with pytest.raises(AdapterError) as error:
        HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(
            {"dimensions": [{"name": "line_id"}]},
            request,
        )
    assert error.value.code == "ASL_REQUIRED_DIMENSION_MISSING"


def test_same_display_name_with_a_different_canonical_identity_is_rejected():
    request = _materialize()
    with pytest.raises(AdapterError) as error:
        HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(
            {"dimensions": [{"name": "line_name", "alias": "产线编号"}]},
            request,
        )
    assert error.value.code == "ASL_REQUIRED_DIMENSION_MISSING"


def test_city_binding_is_not_satisfied_by_province_identity():
    request = _materialize()
    request.dimensions = ["城市"]
    request.trusted_dimension_bindings = []
    from app.domain.models import SemanticDimensionBinding

    request.trusted_dimension_bindings = [
        SemanticDimensionBinding(
            display_name="城市",
            canonical_code="city_code",
            canonical_id="dim-city",
            semantic_model_id=77,
            catalog_version="catalog-v1",
            business_domain_id=300,
        )
    ]
    with pytest.raises(AdapterError) as error:
        HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(
            {"dimensions": [{"name": "province_code", "alias": "城市"}]},
            request,
        )
    assert error.value.code == "ASL_REQUIRED_DIMENSION_MISSING"


def test_transport_contract_never_parses_trusted_dimension_identity():
    """The identity can only be written by the trusted in-process planner."""
    payload = {
        "conversation_id": "dimension-e2e-conversation",
        "message_id": "dimension-e2e-message",
        "question": QUESTION,
        "application_id": "app",
        "semantic_model_id": 77,
        "business_domain_ids": [300],
        "trusted_dimension_bindings": [
            {
                "display_name": "产线编号",
                "canonical_code": "line_id",
                "canonical_id": "dim-line",
                "semantic_model_id": 77,
            }
        ],
    }
    with pytest.raises(ValueError):
        ChatRequest.model_validate(payload)
