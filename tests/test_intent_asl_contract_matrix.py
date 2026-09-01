from __future__ import annotations

import pytest

from app.domain.models import PrimaryIntent, TrustedIdentity
from app.intent.classifier import RuleBasedIntentClassifier
from app.services.intent_asl_contract import (
    build_intent_asl_contract,
    validate_intent_asl_contract_completeness,
    validate_intent_asl_contract_definition,
)
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.turn_admission import TurnAdmissionGate
from app.services.relationship_projection import (
    requires_distinct_relationship_projection,
)


IDENTITY = TrustedIdentity(tenant_id="tenant", user_id="user", roles=["analyst"])


MATRIX = (
    # case, question, intent, object, metric_required, metric, projection,
    # positive filter field, negative filter field, sorted
    (1, "查询紫杉醇释放冠脉球囊导管产品合作经销商", PrimaryIntent.DETAIL_QUERY, "经销商", False, None, "经销商名称", "商品名称", None, False),
    (2, "查询紫杉醇释放冠脉球囊导管产品合作医院", PrimaryIntent.DETAIL_QUERY, "医院", False, None, "医院名称", "商品名称", None, False),
    (3, "查询振德医疗厂家产品", PrimaryIntent.DETAIL_QUERY, "商品", False, None, "商品名称", "厂家名称", None, False),
    (4, "查询紫杉醇释放冠脉球囊导管产品厂家", PrimaryIntent.DETAIL_QUERY, "厂家", False, None, "厂家名称", "商品名称", None, False),
    (5, "查询上海经销商", PrimaryIntent.DETAIL_QUERY, "经销商", False, None, "经销商名称", "地区", None, False),
    (6, "查询紫杉醇释放冠脉球囊导管产品规格", PrimaryIntent.DETAIL_QUERY, "商品", False, None, "商品规格", "商品名称", None, False),
    (7, "查询紫杉醇释放冠脉球囊导管产品销售额", PrimaryIntent.METRIC_QUERY, "产品", True, "销售额", None, "商品名称", None, False),
    (8, "查询紫杉醇释放冠脉球囊导管产品销售量", PrimaryIntent.METRIC_QUERY, "产品", True, "销售量", None, "商品名称", None, False),
    (9, "紫杉醇释放冠脉球囊导管产品销售额最高的经销商", PrimaryIntent.METRIC_QUERY, "经销商", True, "销售额", None, "商品名称", None, True),
    (10, "上海销售规模最大的医院", PrimaryIntent.METRIC_QUERY, "医院", True, "整体业务规模", None, "地区", None, True),
    (11, "紫杉醇释放冠脉球囊导管产品按月销售趋势", PrimaryIntent.TREND_ANALYSIS, "产品", True, "销售额", None, "商品名称", None, False),
    (12, "上海紫杉醇释放冠脉球囊导管产品经销商", PrimaryIntent.DETAIL_QUERY, "经销商", False, None, "经销商名称", "地区", None, False),
    (13, "排除振德医疗厂家的医用外科口罩产品经销商", PrimaryIntent.DETAIL_QUERY, "经销商", False, None, "经销商名称", "商品名称", "厂家名称", False),
)

