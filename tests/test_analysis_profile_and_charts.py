from __future__ import annotations

from app.analysis import AnalysisEngine
from app.domain.models import CanonicalAnalysisRequest, KnowledgeContext, MetricRef, PrimaryIntent


def _request(intent: PrimaryIntent) -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="profile-chart-test",
        tenant_id="tenant",
        user_id="user",
        original_question="test",
        primary_intent=intent,
        metrics=[MetricRef(input="销售额", canonical_name="销售额")],
    )


def test_analysis_includes_bounded_data_profile_without_samples() -> None:
    output = AnalysisEngine().analyze(
        _request(PrimaryIntent.TREND_ANALYSIS),
        ["月份", "销售额"],
        [
            {"月份": "2026-01", "销售额": 100},
            {"月份": "2026-02", "销售额": 120},
        ],
        KnowledgeContext(query=""),
    )
    profile = output.facts["data_profile"]
    assert profile["row_count"] == 2
    sales = next(item for item in profile["columns"] if item["name"] == "销售额")
    assert sales["inferred_type"] == "NUMBER"
    assert sales["numeric_summary"]["mean"] == 110
    assert "sample_values" not in sales


def test_trend_produces_frontend_neutral_line_chart_spec() -> None:
    output = AnalysisEngine().analyze(
        _request(PrimaryIntent.TREND_ANALYSIS),
        ["月份", "销售额"],
        [
            {"月份": "2026-01", "销售额": 100},
            {"月份": "2026-02", "销售额": 120},
        ],
        KnowledgeContext(query=""),
    )
    chart = output.facts["chart_specs"][0]
    assert chart["schema_version"] == "1.0"
    assert chart["chart_type"] == "LINE"
    assert chart["x_field"] == "月份"
    assert chart["y_fields"] == ["销售额"]
    assert chart["data"][1]["销售额"] == 120


def test_chart_data_is_capped_for_response_stability() -> None:
    rows = [
        {"月份": f"2026-{index + 1:02d}", "销售额": index}
        for index in range(200)
    ]
    # Use comparison-like labels directly through the visualization helper to
    # avoid temporal validation obscuring the response-size contract.
    from app.analysis.visualization import build_chart_specs

    chart = build_chart_specs(
        PrimaryIntent.COMPARISON_ANALYSIS,
        ["月份", "销售额"],
        rows + [{"月份": "extra", "销售额": 999}],
        {"metric_column": "销售额"},
    )[0]
    assert chart.point_count == 200
    assert chart.data_truncated is True
    assert len(chart.data) == 200
