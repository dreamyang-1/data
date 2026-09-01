from app.analysis import AnalysisPlanner
from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    MetricRef,
    PrimaryIntent,
)


def request(intent: PrimaryIntent, **updates) -> CanonicalAnalysisRequest:
    value = CanonicalAnalysisRequest(
        conversation_id="c1",
        tenant_id="t1",
        user_id="u1",
        original_question="为什么本月销售额下降",
        primary_intent=intent,
        metrics=[MetricRef(input="销售额", canonical_name="销售额")],
        dimensions=["区域", "渠道"],
    )
    return value.model_copy(update=updates)


def test_root_cause_plan_is_explicit_about_contribution_not_causality():
    plan = AnalysisPlanner().build(request(PrimaryIntent.ROOT_CAUSE_ANALYSIS))

    assert plan is not None
    assert plan.methods == [
        "target_change_confirmation", "dimension_contribution_decomposition"
    ]
    assert plan.metrics == ["销售额"]
    assert plan.dimensions == ["区域", "渠道"]
    assert plan.sufficiency_rules[0].minimum_rows == 2
    assert plan.hypotheses[0].status == "PLANNED"
    assert any("因果" in item for item in plan.conclusion_policy)
    assert plan.data_contract is not None
    assert plan.data_contract.operator == "dimension_contribution_decomposition"


def test_multidimensional_root_cause_plan_uses_surprise_and_explanatory_power():
    plan = AnalysisPlanner().build(request(
        PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        original_question="从多个维度下钻分析销售额下降原因",
    ))

    assert plan is not None
    assert plan.methods == [
        "baseline_current_alignment", "dimension_surprise_ranking",
        "explanatory_power_threshold", "root_cause_drilldown",
    ]
    assert plan.data_contract is not None
    assert plan.data_contract.operator == "multidimensional_attribution"


def test_forecast_plan_exposes_readiness_and_backtest_requirements():
    plan = AnalysisPlanner().build(request(PrimaryIntent.FORECAST_ANALYSIS))

    assert plan is not None
    assert plan.methods[-1] == "backtest"
    assert plan.sufficiency_rules[0].minimum_rows == 8


def test_explicit_object_comparison_uses_descriptive_two_object_minimum():
    plan = AnalysisPlanner().build(request(
        PrimaryIntent.COMPARISON_ANALYSIS,
        original_question="对比甲、乙、丙三家合作方的增长率与合作时长",
        comparison_type="对象间比较",
        dimensions=["合作方"],
        filters=[{
            "field": "合作方名称",
            "operator": "IN",
            "value": ["甲", "乙", "丙"],
        }],
    ))

    assert plan is not None
    rule = plan.sufficiency_rules[0]
    assert rule.code == "MIN_ROWS_EXPLICIT_OBJECT_COMPARISON"
    assert rule.minimum_rows == 2
    assert "指定了 3 个对象" in rule.description
    assert "未返回对象必须披露" in rule.description


def test_open_report_plan_requests_bounded_analysis_ready_data():
    plan = AnalysisPlanner().build(request(PrimaryIntent.REPORT_GENERATION))
    assert plan is not None and plan.exploration_requirements is not None
    assert plan.exploration_requirements.maximum_numeric_metrics == 3
    assert plan.exploration_requirements.maximum_dimensions == 3
    assert plan.exploration_requirements.prefer_time_dimension is True


def test_plain_metric_query_has_no_analysis_plan():
    assert AnalysisPlanner().build(request(PrimaryIntent.METRIC_QUERY)) is None


def test_ranking_operator_gets_plan_even_on_metric_query():
    plan = AnalysisPlanner().build(request(
        PrimaryIntent.METRIC_QUERY,
        operators=[AnalysisOperator.TOP_N],
    ))

    assert plan is not None
    assert plan.methods[0] == "ranking_metric_validation"
    assert plan.sufficiency_rules[0].minimum_rows == 1


def test_unbounded_entity_sort_gets_ranking_plan_without_two_period_roles():
    plan = AnalysisPlanner().build(request(
        PrimaryIntent.COMPARISON_ANALYSIS,
        original_question="列出经销商并按整体业务规模排序",
        comparison_type="对象间比较",
        dimensions=["经销商"],
        metrics=[MetricRef(input="整体业务规模", canonical_name="整体业务规模")],
        operators=[
            AnalysisOperator.COMPARE,
            AnalysisOperator.GROUP_BY,
            AnalysisOperator.SORT,
        ],
    ))

    assert plan is not None
    assert plan.methods[0] == "ranking_metric_validation"
    assert plan.sufficiency_rules[0].minimum_rows == 1


def test_specialized_question_exposes_operator_query_contract():
    plan = AnalysisPlanner().build(request(
        PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        original_question="分析销售额变化的量价因素",
    ))

    assert plan is not None and plan.data_contract is not None
    assert plan.data_contract.operator == "price_volume_decomposition"
    assert set(plan.data_contract.required_columns) == {
        "period_role", "price", "quantity"
    }
