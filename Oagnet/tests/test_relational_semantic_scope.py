import json

import pytest

from agent import (
    _administrative_query_level,
    _known_physical_fields,
    _normalize_semantic_references,
    _validate_asl_output,
)
from prompt_build import PromptBuilder
from vector_store import SearchResult


def _result(record_id: str, kind: str, score: float = 0.8, **metadata) -> SearchResult:
    return SearchResult(
        id=record_id,
        score=score,
        text=metadata.get("entity_name") or metadata.get("attr_name") or record_id,
        metadata={"type": kind, "semantic_model_id": 81, "business_domain_id": 205, **metadata},
    )


def _entity(code: str, name: str, attributes: list[dict]) -> SearchResult:
    return _result(
        f"sm81_bd205:entity:{code}",
        "entity",
        entity_code=code,
        entity_name=name,
        attributes=attributes,
        relations=[],
    )


def _relation(
    source: str,
    code: str,
    target: str,
    source_field: str,
    target_field: str,
) -> SearchResult:
    return _result(
        f"sm81_bd205:relation:{source}.{code}",
        "relation",
        parent=source,
        relation_code=code,
        relation_name=code,
        target_entity=target,
        join_key={"source_field": source_field, "target_field": target_field},
    )


def _where_type(where: dict) -> str:
    for clause in where.get("$and", [where]):
        if "type" in clause:
            value = clause["type"]
            return str(value['$in'][0] if isinstance(value, dict) else value)
    raise AssertionError(f"missing type constraint: {where}")


class RelationalStore:
    def __init__(self, direct: dict[str, list[SearchResult]], scoped: dict[str, list[SearchResult]]):
        self.direct = direct
        self.scoped = scoped
        self.search_calls: list[dict] = []
        self.load_calls: list[dict] = []

    def count(self):
        return 1

    def search(self, _vector, top_k, where):
        self.search_calls.append(where)
        return list(self.direct.get(_where_type(where), []))[:top_k]

    def get_by_where(self, where):
        self.load_calls.append(where)
        return list(self.scoped.get(_where_type(where), []))


def test_exact_dimension_role_is_loaded_from_current_scope_outside_vector_top_k():
    vector_dimensions = [
        _result(
            f"sm81:dimension:distractor-{index}",
            "dimension",
            score=1 - index / 100,
            dim_code=f"distractor_{index}",
            dim_name=f"干扰维度{index}",
        )
        for index in range(4)
    ]
    city = _result(
        "sm81:dimension:city",
        "dimension",
        score=0.01,
        dim_code="city",
        dim_name="城市",
        bind_entities=[{
            "entityName": "城市",
            "mappingTable": "dim_city",
            "mappingColumn": "city_name",
        }],
    )
    store = RelationalStore(
        direct={"dimension": vector_dimensions},
        scoped={"dimension": [*vector_dimensions, city]},
    )
    builder = PromptBuilder(
        store,
        lambda _query: [0.1, 0.2],
        top_k=3,
        semantic_model_id=81,
        business_domain_id=205,
    )

    knowledge = builder.retrieve("调用方语义维度角色：经销商、城市、商品品牌")

    assert "city" in {
        item.metadata["dim_code"] for item in knowledge["dimensions"]
    }


def _asl(dimension_name: str) -> dict:
    return {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "product"},
        "metrics": [],
        "dimensions": [{"name": dimension_name, "attr": None, "level": None, "granularity": None}],
        "filters": [],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }


def _bridge_fixture(*, product_is_direct: bool = True) -> tuple[RelationalStore, PromptBuilder]:
    product = _entity("product", "商品", [
        {"attr_code": "product_code", "field_mapping": "product.product_code"},
        {"attr_code": "product_name", "field_mapping": "product.product_name"},
    ])
    bridge = _entity("product_department_relation", "商品适用科室关系", [
        {"attr_code": "product_code", "field_mapping": "product_dept_relation.product_code"},
        {"attr_code": "dept_code", "field_mapping": "product_dept_relation.dept_code"},
        {"attr_code": "secret", "field_mapping": "product_dept_relation.secret"},
    ])
    department = _entity("department", "科室", [
        {"attr_code": "dept_code", "field_mapping": "department.dept_code"},
        {"attr_code": "dept_name", "field_mapping": "department.dept_name"},
        {"attr_code": "secret", "field_mapping": "department.secret"},
    ])
    product_to_bridge = _relation(
        "product",
        "product_has_department_relation",
        "product_department_relation",
        "product.product_code",
        "product_dept_relation.product_code",
    )
    bridge_to_department = _relation(
        "product_department_relation",
        "product_department_relation_belongs_to_department",
        "department",
        "product_dept_relation.dept_code",
        "department.dept_code",
    )
    dept_name = _result(
        "sm81_bd205:attr:department.dept_name",
        "attribute",
        score=0.95,
        parent="department",
        parent_name="科室",
        attr_code="dept_name",
        attr_name="科室名称",
        field_mapping="department.dept_name",
    )
    product_department = _result(
        "sm81_bd205:attr:product.main_department",
        "attribute",
        score=0.91,
        parent="product",
        parent_name="商品",
        attr_code="main_department",
        attr_name="主要适用科室",
        field_mapping="product.main_department",
    )
    store = RelationalStore(
        direct={
            "entity": [product] if product_is_direct else [department],
            "attribute": [dept_name] if product_is_direct else [dept_name, product_department],
            "relation": [product_to_bridge],
        },
        scoped={
            "entity": [product, bridge, department],
            "relation": [product_to_bridge, bridge_to_department],
        },
    )
    builder = PromptBuilder(
        store,
        lambda _query: [0.1, 0.2],
        top_k=3,
        semantic_model_id=81,
        business_domain_id=205,
    )
    return store, builder


