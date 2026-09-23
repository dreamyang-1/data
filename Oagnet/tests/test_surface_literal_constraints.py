"""Amounts are comparisons; identifiers/models are not interchangeable IDs."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
import agent
import api
from asl_contract import ASLValidationError
from surface_literals import numeric_literal, planner_reference
from test_intent_asl_contract import _detail_ast


def catalog():
    entities = []
    for code, name, attributes in [
        ("sales_order", "销售订单", [
            ("order_code", "订单号", "varchar"),
            ("amount_with_tax", "含税金额", "decimal(18,2)"),
            ("quantity", "数量", "int"),
            ("created_date", "订单日期", "date"),
            ("status", "状态", "varchar"),
        ]),
        ("product", "商品", [("product_name", "商品名称", "varchar"),
                               ("specification", "规格型号", "varchar"),
                               ("id", "商品ID", "bigint")]),
        ("manufacturer", "厂家", [("parent_brand", "商品品牌", "varchar")]),
        ("product_dept_relation", "商品科室关联", [("id", "关联ID", "bigint")]),
    ]:
        entities.append(SimpleNamespace(metadata={
            "entity_code": code, "entity_name": name,
            "attributes": [{"attr_code": col, "attr_name": label, "data_type": dtype,
                            "field_mapping": f"{code}.{col}"} for col, label, dtype in attributes],
        }))
    knowledge = {"entities": entities, "metrics": [SimpleNamespace(metadata={
        "metric_code": "sales_total_including_tax", "metric_name": "含税销售总额",
        "synonyms": ["销售额"],
        "calculation_rule": {"calc_formula": "SUM(sales_order.amount_with_tax)"},
    })]}
    knowledge["_vector_authorized_fields"] = sorted(agent._known_physical_fields(knowledge))
    return knowledge


def extraction(field="含税金额", value=1000, operator="="):
    return {"意图": "明细查询", "实体": ["销售订单", "商品"], "指标": [], "维度": [],
            "展示字段": [{"entity": "销售订单", "field": "订单号"},
                          {"entity": "销售订单", "field": "含税金额"},
                          {"entity": "商品", "field": "商品名称"}],
            "过滤条件": [{"field": field, "op": operator, "value": [value]}]}


@pytest.mark.parametrize("operator,value", [
    ("=", 1000), (">", 1000), (">=", 0), ("<", -2), ("!=", -1),
    ("IN", [0, 1000]), ("NOT IN", [0, 1000]), ("BETWEEN", [-2, 1000]),
])
def test_bound_amount_never_searches_values_or_changes_operator(monkeypatch, operator, value):
    knowledge = catalog()
    reference = {"filters": [{"field": "含税金额", "operator": operator, "value": value}]}
    draft = json.loads(_detail_ast())
    draft["filters"] = [{"field": "product_dept_relation.id", "operator": "=", "value": str(v)}
                        for v in value] if isinstance(value, list) else [
                            {"field": "product_dept_relation.id", "operator": "=", "value": str(value)}]
    def forbidden(*a, **k):
        raise AssertionError("a numeric threshold must not use source value lookup")
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", forbidden)
    content, _ = agent._prepare_surface_literal_constraints(json.dumps(draft), knowledge, reference)
    content, _ = agent._apply_surface_mention_normalization(content, knowledge, {
        "mentions": [{"text": str(v)} for v in (value if isinstance(value, list) else [value])],
    }, 81, [205])
    assert json.loads(content)["filters"] == [
        {"field": "sales_order.amount_with_tax", "operator": operator, "value": value}]
    agent._validate_vector_grounded_asl(content, knowledge)


@pytest.mark.parametrize("field,value,physical", [
    ("订单号", "001000", "sales_order.order_code"),
    ("商品ID", "0012", "product.id"),
    ("状态", "A", "sales_order.status"),
    ("状态", False, "sales_order.status"),
    ("规格型号", "M60 set", "product.specification"),
    ("规格型号", "AT75242", "product.specification"),
])
def test_explicit_codes_letters_models_keep_field_and_literal(field, value, physical):
    content, _ = agent._prepare_surface_literal_constraints(_detail_ast(), catalog(),
        planner_reference(extraction(field, value), None))
    assert json.loads(content)["filters"] == [{"field": physical, "operator": "=", "value": value}]


@pytest.mark.parametrize("value,expected", [
    ("1,000.50", "1000.50"), ("1e3", 1000), ("+10", 10), ("-2.50", "-2.50"),
    ("0", 0), ("001000", 1000), ("0.1234567890123456789", "0.1234567890123456789"),
])
def test_numeric_measure_parsing_does_not_round(value, expected):
    content, _ = agent._prepare_surface_literal_constraints(_detail_ast(), catalog(),
        planner_reference(extraction(value=value), None))
    assert json.loads(content)["filters"][0]["value"] == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1,00", "1000元", "25%", "1e999"])
def test_invalid_numbers_or_ambiguous_units_are_not_guessed(value):
    with pytest.raises(ValueError):
        numeric_literal(value)


def test_same_value_on_two_fields_and_two_bounds_are_preserved():
    ref = {"filters": [{"field": f, "operator": op, "value": v} for f, op, v in [
        ("含税金额", ">", 0), ("含税金额", "<", 1000), ("数量", "=", 1000),
    ]]}
    content, _ = agent._prepare_surface_literal_constraints(_detail_ast(), catalog(), ref)
    assert [(f["field"], f["operator"], f["value"]) for f in json.loads(content)["filters"]] == [
        ("sales_order.amount_with_tax", ">", 0), ("sales_order.amount_with_tax", "<", 1000),
        ("sales_order.quantity", "=", 1000),
    ]


@pytest.mark.parametrize("value,field,label", [(1000, "sales_order.amount_with_tax", "含税金额"),
                                               ("A", "sales_order.status", "状态")])
def test_same_literal_in_a_name_and_scalar_does_not_delete_name_condition(value, field, label):
    named = {"field": "product.product_name", "operator": "=", "value": str(value)}
    draft = json.loads(_detail_ast())
    draft["filters"] = [named]
    reference = {"filters": [
        {"field": "商品名称", "operator": "=", "value": str(value)},
        {"field": label, "operator": "=", "value": value},
    ]}
    content, _ = agent._prepare_surface_literal_constraints(json.dumps(draft), catalog(), reference)
    assert json.loads(content)["filters"] == [named, {"field": field, "operator": "=", "value": value}]


def test_missing_numeric_field_is_specific_not_silently_omitted():
    with pytest.raises(ASLValidationError) as exc:
        agent._prepare_surface_literal_constraints(_detail_ast(), catalog(),
            planner_reference(extraction("未知金额"), None))
    assert exc.value.details["failed_filter"]["value"] == 1000
    assert exc.value.field == "未知金额"


def test_aggregate_threshold_remains_having_not_row_filter(monkeypatch):
    knowledge = catalog()
    draft = json.loads(_detail_ast())
    draft["metrics"] = [{"name": "sales_total_including_tax"}]
    draft["having"] = ["sales_total_including_tax > 1000"]
    reference = {"primary_intent": "METRIC_QUERY", "filters": [
        {"field": "含税销售总额", "operator": ">", "value": 1000}]}
    def forbidden(*a, **k):
        raise AssertionError("aggregate threshold is not an entity value")
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", forbidden)
    content, _ = agent._prepare_surface_literal_constraints(json.dumps(draft), knowledge, reference)
    content, _ = agent._apply_surface_mention_normalization(content, knowledge,
        {"mentions": [{"text": "1000"}]}, 81, [205])
    assert json.loads(content) == draft


@pytest.mark.parametrize("role", ["limit", "限制", "时间范围"])
def test_limit_or_time_number_is_not_a_value_filter(monkeypatch, role):
    def forbidden(*a, **k):
        raise AssertionError("non-filter number queried")
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", forbidden)
    content, repairs = agent._apply_surface_mention_normalization(_detail_ast(), catalog(),
        {"mentions": [{"text": "1000", "role_hint": role}]}, 81, [205])
    assert json.loads(content)["filters"] == []
    assert repairs == []


@pytest.mark.parametrize("mention,canonical", [("M60", "M600"), ("AB", "ABC"),
    ("0012", "12"), ("AT75242", "7"), ("M-60", "M60"), ("1000", "10000")])
def test_substrings_are_not_literal_identity(mention, canonical):
    assert agent._select_surface_mention_match(mention, [{
        "field": "product.specification", "canonical_value": canonical,
        "match_type": "CANONICAL_CONTAINS_MENTION",
    }], {"product.specification"}, allow_role_containment=True) is None


@pytest.mark.parametrize("mention,canonical,kind", [
    ("M60", "Model M60 set", "CANONICAL_CONTAINS_MENTION"),
    ("品牌Model M60 set", "Model M60 set", "MENTION_CONTAINS_CANONICAL"),
    ("GE", "GE", "EXACT"), ("MMT-866A", "MMT-866A", "EXACT"),
])
def test_complete_model_tokens_and_letter_names_still_resolve(mention, canonical, kind):
    assert agent._select_surface_mention_match(mention, [{
        "field": "product.specification", "canonical_value": canonical, "match_type": kind,
    }], {"product.specification"}, allow_role_containment=True) == ("product.specification", canonical)


def test_bare_number_never_expands_to_unrelated_id(monkeypatch):
    knowledge = catalog()
    seen = []
    monkeypatch.setattr(agent, "load_published_entity_attribute_candidates", lambda *a: [
        {"entity_code": "product_dept_relation", "field": "product_dept_relation.id"},
    ])
    def resolve(_sm, _scope, candidates, value):
        seen.extend(c["field"] for c in candidates)
        return [{"field": "product_dept_relation.id", "canonical_value": value, "match_type": "EXACT"}]
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", resolve)
    content, _ = agent._apply_surface_mention_normalization(_detail_ast(), knowledge,
        {"mentions": [{"text": "1000"}]}, 81, [205])
    assert "product_dept_relation.id" not in seen
    assert json.loads(content)["filters"] == []


def test_detail_shape_restored_from_requested_grounded_columns():
    raw = extraction()
    draft = json.loads(_detail_ast())
    draft["metrics"] = [{"name": "sales_total_including_tax"}]
    draft["dimensions"] = [{"name": "product.product_name"}]
    content, repairs = agent._restore_surface_detail_shape(json.dumps(draft), catalog(),
        planner_reference(raw, None), raw, "查询单笔销售额为1000的订单")
    result = json.loads(content)
    assert result["metrics"] == []
    assert [x["name"] for x in result["dimensions"]] == [
        "sales_order.order_code", "sales_order.amount_with_tax", "product.product_name"]
    assert repairs
    agent._validate_vector_grounded_asl(content, catalog())
    agent._validate_asl_output(content, catalog(), "查询单笔销售额为1000的订单", [])


@pytest.mark.parametrize("query", ["统计金额1000的订单笔数", "查询销售额趋势", "按产品汇总订单销售额"])
def test_aggregate_question_overrides_wrong_detail_hint(query):
    raw = extraction()
    content = _detail_ast()
    assert agent._restore_surface_detail_shape(content, catalog(), planner_reference(raw, None), raw, query) == (content, [])


def test_api_does_not_discard_planner_extraction(monkeypatch):
    raw = extraction()
    observed = {}
    def generate(*args, **kwargs):
        observed.update(kwargs)
        raise ASLValidationError("ASL_FILTER_INVALID", "test stop")
    monkeypatch.setattr(api, "main", generate)
    request = api.QueryRequest(query="单笔销售额为1000的订单", semantic_model_id=81,
                               structured_extraction=raw)
    with pytest.raises(HTTPException):
        api.agent_query(request)
    assert observed["structured_extraction"] == raw


def test_main_uses_planner_payload_preserves_shape_and_validates(monkeypatch):
    knowledge = catalog()
    raw = extraction()
    observed = {}
    class Builder:
        def __init__(self, *a, **k):
            self.last_knowledge = deepcopy(knowledge)
        def build(self, query):
            return "scoped catalog"
    draft = json.loads(_detail_ast())
    draft["metrics"] = [{"name": "sales_total_including_tax"}]
    draft["filters"] = [{"field": "product_dept_relation.id", "operator": "=", "value": "1000"}]
    def model(**kwargs):
        observed.update(kwargs)
        return SimpleNamespace(invoke=lambda _: {"messages": [SimpleNamespace(content=json.dumps(draft))]})
    monkeypatch.setattr(agent, "PromptBuilder", Builder)
    monkeypatch.setattr(agent, "_get_chat_model", lambda: object())
    monkeypatch.setattr(agent, "create_deep_agent", model)
    monkeypatch.setattr(agent, "_normalize_dynamic_subject", lambda value, *a: value)
    result = agent.main("查询单笔销售额为1000的订单", store=object(), semantic_model_id=81,
        business_domain_ids=[205], surface_evidence={"mentions": [{"text": "1000"}]},
        structured_extraction=raw,
        structured_reference={"primary_intent": "METRIC_QUERY", "metrics": [{"input": "销售额"}]})
    ast = json.loads(result)
    assert ast["metrics"] == []
    assert ast["filters"] == [{"field": "sales_order.amount_with_tax", "operator": "=", "value": 1000}]
    assert "Planner extraction" in observed["system_prompt"]
    assert '"op":"="' in observed["system_prompt"]
