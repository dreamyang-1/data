from app.domain.models import (
    CanonicalAnalysisRequest,
    PrimaryIntent,
    SemanticFilterBinding,
)
from app.services.orchestrator import DataAnalysisOrchestrator


def _request(bindings):
    return CanonicalAnalysisRequest(
        conversation_id="empty-filter-explanation",
        tenant_id="tenant",
        user_id="user",
        original_question="查询北京市费森尤斯销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        filters=[
            {"field": "省份", "operator": "=", "value": "北京市"},
            {"field": "商品品牌", "operator": "=", "value": "费森尤斯"},
        ],
        semantic_filter_bindings=bindings,
    )


def test_filter_binding_coverage_reports_partial_followup_bindings():
    request = _request([
        SemanticFilterBinding(
            filter_index=1,
            input_value="费森尤斯",
            canonical_value="费森尤斯",
            canonical_name="商品品牌",
            attribute_code="parent_brand",
            score=1.0,
        ),
    ])
    complete, missing = DataAnalysisOrchestrator._filter_binding_coverage(request)
    assert complete is False
    assert missing == ["省份"]


def test_filter_binding_coverage_accepts_all_current_filter_values():
    request = _request([
        SemanticFilterBinding(
            filter_index=0,
            input_value="北京市",
            canonical_value="北京市",
            canonical_name="省份",
            attribute_code="province_name",
            score=1.0,
        ),
        SemanticFilterBinding(
            filter_index=1,
            input_value="费森尤斯",
            canonical_value="费森尤斯",
            canonical_name="商品品牌",
            attribute_code="parent_brand",
            score=1.0,
        ),
    ])
    complete, missing = DataAnalysisOrchestrator._filter_binding_coverage(request)
    assert complete is True
    assert missing == []