def test_bridge_query_completes_same_scope_path_and_canonicalizes_unique_attr_code():
    store, builder = _bridge_fixture()

    prompt = builder.build("超声血管导引穿刺套件适用于哪些科室")
    knowledge = builder.last_knowledge

    assert {item.metadata["entity_code"] for item in knowledge["entities"]} == {
        "product",
        "product_department_relation",
        "department",
    }
    assert {item.metadata["relation_code"] for item in knowledge["relations"]} == {
        "product_has_department_relation",
        "product_department_relation_belongs_to_department",
    }
    assert knowledge["attributes"][0].metadata["field_mapping"] == "department.dept_name"
    assert "department.dept_name" in prompt
    assert "product_department_relation_belongs_to_department" in prompt
    assert all("semantic_model_id" in str(where) for where in store.load_calls)
    assert all("business_domain_id" in str(where) for where in store.load_calls)

    fields = _known_physical_fields(knowledge)
    assert "department.dept_name" in fields
    assert "product_dept_relation.product_code" in fields
    assert "product_dept_relation.dept_code" in fields
    assert "department.secret" not in fields
    assert "product_dept_relation.secret" not in fields

    normalized = _normalize_semantic_references(
        json.dumps(_asl("dept_name"), ensure_ascii=False),
        knowledge,
    )
    assert json.loads(normalized)["dimensions"][0]["name"] == "department.dept_name"
    validated = json.loads(_validate_asl_output(normalized, knowledge))
    assert validated["dimensions"][0]["name"] == "department.dept_name"

    with pytest.raises(ValueError, match="not retrieved"):
        _validate_asl_output(
            json.dumps(_asl("department.secret"), ensure_ascii=False),
            knowledge,
        )


def test_dimension_owned_field_is_authorized_and_clears_stale_ambiguity():
    product = _entity("product", "商品", [
        {
            "attr_code": "product_name",
            "field_mapping": "product.product_name",
        },
    ])
    city_dimension = _result(
        "sm81_bd205:dimension:city",
        "dimension",
        dim_code="city",
        dim_name="城市",
        field_mapping={
            "fact_table_field": "dim_city.city_name",
            "dim_table_field": "",
        },
        attribute_mappings=[{
            "mapping_table": "dim_city",
            "mapping_column": "city_name",
            "field_path": "dim_city.city_name",
        }],
        bind_entities=[],
    )
    knowledge = {
        "entities": [product],
        "attributes": [],
        "relations": [],
        "metrics": [],
        "dimensions": [city_dimension],
    }
    ast = _asl("product.product_name")
    ast["filters"] = [{
        "field": "dim_city.city_name",
        "operator": "=",
        "value": "上海市",
    }]
    ast["ambiguity"] = [{
        "type": "filter",
        "question": (
            "过滤值 上海市 使用了未注册字段 dim_city.city_name，"
            "请确认筛选口径。"
        ),
        "candidates": ["dim_city.city_name"],
    }]

    assert "dim_city.city_name" in _known_physical_fields(knowledge)
    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查询上海市的商品",
        81,
        205,
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "dim_city.city_name",
        "operator": "=",
        "value": "上海市",
    }]
    assert result["ambiguity"] == []
    _validate_asl_output(normalized, knowledge, "查询上海市的商品")