EXPECTED_FILTERS = {
    1: ([{"field": "商品名称", "operator": "EQ", "value": "紫杉醇释放冠脉球囊导管"}], []),
    2: ([{"field": "商品名称", "operator": "EQ", "value": "紫杉醇释放冠脉球囊导管"}], []),
    3: ([{"field": "厂家名称", "operator": "EQ", "value": "振德医疗"}], []),
    4: ([{"field": "商品名称", "operator": "EQ", "value": "紫杉醇释放冠脉球囊导管"}], []),
    5: ([{"field": "地区", "operator": "EQ", "value": "上海市"}], []),
    6: ([{"field": "商品名称", "operator": "EQ", "value": "紫杉醇释放冠脉球囊导管"}], []),
    7: ([{"field": "商品名称", "operator": "EQ", "value": "紫杉醇释放冠脉球囊导管"}], []),
    8: ([{"field": "商品名称", "operator": "EQ", "value": "紫杉醇释放冠脉球囊导管"}], []),
    9: ([{"field": "商品名称", "operator": "EQ", "value": "紫杉醇释放冠脉球囊导管"}], []),
    10: ([{"field": "地区", "operator": "EQ", "value": "上海市"}], []),
    11: ([{"field": "商品名称", "operator": "EQ", "value": "紫杉醇释放冠脉球囊导管"}], []),
    12: ([
        {"field": "地区", "operator": "EQ", "value": "上海市"},
        {"field": "商品名称", "operator": "EQ", "value": "紫杉醇释放冠脉球囊导管"},
    ], []),
    13: (
        [{"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"}],
        [{"field": "厂家名称", "operator": "NE", "value": "振德医疗"}],
    ),
}


@pytest.mark.parametrize(
    "case,question,intent,query_object,metric_required,metric,projection,positive_field,negative_field,sorted_result",
    MATRIX,
)
def test_intent_asl_contract_regression_matrix(
    case,
    question,
    intent,
    query_object,
    metric_required,
    metric,
    projection,
    positive_field,
    negative_field,
    sorted_result,
):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, f"matrix-{case}")
    contract = build_intent_asl_contract(request)

    assert request.primary_intent == intent
    assert contract["query_object"] == query_object
    assert contract["metric_required"] is metric_required
    assert [item.input for item in request.metrics] == ([metric] if metric else [])
    assert contract["required_metrics"] == ([metric] if metric else [])
    assert contract["required_projections"] == ([projection] if projection else [])
    assert contract["projection_mode"] == (
        "ROWS" if case == 6
        else "DISTINCT" if intent == PrimaryIntent.DETAIL_QUERY
        else None
    )
    assert any(item["field"] == positive_field for item in contract["filters"])
    assert (
        [item["field"] for item in contract["negative_filters"]]
        == ([negative_field] if negative_field else [])
    )
    assert (contract["filters"], contract["negative_filters"]) == EXPECTED_FILTERS[case]
    assert (contract["sorting"] is not None) is sorted_result
    if sorted_result:
        assert contract["sorting"]["direction"] == "DESC"
        assert contract["sorting"]["limit"] == 1
    assert contract["time_dimension_required"] is (intent == PrimaryIntent.TREND_ANALYSIS)
    period_independent = any(
        item in {
            "TIME_SCOPE=PROFILE_SNAPSHOT",
            "TIME_SCOPE=ALL_TIME",
            "TIME_SCOPE=ALL_AVAILABLE_HISTORY",
        }
        for item in request.assumptions
    )
    assert contract["time_policy"] == (
        "REQUIRED" if intent == PrimaryIntent.TREND_ANALYSIS
        else "FORBIDDEN" if intent == PrimaryIntent.DETAIL_QUERY or period_independent
        else "OPTIONAL"
    )
    assert validate_intent_asl_contract_definition(contract) == []


def test_canonical_detail_rewrite_does_not_disable_relationship_set_semantics():
    request = RuleBasedIntentClassifier().classify(
        "查询A产品合作经销商名单", IDENTITY, "canonical-detail-rewrite",
    )
    request.rewritten_question = "查询明细；对象：经销商；字段：经销商名称"

    assert requires_distinct_relationship_projection(request) is True
    assert build_intent_asl_contract(request)["projection_mode"] == "DISTINCT"


