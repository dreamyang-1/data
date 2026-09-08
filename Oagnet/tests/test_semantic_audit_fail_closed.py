import json

from agent import (
    _normalize_relation_name_filters,
    _normalize_semantic_references,
    _validate_asl_output,
)
from vector_store import SearchResult


def _result(record_id: str, kind: str, **metadata) -> SearchResult:
    return SearchResult(
        id=record_id,
        score=0.5,
        text=record_id,
        metadata={"type": kind, **metadata},
    )


def _entity(code: str, name: str, attributes: list[dict]) -> SearchResult:
    return _result(
        f"entity:{code}",
        "entity",
        entity_code=code,
        entity_name=name,
        business_domain_id=205,
        attributes=attributes,
    )


def _relation(
    code: str,
    source: str,
    target: str,
    source_field: str,
    target_field: str,
) -> SearchResult:
    return _result(
        f"relation:{code}",
        "relation",
        relation_code=code,
        parent=source,
        target_entity=target,
        join_key={
            "source_field": source_field,
            "target_field": target_field,
        },
    )


def _asl(*, dimensions=None, filters=None) -> dict:
    return {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": None},
        "metrics": [],
        "dimensions": dimensions or [],
        "filters": filters or [],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }


def test_qualified_parent_company_projection_never_falls_back_to_dealer_name():
    dealer = _entity("dealer", "dealer", [{
        "attr_code": "dealer_name",
        "attr_name": "dealer name",
        "field_mapping": "dealer.dealer_name",
        "is_main_attribute": True,
    }])
    knowledge = {
        "entities": [dealer], "attributes": [], "relations": [],
        "metrics": [], "dimensions": [],
    }
    ast = _asl(dimensions=[{
        "name": "dealer.parent_company_name",
        "alias": "parent company name",
    }])
    ast["subject"] = {"entity": "dealer"}

    normalized = _normalize_semantic_references(
        json.dumps(ast),
        knowledge,
        "show each dealer's parent company name",
        81,
        205,
    )
    result = json.loads(normalized)

    assert result["dimensions"] == []
    assert result["ambiguity"][-1]["type"] == "dimension"
    assert "dealer.parent_company_name" in result["ambiguity"][-1]["question"]
    _validate_asl_output(normalized, knowledge, "dealer parent company name")


def test_generic_company_projection_can_still_use_unique_main_display_name():
    dealer = _entity("dealer", "dealer", [{
        "attr_code": "dealer_name",
        "attr_name": "dealer name",
        "field_mapping": "dealer.dealer_name",
        "is_main_attribute": True,
    }])
    knowledge = {
        "entities": [dealer], "attributes": [], "relations": [],
        "metrics": [], "dimensions": [],
    }
    ast = _asl(dimensions=[{
        "name": "dealer.company_name",
        "alias": "company name",
    }])
    ast["subject"] = {"entity": "dealer"}

    result = json.loads(_normalize_semantic_references(
        json.dumps(ast), knowledge, "show dealer company names", 81, 205,
    ))

    assert result["dimensions"][0]["name"] == "dealer.dealer_name"
    assert result["ambiguity"] == []


def _brand_knowledge() -> dict:
    product = _entity("product", "product", [
        {
            "attr_code": "manufacturer_code",
            "attr_name": "manufacturer code",
            "field_mapping": "product.manufacturer_code",
        },
        {
            "attr_code": "product_name",
            "attr_name": "product name",
            "field_mapping": "product.product_name",
            "is_main_attribute": True,
        },
        {
            "attr_code": "product_category",
            "attr_name": "product category",
            "field_mapping": "product.product_category",
        },
        {
            "attr_code": "product_type",
            "attr_name": "product type",
            "field_mapping": "product.product_type",
        },
        {
            "attr_code": "shipping_address",
            "attr_name": "shipping address",
            "field_mapping": "product.shipping_address",
        },
        {
            "attr_code": "contact_name",
            "attr_name": "contact name",
            "field_mapping": "product.contact_name",
        },
        {
            "attr_code": "created_date",
            "attr_name": "created date",
            "field_mapping": "product.created_date",
        },
        {
            "attr_code": "status_name",
            "attr_name": "status name",
            "field_mapping": "product.status_name",
        },
        {
            "attr_code": "sales_amount",
            "attr_name": "sales amount",
            "field_mapping": "product.sales_amount",
            "data_type": "decimal(18,2)",
        },
    ])
    manufacturer = _entity("manufacturer", "manufacturer", [
        {
            "attr_code": "manufacturer_code",
            "attr_name": "manufacturer code",
            "field_mapping": "manufacturer.manufacturer_code",
        },
        {
            "attr_code": "manufacturer_name",
            "attr_name": "manufacturer name",
            "field_mapping": "manufacturer.manufacturer_name",
            "is_main_attribute": True,
        },
        {
            "attr_code": "parent_brand",
            "attr_name": "parent brand",
            "field_mapping": "manufacturer.parent_brand",
        },
        {
            "attr_code": "office_address",
            "attr_name": "office address",
            "field_mapping": "manufacturer.office_address",
        },
        {
            "attr_code": "support_phone",
            "attr_name": "support phone",
            "field_mapping": "manufacturer.support_phone",
        },
    ])
    return {
        "entities": [product, manufacturer],
        "attributes": [],
        "relations": [_relation(
            "manufacturer_product",
            "manufacturer",
            "product",
            "manufacturer.manufacturer_code",
            "product.manufacturer_code",
        )],
        "metrics": [],
        "dimensions": [],
    }