def test_bare_attribute_code_is_not_canonicalized_when_mapping_is_ambiguous():
    entity = _entity("product", "商品", [])
    knowledge = {
        "entities": [entity],
        "attributes": [
            _result(
                "attr:department.name",
                "attribute",
                parent="department",
                attr_code="name",
                field_mapping="department.name",
            ),
            _result(
                "attr:product.name",
                "attribute",
                parent="product",
                attr_code="name",
                field_mapping="product.name",
            ),
        ],
        "relations": [],
        "metrics": [],
        "dimensions": [],
    }

    normalized = _normalize_semantic_references(
        json.dumps(_asl("name"), ensure_ascii=False),
        knowledge,
    )

    assert json.loads(normalized)["dimensions"][0]["name"] == "name"
    with pytest.raises(ValueError, match="dimension was not retrieved"):
        _validate_asl_output(normalized, knowledge)


def test_relationship_code_filter_with_name_value_uses_unique_authorized_name_field():
    _, builder = _bridge_fixture(product_is_direct=False)
    builder.build("超声血管导引穿刺套件适用于哪些科室")
    knowledge = builder.last_knowledge
    completed_product = next(
        item for item in knowledge["entities"]
        if item.metadata.get("entity_code") == "product"
    )
    assert {item["field_mapping"] for item in completed_product.metadata["attributes"]} == {
        "product.product_code",
        "product.product_name",
    }
    assert "product.main_department" in _known_physical_fields(knowledge)
    ast = _asl("department.dept_name")
    ast["filters"] = [{
        "field": "product_dept_relation.product_code",
        "operator": "=",
        "value": "超声血管导引穿刺套件",
    }]
    ast["ambiguity"] = [{
        "type": "filter",
        "question": "未找到商品编码 product_code，请确认使用编码还是商品名称。",
        "candidates": [
            "使用 product_code 精确匹配",
            "使用 product_name LIKE '%超声血管导引穿刺套件%' 模糊匹配",
        ],
    }]

    resolver_calls = []

    def exact_resolver(semantic_model_id, business_domain_id, candidates, value):
        resolver_calls.append(
            (semantic_model_id, business_domain_id, candidates, value)
        )
        return ["product.product_name"]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "超声血管导引穿刺套件适用于哪些科室",
        81,
        205,
        exact_resolver,
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "product.product_name",
        "operator": "=",
        "value": "超声血管导引穿刺套件",
    }]
    assert result["ambiguity"] == []
    assert resolver_calls == [(
        81,
        205,
        [{
            "entity_code": "product",
            "field": "product.product_name",
            "business_domain_id": 205,
        }],
        "超声血管导引穿刺套件",
    )]
    assert json.loads(_validate_asl_output(normalized, knowledge))["filters"] == result["filters"]


def test_relationship_name_filter_resolves_one_field_among_multiple_endpoint_names():
    _, builder = _bridge_fixture(product_is_direct=False)
    builder.build("紫杉醇释放冠脉球囊导管的合作经销商")
    knowledge = builder.last_knowledge
    bridge = next(
        item for item in knowledge["entities"]
        if item.metadata.get("entity_code") == "product_department_relation"
    )
    product = next(
        item for item in knowledge["entities"]
        if item.metadata.get("entity_code") == "product"
    )
    next(
        item for item in product.metadata["attributes"]
        if item.get("attr_code") == "product_name"
    )["is_main_attribute"] = True
    bridge.metadata["attributes"].append({
        "attr_code": "relation_name",
        "field_mapping": "product_dept_relation.relation_name",
        "is_main_attribute": True,
    })
    ast = _asl("department.dept_name")
    ast["filters"] = [{
        "field": "product_dept_relation.product_code",
        "operator": "LIKE",
        "value": "%紫杉醇释放冠脉球囊导管%",
    }]
    calls = []

    def resolver(_sm, _bd, candidates, value):
        calls.append((candidates, value))
        return ["product.product_name"]

    result = json.loads(_normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "紫杉醇释放冠脉球囊导管的合作经销商",
        81,
        205,
        resolver,
    ))

    assert result["filters"] == [{
        "field": "product.product_name",
        "operator": "=",
        "value": "紫杉醇释放冠脉球囊导管",
    }]
    assert {item["field"] for item in calls[0][0]} == {
        "product.product_name",
        "product_dept_relation.relation_name",
    }


def test_exclusion_name_is_moved_off_related_manufacturer_code():
    product = _entity("product", "商品", [{
        "attr_code": "manufacturer_code",
        "field_mapping": "product.manufacturer_code",
    }])
    manufacturer = _entity("manufacturer", "生产厂家", [
        {
            "attr_code": "manufacturer_code",
            "field_mapping": "manufacturer.manufacturer_code",
            "is_primary_key": True,
        },
        {
            "attr_code": "manufacturer_name",
            "field_mapping": "manufacturer.manufacturer_name",
            "is_main_attribute": True,
        },
    ])
    knowledge = {
        "entities": [product, manufacturer],
        "attributes": [],
        "relations": [_relation(
            "product",
            "product_belongs_to_manufacturer",
            "manufacturer",
            "product.manufacturer_code",
            "manufacturer.manufacturer_code",
        )],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl("manufacturer.manufacturer_name")
    ast["filters"] = [{
        "field": "product.manufacturer_code",
        "operator": "!=",
        "value": "上海洁安",
    }]

    result = json.loads(_normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "排除上海洁安厂家",
        81,
        205,
        lambda *_args: ["manufacturer.manufacturer_name"],
    ))

    assert result["filters"] == [{
        "field": "manufacturer.manufacturer_name",
        "operator": "!=",
        "value": "上海洁安",
    }]


