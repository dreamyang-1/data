from app.analysis.insight_discovery import discover_insights
from app.analysis import AnalysisEngine
from app.domain.models import CanonicalAnalysisRequest, KnowledgeContext, PrimaryIntent


def test_discovers_and_ranks_bounded_general_insights():
    rows = [
        {"date": f"2026-01-{index:02d}", "region": "华东" if index <= 8 else "华南",
         "sales": index * 10, "orders": index * 20}
        for index in range(1, 11)
    ]
    findings = discover_insights(["date", "region", "sales", "orders"], rows)
    kinds = {item["type"] for item in findings}
    assert {"DOMINANT_CATEGORY", "TIME_TREND", "CORRELATION"} <= kinds
    assert findings == sorted(findings, key=lambda item: item["priority_score"], reverse=True)
    correlation = next(item for item in findings if item["type"] == "CORRELATION")
    assert correlation["evidence"]["coefficient"] == 1.0
    assert correlation["evidence"]["causality_established"] is False


def test_ignores_ids_and_requires_enough_correlation_samples():
    rows = [
        {"customer_id": index, "segment": "A" if index % 2 else "B", "value": index}
        for index in range(1, 6)
    ]
    findings = discover_insights(["customer_id", "segment", "value"], rows)
    assert all("customer_id" not in item["subject"] for item in findings)
    assert all(item["type"] != "CORRELATION" for item in findings)


def test_reports_robust_outlier_without_dropping_values():
    rows = [{"group": str(index), "value": value} for index, value in enumerate([10, 11, 9, 10, 12, 100])]
    findings = discover_insights(["group", "value"], rows)
    outlier = next(item for item in findings if item["type"] == "ROBUST_OUTLIERS")
    assert outlier["evidence"]["outlier_count"] == 1
    assert outlier["evidence"]["sample_count"] == 6


def test_report_exposes_ranked_discovery_with_governance_warning():
    rows = [
        {"date": f"2026-01-{index:02d}", "sales": index * 10, "orders": index * 20}
        for index in range(1, 8)
    ]
    request = CanonicalAnalysisRequest(
        conversation_id="insight-report", tenant_id="tenant", user_id="user",
        original_question="分析这批数据并找出值得关注的问题",
        primary_intent=PrimaryIntent.REPORT_GENERATION,
    )
    output = AnalysisEngine().analyze(
        request, ["date", "sales", "orders"], rows, KnowledgeContext(query="")
    )
    assert output.facts["discovered_insights"]
    assert output.facts["insight_ranking_method"] == "bounded_impact_times_confidence"
    assert any("因果" in warning for warning in output.warnings)
