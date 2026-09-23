import copy
import json

import pytest
import agent
from test_live_semantic_regressions import _entity, _asl


def knowledge():
    return {"entities": [_entity("dealer", "经销商", [
        {"attr_code": "dealer_name", "attr_name": "经销商名称", "field_mapping": "dealer.dealer_name"},
        {"attr_code": "city", "attr_name": "城市", "field_mapping": "dealer.city"},
    ])], "dimensions": [], "metrics": [], "relations": [],
        "_vector_authorized_fields": ["dealer.dealer_name", "dealer.city"]}


def reference():
    return {"primary_intent": "DETAIL_QUERY", "entity": "经销商", "fields": ["经销商名称", "联系方式"],
            "dimensions": [], "metrics": [], "filters": [{"field": "城市", "value": "上海"}]}


def draft():
    ast = _asl(dimensions=[{"name": "dealer.dealer_name"}])
    ast["subject"] = {"entity": "dealer"}
    ast["filters"] = [{"field": "dealer.city", "operator": "=", "value": "上海"}]
    return ast


def relax(ast, ref=None, original=None, catalog=None):
    result, repairs = agent._allow_partial_surface_projections(
        json.dumps(ast, ensure_ascii=False), catalog or knowledge(), ref or reference(),
        json.dumps(original or ast, ensure_ascii=False),
    )
    return json.loads(result), repairs


def test_missing_contact_is_notice_and_valid_name_filter_are_preserved():
    ast = draft()
    ast["ambiguity"] = [{"type": "dimension", "question": "联系方式未匹配，请确认展示字段", "candidates": []}]
    actual, repairs = relax(ast)
    assert actual["ambiguity"] == []
    assert actual["filters"] == ast["filters"]
    assert actual["dimensions"] == ast["dimensions"]
    assert repairs[0]["field"] == "联系方式"
    agent._validate_vector_grounded_asl(json.dumps(actual), knowledge())
    agent._validate_asl_output(json.dumps(actual), knowledge(), "上海经销商及联系方式")


@pytest.mark.parametrize("field", ["邮箱", "地址", "开票备注"])
def test_silently_omitted_display_field_also_has_specific_notice(field):
    ref = reference()
    ref["fields"] = ["经销商名称", field]
    actual, repairs = relax(draft(), ref)
    assert actual == draft()
    assert repairs[0]["field"] == field


def test_expanded_email_address_reference_can_resolve_generic_contact_ambiguity():
    ref = reference()
    ref["fields"] = ["经销商名称", "邮箱", "地址"]
    ast = draft()
    ast["ambiguity"] = [{"type": "dimension", "question": "联系方式缺少目录匹配", "candidates": []}]
    actual, repairs = relax(ast, ref)
    assert actual["ambiguity"] == []
    assert [item["field"] for item in repairs] == ["邮箱", "地址"]


def test_ambiguous_catalog_field_is_reported_without_random_projection():
    catalog = knowledge()
    for code in ("office_phone", "mobile_phone"):
        catalog["entities"][0].metadata["attributes"].append({
            "attr_code": code, "attr_name": "电话", "field_mapping": "dealer." + code})
        catalog["_vector_authorized_fields"].append("dealer." + code)
    ast = draft()
    ast["ambiguity"] = [{"type": "dimension", "phrase": "电话", "question": "电话对应多个字段", "candidates": []}]
    ref = reference()
    ref["fields"] = ["经销商名称", "电话"]
    actual, repairs = relax(ast, ref, catalog=catalog)
    assert actual["ambiguity"] == []
    assert actual["dimensions"] == draft()["dimensions"]
    assert repairs[0]["field"] == "电话"


def test_real_semantic_normalization_then_partial_output():
    raw = draft()
    raw["dimensions"].append({"name": "dealer.contact_info"})
    normalized = agent._normalize_semantic_references(json.dumps(raw), knowledge(), "上海经销商及联系方式", 81, 205)
    assert json.loads(normalized)["ambiguity"]
    actual, repairs = relax(json.loads(normalized), original=raw)
    assert actual["ambiguity"] == []
    assert actual["dimensions"] == [{"name": "dealer.dealer_name"}]
    assert repairs