def test_unresolved_natural_name_on_code_key_requires_clarification_before_validation():
    _, builder = _bridge_fixture(product_is_direct=False)
    builder.build("未知商品的适用科室")
    knowledge = builder.last_knowledge
    ast = _asl("department.dept_name")
    ast["filters"] = [{
        "field": "product_dept_relation.product_code",
        "operator": "=",
        "value": "未知商品",
    }]

    with pytest.raises(ValueError, match="cannot be used as a code/id/join key"):
        _validate_asl_output(
            json.dumps(ast, ensure_ascii=False),
            knowledge,
            "未知商品的适用科室",
        )


def test_relationship_name_filter_keeps_clarification_when_exact_value_is_absent():
    _, builder = _bridge_fixture(product_is_direct=False)
    builder.build("不存在商品适用于哪些科室")
    knowledge = builder.last_knowledge
    ast = _asl("department.dept_name")
    ast["filters"] = [{
        "field": "product_dept_relation.product_code",
        "operator": "=",
        "value": "不存在商品",
    }]

    result = json.loads(_normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "不存在商品适用于哪些科室",
        81,
        205,
        lambda *_args: [],
    ))

    assert result["filters"] == ast["filters"]
    assert result["ambiguity"][-1]["type"] == "filter"


def _manufacturer_filter_knowledge() -> dict:
    manufacturer = _entity(
        "main_data_domain_ent_manufacturer",
        "生产厂家",
        [
            {
                "attr_code": "manufacturer_code",
                "attr_name": "厂家编码",
                "field_mapping": "manufacturer.manufacturer_code",
                "is_primary_key": True,
            },
            {
                "attr_code": "manufacturer_name",
                "attr_name": "生产厂家名称",
                "field_mapping": "manufacturer.manufacturer_name",
                "is_main_attribute": True,
            },
            {
                "attr_code": "standard_name",
                "attr_name": "标准生产厂家名称",
                "field_mapping": "manufacturer.standard_name",
            },
            {
                "attr_code": "country",
                "attr_name": "厂家国别",
                "field_mapping": "manufacturer.country",
            },
            {
                "attr_code": "parent_brand",
                "attr_name": "母品牌",
                "field_mapping": "manufacturer.parent_brand",
            },
        ],
    )
    manufacturer.metadata["entity_alias"] = "厂家、品牌、厂牌"
    return {
        "entities": [manufacturer],
        "attributes": [],
        "relations": [],
        "metrics": [],
        "dimensions": [],
    }


def test_structured_entity_value_moves_to_unique_source_verified_attribute():
    knowledge = _manufacturer_filter_knowledge()
    ast = _asl("manufacturer.manufacturer_name")
    ast["subject"] = {"entity": "main_data_domain_ent_manufacturer"}
    ast["filters"] = [{
        "field": "manufacturer.manufacturer_name",
        "operator": "LIKE",
        "value": "%BD%",
    }]
    resolver_calls = []

    def resolver(semantic_model_id, business_domain_id, candidates, value):
        resolver_calls.append(
            (semantic_model_id, business_domain_id, candidates, value)
        )
        return ["manufacturer.parent_brand"]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "提供做BD品牌产品的经销商",
        81,
        205,
        exact_attribute_value_resolver=resolver,
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "manufacturer.parent_brand",
        "operator": "=",
        "value": "BD",
    }]
    assert resolver_calls[0][0:2] == (81, 205)
    assert resolver_calls[0][3] == "BD"
    assert {
        item["field"] for item in resolver_calls[0][2]
    } == {
        "manufacturer.manufacturer_name",
        "manufacturer.standard_name",
        "manufacturer.country",
        "manufacturer.parent_brand",
    }


