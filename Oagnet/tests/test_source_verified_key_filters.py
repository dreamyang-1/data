"""Source-backed literal codes must survive the natural-name safety gate."""
import json
from types import SimpleNamespace

import pytest
import agent
from asl_contract import ASLValidationError


def fixture():
    knowledge = {"entities": [SimpleNamespace(metadata={
        "entity_code": "product", "entity_name": "商品",
        "attributes": json.dumps([
            {"attr_code": "product_name", "attr_name": "商品名称",
             "field_mapping": "product.product_name"},
            {"attr_code": "material_code", "attr_name": "厂家物料编码",
             "field_mapping": "product.material_code"},
        ], ensure_ascii=False),
    })]}
    ast = {"version": "2.0", "intent": "query", "subject": {"entity": "product"},
           "metrics": [], "dimensions": [{"name": "product.product_name", "attr": None,
                                            "level": None, "granularity": None}],
           "filters": [], "time_context": None,
           "sort": None, "limit": None, "having": [], "ambiguity": []}
    return knowledge, ast


def test_source_lookup_replaces_wildcard_draft_and_validates_text_code(monkeypatch):
    knowledge, ast = fixture()
    ast["filters"] = [{"field": "product.product_name", "operator": "LIKE",
                       "value": "%品牌Model M60 set%"}]
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", lambda *_: [])
    monkeypatch.setattr(agent, "load_published_entity_attribute_candidates", lambda *_: [
        {"entity_code": "product", "field": "product.material_code"},
    ])
    def resolve(_sm, _domain, candidates, _mention):
        return [{"field": "product.material_code", "canonical_value": "Model M60 set",
                 "match_type": "MENTION_CONTAINS_CANONICAL"}] if any(
                     c["field"] == "product.material_code" for c in candidates) else []
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", resolve)
    content, _ = agent._apply_surface_mention_normalization(
        json.dumps(ast), knowledge,
        {"mentions": [{"text": "品牌Model M60 set", "role_hint": "商品"}]}, 81, 205,
    )
    assert json.loads(content)["filters"] == [
        {"field": "product.material_code", "operator": "=", "value": "Model M60 set"}]
    assert ("product.material_code", "Model M60 set") in knowledge["_source_verified_filter_values"]
    agent._validate_asl_output(content, knowledge, "查询品牌Model M60 set的销售额")


@pytest.mark.parametrize("proof", [set(), {("product.product_name", "Model M60 set")},
                                    {("product.material_code", "Other set")}])
def test_unverified_key_value_still_rejected_with_field_diagnostics(proof):
    knowledge, ast = fixture()
    knowledge["_source_verified_filter_values"] = proof
    ast["filters"] = [{"field": "product.material_code", "operator": "=", "value": "Model M60 set"}]
    with pytest.raises(ASLValidationError) as exc:
        agent._validate_asl_output(json.dumps(ast), knowledge, "查询Model M60 set")
    assert exc.value.code == "ASL_FILTER_INVALID"
    assert exc.value.field == "product.material_code"
    assert exc.value.details["reason"] == "NATURAL_NAME_ON_KEY"


def test_verified_exact_code_does_not_authorize_wildcard_or_other_in_values():
    knowledge, ast = fixture()
    knowledge["_source_verified_filter_values"] = {("product.material_code", "Model M60 set")}
    for operator, value in [("LIKE", "%Model M60 set%"),
                            ("IN", ["Model M60 set", "Other set"])]:
        ast["filters"] = [{"field": "product.material_code", "operator": operator, "value": value}]
        with pytest.raises(ASLValidationError):
            agent._validate_asl_output(json.dumps(ast), knowledge, "Model M60 set和Other set")
