from __future__ import annotations

import pytest

from app.analysis import AnalysisEngine
from app.analysis.multidimensional_attribution import (
    MultiDimensionalAttributionError,
    analyze_multidimensional_attribution,
)
from app.domain.models import CanonicalAnalysisRequest, KnowledgeContext, PrimaryIntent


def absolute_rows() -> list[dict]:
    return [
        {"dimension_name": "区域", "element_value": "华东", "baseline": 60, "current": 30},
        {"dimension_name": "区域", "element_value": "华西", "baseline": 40, "current": 40},
        {"dimension_name": "渠道", "element_value": "线上", "baseline": 50, "current": 20},
        {"dimension_name": "渠道", "element_value": "线下", "baseline": 50, "current": 50},
    ]


def test_absolute_attribution_ranks_multiple_dimensions_and_elements() -> None:
    result = analyze_multidimensional_attribution(absolute_rows(), derived=False)

    assert result["status"] == "success"
    assert result["root_causes"] == {"渠道": ["线上"], "区域": ["华东"]}
    assert result["dimension_details"]["区域"]["explanatory_power"] == pytest.approx(1)
    assert result["causality_established"] is False


def test_cross_dimension_total_mismatch_blocks_attribution() -> None:
    rows = absolute_rows()
    rows[-1]["current"] = 40

    result = analyze_multidimensional_attribution(rows, derived=False)

    assert result["status"] == "no_root_cause"
    assert result["root_causes"] == {}
    assert all(
        detail["reason"] == "TOTAL_MISMATCH_ACROSS_DIMENSIONS"
        for detail in result["dimension_details"].values()
    )
    assert result["warnings"]


def test_ratio_attribution_is_additive_and_exposes_component_effects() -> None:
    rows = [
        {"dimension_name": "区域", "element_value": "华东", "baseline_numerator": 60, "baseline_denominator": 100, "current_numerator": 36, "current_denominator": 100},
        {"dimension_name": "区域", "element_value": "华西", "baseline_numerator": 30, "baseline_denominator": 60, "current_numerator": 26, "current_denominator": 60},
        {"dimension_name": "区域", "element_value": "华北", "baseline_numerator": 10, "baseline_denominator": 40, "current_numerator": 8, "current_denominator": 40},
        {"dimension_name": "渠道", "element_value": "线上", "baseline_numerator": 70, "baseline_denominator": 140, "current_numerator": 42, "current_denominator": 140},
        {"dimension_name": "渠道", "element_value": "线下", "baseline_numerator": 30, "baseline_denominator": 60, "current_numerator": 28, "current_denominator": 60},
    ]

    result = analyze_multidimensional_attribution(rows, derived=True)

    assert result["status"] == "success"
    region = result["dimension_details"]["区域"]
    assert region["total_delta"] == pytest.approx(-0.15)
    assert region["reconciliation_residual"] == pytest.approx(0)
    assert sum(item["effect"] for item in region["candidates"]) == pytest.approx(-0.14)
    assert {"numerator_effect", "denominator_effect"} <= region["candidates"][0].keys()


def test_near_total_coverage_is_not_mislabeled_as_root_cause() -> None:
    rows = [
        {"dimension_name": "区域", "element_value": "华东", "baseline": 60, "current": 40},
        {"dimension_name": "区域", "element_value": "华西", "baseline": 40, "current": 20},
    ]
    result = analyze_multidimensional_attribution(rows, derived=False)

    assert result["status"] == "no_root_cause"
    assert result["dimension_details"]["区域"]["reason"] == "CANDIDATES_COVER_NEARLY_ALL_VALUE"


def test_duplicate_dimension_element_is_rejected() -> None:
    with pytest.raises(MultiDimensionalAttributionError, match="只能出现一行"):
        analyze_multidimensional_attribution([absolute_rows()[0], absolute_rows()[0]], derived=False)


def test_explicit_issue_direction_must_match_observed_change() -> None:
    result = analyze_multidimensional_attribution(
        absolute_rows(), derived=False, issue_type="rise"
    )

    assert result["status"] == "no_anomaly_direction"
    assert all(
        detail["reason"] == "ISSUE_DIRECTION_MISMATCH"
        for detail in result["dimension_details"].values()
    )


def test_non_positive_ratio_denominator_is_skipped_safely() -> None:
    rows = [
        {"dimension_name": "区域", "element_value": "华东", "baseline_numerator": 1, "baseline_denominator": 0, "current_numerator": 1, "current_denominator": 0},
        {"dimension_name": "区域", "element_value": "华西", "baseline_numerator": 1, "baseline_denominator": 0, "current_numerator": 1, "current_denominator": 0},
    ]

    result = analyze_multidimensional_attribution(rows, derived=True)

    assert result["status"] == "no_root_cause"
    assert result["dimension_details"]["区域"]["reason"] == "NON_POSITIVE_DENOMINATOR"


def test_high_cardinality_dimension_is_bounded() -> None:
    rows = [
        {"dimension_name": "SKU", "element_value": str(index), "baseline": 1, "current": 0}
        for index in range(4)
    ]
    result = analyze_multidimensional_attribution(
        rows, derived=False, maximum_values_per_dimension=3
    )

    assert result["status"] == "no_root_cause"
    assert result["dimension_details"]["SKU"]["reason"] == "HIGH_CARDINALITY_DIMENSION"


def test_engine_routes_explicit_multidimensional_request() -> None:
    request = CanonicalAnalysisRequest(
        conversation_id="c", tenant_id="t", user_id="u",
        original_question="从各维度下钻分析销售额下降原因",
        primary_intent=PrimaryIntent.ROOT_CAUSE_ANALYSIS,
    )
    rows = absolute_rows()
    output = AnalysisEngine().analyze(request, list(rows[0]), rows, KnowledgeContext(query=""))

    assert output.method == "multidimensional_attribution"
    assert "渠道" in output.answer
    assert output.facts["causality_established"] is False
