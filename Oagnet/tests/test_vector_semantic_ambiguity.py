import json
from types import SimpleNamespace

import pytest

from agent import _ambiguity_asl, _vector_semantic_ambiguities


def _record(record_id: str, score: float, **metadata):
    return SimpleNamespace(id=record_id, score=score, metadata=metadata)


def _knowledge(type_name: str, records: list[SimpleNamespace]) -> dict:
    return {"_ambiguity_candidates": {type_name: records}}


def test_multiple_metrics_for_same_explicit_alias_return_all_canonical_choices():
    records = [
        _record(
            "m-sales-tax", 0.96, type="metric", metric_code="annual_total_sales",
            metric_name="含税销售总额", synonyms=["销售额"], business_domain_id=205,
        ),
        _record(
            "m-sales-net", 0.93, type="metric", metric_code="net_sales_amount",
            metric_name="不含税销售额", synonyms=["销售额"], business_domain_id=205,
        ),
    ]

    ambiguities = _vector_semantic_ambiguities(
        _knowledge("metric", records), "查询销售额"
    )

    assert len(ambiguities) == 1
    assert ambiguities[0]["type"] == "metric"
    assert ambiguities[0]["phrase"] == "销售额"
    assert ambiguities[0]["candidates"] == [
        "含税销售总额（annual_total_sales）",
        "不含税销售额（net_sales_amount）",
    ]
    assert len(ambiguities[0]["candidate_details"]) == 2


def test_independently_named_metrics_are_not_treated_as_one_ambiguity():
    records = [
        _record(
            "m-amount", 0.96, type="metric", metric_code="sales_amount",
            metric_name="销售额", synonyms=[],
        ),
        _record(
            "m-count", 0.95, type="metric", metric_code="order_count",
            metric_name="订单笔数", synonyms=[],
        ),
    ]

    assert _vector_semantic_ambiguities(
        _knowledge("metric", records), "查询销售额和订单笔数"
    ) == []


def test_caller_bound_metric_is_not_reopened_by_vector_candidates():
    records = [
        _record(
            "m-1", 0.96, type="metric", metric_code="m1",
            metric_name="指标一", synonyms=["销售"],
        ),
        _record(
            "m-2", 0.95, type="metric", metric_code="m2",
            metric_name="指标二", synonyms=["销售"],
        ),
    ]

    assert _vector_semantic_ambiguities(
        _knowledge("metric", records),
        "查询销售",
        preferred_metric_codes=["m1"],
    ) == []


def test_selected_canonical_code_does_not_repeat_relation_ambiguity():
    records = [
        _record(
            "r-dealer", 0.96, type="relation", relation_code="dealer_product",
            relation_name="合作", parent="dealer",
        ),
        _record(
            "r-hospital", 0.95, type="relation", relation_code="hospital_product",
            relation_name="合作", parent="hospital",
        ),
    ]

    assert _vector_semantic_ambiguities(
        _knowledge("relation", records),
        "原问题查询合作；用户已选择合作（dealer_product）",
    ) == []


@pytest.mark.parametrize(
    ("type_name", "ambiguity_type", "records"),
    [
        (
            "entity", "subject",
            [
                {"entity_code": "dealer", "entity_name": "经销商", "entity_alias": ["合作方"]},
                {"entity_code": "supplier", "entity_name": "供应商", "entity_alias": ["合作方"]},
            ],
        ),
        (
            "dimension", "dimension",
            [
                {"dim_code": "dealer_city", "dim_name": "经销商城市", "synonyms": ["城市"]},
                {"dim_code": "hospital_city", "dim_name": "医院城市", "synonyms": ["城市"]},
            ],
        ),
        (
            "attribute", "filter_slot",
            [
                {"attr_code": "dealer_level", "attr_name": "等级", "parent_name": "经销商"},
                {"attr_code": "hospital_level", "attr_name": "等级", "parent_name": "医院"},
            ],
        ),
        (
            "relation", "schema_relation",
            [
                {"relation_code": "dealer_product", "relation_name": "合作", "parent": "dealer"},
                {"relation_code": "hospital_product", "relation_name": "合作", "parent": "hospital"},
            ],
        ),
        (
            "entity_attribute_value", "entity_value",
            [
                {"attr_code": "brand", "attr_value": "华康", "entity_name": "商品品牌"},
                {"attr_code": "dealer", "attr_value": "华康", "entity_name": "经销商"},
            ],
        ),
    ],
)
def test_all_vectorized_semantic_types_return_choices(
    type_name, ambiguity_type, records
):
    candidates = [
        _record(f"{type_name}-{index}", 0.95 - index * 0.01, type=type_name, **item)
        for index, item in enumerate(records)
    ]

    ambiguities = _vector_semantic_ambiguities(
        _knowledge(type_name, candidates),
        "查询合作方按城市和等级查看合作华康",
    )

    assert any(item["type"] == ambiguity_type for item in ambiguities)


def test_unique_long_attribute_phrase_suppresses_nested_generic_alias():
    records = [
        _record(
            "dealer-name", 0.96, type="attribute", attr_code="dealer_name",
            attr_name="名称", parent_name="经销商",
        ),
        _record(
            "hospital-name", 0.95, type="attribute", attr_code="hospital_name",
            attr_name="名称", parent_name="医院",
        ),
    ]

    assert _vector_semantic_ambiguities(
        _knowledge("attribute", records), "查询经销商名称"
    ) == []


def test_ambiguity_asl_is_a_valid_non_executable_choice_envelope():
    ambiguity = {
        "type": "dimension",
        "question": "请选择维度",
        "candidates": ["医院城市（hospital_city）", "经销商城市（dealer_city）"],
    }

    payload = json.loads(_ambiguity_asl([ambiguity]))

    assert payload["version"] == "2.0"
    assert payload["metrics"] == []
    assert payload["dimensions"] == []
    assert payload["ambiguity"] == [ambiguity]
