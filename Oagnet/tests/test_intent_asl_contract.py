import json
from types import SimpleNamespace

import pytest
import agent

from agent import _administrative_mention_level, _apply_surface_mention_normalization, _normalize_surface_detail_projections, _contract_filter_candidates, _select_surface_mention_match, _validate_asl_output, _validate_intent_asl_contract
from asl_contract import ASLValidationError


def _entity(code: str, name: str, field: str, attr_name: str):
    table, column = field.split(".", 1)
    return SimpleNamespace(metadata={
        "entity_code": code,
        "entity_name": name,
        "attributes": json.dumps([{
            "attr_code": column,
            "attr_name": attr_name,
            "field_mapping": {"mappingTable": table, "mappingColumn": column},
            "is_display_name": attr_name.endswith("名称"),
        }], ensure_ascii=False),
    })


def _detail_ast(subject: str = "sales_order") -> str:
    return json.dumps({
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": subject},
        "metrics": [],
        "dimensions": [],
        "filters": [],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }, ensure_ascii=False)








def test_unresolvable_projection_label_is_skipped_instead_of_unanswerable_ambiguity():
    """回归：元数据解析不出任何候选的投影标签不得报 ASL_DETAIL_PROJECTION_MISSING。

    2026-10-16 晨 49 现场：指标展示名（含税金额）被并入 required_projections，
    元数据解析出零候选，报错被转换为 type=context、candidates=[] 的不可回答
    歧义，子任务禁追问下必然 SYSTEM_FAILURE→SAFE_FALLBACK（整体
    PARTIAL_SUCCESS，任务级明细查询永远无内容）。
    """
    knowledge = {"entities": [
        _entity("dealer", "经销商", "dealer.dealer_name", "经销商名称"),
    ]}
    contract = {
        "version": "1.0",
        "intent": "DETAIL_QUERY",
        "query_object": "经销商",
        "metric_required": False,
        "required_metrics": [],
        "required_metric_codes": [],
        "required_projections": ["含税金额", "经销商名称"],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["display_fields"] = [
        {"name": "dealer.dealer_name"},
        {"name": "sales_order.amount_with_tax"},
    ]

    # 零候选投影跳过校验，可解析投影仍正常校验，整体不得抛错
    _validate_intent_asl_contract(json.dumps(ast, ensure_ascii=False), contract, knowledge)


def test_unresolvable_grouping_label_is_skipped_instead_of_unanswerable_ambiguity():
    """与投影分支同理：零候选的分组标签跳过校验而非制造不可回答歧义。"""
    knowledge = {"entities": [
        _entity("dealer", "经销商", "dealer.dealer_name", "经销商名称"),
    ]}
    contract = {
        "version": "1.0",
        "intent": "METRIC_QUERY",
        "query_object": "经销商",
        "metric_required": True,
        "required_metrics": [],
        "required_metric_codes": ["sales_amount"],
        "required_projections": [],
        "required_groupings": ["渠道业态"],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["metrics"] = [{"name": "sales_amount"}]
    ast["dimensions"] = [{"name": "dealer.dealer_name"}]

    _validate_intent_asl_contract(json.dumps(ast, ensure_ascii=False), contract, knowledge)








def test_surface_detail_projection_uses_unique_display_attribute():
    def entity(code, name, attributes):
        return SimpleNamespace(metadata={
            "entity_code": code,
            "entity_name": name,
            "attributes": json.dumps(attributes, ensure_ascii=False),
        })

    knowledge = {
        "entities": [
            entity("department", "科室", [
                {
                    "attribute_id": "department-code-id",
                    "attr_code": "dept_code",
                    "attr_name": "科室编码",
                    "field_mapping": "department.dept_code",
                },
                {
                    "attribute_id": "department-name-id",
                    "attr_code": "dept_name",
                    "attr_name": "科室名称",
                    "field_mapping": "department.dept_name",
                },
            ]),
        ],
        "dimensions": [SimpleNamespace(metadata={
            "dim_code": "applicable_department",
            "dim_name": "适用科室",
            "bind_entities": [{
                "attr": "department-code-id",
                "attrName": "科室编码",
            }],
        })],
    }
    ast = json.loads(_detail_ast("product"))
    ast["dimensions"] = [{
        "name": "applicable_department",
        "attr": "department-code-id",
        "level": None,
        "granularity": None,
        "alias": "适用科室",
    }]

    repaired, repairs = _normalize_surface_detail_projections(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "请提供百特Prismaflex M60 set使用科室。",
    )

    assert json.loads(repaired)["dimensions"] == [{
        "name": "department.dept_name",
        "attr": None,
        "level": None,
        "granularity": None,
        "alias": "适用科室",
    }]
    assert repairs == [{
        "type": "REPLACE_CODE_BACKED_DETAIL_PROJECTION",
        "original_dimension": "applicable_department",
        "resolved_field": "department.dept_name",
        "source": "SURFACE_CATALOG_DISPLAY_ATTRIBUTE",
    }]








def test_negative_filter_polarity_is_part_of_contract_validation():
    ast = json.loads(_detail_ast("dealer"))
    ast["dimensions"] = [{"name": "dealer.dealer_name"}]
    ast["filters"] = [{"field": "manufacturer.name", "operator": "=", "value": "B厂家"}]
    contract = {
        "intent": "DETAIL_QUERY", "query_object": "经销商",
        "metric_required": False, "required_projections": ["经销商名称"],
        "required_metric_codes": [], "filters": [],
        "negative_filters": [{"field": "厂家名称", "operator": "NE", "value": "B厂家"}],
        "sorting": None, "time_dimension_required": False,
    }
    with pytest.raises(ASLValidationError) as exc:
        _validate_intent_asl_contract(json.dumps(ast, ensure_ascii=False), contract)
    assert exc.value.code == "ASL_FILTER_INVALID"




def _province_order_count_contract() -> dict:
    return {
        "intent": "METRIC_QUERY",
        "query_object": None,
        "metric_required": True,
        "required_metric_codes": ["order_count"],
        "required_projections": [],
        "required_groupings": [],
        "filters": [{"field": "省份名称", "operator": "EQ", "value": "江苏省"}],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }




def test_province_contract_rejects_recalled_city_binding_and_uses_published_level(
    monkeypatch,
):
    knowledge = {
        "entities": [],
        "dimensions": [SimpleNamespace(metadata={
            "dim_code": "province",
            "dim_name": "省份名称",
            "bind_entities": [{
                "entity": "city",
                "mappingTable": "dim_city",
                "mappingColumn": "city_name",
            }],
        })],
    }
    monkeypatch.setattr(
        agent,
        "get_registered_entity_attributes",
        lambda *_args: [{
            "entity_code": "province",
            "attr_code": "province_name",
            "attr_name": "省份名称",
            "field_mapping": "dim_province.province_name",
            "is_main_attribute": True,
        }],
    )
    monkeypatch.setattr(
        agent,
        "get_table_field_by_scope",
        lambda **_kwargs: {
            "tables": [{
                "table_name": "dim_province",
                "fields": [{"field_name": "province_name"}],
            }],
        },
    )

    candidates = _contract_filter_candidates(
        "省份名称",
        knowledge,
        semantic_model_id=81,
        domain_scope=205,
        query_object="经销商",
    )

    assert candidates == ["dim_province.province_name"]








def test_filter_resolution_prefers_governed_name_over_synonym_collision():
    knowledge = {
        "entities": [SimpleNamespace(metadata={
            "entity_code": "sales_order",
            "entity_name": "销售订单",
            "attributes": json.dumps([
                {
                    "attr_code": "business_city",
                    "attr_name": "业务市",
                    "synonyms": ["地区", "城市"],
                    "field_mapping": {
                        "mappingTable": "sales_order",
                        "mappingColumn": "business_city",
                    },
                },
                {
                    "attr_code": "business_province",
                    "attr_name": "业务省",
                    "synonyms": ["地区", "业务城市"],
                    "field_mapping": {
                        "mappingTable": "sales_order",
                        "mappingColumn": "business_province",
                    },
                },
            ], ensure_ascii=False),
        })],
    }

    assert _contract_filter_candidates("业务城市", knowledge) == [
        "sales_order.business_city"
    ]


@pytest.mark.parametrize(
    "label,field,entity_name,attr_name",
    (
        ("医院名称", "hospital.hospital_name", "医院", "医院名称"),
        (
            "厂家名称",
            "manufacturer.manufacturer_name",
            "生产厂家",
            "厂家名称",
        ),
    ),
)
def test_filter_contract_falls_back_to_current_registered_attribute(
    monkeypatch, label, field, entity_name, attr_name
):
    table, column = field.split(".", 1)
    monkeypatch.setattr(agent, "get_registered_entity_attributes", lambda *_args: [{
        "entity_code": table,
        "entity_name": entity_name,
        "entity_alias": [],
        "business_domain_id": 205,
        "data_source_id": 58,
        "attr_code": column,
        "attr_name": attr_name,
        "description": attr_name,
        "field_mapping": field,
        "is_main_attribute": True,
        "is_primary_key": False,
        "is_unique": False,
    }])
    monkeypatch.setattr(agent, "get_table_field_by_scope", lambda **_kwargs: {
        "tables": [{
            "table_name": table,
            "fields": [{"field_name": column}],
        }],
    })

    assert _contract_filter_candidates(
        label,
        {},
        semantic_model_id=81,
        domain_scope=[205],
    ) == [field]
















def test_product_brand_role_prefers_published_brand_attribute():
    knowledge = {
        "entities": [
            _entity(
                "manufacturer", "厂家",
                "manufacturer.parent_brand", "母品牌",
            ),
            _entity(
                "product", "商品",
                "product.product_name", "商品名称",
            ),
        ],
    }

    assert _contract_filter_candidates("商品品牌", knowledge) == [
        "manufacturer.parent_brand"
    ]


















def test_time_bound_contract_rejects_asl_without_registered_anchor():
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "销售订单",
        "metric_required": True,
        "required_metrics": ["销售额"],
        "required_metric_codes": ["annual_total_sales"],
        "required_projections": [],
        "required_groupings": [],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
        "time_policy": "REQUIRED",
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "annual_total_sales"}]

    with pytest.raises(ASLValidationError) as exc:
        _validate_intent_asl_contract(
            json.dumps(ast, ensure_ascii=False), contract,
        )

    assert exc.value.code == "ASL_TIME_ANCHOR_MISSING"












