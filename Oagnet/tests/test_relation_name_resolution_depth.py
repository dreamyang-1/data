import json

from agent import _normalize_semantic_references, _validate_asl_output
from vector_store import SearchResult


def _entity(code: str, table: str, attributes: list[dict]) -> SearchResult:
    return SearchResult(
        id=f"entity:{code}",
        score=0.9,
        text=code,
        metadata={
            "type": "entity",
            "semantic_model_id": 81,
            "business_domain_id": 205,
            "entity_code": code,
            "entity_name": code,
            "table_name": table,
            "attributes": attributes,
            "relations": [],
        },
    )


def _relation(
    source: str,
    target: str,
    source_field: str,
    target_field: str,
) -> SearchResult:
    return SearchResult(
        id=f"relation:{source}:{target}",
        score=0.9,
        text=f"{source}:{target}",
        metadata={
            "type": "relation",
            "semantic_model_id": 81,
            "business_domain_id": 205,
            "parent": source,
            "target_entity": target,
            "relation_code": f"{source}_to_{target}",
            "join_key": {
                "source_field": source_field,
                "target_field": target_field,
            },
        },
    )


def test_department_names_on_dealer_key_resolve_across_registered_four_hop_path():
    entities = [
        _entity("dealer_profile", "dealer_result", [
            {
                "attr_code": "dealer_code",
                "field_mapping": "dealer_result.dealer_code",
                "is_unique": True,
            },
        ]),
        _entity("dealer", "dealer", [
            {"attr_code": "dealer_code", "field_mapping": "dealer.dealer_code"},
            {
                "attr_code": "dealer_name",
                "field_mapping": "dealer.dealer_name",
                "is_main_attribute": True,
            },
        ]),
        _entity("dealer_product_relation", "dealer_product_relation", [
            {"attr_code": "dealer_code", "field_mapping": "dealer_product_relation.dealer_code"},
            {"attr_code": "product_code", "field_mapping": "dealer_product_relation.product_code"},
        ]),
        _entity("product", "product", [
            {"attr_code": "product_code", "field_mapping": "product.product_code"},
        ]),
        _entity("product_department_relation", "product_dept_relation", [
            {"attr_code": "product_code", "field_mapping": "product_dept_relation.product_code"},
            {"attr_code": "dept_code", "field_mapping": "product_dept_relation.dept_code"},
        ]),
        _entity("department", "department", [
            {"attr_code": "dept_code", "field_mapping": "department.dept_code"},
            {
                "attr_code": "dept_name",
                "field_mapping": "department.dept_name",
                "is_main_attribute": True,
            },
        ]),
        _entity("product_line", "product_line", [
            {"attr_code": "line_code", "field_mapping": "product_line.line_code"},
            {
                "attr_code": "line_level1",
                "field_mapping": "product_line.line_level1",
            },
        ]),
    ]
    relations = [
        _relation(
            "dealer_profile", "dealer",
            "dealer_result.dealer_code", "dealer.dealer_code",
        ),
        _relation(
            "dealer", "dealer_product_relation",
            "dealer.dealer_code", "dealer_product_relation.dealer_code",
        ),
        _relation(
            "dealer_product_relation", "product",
            "dealer_product_relation.product_code", "product.product_code",
        ),
        _relation(
            "product", "product_department_relation",
            "product.product_code", "product_dept_relation.product_code",
        ),
        _relation(
            "product_department_relation", "department",
            "product_dept_relation.dept_code", "department.dept_code",
        ),
        _relation(
            "dealer", "product_line",
            "dealer.dealer_code", "product_line.line_code",
        ),
    ]
    knowledge = {
        "entities": entities,
        "attributes": [],
        "relations": relations,
        "metrics": [],
        "dimensions": [],
    }
    ast = {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "dealer"},
        "metrics": [],
        "dimensions": [{
            "name": "dealer.dealer_name",
            "attr": None,
            "level": None,
            "granularity": None,
        }],
        "filters": [{
            "field": "dealer_result.dealer_code",
            "operator": "IN",
            "value": ["泌尿外科", "肾脏内科"],
        }],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }
    candidate_fields: list[set[str]] = []

    def resolve_department(_model, _domain, candidates, value):
        fields = {candidate["field"] for candidate in candidates}
        candidate_fields.append(fields)
        if value == "泌尿外科":
            return [
                field for field in (
                    "department.dept_name", "product_line.line_level1",
                )
                if field in fields
            ]
        if value == "肾脏内科" and "department.dept_name" in fields:
            return ["department.dept_name"]
        return []

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "我的产品适用于泌尿外科、肾脏内科，帮我推荐上海合适的经销商",
        81,
        205,
        exact_value_resolver=lambda *_args: [],
        exact_attribute_value_resolver=resolve_department,
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "department.dept_name",
        "operator": "IN",
        "value": ["泌尿外科", "肾脏内科"],
    }]
    assert result["ambiguity"] == []
    assert candidate_fields
    assert all("department.dept_name" in fields for fields in candidate_fields)
    _validate_asl_output(normalized, knowledge, "泌尿外科、肾脏内科")


