from __future__ import annotations

import json

import pytest

from app.adapters.semantic_query import CompositeSemanticQueryTool
from app.domain.models import (
    CanonicalAnalysisRequest, ConversationControl, MetricRef, PrimaryIntent,
    TrustedIdentity,
)
from app.services.semantic_context import (
    apply_minimum_sufficient_context, attach_relationship_paths,
    build_semantic_context_snapshot, build_semantic_plan_evidence,
    enrich_semantic_context_snapshot,
)


def make_request() -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="conversation-1", tenant_id="tenant-1", user_id="user-1",
        original_question="统计各经销商已合作医院数量",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        conversation_control=ConversationControl.NEW_REQUEST,
        metrics=[MetricRef(input="已合作医院数")], entity="sales_order",
        dimensions=["dealer"], semantic_model_id=81, intent_confidence=0.92,
    )


def test_snapshot_records_asset_provenance_and_is_deterministic():
    item = make_request()
    item.metrics = [MetricRef(
        input="已合作医院数", metric_id="81:cooperating_hospital_count",
        version="current", canonical_name="已合作医院数", unit="家",
    )]
    first = build_semantic_context_snapshot(item, 81)
    second = build_semantic_context_snapshot(item, 81)
    assert first == second
    assert first.assets[0].asset_id == "81:cooperating_hospital_count"
    assert first.assets[0].source == "SEMANTIC_LAYER"
    assert first.assets[0].confidence == 1.0
    assert {asset.asset_type for asset in first.assets} == {"METRIC", "ENTITY", "DIMENSION"}


def test_snapshot_is_excluded_from_downstream_request_contract():
    item = make_request()
    item.semantic_context_snapshot = build_semantic_context_snapshot(item, 81)
    assert "semantic_context_snapshot" not in item.model_dump()
    assert "semantic_context_snapshot" not in json.loads(item.model_dump_json())


def test_definition_enrichment_adds_metric_semantics_and_declared_rule():
    item = make_request()
    item.metrics = [MetricRef(
        input="已合作医院数", metric_id="81:cooperating_hospital_count",
        version="current", canonical_name="已合作医院数", unit="家",
    )]
    snapshot = enrich_semantic_context_snapshot(
        build_semantic_context_snapshot(item, 81),
        [{
            "metric_id": "81:cooperating_hospital_count",
            "semantic_model_id": 81,
            "business_domain_id": 205,
            "calculation_formula": "COUNT(DISTINCT hospital.hospital_code)",
            "bound_entities": [{"resolved_entity_code": "sales_order"}],
            "global_filters": [
                "{'filterCondition': 'sales_order.quantity > 0'}"
            ],
        }],
    )
    metric = snapshot.assets[0]
    assert metric.aggregation == "COUNT"
    assert metric.additivity == "NON_ADDITIVE"
    assert metric.grain == ["sales_order"]
    assert metric.metadata["metadata_source"] == "SEMANTIC_LAYER_DEFINITION"
    assert snapshot.business_rules[0].rule_text == "sales_order.quantity > 0"
    assert snapshot.business_rules[0].source == "SEMANTIC_LAYER"


def test_relationship_projection_preserves_join_contract_and_risk():
    snapshot = build_semantic_context_snapshot(make_request(), 81)
    snapshot = attach_relationship_paths(snapshot, {"paths": [{
        "path_id": "path_123456789012345678901234",
        "source_entity": "sales_order", "target_entity": "dealer",
        "canonical_path": "sales_order -> dealer", "risk": "LOW",
        "relationships": [{
            "relationship_id": "sales_order_dealer",
            "join_condition": "sales_order.dealer_code = dealer.dealer_code",
            "cardinality": "MANY_TO_ONE",
        }],
    }]})
    path = snapshot.relationship_paths[0]
    assert path.canonical_path == ["sales_order", "dealer"]
    assert path.join_conditions == ["sales_order.dealer_code = dealer.dealer_code"]
    assert path.cardinalities == ["MANY_TO_ONE"]
    assert path.risk == "LOW"


def test_minimum_sufficient_context_keeps_required_and_drops_low_priority():
    item = make_request()
    item.fields = [f"field_{index}_" + ("x" * 200) for index in range(20)]
    snapshot = build_semantic_context_snapshot(item, 81)
    selected = apply_minimum_sufficient_context(
        snapshot, item.original_question, char_budget=1000
    )
    summary = selected.retrieval_summary
    assert summary is not None
    assert any(entry.required for entry in summary.selected)
    assert summary.dropped
    assert all(
        entry.ref_type in {"METRIC", "ENTITY"}
        for entry in summary.selected if entry.required
    )


def test_minimum_sufficient_context_is_deterministic():
    item = make_request()
    snapshot = build_semantic_context_snapshot(item, 81)
    first = apply_minimum_sufficient_context(snapshot, item.original_question)
    second = apply_minimum_sufficient_context(snapshot, item.original_question)
    assert first == second


def test_plan_evidence_records_filters_paths_rules_and_risk():
    item = make_request()
    item.filters = [{"field": "product.product_name", "operator": "EQ", "value": "导管"}]
    item.assumptions = ["DEFAULT_TIME_GRANULARITY=month"]
    snapshot = attach_relationship_paths(
        build_semantic_context_snapshot(item, 81),
        {"paths": [{
            "path_id": "path_123456789012345678901234",
            "source_entity": "sales_order", "target_entity": "product",
            "canonical_path": "sales_order -> product", "risk": "FANOUT",
            "relationships": [{
                "relationship_id": "sales_order_product",
                "join_condition": "sales_order.product_code = product.product_code",
                "cardinality": "ONE_TO_MANY",
            }],
        }]},
    )
    plan = build_semantic_plan_evidence(item, snapshot)
    assert plan.relationship_path_ids == ["path_123456789012345678901234"]
    assert plan.filter_resolutions[0].resolution == "SEMANTIC_FIELD"
    assert plan.time_grain == "month"
    assert plan.validation_status == "PARTIAL"
    assert "FANOUT" in plan.warnings[0]


def test_plan_evidence_is_internal_and_deterministic():
    item = make_request()
    snapshot = apply_minimum_sufficient_context(
        build_semantic_context_snapshot(item, 81), item.original_question
    )
    first = build_semantic_plan_evidence(item, snapshot)
    second = build_semantic_plan_evidence(item, snapshot)
    assert first == second
    item.semantic_plan_evidence = first
    assert "semantic_plan_evidence" not in item.model_dump()


@pytest.mark.asyncio
async def test_composite_query_records_resolved_semantic_snapshot():
    class Semantic:
        async def resolve_metrics(self, _request, _model_id):
            return [MetricRef(
                input="已合作医院数", metric_id="81:cooperating_hospital_count",
                version="v2", canonical_name="已合作医院数", unit="家",
            )]

    class Retrieval:
        async def query(self, received, *_args, **_kwargs):
            assert received.semantic_context_snapshot is not None
            assert received.semantic_context_snapshot.assets[0].version == "v2"
            return "ok"

    result = await CompositeSemanticQueryTool(Semantic(), Retrieval()).query(
        make_request(), TrustedIdentity(tenant_id="tenant-1", user_id="user-1", roles=[]),
        semantic_model_id=81, business_domain_id=None,
    )
    assert result == "ok"