def test_brand_catalog_migration_exposes_only_identity_attributes():
    knowledge = _brand_knowledge()
    ast = _asl(filters=[{
        "field": "product.manufacturer_code",
        "operator": "=",
        "value": "Acme products",
    }])
    candidate_sets: list[set[str]] = []

    def catalog(_model, _domain, candidates, _value):
        fields = {candidate["field"] for candidate in candidates}
        candidate_sets.append(fields)
        return [{
            "field": "manufacturer.parent_brand",
            "canonical_value": "Acme",
            "match_type": "MENTION_CONTAINS_CANONICAL",
            "is_main_attribute": False,
        }]

    normalized = _normalize_semantic_references(
        json.dumps(ast),
        knowledge,
        "find dealers selling Acme products",
        81,
        205,
        exact_value_resolver=lambda *_args: [],
        exact_attribute_value_resolver=lambda *_args: [],
        catalog_value_resolver=catalog,
    )
    result = json.loads(normalized)
    exposed = set().union(*candidate_sets)

    assert result["filters"] == [{
        "field": "manufacturer.parent_brand",
        "operator": "=",
        "value": "Acme",
    }]
    assert {
        "manufacturer.parent_brand", "manufacturer.manufacturer_name",
        "product.product_name", "product.product_category", "product.product_type",
    } <= exposed
    assert not exposed.intersection({
        "product.shipping_address", "product.contact_name", "product.created_date",
        "product.status_name", "product.sales_amount", "manufacturer.office_address",
        "manufacturer.support_phone",
    })


def test_equal_rank_brand_catalog_matches_remain_a_clarification():
    knowledge = _brand_knowledge()
    ast = _asl(filters=[{
        "field": "product.manufacturer_code",
        "operator": "=",
        "value": "Acme products",
    }])

    def catalog(_model, _domain, _candidates, _value):
        return [
            {
                "field": "manufacturer.parent_brand",
                "canonical_value": "Acme",
                "match_type": "EXACT",
                "is_main_attribute": False,
            },
            {
                "field": "manufacturer.manufacturer_name",
                "canonical_value": "Acme",
                "match_type": "EXACT",
                "is_main_attribute": False,
            },
        ]

    _normalize_relation_name_filters(
        ast,
        knowledge,
        "find Acme products",
        semantic_model_id=81,
        business_domain_id=205,
        exact_value_resolver=lambda *_args: [],
        exact_attribute_value_resolver=lambda *_args: [],
        catalog_value_resolver=catalog,
    )

    assert ast["filters"][0]["field"] == "product.manufacturer_code"
    assert ast["ambiguity"][-1]["type"] == "filter"
    assert set(ast["ambiguity"][-1]["candidates"]) == {
        "manufacturer.manufacturer_name=Acme", "manufacturer.parent_brand=Acme",
    }


def test_department_names_on_unconnected_department_code_never_execute():
    relation_entity = _entity("product_department_relation", "product department", [
        {
            "attr_code": "product_code",
            "attr_name": "product code",
            "field_mapping": "product_dept_relation.product_code",
        },
        {
            "attr_code": "dept_code",
            "attr_name": "department code",
            "field_mapping": "product_dept_relation.dept_code",
        },
    ])
    product = _entity("product", "product", [{
        "attr_code": "product_code",
        "attr_name": "product code",
        "field_mapping": "product.product_code",
    }, {
        "attr_code": "product_name",
        "attr_name": "product name",
        "field_mapping": "product.product_name",
        "is_main_attribute": True,
    }])
    knowledge = {
        "entities": [relation_entity, product],
        "attributes": [],
        "relations": [_relation(
            "product_department_product",
            "product_department_relation",
            "product",
            "product_dept_relation.product_code",
            "product.product_code",
        )],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl(
        dimensions=[{"name": "product.product_name"}],
        filters=[{
            "field": "product_dept_relation.dept_code",
            "operator": "IN",
            "value": ["泌尿外科", "肾脏内科"],
        }],
    )

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "产品适用于泌尿外科、肾脏内科",
        81,
        205,
        exact_value_resolver=lambda *_args: [],
        exact_attribute_value_resolver=lambda *_args: [],
        catalog_value_resolver=lambda *_args: [],
    )
    result = json.loads(normalized)

    assert result["filters"][0]["field"] == "product_dept_relation.dept_code"
    assert result["ambiguity"][-1]["type"] == "filter"
    _validate_asl_output(normalized, knowledge, "泌尿外科、肾脏内科")