def test_explicit_department_wording_wins_when_source_value_also_exists_in_product_line():
    entities = [
        _entity("dealer", "dealer", [
            {
                "attr_code": "dealer_code",
                "attr_name": "经销商编码",
                "field_mapping": "dealer.dealer_code",
                "is_primary_key": True,
            },
            {
                "attr_code": "dealer_name",
                "attr_name": "经销商名称",
                "field_mapping": "dealer.dealer_name",
                "is_main_attribute": True,
            },
        ]),
        _entity("department", "department", [
            {
                "attr_code": "dept_code",
                "attr_name": "科室编码",
                "field_mapping": "department.dept_code",
            },
            {
                "attr_code": "dept_name",
                "attr_name": "科室名称",
                "field_mapping": "department.dept_name",
                "is_main_attribute": True,
                "is_unique": True,
            },
        ]),
        _entity("product_line", "product_line", [
            {
                "attr_code": "line_code",
                "attr_name": "产品线编码",
                "field_mapping": "product_line.line_code",
            },
            {
                "attr_code": "line_level1",
                "attr_name": "一级产品线",
                "field_mapping": "product_line.line_level1",
            },
        ]),
    ]
    knowledge = {
        "entities": entities,
        "attributes": [],
        "relations": [
            _relation(
                "dealer", "department",
                "dealer.dealer_code", "department.dept_code",
            ),
            _relation(
                "dealer", "product_line",
                "dealer.dealer_code", "product_line.line_code",
            ),
        ],
        "metrics": [],
        "dimensions": [],
    }
    ast = {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "dealer"},
        "metrics": [],
        "dimensions": [{
            "name": "dealer.dealer_name",
            "attr": None,
            "level": None,
            "granularity": None,
        }],
        "filters": [{
            "field": "dealer.dealer_code",
            "operator": "=",
            "value": "心血管内科",
        }],
        "time_context": None,
        "sort": None,
        "limit": 3,
        "having": [],
        "ambiguity": [],
    }

    def resolve_value(_model, _domain, candidates, value):
        fields = {candidate["field"] for candidate in candidates}
        return [
            field for field in (
                "department.dept_name", "product_line.line_level1",
            )
            if value == "心血管内科" and field in fields
        ]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "限定地区上海、适用科室心血管内科，自动筛选经销商并展示 TOP3 画像",
        81,
        205,
        exact_value_resolver=lambda *_args: [],
        exact_attribute_value_resolver=resolve_value,
        catalog_value_resolver=lambda *_args: [],
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "department.dept_name",
        "operator": "=",
        "value": "心血管内科",
    }]
    assert result["ambiguity"] == []
    _validate_asl_output(normalized, knowledge, "适用科室心血管内科")


def test_normalized_brand_field_wins_over_denormalized_dealer_profile_list():
    entities = [
        _entity("product", "product", [
            {
                "attr_code": "product_code",
                "attr_name": "商品编码",
                "field_mapping": "product.product_code",
                "is_primary_key": True,
            },
        ]),
        _entity("manufacturer", "manufacturer", [
            {
                "attr_code": "manufacturer_code",
                "attr_name": "厂家编码",
                "field_mapping": "manufacturer.manufacturer_code",
            },
            {
                "attr_code": "parent_brand",
                "attr_name": "母品牌",
                "field_mapping": "manufacturer.parent_brand",
            },
        ]),
        _entity("dealer_result", "dealer_result", [
            {
                "attr_code": "dealer_code",
                "attr_name": "经销商编码",
                "field_mapping": "dealer_result.dealer_code",
            },
            {
                "attr_code": "manufacturer_list",
                "attr_name": "合作厂家清单",
                "field_mapping": "dealer_result.manufacturer_list",
            },
        ]),
    ]
    knowledge = {
        "entities": entities,
        "attributes": [],
        "relations": [
            _relation(
                "product", "manufacturer",
                "product.product_code", "manufacturer.manufacturer_code",
            ),
            _relation(
                "product", "dealer_result",
                "product.product_code", "dealer_result.dealer_code",
            ),
        ],
        "metrics": [],
        "dimensions": [],
    }
    ast = {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "product"},
        "metrics": [],
        "dimensions": [],
        "filters": [{
            "field": "product.product_code",
            "operator": "=",
            "value": "费森尤斯",
        }],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }

    def resolve_value(_model, _domain, candidates, value):
        fields = {candidate["field"] for candidate in candidates}
        return [
            field for field in (
                "manufacturer.parent_brand", "dealer_result.manufacturer_list",
            )
            if value == "费森尤斯" and field in fields
        ]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "上海地区费森尤斯产品近一年销售趋势如何",
        81,
        205,
        exact_value_resolver=lambda *_args: [],
        exact_attribute_value_resolver=resolve_value,
        catalog_value_resolver=lambda *_args: [],
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "manufacturer.parent_brand",
        "operator": "=",
        "value": "费森尤斯",
    }]
    assert not any(
        ambiguity.get("type") == "filter"
        for ambiguity in result["ambiguity"]
    )
