"""Source normalization must preserve alternative groups and semantic roles."""
import json

import pytest
import agent
from test_intent_asl_contract import _entity, _detail_ast


@pytest.fixture
def catalog(monkeypatch):
    knowledge = {"entities": [_entity(
        "manufacturer", "生产厂家", "manufacturer.parent_brand", "母厂牌",
    )]}
    monkeypatch.setattr(agent, "load_published_entity_attribute_candidates", lambda *a: [])
    return knowledge


def normalize(knowledge, filters, mentions):
    draft = json.loads(_detail_ast("sales_order"))
    draft["filters"] = filters
    result, repairs = agent._apply_surface_mention_normalization(
        json.dumps(draft, ensure_ascii=False), knowledge, {"mentions": mentions}, 81, [205],
    )
    return json.loads(result), repairs


@pytest.mark.parametrize("operator", ["IN", "NOT IN"])
def test_members_are_normalized_in_place_without_appended_equalities(monkeypatch, catalog, operator):
    def resolve(_model, _scope, candidates, value):
        assert {c["field"] for c in candidates} == {"manufacturer.parent_brand"}
        return [{"field": "manufacturer.parent_brand", "canonical_value": value + "品牌",
                 "match_type": "CANONICAL_CONTAINS_MENTION"}]
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", resolve)
    result, _ = normalize(catalog, [
        {"field": "manufacturer.parent_brand", "operator": operator, "value": ["万益特", "贝朗"]},
    ], [{"text": name} for name in ["万益特", "贝朗"]])
    assert result["filters"] == [{"field": "manufacturer.parent_brand", "operator": operator,
                                  "value": ["万益特品牌", "贝朗品牌"]}]


@pytest.mark.parametrize("operator", ["IN", "NOT IN"])
def test_cross_field_hit_keeps_set_instead_of_turning_or_into_and(monkeypatch, catalog, operator):
    catalog["entities"] = [_entity("manufacturer", "生产厂家", "manufacturer.manufacturer_name", "厂家名称")]
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", lambda *a: [
        {"field": "manufacturer.manufacturer_name", "canonical_value": "广州万益特医疗用品有限公司",
         "match_type": "CANONICAL_CONTAINS_MENTION"},
    ])
    filters = [{"field": "manufacturer.parent_brand", "operator": operator, "value": ["万益特", "贝朗"]}]
    result, repairs = normalize(catalog, filters, [{"text": "万益特", "role_hint": "厂牌"}])
    assert result["filters"] == filters
    assert any(r["type"] == "PRESERVE_SET_FILTER" for r in repairs)
    assert not any(r["type"] == "ADD_SOURCE_RESOLVED_ENTITY_FILTER" for r in repairs)


def test_missing_set_member_does_not_drop_group_or_claim_filter_removed(monkeypatch, catalog):
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", lambda *a: [])
    filters = [{"field": "manufacturer.parent_brand", "operator": "IN", "value": ["未知厂牌", "贝朗"]}]
    result, repairs = normalize(catalog, filters, [{"text": "未知厂牌"}])
    assert result["filters"] == filters
    assert repairs[-1]["type"] == "PRESERVE_SET_FILTER"
    assert repairs[-1]["reason"] == "VALUE_UNRESOLVED"


@pytest.mark.parametrize("role", ["实体", "业务对象", "指标", "维度", "展示字段", "时间范围"])
def test_non_value_roles_do_not_query_value_catalog_or_warn(monkeypatch, catalog, role):
    def forbidden(*args):
        raise AssertionError("semantic role is not a literal value")
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", forbidden)
    result, repairs = normalize(catalog, [], [{"text": "商品", "role_hint": role}])
    assert result["filters"] == []
    assert repairs == []


def test_explicit_value_with_same_word_as_entity_still_matches(monkeypatch, catalog):
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", lambda *a: [
        {"field": "manufacturer.parent_brand", "canonical_value": "商品", "match_type": "EXACT"},
    ])
    result, _ = normalize(catalog, [], [
        {"text": "商品", "role_hint": "实体"}, {"text": "商品", "role_hint": "筛选值"},
    ])
    assert result["filters"] == [{"field": "manufacturer.parent_brand", "operator": "=", "value": "商品"}]


def test_repeated_scalar_filters_collapse_after_canonicalization(monkeypatch, catalog):
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", lambda *a: [
        {"field": "manufacturer.parent_brand", "canonical_value": "贝朗", "match_type": "EXACT"},
    ])
    single = {"field": "manufacturer.parent_brand", "operator": "=", "value": "贝朗"}
    result, _ = normalize(catalog, [single.copy(), single.copy()], [{"text": "贝朗"}])
    assert result["filters"] == [single]


@pytest.mark.parametrize("operator", ["IN", "NOT IN"])
def test_draft_already_contains_standardized_member_without_adding_scalar(monkeypatch, catalog, operator):
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", lambda *a: [
        {"field": "manufacturer.parent_brand", "canonical_value": "万益特品牌",
         "match_type": "CANONICAL_CONTAINS_MENTION"},
    ])
    filters = [{"field": "manufacturer.parent_brand", "operator": operator,
                "value": ["万益特品牌", "贝朗"]}]
    result, _ = normalize(catalog, filters, [{"text": "万益特", "role_hint": "厂牌"}])
    assert result["filters"] == filters
