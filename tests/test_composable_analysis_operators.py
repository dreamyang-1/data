import pytest

from app.analysis import AnalysisEngine
from app.analysis.operators import AnalysisOperatorRegistry, OperatorInputError
from app.domain.models import CanonicalAnalysisRequest, KnowledgeContext, PrimaryIntent


REGISTRY = AnalysisOperatorRegistry()


def test_price_volume_decomposition_reconciles_exactly():
    result = REGISTRY.execute("price_volume_decomposition", [
        "period_role", "单价", "销量"
    ], [
        {"period_role": "BASE", "单价": 10, "销量": 100},
        {"period_role": "CURRENT", "单价": 12, "销量": 110},
    ])

    assert result.facts["total_change"] == 320
    assert result.facts["volume_effect"] == 100
    assert result.facts["price_effect"] == 200
    assert result.facts["interaction_effect"] == 20
    assert result.facts["reconciliation_residual"] == 0
    assert result.facts["causality_established"] is False


def test_price_volume_rejects_missing_period_roles():
    with pytest.raises(OperatorInputError, match="period_role"):
        REGISTRY.execute("price_volume_decomposition", [
            "period_role", "price", "quantity"
        ], [
            {"period_role": "BASE", "price": 10, "quantity": 100},
            {"period_role": "BASE", "price": 12, "quantity": 110},
        ])


def test_funnel_calculates_each_transition_and_largest_dropoff():
    result = REGISTRY.execute("funnel_conversion_analysis", [
        "stage_order", "stage", "count"
    ], [
        {"stage_order": 2, "stage": "下单", "count": 400},
        {"stage_order": 1, "stage": "访问", "count": 1000},
        {"stage_order": 3, "stage": "支付", "count": 300},
    ])

    assert result.facts["overall_conversion_rate"] == pytest.approx(0.3)
    assert result.facts["transitions"][0]["dropoff_rate"] == pytest.approx(0.6)


def test_funnel_rejects_non_monotonic_counts():
    with pytest.raises(OperatorInputError, match="不能大于"):
        REGISTRY.execute("funnel_conversion_analysis", [
            "stage_order", "stage", "count"
        ], [
            {"stage_order": 1, "stage": "访问", "count": 100},
            {"stage_order": 2, "stage": "下单", "count": 120},
        ])


def test_structural_shift_reconciles_share_changes_to_zero():
    result = REGISTRY.execute("structural_share_shift", [
        "period_role", "渠道", "销售额"
    ], [
        {"period_role": "BASE", "渠道": "线上", "销售额": 60},
        {"period_role": "BASE", "渠道": "线下", "销售额": 40},
        {"period_role": "CURRENT", "渠道": "线上", "销售额": 90},
        {"period_role": "CURRENT", "渠道": "线下", "销售额": 10},
    ])

    assert result.facts["shifts"][0]["share_change"] == pytest.approx(0.3)
    assert result.facts["share_change_residual"] == pytest.approx(0)


def test_contribution_operator_ranks_absolute_effect_and_marks_non_causal():
    result = REGISTRY.execute("dimension_contribution_decomposition", [
        "区域", "销售额贡献"
    ], [
        {"区域": "华东", "销售额贡献": -80},
        {"区域": "华南", "销售额贡献": 30},
    ])

    assert result.facts["ranked_candidates"][0]["label"] == "华东"
    assert result.facts["contribution_sum"] == -50
    assert result.facts["causality_established"] is False


def test_engine_routes_funnel_question_to_composable_operator():
    request = CanonicalAnalysisRequest(
        conversation_id="c", tenant_id="t", user_id="u",
        original_question="分析访问到支付的漏斗转化",
        primary_intent=PrimaryIntent.COMPOSITION_ANALYSIS,
    )
    output = AnalysisEngine().analyze(request, [
        "stage_order", "stage", "count"
    ], [
        {"stage_order": 1, "stage": "访问", "count": 1000},
        {"stage_order": 2, "stage": "支付", "count": 250},
    ], KnowledgeContext(query=request.original_question))

    assert output.method == "funnel_conversion_analysis"
    assert "25.00%" in output.answer
