import pytest

from app.analysis import AnalysisEngine
from app.analysis.engine import AnalysisOutput
from app.analysis.interpretation import AnswerPlanner, InsightInterpretationLayer
from app.domain.models import (
    CanonicalAnalysisRequest,
    KnowledgeContext,
    MetricRef,
    PrimaryIntent,
)


def trend_request() -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="trend-interpretation",
        tenant_id="tenant",
        user_id="user",
        original_question="上海市紫杉醇释放冠脉球囊导管整体销售趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        metrics=[MetricRef(input="含税销售总额", canonical_name="含税销售总额")],
    )


def test_trend_interpretation_turns_diagnostics_into_business_language():
    request = trend_request()
    analysis = AnalysisEngine().analyze(
        request,
        ["交易日期", "含税销售总额"],
        [
            {"交易日期": "2025-10", "含税销售总额": 546_555_657.1559},
            {"交易日期": "2025-11", "含税销售总额": 143_545_466.5077},
            {"交易日期": "2025-12", "含税销售总额": 207_407_016.6056},
        ],
        KnowledgeContext(query=""),
    )

    interpreted = InsightInterpretationLayer().interpret(request, analysis)
    plan = AnswerPlanner().plan(interpreted)
    answer = plan.render()

    assert interpreted.overall_trend == "下降"
    assert interpreted.recovery is not None
    assert interpreted.recovery["trough_period"] == "2025-11"
    assert interpreted.recovery["recovery_ratio"] == pytest.approx(0.15846, rel=1e-3)
    assert "低位修复" in answer
    assert "趋势反转" in answer
    assert "2025-11" in answer
    assert "62.05%" in answer
    assert "稳健斜率" not in answer
    assert "方向一致性" not in answer
    assert "最大单期" not in answer
    assert "事实" in answer
    assert "分析判断" in answer
    assert "分析边界" in answer


def test_answer_plan_keeps_internal_diagnostics_out_of_user_answer():
    request = trend_request()
    analysis = AnalysisEngine().analyze(
        request,
        ["月份", "含税销售总额"],
        [
            {"月份": "2026-01", "含税销售总额": 100},
            {"月份": "2026-02", "含税销售总额": 120},
            {"月份": "2026-03", "含税销售总额": 135},
        ],
        KnowledgeContext(query=""),
    )
    interpreted = InsightInterpretationLayer().interpret(request, analysis)
    plan = AnswerPlanner().plan(interpreted)

    assert "direction_consistency" in plan.omitted_internal_fields
    assert "robust_slope_per_period" in plan.omitted_internal_fields
    assert "direction_consistency" not in plan.render()


def test_answer_plan_does_not_repeat_deterministic_table_as_key_fact():
    request = CanonicalAnalysisRequest(
        conversation_id="detail-no-duplicate",
        tenant_id="tenant",
        user_id="user",
        original_question="查询经销商并按销售额排序",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="含税销售总额", canonical_name="含税销售总额")],
    )
    answer = "| 经销商 | 含税销售总额 |\n| --- | --- |\n| 甲公司 | 100 |"
    analysis = AnalysisOutput(
        answer=answer,
        method="deterministic_result",
        facts={},
    )
    plan = AnswerPlanner().plan(InsightInterpretationLayer().interpret(request, analysis))

    assert plan.headline == answer
    assert plan.key_facts == []
    assert plan.render().count("| 甲公司 | 100 |") == 1