@pytest.mark.parametrize(
    "question,expected_intent,expected_object,expected_metric,expected_filters,expected_sorting",
    (
        (
            "查询上海市医用外科口罩产品的经销商，排除上海洁安厂家，并按整体业务规模排序。",
            PrimaryIntent.METRIC_QUERY,
            "经销商",
            "整体业务规模",
            [
                {"field": "业务城市", "operator": "EQ", "value": "上海市"},
                {"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"},
                {"field": "厂家名称", "operator": "NE", "value": "上海洁安"},
            ],
            {"required": True, "direction": "DESC", "limit": None},
        ),
        (
            "查询上海市空心纤维血液透析器产品销售额。",
            PrimaryIntent.METRIC_QUERY,
            "产品",
            "销售额",
            [
                {"field": "商品名称", "operator": "EQ", "value": "空心纤维血液透析器"},
                {"field": "地区", "operator": "EQ", "value": "上海市"},
            ],
            None,
        ),
    ),
)
def test_p0_composite_query_contracts_are_lossless(
    question,
    expected_intent,
    expected_object,
    expected_metric,
    expected_filters,
    expected_sorting,
):
    classifier = RuleBasedIntentClassifier()
    request = classifier.classify(question, IDENTITY, "p0-composite")
    decision = TurnAdmissionGate().evaluate(
        question=question,
        current=request,
        previous=None,
        message_id="p0-message",
    )
    request.turn_admission = decision
    contract = build_intent_asl_contract(request)

    assert request.primary_intent == expected_intent
    assert request.entity == expected_object
    assert [item.input for item in request.metrics] == [expected_metric]
    assert request.filters == expected_filters
    assert "产品" not in request.dimensions
    assert contract["query_object"] == expected_object
    assert [*contract["filters"], *contract["negative_filters"]] == expected_filters
    assert contract["sorting"] == expected_sorting
    assert validate_intent_asl_contract_definition(contract) == []
    assert validate_intent_asl_contract_completeness(contract, request) == []


def test_contract_completeness_rejects_post_classification_filter_loss():
    question = "查询上海市空心纤维血液透析器产品销售额。"
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "p0-loss-detection"
    )
    decision = TurnAdmissionGate().evaluate(
        question=question,
        current=request,
        previous=None,
        message_id="p0-loss-message",
    )
    request.turn_admission = decision
    request.filters = [
        item for item in request.filters
        if item.get("field") != "商品名称"
    ]

    errors = validate_intent_asl_contract_completeness(
        build_intent_asl_contract(request), request
    )

    assert [item["code"] for item in errors] == ["EXPLICIT_FILTER_MISSING"]
    assert errors[0]["expected"] == [
        {"field": "商品名称", "operator": "EQ", "value": "空心纤维血液透析器"}
    ]


def test_relationship_count_projection_is_an_audited_contract_transform():
    question = "统计上海市紫杉醇释放冠脉球囊导管已合作医院数。"
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "relationship-count-transform"
    )
    request.turn_admission = TurnAdmissionGate().evaluate(
        question=question,
        current=request,
        previous=None,
        message_id="relationship-count-message",
    )

    projection_request = (
        DataAnalysisOrchestrator._relationship_count_projection_request(request)
    )
    assert projection_request is not None
    contract = build_intent_asl_contract(projection_request)

    assert projection_request.execution_contract_transform == (
        "RELATIONSHIP_COUNT_TO_DISTINCT_PROJECTION"
    )
    assert contract["intent"] == PrimaryIntent.DETAIL_QUERY.value
    assert contract["required_projections"] == ["医院名称"]
    assert validate_intent_asl_contract_completeness(
        contract, projection_request
    ) == []


def test_relationship_count_projection_still_rejects_filter_loss():
    question = "统计上海市紫杉醇释放冠脉球囊导管已合作医院数。"
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "relationship-count-filter-loss"
    )
    request.turn_admission = TurnAdmissionGate().evaluate(
        question=question,
        current=request,
        previous=None,
        message_id="relationship-count-filter-message",
    )
    projection_request = (
        DataAnalysisOrchestrator._relationship_count_projection_request(request)
    )
    assert projection_request is not None
    projection_request.filters = [
        item for item in projection_request.filters
        if item.get("field") != "商品名称"
    ]

    errors = validate_intent_asl_contract_completeness(
        build_intent_asl_contract(projection_request), projection_request
    )

    assert [item["code"] for item in errors] == ["EXPLICIT_FILTER_MISSING"]
