"""Same-place hierarchy defaults must agree before and after ASL generation."""
import json
from types import SimpleNamespace

import pytest

from agent import (
    _administrative_vector_defaults,
    _apply_administrative_vector_defaults,
    _prefer_finest_administrative_matches,
    _vector_semantic_ambiguities,
)


def candidate(kind, value="上海市", owner=None, **extra):
    return dict(
        type="entity_attribute_value", semantic_model_id=81, business_domain_id=205,
        attr_code=f"{kind}_name", attr_value=value,
        source_field=f"{owner or 'dim_' + kind}.{kind}_name", **extra,
    )


def knowledge(items):
    records = [SimpleNamespace(id=str(i), score=0.99 - i * 0.01, metadata=m)
               for i, m in enumerate(items)]
    return {"entity_attribute_values": records,
            "_ambiguity_candidates": {"entity_attribute_value": records}}


@pytest.mark.parametrize("name", ["上海市", "北京市", "天津市", "重庆市", "示例地区"])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_place_province_city_never_prompts_and_uses_city(name, reverse):
    items = [candidate("province", name), candidate("city", name)]
    if reverse:
        items.reverse()
    query = f"各经销商在{name}的区域医院覆盖率"
    info = knowledge(items)
    assert _vector_semantic_ambiguities(info, query) == []
    assert _administrative_vector_defaults(info, query) == [candidate("city", name)]


def test_three_levels_choose_smallest_recalled_level():
    items = [candidate(kind, "示例区") for kind in ("district", "province", "city")]
    assert _prefer_finest_administrative_matches(items, "查询示例区") == items[:1]


@pytest.mark.parametrize("selection", ["province_name", "dim_province.province_name", "按省份口径"])
def test_explicit_previous_selection_wins_over_finer_default(selection):
    items = [candidate("province"), candidate("city")]
    query = f"查询上海市，用户选择：{selection}"
    assert _prefer_finest_administrative_matches(items, query) == items[:1]
    assert _vector_semantic_ambiguities(knowledge(items), query) == []


@pytest.mark.parametrize("items", [
    [candidate("province")],
    [candidate("province", "甲省"), candidate("city", "乙市")],
    [candidate("province", owner="hospital"), candidate("city", owner="dealer")],
    [candidate("province", province_code="A"), candidate("city", province_code="B")],
    [candidate("province"), {**candidate("city"), "business_domain_id": 206}],
    [candidate("province"), {**candidate("city"), "semantic_model_id": 82}],
    [candidate("province"), candidate("city", attr_name="城市详细地址")],
    [candidate("province", owner="dealer"), candidate("city", owner="dealer", attr_name="注册城市")],
])
def test_non_equivalent_candidates_are_not_collapsed(items):
    assert _prefer_finest_administrative_matches(items, "查询上海市") == items
    assert _administrative_vector_defaults(knowledge(items), "查询上海市") == []


def test_same_finest_level_alternatives_are_retained():
    items = [candidate("province"), candidate("city"),
             {**candidate("city"), "attr_code": "other_city_name"}]
    assert _prefer_finest_administrative_matches(items, "上海市") == items[1:]
    assert _vector_semantic_ambiguities(knowledge(items), "上海市")


def test_final_binding_changes_only_same_place_filter_not_grouping_or_other_filters():
    items = [candidate("province"), candidate("city")]
    ast = {"filters": [
        {"field": "dim_province.province_name", "operator": "=", "value": "上海市"},
        {"field": "hospital.province_name", "operator": "=", "value": "上海市"},
        {"field": "sales.amount", "operator": ">", "value": 1000},
    ], "dimensions": [{"name": "dealer.dealer_name"}], "ambiguity": []}
    result, repairs = _apply_administrative_vector_defaults(
        json.dumps(ast), knowledge(items), "各经销商在上海市的区域医院覆盖率",
    )
    actual = json.loads(result)
    assert actual["filters"][0]["field"] == "dim_city.city_name"
    assert actual["filters"][1:] == ast["filters"][1:]
    assert actual["dimensions"] == ast["dimensions"]
    assert len(repairs) == 1


def test_final_binding_respects_explicit_province_and_no_city_is_invented():
    ast = {"filters": [{"field": "dim_province.province_name", "operator": "=", "value": "上海市"}]}
    for items, query in [([candidate("province")], "上海市"),
                         ([candidate("province"), candidate("city")], "上海市（province_name）")]:
        content, repairs = _apply_administrative_vector_defaults(json.dumps(ast), knowledge(items), query)
        assert json.loads(content) == ast
        assert repairs == []


@pytest.mark.parametrize("all_supported", [False, True])
def test_in_list_moves_only_when_all_values_support_same_target(all_supported):
    items = [candidate("province", name) for name in ("上海市", "北京市")]
    items.append(candidate("city"))
    if all_supported:
        items.append(candidate("city", "北京市"))
    ast = {"filters": [{"field": "dim_province.province_name", "operator": "IN", "value": ["上海市", "北京市"]}]}
    content, _ = _apply_administrative_vector_defaults(json.dumps(ast), knowledge(items), "查询上海市和北京市")
    actual = json.loads(content)["filters"][0]
    assert actual["field"] == ("dim_city.city_name" if all_supported else "dim_province.province_name")
    assert actual["value"] == ast["filters"][0]["value"]


@pytest.mark.parametrize("model_field", ["province_name", "city_name"])
@pytest.mark.parametrize("model_asks", [False, True])
def test_main_passes_pre_model_gate_and_returns_city_without_bypassing_validation_structured_handoff_required(monkeypatch, model_field, model_asks):
    # STALE_TEST: query-only/advisory extraction and silent slot dropping were retired.
    # Parameter binding, optional display and scalar shapes are covered by
    # test_structured_binding.py and test_scalar_metric_grain.py.
    import agent
    result = agent.main('legacy question', semantic_model_id=81, business_domain_ids=[205])
    ast = json.loads(result)
    assert ast['ambiguity'] and '上游未提供' in ast['ambiguity'][0]['question']
    assert ast['metrics'] == [] and ast['filters'] == []


def test_only_resolved_hierarchy_question_is_cleared():
    info = knowledge([candidate("province"), candidate("city")])
    resolved = {"type": "entity_value", "phrase": "上海市", "candidates": ["province_name", "city_name"]}
    unrelated = [
        {"type": "entity_value", "phrase": "上海市", "candidates": ["hospital.province_name", "dealer.city_name"]},
        {"type": "entity_value", "phrase": "北京市", "candidates": ["province_name", "city_name"]},
        {"type": "dimension", "phrase": "上海市", "candidates": ["province_name", "city_name"]},
    ]
    ast = {"filters": [{"field": "dim_city.city_name", "operator": "=", "value": "上海市"}],
           "ambiguity": [resolved, *unrelated]}
    content, _ = _apply_administrative_vector_defaults(json.dumps(ast), info, "上海市")
    assert json.loads(content)["ambiguity"] == unrelated
    ast["filters"] = []
    content, _ = _apply_administrative_vector_defaults(json.dumps(ast), info, "上海市")
    assert json.loads(content)["ambiguity"] == ast["ambiguity"]
