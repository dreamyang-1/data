from __future__ import annotations

from app.analysis.query_insight import build_query_result_insight
from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent


def _request(
    intent: PrimaryIntent = PrimaryIntent.METRIC_QUERY,
) -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="query-insight",
        tenant_id="tenant",
        user_id="user",
        original_question="查询各经销商的区域医院覆盖率",
        primary_intent=intent,
    )


def test_query_result_insight_describes_scale_and_numeric_distribution() -> None:
    output = build_query_result_insight(
        _request(),
        ["经销商", "区域医院覆盖率"],
        [
            {"经销商": "甲", "区域医院覆盖率": 0.0},
            {"经销商": "乙", "区域医院覆盖率": 0.5},
            {"经销商": "丙", "区域医院覆盖率": 1.0},
        ],
        total_row_count=3,
        total_row_count_confirmed=True,
        truncated=False,
    )

    assert output is not None
    assert output.method == "validated_query_result_summary"
    assert "共命中3条结果" in output.answer
    assert "0.00%至100.00%" in output.answer
    assert "平均值为50.00%" in output.answer
    assert "经销商包含3个不同值" in output.answer
    assert "完整查询结果" in output.answer
    assert output.facts["llm_role"] == "PRESENTATION_ONLY"
    assert output.warnings == []


def test_query_result_insight_discloses_preview_boundary() -> None:
    output = build_query_result_insight(
        _request(PrimaryIntent.DETAIL_QUERY),
        ["经销商"],
        [{"经销商": "甲"}, {"经销商": "乙"}],
        total_row_count=200,
        total_row_count_confirmed=True,
        truncated=True,
    )

    assert output is not None
    assert "当前用于展示和概括的是2条返回记录" in output.answer
    assert "不能代替对200条完整结果的全量统计" in output.warnings[0]


def test_empty_query_result_does_not_request_model_insight() -> None:
    assert build_query_result_insight(
        _request(),
        ["销售额"],
        [],
        total_row_count=0,
        total_row_count_confirmed=True,
        truncated=False,
    ) is None