def test_separated_brand_value_uses_unique_catalog_field_and_canonical_value():
    knowledge = _manufacturer_filter_knowledge()
    ast = _asl("manufacturer.manufacturer_name")
    ast["subject"] = {"entity": "main_data_domain_ent_manufacturer"}
    ast["filters"] = [{
        "field": "manufacturer.parent_brand",
        "operator": "LIKE",
        "value": "%江苏苏云%",
    }]

    def resolver(_model, _domain, _candidates, value):
        assert value == "江苏苏云"
        return [
            {
                "field": "manufacturer.manufacturer_name",
                "canonical_value": "江苏苏云医疗器材有限公司",
                "match_type": "CANONICAL_CONTAINS_MENTION",
                "is_main_attribute": True,
            },
            {
                "field": "manufacturer.parent_brand",
                "canonical_value": "江苏苏云",
                "match_type": "EXACT",
                "is_main_attribute": False,
            },
        ]

    result = json.loads(_normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查询江苏苏云品牌的产品",
        81,
        205,
        catalog_value_resolver=resolver,
    ))

    assert result["filters"] == [{
        "field": "manufacturer.parent_brand",
        "operator": "=",
        "value": "江苏苏云",
    }]


def test_subject_wrapper_alias_maps_only_to_unique_recalled_entity():
    dealer = _entity("dealer", "经销商", [{
        "attr_code": "dealer_name",
        "attr_name": "经销商名称",
        "field_mapping": "dealer.dealer_name",
        "is_main_attribute": True,
    }])
    knowledge = {
        "entities": [dealer],
        "attributes": [],
        "relations": [],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl("dealer.dealer_name")
    ast["subject"] = {"entity": "ent_dealer"}

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False), knowledge,
    )

    assert json.loads(normalized)["subject"] == {"entity": "dealer"}
    _validate_asl_output(normalized, knowledge)


def test_natural_brand_on_related_code_uses_unique_source_backed_attribute():
    order = _entity("sales_order", "销售订单", [{
        "attr_code": "product_code",
        "attr_name": "商品编码",
        "field_mapping": "sales_order.product_code",
    }])
    product = _entity("product", "商品", [
        {
            "attr_code": "product_code",
            "attr_name": "商品编码",
            "field_mapping": "product.product_code",
        },
        {
            "attr_code": "manufacturer_code",
            "attr_name": "厂家编码",
            "field_mapping": "product.manufacturer_code",
        },
    ])
    manufacturer = _entity("manufacturer", "生产厂家", [
        {
            "attr_code": "manufacturer_code",
            "attr_name": "厂家编码",
            "field_mapping": "manufacturer.manufacturer_code",
        },
        {
            "attr_code": "manufacturer_name",
            "attr_name": "厂家名称",
            "field_mapping": "manufacturer.manufacturer_name",
            "is_main_attribute": True,
        },
        {
            "attr_code": "parent_brand",
            "attr_name": "母品牌",
            "field_mapping": "manufacturer.parent_brand",
        },
    ])
    knowledge = {
        "entities": [order, product, manufacturer],
        "attributes": [],
        "relations": [
            _relation(
                "sales_order", "order_belongs_to_product", "product",
                "sales_order.product_code", "product.product_code",
            ),
            _relation(
                "product", "product_belongs_to_manufacturer", "manufacturer",
                "product.manufacturer_code", "manufacturer.manufacturer_code",
            ),
        ],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl("manufacturer.manufacturer_name")
    ast["subject"] = {"entity": "sales_order"}
    ast["filters"] = [{
        "field": "sales_order.product_code",
        "operator": "=",
        "value": "费森尤斯",
    }]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "费森尤斯产品的销售趋势",
        81,
        205,
        exact_value_resolver=lambda *_args: [],
        exact_attribute_value_resolver=lambda *_args: [
            "manufacturer.parent_brand"
        ],
        catalog_value_resolver=lambda *_args: [{
            "field": "manufacturer.parent_brand",
            "canonical_value": "费森尤斯",
            "match_type": "EXACT",
            "is_main_attribute": False,
        }],
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "manufacturer.parent_brand",
        "operator": "=",
        "value": "费森尤斯",
    }]
    _validate_asl_output(normalized, knowledge, "费森尤斯产品的销售趋势")