def test_surface_mention_unique_hit_is_normalized_to_canonical_value(monkeypatch):
    knowledge = {
        "entities": [
            _entity("dim_city", "城市", "dim_city.city_name", "城市名称"),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_quantity"}]
    ast["filters"] = [{
        "field": "dim_city.city_name", "operator": "=", "value": "上海",
    }]
    monkeypatch.setattr(
        agent,
        "resolve_entity_attribute_catalog_matches",
        lambda *_args: [{
            "field": "dim_city.city_name",
            "canonical_value": "上海市",
            "match_type": "CANONICAL_CONTAINS_MENTION",
        }],
    )

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        {"mentions": [{"text": "上海"}]},
        semantic_model_id=81,
        domain_scope=205,
    )

    assert json.loads(normalized)["filters"] == [{
        "field": "dim_city.city_name",
        "operator": "=",
        "value": "上海市",
    }]
    assert repairs == [{
        "type": "ADD_SOURCE_RESOLVED_ENTITY_FILTER",
        "mention": "上海",
        "canonical_value": "上海市",
        "resolved_field": "dim_city.city_name",
        "source": "SURFACE_MENTION_RECALL",
    }]
    assert knowledge["_source_canonical_values"]["上海"] == "上海市"




def test_surface_mention_role_hint_prefers_brand_over_project(monkeypatch):
    knowledge = {
        "entities": [
            _entity(
                "manufacturer", "厂家",
                "manufacturer.parent_brand", "母品牌",
            ),
            _entity(
                "project", "项目",
                "project.project_name", "项目名称",
            ),
        ],
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["filters"] = []
    seen_fields = []

    def resolve(_model, _domains, candidates, _literal):
        seen_fields.extend(item["field"] for item in candidates)
        return [{
            "field": "manufacturer.parent_brand",
            "canonical_value": "万益特",
            "match_type": "EXACT",
        }]

    monkeypatch.setattr(
        agent,
        "_contract_filter_candidates",
        lambda role, *_args, **_kwargs: (
            ["manufacturer.parent_brand"] if role == "母品牌" else []
        ),
    )
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", resolve)

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        {"mentions": [{"text": "万益特", "role_hint": "母品牌"}]},
        semantic_model_id=81,
        domain_scope=205,
    )

    assert seen_fields == ["manufacturer.parent_brand"]
    assert json.loads(normalized)["filters"] == [{
        "field": "manufacturer.parent_brand",
        "operator": "=",
        "value": "万益特",
    }]
    assert repairs[0]["resolved_field"] == "manufacturer.parent_brand"


def test_surface_brand_role_falls_back_within_manufacturer_not_project(monkeypatch):
    manufacturer = _entity(
        "manufacturer", "厂家", "manufacturer.parent_brand", "母品牌"
    )
    manufacturer_attributes = json.loads(manufacturer.metadata["attributes"])
    manufacturer_attributes.append({
        "attr_code": "manufacturer_name",
        "attr_name": "厂家名称",
        "field_mapping": {
            "mappingTable": "manufacturer",
            "mappingColumn": "manufacturer_name",
        },
        "is_display_name": True,
    })
    manufacturer.metadata["attributes"] = json.dumps(
        manufacturer_attributes, ensure_ascii=False
    )
    knowledge = {
        "entities": [
            manufacturer,
            _entity("project", "项目", "project.project_name", "项目名称"),
        ],
    }
    ast = json.loads(_detail_ast("dealer"))
    seen_batches = []

    def resolve(_model, _domains, candidates, _literal):
        fields = [item["field"] for item in candidates]
        seen_batches.append(fields)
        if "manufacturer.manufacturer_name" in fields:
            return [{
                "field": "manufacturer.manufacturer_name",
                "canonical_value": "万益特医疗用品有限公司",
                "match_type": "CANONICAL_CONTAINS_MENTION",
                "is_main_attribute": True,
            }]
        if "project.project_name" in fields:
            return [{
                "field": "project.project_name",
                "canonical_value": "万益特急重症",
                "match_type": "CANONICAL_CONTAINS_MENTION",
                "is_main_attribute": True,
            }]
        return []

    monkeypatch.setattr(
        agent,
        "_contract_filter_candidates",
        lambda role, *_args, **_kwargs: (
            ["manufacturer.parent_brand"] if role == "商品品牌" else []
        ),
    )
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", resolve)

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        {"mentions": [{"text": "万益特", "role_hint": "商品品牌"}]},
        semantic_model_id=81,
        domain_scope=205,
    )

    assert ["manufacturer.parent_brand"] in seen_batches
    assert any(
        "manufacturer.manufacturer_name" in batch for batch in seen_batches
    )
    assert all("project.project_name" not in batch for batch in seen_batches)
    assert json.loads(normalized)["filters"] == [{
        "field": "manufacturer.manufacturer_name",
        "operator": "=",
        "value": "万益特医疗用品有限公司",
    }]
    assert repairs[0]["resolved_field"] == "manufacturer.manufacturer_name"