@pytest.mark.parametrize("kind", ["filter", "entity", "time", "metric"])
def test_non_display_ambiguities_remain_blocking(kind):
    ast = draft()
    ast["ambiguity"] = [{"type": kind, "question": "联系方式不能作为该约束", "candidates": []}]
    actual, _ = relax(ast)
    assert actual["ambiguity"] == ast["ambiguity"]


@pytest.mark.parametrize("key,value", [("sort", {"field": "dealer.contact_info"}),
    ("having", ["dealer.contact_info IS NOT NULL"]), ("metrics", [{"name": "sales"}]),
    ("time_context", {"anchor": "dealer.contact_info"})])
def test_execution_constraints_not_silently_removed(key, value):
    ast = draft()
    ast[key] = value
    ast["dimensions"].append({"name": "dealer.contact_info"})
    actual, repairs = relax(ast)
    assert actual == ast and repairs == []


def test_contact_filter_or_grouping_does_not_become_optional():
    for key in ("filters", "dimensions"):
        ref = reference()
        ref[key] = [{"field": "联系方式", "value": "123"}] if key == "filters" else ["联系方式"]
        actual, repairs = relax(draft(), ref)
        assert actual == draft() and repairs == []


def test_actual_caller_entity_dimensions_do_not_block_optional_display_degradation():
    # The existing classifier populates entity hints here even for DETAIL_QUERY.
    ref = reference()
    ref.update(dimensions=["商品", "经销商"], fields=["经销商名称", "邮箱", "地址"],
               operators=["FILTER", "RENDER_TABLE"], time_range={"start": "2025-09-23", "end": "2026-09-23"})
    ast = draft()
    ast["sort"] = {"field": "dealer.dealer_name", "direction": "ASC", "field_type": "field"}
    actual, repairs = relax(ast, ref)
    assert actual == ast
    assert [item["field"] for item in repairs] == ["邮箱", "地址"]


def test_all_requested_outputs_missing_must_not_substitute_unrequested_name():
    ref = reference()
    ref["fields"] = ["联系方式"]
    assert relax(draft(), ref)[1] == []
    ast = draft()
    ast["dimensions"] = []
    assert relax(ast)[1] == []


def test_existing_contact_expansion_is_not_reported_missing():
    catalog = knowledge()
    catalog["entities"][0].metadata["attributes"].append(
        {"attr_code": "email", "attr_name": "邮箱", "field_mapping": "dealer.email"})
    catalog["_vector_authorized_fields"].append("dealer.email")
    ast = draft()
    ast["dimensions"].append({"name": "dealer.email"})
    assert relax(ast, catalog=catalog)[1] == []


def test_generation_integration_emits_repairs_without_bypassing_validator(monkeypatch):
    catalog = knowledge()
    ast = draft()
    ast["ambiguity"] = [{"type": "dimension", "question": "联系方式未匹配", "candidates": []}]
    class Builder:
        def __init__(self, *args, **kwargs):
            self.last_knowledge = copy.deepcopy(catalog)
        def build(self, query):
            return "offline catalog"
    class Model:
        def invoke(self, payload):
            return {"messages": [type("Message", (), {"content": json.dumps(ast)})()]}
    monkeypatch.setattr(agent, "PromptBuilder", Builder)
    monkeypatch.setattr(agent, "create_deep_agent", lambda **kwargs: Model())
    monkeypatch.setattr(agent, "_normalize_dynamic_subject", lambda content, *args: content)
    output = agent.main("上海经销商及联系方式", store=object(), semantic_model_id=81,
                        surface_evidence={"mentions": []}, structured_reference=reference(),
                        include_evidence=True)
    assert json.loads(output["result"])["ambiguity"] == []
    assert any(item["type"] == "OMIT_UNAVAILABLE_DISPLAY_FIELD" for item in output["asl_repair"])