def test_composite_brand_and_product_value_is_split_after_both_source_checks():
    knowledge = _manufacturer_filter_knowledge()
    knowledge["entities"].append(_entity("product", "商品", [
        {
            "attr_code": "product_code",
            "attr_name": "商品编码",
            "field_mapping": "product.product_code",
            "is_primary_key": True,
        },
        {
            "attr_code": "product_name",
            "attr_name": "商品名称",
            "field_mapping": "product.product_name",
            "is_main_attribute": True,
        },
    ]))
    ast = _asl("product.product_name")
    ast["filters"] = [{
        "field": "product.product_name",
        "operator": "LIKE",
        "value": "%BD品牌超声血管导引穿刺套件%",
    }]

    def resolver(_model, _domain, _candidates, value):
        if value == "BD":
            return [{
                "field": "manufacturer.parent_brand",
                "canonical_value": "BD",
                "match_type": "EXACT",
                "is_main_attribute": False,
            }]
        if value == "超声血管导引穿刺套件":
            return [{
                "field": "product.product_name",
                "canonical_value": "超声血管导引穿刺套件",
                "match_type": "EXACT",
                "is_main_attribute": True,
            }]
        return []

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "提供做BD品牌超声血管导引穿刺套件产品的经销商",
        81,
        205,
        catalog_value_resolver=resolver,
    )

    assert json.loads(normalized)["filters"] == [
        {
            "field": "product.product_name",
            "operator": "=",
            "value": "超声血管导引穿刺套件",
        },
        {
            "field": "manufacturer.parent_brand",
            "operator": "=",
            "value": "BD",
        },
    ]


def test_composite_brand_and_category_uses_catalog_fields_not_product_name():
    knowledge = _manufacturer_filter_knowledge()
    knowledge["entities"].extend([
        _entity("product", "商品", [{
            "attr_code": "product_name",
            "attr_name": "商品名称",
            "field_mapping": "product.product_name",
            "is_main_attribute": True,
        }]),
        _entity("product_category", "商品分类", [{
            "attr_code": "product_type",
            "attr_name": "商品类型",
            "field_mapping": "product_category.product_type",
        }]),
        _entity("dealer_result", "经销商画像", [{
            "attr_code": "manufacturer_list",
            "attr_name": "合作厂家清单",
            "field_mapping": "dealer_result.manufacturer_list",
        }]),
    ])
    ast = _asl("product.product_name")
    ast["filters"] = [{
        "field": "product.product_name",
        "operator": "LIKE",
        "value": "%江苏苏云品牌低值耗材%",
    }]

    def resolver(_model, _domain, _candidates, value):
        candidate_fields = {
            candidate["field"] for candidate in _candidates
        }
        assert "dealer_result.manufacturer_list" not in candidate_fields
        if value == "江苏苏云":
            return [
                {
                    "field": "manufacturer.manufacturer_name",
                    "canonical_value": "江苏苏云医疗器材有限公司",
                    "match_type": "CANONICAL_CONTAINS_MENTION",
                    "is_main_attribute": True,
                },
                {
                    "field": "manufacturer.standard_name",
                    "canonical_value": "江苏苏云医疗器材有限公司",
                    "match_type": "CANONICAL_CONTAINS_MENTION",
                    "is_main_attribute": False,
                },
                {
                    "field": "manufacturer.parent_brand",
                    "canonical_value": "江苏苏云",
                    "match_type": "EXACT",
                    "is_main_attribute": False,
                },
            ]
        if value == "低值耗材":
            return [
                {
                    "field": "product_category.product_type",
                    "canonical_value": "低值耗材",
                    "match_type": "EXACT",
                    "is_main_attribute": False,
                },
                {
                    "field": "product.product_name",
                    "canonical_value": "某低值耗材商品",
                    "match_type": "CANONICAL_CONTAINS_MENTION",
                    "is_main_attribute": True,
                },
            ]
        return []

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查询江苏苏云品牌低值耗材的经销商清单",
        81,
        205,
        catalog_value_resolver=resolver,
    )

    assert json.loads(normalized)["filters"] == [
        {
            "field": "product_category.product_type",
            "operator": "=",
            "value": "低值耗材",
        },
        {
            "field": "manufacturer.parent_brand",
            "operator": "=",
            "value": "江苏苏云",
        },
    ]


def test_catalog_category_before_partner_possessive_is_not_a_region():
    question = "查询上海市江苏苏云品牌低值耗材的经销商清单"

    assert _administrative_query_level("低值耗材", question) is None
    assert _administrative_query_level("上海市", question) == "city"


def test_administrative_short_name_uses_unique_source_verified_catalog_value():
    dealer = _entity("dealer", "经销商", [{
        "attr_code": "province",
        "attr_name": "所在省市",
        "field_mapping": "dealer.province",
    }])
    knowledge = {
        "entities": [dealer],
        "attributes": [],
        "relations": [],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl("dealer.province")
    ast["subject"] = {"entity": "dealer"}
    ast["filters"] = [{
        "field": "dealer.province",
        "operator": "=",
        "value": "杭州",
    }]

    def resolver(_model, _domain, _candidates, value):
        return ["dealer.province"] if value == "杭州市" else []

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查询杭州的经销商",
        81,
        205,
        exact_attribute_value_resolver=resolver,
    )

    assert json.loads(normalized)["filters"] == [{
        "field": "dealer.province",
        "operator": "=",
        "value": "杭州市",
    }]