def test_surface_product_name_hint_finds_published_specification(monkeypatch):
    knowledge = {
        "entities": [
            _entity(
                "product", "product",
                "product.product_name", "product name",
            ),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_including_tax"}]
    ast["filters"] = [{
        "field": "product.product_name",
        "operator": "=",
        "value": "MMT-866A",
    }]
    seen_batches = []

    def resolve(_model, _domains, candidates, _literal):
        fields = [item["field"] for item in candidates]
        seen_batches.append(fields)
        if "product.specification" in fields:
            return [{
                "field": "product.specification",
                "canonical_value": "MMT-866A",
                "match_type": "EXACT",
                "is_main_attribute": False,
            }]
        return []

    monkeypatch.setattr(
        agent,
        "_contract_filter_candidates",
        lambda role, *_args, **_kwargs: (
            ["product.product_name"] if role == "product name" else []
        ),
    )
    monkeypatch.setattr(
        agent,
        "load_published_entity_attribute_candidates",
        lambda *_args: [
            {
                "entity_code": "product",
                "business_domain_id": 205,
                "field": "product.product_name",
            },
            {
                "entity_code": "product",
                "business_domain_id": 205,
                "field": "product.specification",
            },
            {
                "entity_code": "project",
                "business_domain_id": 205,
                "field": "project.project_name",
            },
        ],
    )
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", resolve)

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        {"mentions": [{"text": "MMT-866A", "role_hint": "product name"}]},
        semantic_model_id=81,
        domain_scope=205,
    )

    assert any("product.specification" in batch for batch in seen_batches)
    assert all("project.project_name" not in batch for batch in seen_batches)
    assert json.loads(normalized)["filters"] == [{
        "field": "product.specification",
        "operator": "=",
        "value": "MMT-866A",
    }]
    assert repairs[-1]["resolved_field"] == "product.specification"


def test_exact_vector_value_identity_overrides_wrong_surface_role(monkeypatch):
    product = _entity(
        "product", "product", "product.product_name", "product name",
    )
    product_attributes = json.loads(product.metadata["attributes"])
    product_attributes.append({
        "attr_code": "specification",
        "attr_name": "specification",
        "field_mapping": {
            "mappingTable": "product",
            "mappingColumn": "specification",
        },
        "is_main_attribute": False,
    })
    product.metadata["attributes"] = json.dumps(product_attributes)
    knowledge = {
        "entities": [
            product,
            _entity(
                "manufacturer", "manufacturer",
                "manufacturer.parent_brand", "brand",
            ),
        ],
        "entity_attribute_values": [SimpleNamespace(metadata={
            "entity_code": "product",
            "attr_code": "specification",
            "canonical_value": "MMT-866A",
        })],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_including_tax"}]
    ast["filters"] = [{
        "field": "product.product_name",
        "operator": "=",
        "value": "MMT-866A",
    }]
    seen_fields = []

    def resolve(_model, _domains, candidates, _literal):
        fields = [item["field"] for item in candidates]
        seen_fields.extend(fields)
        return [{
            "field": "product.specification",
            "canonical_value": "MMT-866A",
            "match_type": "EXACT",
            "is_main_attribute": False,
        }] if "product.specification" in fields else []

    monkeypatch.setattr(
        agent,
        "_contract_filter_candidates",
        lambda *_args, **_kwargs: ["manufacturer.parent_brand"],
    )
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", resolve)

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast),
        knowledge,
        {"mentions": [{"text": "MMT-866A", "role_hint": "product name"}]},
        semantic_model_id=81,
        domain_scope=205,
    )

    assert seen_fields == ["product.specification"]
    assert json.loads(normalized)["filters"] == [{
        "field": "product.specification",
        "operator": "=",
        "value": "MMT-866A",
    }]
    assert repairs[-1]["resolved_field"] == "product.specification"


