from __future__ import annotations

import pytest

from app.analysis import AnalysisEngine, AnalysisError
from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    KnowledgeContext,
    MetricRef,
    PrimaryIntent,
    TrustedIdentity,
)
from app.intent.classifier import RuleBasedIntentClassifier


def _request() -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="supplier-ranking",
        tenant_id="tenant",
        user_id="user",
        original_question="查询本月医用外科口罩科室匹配度前十的供应商",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        operators=[AnalysisOperator.AGGREGATE, AnalysisOperator.TOP_N],
        metrics=[MetricRef(input="科室匹配度", canonical_name="科室匹配度")],
        dimensions=["供应商"],
        ranking_limit=10,
    )


def test_classifier_recognizes_supplier_coverage_top_ten_without_ming_suffix() -> None:
    request = RuleBasedIntentClassifier().classify(
        "查询本月医用外科口罩科室匹配度前十的供应商",
        TrustedIdentity(tenant_id="t", user_id="u"),
        "c1",
    )
    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert AnalysisOperator.TOP_N in request.operators
    assert request.ranking_limit == 10
    assert request.metrics[0].input == "科室匹配度"
    assert request.dimensions == ["供应商"]


def test_generic_product_placeholder_requires_clarification() -> None:
    request = RuleBasedIntentClassifier().classify(
        "查询某商品科室匹配度前十的供应商",
        TrustedIdentity(tenant_id="t", user_id="u"),
        "c1",
    )
    assert "product" in request.missing_slots


def test_coverage_ranking_reconciles_counts_and_percentages() -> None:
    output = AnalysisEngine().analyze_ranking(
        _request(),
        ["供应商", "匹配科室数", "有效科室总数", "科室匹配度"],
        [
            {"供应商": "供应商1", "匹配科室数": 5, "有效科室总数": 5, "科室匹配度": 100},
            {"供应商": "供应商2", "匹配科室数": 4, "有效科室总数": 5, "科室匹配度": 80},
            {"供应商": "供应商3", "匹配科室数": 2, "有效科室总数": 5, "科室匹配度": 0.4},
        ],
        KnowledgeContext(query=""),
    )
    assert output.method == "validated_top_n_ranking"
    assert output.facts["coverage_reconciled"] is True
    assert output.facts["rankings"][1]["value"] == pytest.approx(0.8)
    assert "80.00%" in output.answer
    assert output.facts["chart_specs"][0]["chart_type"] == "BAR"


def test_coverage_ranking_rejects_inconsistent_rate() -> None:
    with pytest.raises(AnalysisError, match="应为80.00%"):
        AnalysisEngine().analyze_ranking(
            _request(),
            ["供应商", "匹配科室数", "有效科室总数", "科室匹配度"],
            [{"供应商": "供应商2", "匹配科室数": 4, "有效科室总数": 5, "科室匹配度": 60}],
            KnowledgeContext(query=""),
        )


def test_coverage_ranking_rejects_mixed_denominator_scope() -> None:
    with pytest.raises(AnalysisError, match="统一统计范围"):
        AnalysisEngine().analyze_ranking(
            _request(),
            ["供应商", "匹配科室数", "有效科室总数", "科室匹配度"],
            [
                {"供应商": "供应商1", "匹配科室数": 5, "有效科室总数": 5, "科室匹配度": 100},
                {"供应商": "供应商2", "匹配科室数": 4, "有效科室总数": 6, "科室匹配度": 66.67},
            ],
            KnowledgeContext(query=""),
        )


def test_coverage_ranking_rejects_unsorted_sql_result() -> None:
    with pytest.raises(AnalysisError, match="顺序不正确"):
        AnalysisEngine().analyze_ranking(
            _request(),
            ["供应商", "匹配科室数", "有效科室总数", "科室匹配度"],
            [
                {"供应商": "供应商2", "匹配科室数": 4, "有效科室总数": 5, "科室匹配度": 80},
                {"供应商": "供应商1", "匹配科室数": 5, "有效科室总数": 5, "科室匹配度": 100},
            ],
            KnowledgeContext(query=""),
        )