def _dealer_geography_knowledge(*, include_duplicate_province: bool = False):
    dealer_attributes = [
        {
            "attr_code": "dealer_code",
            "attr_name": "经销商编码",
            "field_mapping": "dealer.dealer_code",
        },
        {
            "attr_code": "dealer_name",
            "attr_name": "经销商名称",
            "field_mapping": "dealer.dealer_name",
        },
        {
            "attr_code": "province",
            "attr_name": "所在省市",
            "field_mapping": "dealer.province",
        },
        {
            "attr_code": "city",
            "attr_name": "所在城市",
            "field_mapping": "dealer.city",
        },
    ]
    if include_duplicate_province:
        dealer_attributes.append({
            "attr_code": "registered_province",
            "attr_name": "注册省份",
            "field_mapping": "dealer.registered_province",
        })
    dealer = _entity("dealer", "经销商", dealer_attributes)
    profile = _entity("dealer_profile", "经销商画像", [
        {
            "attr_code": "dealer_code",
            "attr_name": "经销商编码",
            "field_mapping": "dealer_result.dealer_code",
        },
        {
            "attr_code": "dealer_name",
            "attr_name": "经销商名称",
            "field_mapping": "dealer_result.dealer_name",
        },
        {
            "attr_code": "address",
            "attr_name": "详细地址",
            "field_mapping": "dealer_result.address",
        },
    ])
    unrelated = _entity("market_area", "市场区域", [{
        "attr_code": "province",
        "attr_name": "省份",
        "field_mapping": "market_area.province",
    }])
    relation = _relation(
        "dealer",
        "dealer_has_profile",
        "dealer_profile",
        "dealer.dealer_code",
        "dealer_result.dealer_code",
    )
    return {
        "entities": [dealer, profile, unrelated],
        "attributes": [],
        "relations": [relation],
        "metrics": [],
        "dimensions": [],
    }


def test_geographic_slot_uses_source_verified_admin_field_on_result_entity():
    knowledge = _dealer_geography_knowledge()
    ast = _asl("dealer.dealer_name")
    ast["subject"] = {"entity": "dealer"}
    ast["filters"] = [{
        "field": "dealer_result.address",
        "operator": "LIKE",
        "value": "%海州%",
    }]
    ast["ambiguity"] = [{
        "type": "filter",
        "question": "过滤值海州在经销商画像的多个属性中存在，请确认筛选属性。",
        "candidates": ["dealer_result.address", "dealer_result.dealer_name"],
    }]
    candidate_sets = []

    def resolver(_model, _domain, candidates, value):
        candidate_sets.append({item["field"] for item in candidates})
        if value == "海州市":
            # Both columns contain the same canonical value.  泛指“地区”按
            # documented level precedence chooses province, never list order.
            return ["dealer.city", "dealer.province", "market_area.province"]
        return ["dealer_result.address", "dealer_result.dealer_name"]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "提供海州地区经销商及联系方式",
        900,
        901,
        exact_attribute_value_resolver=resolver,
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "dealer.province",
        "operator": "=",
        "value": "海州市",
    }]
    assert result["ambiguity"] == []
    assert candidate_sets
    assert all("dealer_result.address" not in fields for fields in candidate_sets)


def test_explicit_city_level_prefers_city_for_same_source_canonical_value():
    knowledge = _dealer_geography_knowledge()
    ast = _asl("dealer.dealer_name")
    ast["subject"] = {"entity": "dealer"}
    ast["filters"] = [{
        "field": "dealer.province",
        "operator": "=",
        "value": "海州市",
    }]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查询海州市经销商",
        900,
        901,
        exact_attribute_value_resolver=lambda *_args: [
            "dealer.province", "dealer.city",
        ],
    )

    assert json.loads(normalized)["filters"][0] == {
        "field": "dealer.city",
        "operator": "=",
        "value": "海州市",
    }


def test_result_dimension_geography_wins_over_fact_subject_geography():
    knowledge = _dealer_geography_knowledge()
    product = _entity("product", "商品", [{
        "attr_code": "product_code",
        "attr_name": "商品编码",
        "field_mapping": "product.product_code",
    }, {
        "attr_code": "city",
        "attr_name": "业务城市",
        "field_mapping": "product.city",
    }])
    knowledge["entities"].append(product)
    knowledge["relations"].append(_relation(
        "product", "product_has_dealer", "dealer",
        "product.product_code", "dealer.dealer_code",
    ))
    ast = _asl("dealer.dealer_name")
    ast["subject"] = {"entity": "product"}
    ast["dimensions"] = [{
        "name": "dealer.dealer_name", "attr": None,
        "level": None, "granularity": None,
    }]
    ast["filters"] = [{
        "field": "product.city", "operator": "=", "value": "上海市",
    }]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "统计上海市合作经销商数",
        900,
        901,
        exact_attribute_value_resolver=lambda *_args: [
            "product.city", "dealer.city",
        ],
    )

    result = json.loads(normalized)
    assert result["filters"] == [{
        "field": "dealer.city", "operator": "=", "value": "上海市",
    }]
    assert result["ambiguity"] == []