def test_surface_role_containment_does_not_bind_short_id_inside_model_code():
    selected = _select_surface_mention_match(
        "AT75242",
        [{
            "field": "product.id",
            "canonical_value": "7",
            "match_type": "MENTION_CONTAINS_CANONICAL",
        }],
        {"product.id"},
        allow_role_containment=True,
    )

    assert selected is None


def test_surface_mention_multiple_hits_pick_highest_similarity(monkeypatch):
    knowledge = {
        "entities": [
            _entity("product", "商品", "product.product_name", "商品名称"),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_quantity"}]
    ast["filters"] = [{
        "field": "product.product_name", "operator": "=", "value": "透析器",
    }]
    monkeypatch.setattr(
        agent,
        "resolve_entity_attribute_catalog_matches",
        lambda *_args: [
            {
                "field": "product.product_name",
                "canonical_value": "透析器耗材",
                "match_type": "ORDERED_SUBSEQUENCE",
            },
            {
                "field": "product.product_name",
                "canonical_value": "空心纤维血液透析器",
                "match_type": "ORDERED_SUBSEQUENCE",
            },
        ],
    )
    selected = _select_surface_mention_match(
        "空心纤维血液透析器",
        [
            {
                "field": "product.product_name",
                "canonical_value": "透析器耗材",
                "match_type": "ORDERED_SUBSEQUENCE",
            },
            {
                "field": "product.product_name",
                "canonical_value": "空心纤维血液透析器",
                "match_type": "ORDERED_SUBSEQUENCE",
            },
        ],
        {"product.product_name"},
    )
    assert selected == ("product.product_name", "空心纤维血液透析器")

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        {"mentions": [{"text": "透析器"}]},
        semantic_model_id=81,
        domain_scope=205,
    )
    assert json.loads(normalized)["filters"][0]["value"] in {
        "透析器耗材", "空心纤维血液透析器",
    }
    assert repairs[0]["type"] == "ADD_SOURCE_RESOLVED_ENTITY_FILTER"


def test_surface_mention_tied_similarity_picks_one_deterministically_bounded():
    candidates = {"product.product_name"}
    matches = [
        {
            "field": "product.product_name",
            "canonical_value": "A型透析器",
            "match_type": "EXACT",
        },
        {
            "field": "product.product_name",
            "canonical_value": "B型透析器",
            "match_type": "EXACT",
        },
    ]
    selected = _select_surface_mention_match(
        "A型透析器", matches, candidates
    )
    assert selected is not None
    assert selected[1] == "A型透析器"

    tied = _select_surface_mention_match(
        "透析器",
        [
            {
                "field": "product.product_name",
                "canonical_value": "A型透析器",
                "match_type": "CANONICAL_CONTAINS_MENTION",
            },
            {
                "field": "product.product_name",
                "canonical_value": "B型透析器",
                "match_type": "CANONICAL_CONTAINS_MENTION",
            },
        ],
        candidates,
    )
    assert tied is not None and tied[1] in {"A型透析器", "B型透析器"}


def test_surface_mention_administrative_tie_break_prefers_city_level(monkeypatch):
    knowledge = {
        "entities": [
            _entity("dim_city", "城市", "dim_city.city_name", "城市名称"),
            _entity("dim_province", "省份", "dim_province.province_name", "省份名称"),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_quantity"}]
    ast["filters"] = []
    monkeypatch.setattr(
        agent,
        "resolve_entity_attribute_catalog_matches",
        lambda *_args: [
            {
                "field": "dim_province.province_name",
                "canonical_value": "上海市",
                "match_type": "CANONICAL_CONTAINS_MENTION",
            },
            {
                "field": "dim_city.city_name",
                "canonical_value": "上海市",
                "match_type": "CANONICAL_CONTAINS_MENTION",
            },
        ],
    )

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        {"mentions": [{"text": "上海"}]},
        semantic_model_id=81,
        domain_scope=205,
    )

    assert json.loads(normalized)["filters"] == [{
        "field": "dim_city.city_name",
        "operator": "=",
        "value": "上海市",
    }]
    assert repairs[0]["resolved_field"] == "dim_city.city_name"
    assert _administrative_mention_level("上海") is None
    assert _administrative_mention_level("江苏省") == "province"
    assert _administrative_mention_level("浦东新区") == "district"


def test_surface_mention_suffix_stated_level_wins_the_tie(monkeypatch):
    field_kinds = {
        "dim_city.city_name": "city",
        "dim_province.province_name": "province",
    }
    matches = [
        {
            "field": "dim_city.city_name",
            "canonical_value": "上海市",
            "match_type": "EXACT",
        },
        {
            "field": "dim_province.province_name",
            "canonical_value": "上海市",
            "match_type": "EXACT",
        },
    ]
    assert _select_surface_mention_match(
        "江苏省", matches, set(field_kinds), field_kinds
    ) == ("dim_province.province_name", "上海市")
    knowledge = {
        "entities": [
            _entity("product", "商品", "product.product_name", "商品名称"),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_quantity"}]
    ast["filters"] = [{
        "field": "product.product_name", "operator": "=", "value": "不存在的东西",
    }]
    monkeypatch.setattr(
        agent, "resolve_entity_attribute_catalog_matches", lambda *_args: [],
    )
    monkeypatch.setattr(
        agent, "load_published_entity_attribute_candidates", lambda *_args: [],
    )

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        {"mentions": [{"text": "不存在的东西"}]},
        semantic_model_id=81,
        domain_scope=205,
    )

    assert json.loads(normalized)["filters"] == []
    assert repairs == [{
        "type": "DROP_UNMATCHED_SURFACE_MENTION",
        "mention": "不存在的东西",
        "source": "SURFACE_MENTION_RECALL",
    }]


def test_nonexact_projection_label_is_recall_only_not_a_filter(monkeypatch):
    knowledge = {
        "entities": [
            _entity(
                "department", "department",
                "department.dept_name", "department name",
            ),
        ],
    }
    ast = json.loads(_detail_ast("product"))
    ast["dimensions"] = [{
        "name": "department.dept_name",
        "attr": None,
        "level": None,
        "granularity": None,
    }]
    # Simulate a draft predicate invented from the return-object word.
    ast["filters"] = [{
        "field": "department.dept_name",
        "operator": "=",
        "value": "科室",
    }]
    monkeypatch.setattr(
        agent,
        "resolve_entity_attribute_catalog_matches",
        lambda *_args: [{
            "field": "department.dept_name",
            "canonical_value": "通用科室",
            "match_type": "CANONICAL_CONTAINS_MENTION",
        }],
    )

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        {"mentions": [{
            "text": "科室",
            "role_hint": "department.dept_name",
        }]},
        semantic_model_id=81,
        domain_scope=205,
    )

    assert json.loads(normalized)["filters"] == []
    assert repairs == [{
        "type": "DROP_NONEXACT_PROJECTION_FILTER",
        "mention": "科室",
        "canonical_value": "通用科室",
        "resolved_field": "department.dept_name",
        "source": "SURFACE_MENTION_RECALL",
    }]


def test_exact_projected_value_can_still_be_an_explicit_filter(monkeypatch):
    knowledge = {
        "entities": [
            _entity(
                "department", "department",
                "department.dept_name", "department name",
            ),
        ],
    }
    ast = json.loads(_detail_ast("product"))
    ast["dimensions"] = [{
        "name": "department.dept_name",
        "attr": None,
        "level": None,
        "granularity": None,
    }]
    monkeypatch.setattr(
        agent,
        "resolve_entity_attribute_catalog_matches",
        lambda *_args: [{
            "field": "department.dept_name",
            "canonical_value": "通用科室",
            "match_type": "EXACT",
        }],
    )

    normalized, repairs = _apply_surface_mention_normalization(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        {"mentions": [{
            "text": "通用科室",
            "role_hint": "department.dept_name",
        }]},
        semantic_model_id=81,
        domain_scope=205,
    )

    assert json.loads(normalized)["filters"] == [{
        "field": "department.dept_name",
        "operator": "=",
        "value": "通用科室",
    }]
    assert repairs[-1]["type"] == "ADD_SOURCE_RESOLVED_ENTITY_FILTER"




def test_missing_contract_filter_without_repair_still_fails_closed():
    """不接入修复时同一输入必须仍被校验拦截（校验本身不放宽）。"""
    knowledge = {
        "entities": [_entity("dealer", "经销商", "dealer.dealer_name", "经销商名称")],
    }
    contract = {
        "intent": "DETAIL_QUERY",
        "query_object": "经销商",
        "metric_required": False,
        "required_projections": ["经销商名称"],
        "required_groupings": [],
        "filters": [{
            "field": "经销商名称", "operator": "IN",
            "value": ["国药控股北京有限公司", "华润安徽医药有限公司"],
        }],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["display_fields"] = [{"name": "dealer.dealer_name"}]
    with pytest.raises(ASLValidationError) as excinfo:
        _validate_intent_asl_contract(json.dumps(ast, ensure_ascii=False), contract, knowledge)
    assert "omitted a required positive filter" in str(excinfo.value)


def _product_entity(code: str, name_label: str):
    table, column = "product", code
    return SimpleNamespace(metadata={
        "entity_code": "product" if code == "product_name" else f"product_{code}",
        "entity_name": name_label,
        "attributes": json.dumps([
            {
                "attr_code": "product_code",
                "attr_name": "商品编码",
                "field_mapping": {"mappingTable": table, "mappingColumn": "product_code"},
            },
            {
                "attr_code": "product_name",
                "attr_name": "商品名称",
                "field_mapping": {"mappingTable": table, "mappingColumn": "product_name"},
                "is_display_name": True,
            },
        ], ensure_ascii=False),
    })
