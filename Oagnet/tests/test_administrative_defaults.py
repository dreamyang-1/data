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
def test_main_passes_pre_model_gate_and_returns_city_without_bypassing_validation(monkeypatch, model_field, model_asks):
    import agent
    from copy import deepcopy

    items = [candidate("province", owner="dealer"), candidate("city", owner="dealer")]
    catalog = knowledge(items)
    catalog["entities"] = [SimpleNamespace(id="dealer", score=1, metadata={
        "type": "entity", "entity_code": "dealer", "entity_name": "经销商",
        "semantic_model_id": 81, "business_domain_id": 205,
        "attributes": [{"attr_code": code, "attr_name": label, "field_mapping": f"dealer.{code}"}
                       for code, label in [("dealer_name", "经销商名称"), ("province_name", "省份名称"), ("city_name", "城市名称")]],
    })]
    ast = {"version": "2.0", "intent": "query", "subject": {"entity": "dealer"}, "metrics": [],
           "dimensions": [{"name": "dealer.dealer_name", "attr": None, "level": None, "granularity": None}],
           "filters": [{"field": f"dealer.{model_field}", "operator": "=", "value": "上海市"}],
           "time_context": None, "sort": None, "limit": None, "having": [], "ambiguity": []}
    if model_asks:
        ast["ambiguity"] = [{"type": "entity_value", "phrase": "上海市",
                             "question": "上海市请选择层级", "candidates": ["上海市（province_name）", "上海市（city_name）"]}]
    calls = []
    class Builder:
        def __init__(self, *args, **kwargs):
            self.last_knowledge = deepcopy(catalog)
        def build(self, query):
            return "offline scoped catalog"
    def factory(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(invoke=lambda _: {"messages": [SimpleNamespace(content=json.dumps(ast))]})
    monkeypatch.setattr(agent, "PromptBuilder", Builder)
    monkeypatch.setattr(agent, "create_deep_agent", factory)
    monkeypatch.setattr(agent, "_get_chat_model", lambda: object())
    monkeypatch.setattr(agent, "_normalize_dynamic_subject", lambda value, *args: value)
    result = agent.main("查询上海市经销商", store=object(), semantic_model_id=81,
                        business_domain_ids=[205], include_evidence=True)
    actual = json.loads(result["result"])
    assert len(calls) == 1  # Used to return a blocking candidate envelope before the model.
    assert actual["filters"] == [{"field": "dealer.city_name", "operator": "=", "value": "上海市"}]
    assert actual["ambiguity"] == []
    assert actual["dimensions"] == ast["dimensions"]
    assert result["asl_validation"] == "PASS"


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