def test_equal_distance_and_level_admin_fields_remain_ambiguous():
    knowledge = _dealer_geography_knowledge(include_duplicate_province=True)
    ast = _asl("dealer.dealer_name")
    ast["subject"] = {"entity": "dealer"}
    ast["filters"] = [{
        "field": "dealer_result.address",
        "operator": "LIKE",
        "value": "%海州%",
    }]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查询海州地区经销商",
        900,
        901,
        exact_attribute_value_resolver=lambda *_args: [
            "dealer.province", "dealer.registered_province",
        ],
    )
    result = json.loads(normalized)

    assert result["filters"] == ast["filters"]
    assert result["ambiguity"][-1]["candidates"] == [
        "dealer.province=海州",
        "dealer.registered_province=海州",
    ]


def test_current_exact_entity_attribute_wins_over_another_matching_attribute():
    knowledge = _manufacturer_filter_knowledge()
    ast = _asl("manufacturer.manufacturer_name")
    ast["subject"] = {"entity": "main_data_domain_ent_manufacturer"}
    ast["filters"] = [{
        "field": "manufacturer.manufacturer_name",
        "operator": "LIKE",
        "value": "%BD%",
    }]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查询BD厂家",
        81,
        205,
        exact_attribute_value_resolver=lambda *_args: [
            "manufacturer.manufacturer_name",
            "manufacturer.parent_brand",
        ],
    )

    assert json.loads(normalized)["filters"] == [{
        "field": "manufacturer.manufacturer_name",
        "operator": "=",
        "value": "BD",
    }]


def test_ambiguous_entity_attribute_match_requires_clarification():
    knowledge = _manufacturer_filter_knowledge()
    ast = _asl("manufacturer.manufacturer_name")
    ast["subject"] = {"entity": "main_data_domain_ent_manufacturer"}
    ast["filters"] = [{
        "field": "manufacturer.manufacturer_name",
        "operator": "LIKE",
        "value": "%Global%",
    }]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查询Global品牌厂家",
        81,
        205,
        exact_attribute_value_resolver=lambda *_args: [
            "manufacturer.standard_name",
            "manufacturer.parent_brand",
        ],
    )
    result = json.loads(normalized)

    assert result["filters"] == ast["filters"]
    assert result["ambiguity"][-1]["type"] == "filter"
    assert result["ambiguity"][-1]["candidates"] == [
        "manufacturer.parent_brand",
        "manufacturer.standard_name",
    ]


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_relationship_name_filter_keeps_clarification_without_unique_name_field(candidate_count):
    _, builder = _bridge_fixture()
    builder.build("中文商品适用于哪些科室")
    knowledge = builder.last_knowledge
    product = next(
        item for item in knowledge["entities"]
        if item.metadata.get("entity_code") == "product"
    )
    attributes = [
        attribute for attribute in product.metadata["attributes"]
        if attribute.get("attr_code") != "product_name"
    ]
    if candidate_count == 2:
        attributes.extend([
            {
                "attr_code": "display_name",
                "attr_name": "商品名称",
                "field_mapping": "product.display_name",
            },
            {
                "attr_code": "official_name",
                "attr_name": "商品名称",
                "field_mapping": "product.official_name",
            },
        ])
    product.metadata["attributes"] = attributes
    ast = _asl("department.dept_name")
    ast["filters"] = [{
        "field": "product_dept_relation.product_code",
        "operator": "=",
        "value": "中文商品",
    }]

    result = json.loads(_normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "中文商品适用于哪些科室",
    ))

    assert result["filters"][0]["field"] == "product_dept_relation.product_code"
    assert result["ambiguity"]
    assert result["ambiguity"][-1]["type"] == "filter"
    assert len(result["ambiguity"][-1]["candidates"]) == candidate_count


def test_relationship_code_filter_with_code_like_value_is_not_rewritten():
    _, builder = _bridge_fixture()
    builder.build("P-001适用于哪些科室")
    knowledge = builder.last_knowledge
    ast = _asl("department.dept_name")
    ast["filters"] = [{
        "field": "product_dept_relation.product_code",
        "operator": "=",
        "value": "P-001",
    }]

    result = json.loads(_normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "P-001适用于哪些科室",
    ))

    assert result["filters"][0] == ast["filters"][0]
    assert result["ambiguity"] == []
