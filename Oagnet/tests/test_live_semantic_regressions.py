import json

from agent import (
    _normalize_explicit_time_granularity,
    _normalize_semantic_references,
    _validate_asl_output,
)
from prompt_build import PromptBuilder
from vector_store import SearchResult


def _result(record_id: str, kind: str, **metadata) -> SearchResult:
    return SearchResult(
        id=record_id,
        score=float(metadata.pop("score", 0.5)),
        text=record_id,
        metadata={"type": kind, "semantic_model_id": 81, "business_domain_id": 205, **metadata},
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


def _asl(*, dimensions=None, filters=None, metrics=None, time_context=None):
    return {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": None},
        "metrics": metrics or [],
        "dimensions": dimensions or [],
        "filters": filters or [],
        "time_context": time_context,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }


def _where_type(where: dict) -> str:
    return next(
        str(clause["type"])
        for clause in where.get("$and", [where])
        if "type" in clause
    )


class _ScopedMetricStore:
    def __init__(self, desired: SearchResult):
        self.desired = desired
        self.distractors = [
            _result(
                f"metric:distractor-{index}",
                "metric",
                score=1 - index / 100,
                metric_code=f"distractor_{index}",
                metric_name=f"干扰指标{index}",
            )
            for index in range(40)
        ]

    def count(self):
        return 41

    def search(self, _vector, top_k: int, where: dict):
        return self.distractors[:top_k] if _where_type(where) == "metric" else []

    def get_by_where(self, where: dict):
        return [*self.distractors, self.desired] if _where_type(where) == "metric" else []


def test_caller_bound_metric_is_loaded_by_scope_even_outside_vector_top40():
    desired = _result(
        "metric:annual_total_sales",
        "metric",
        metric_code="annual_total_sales",
        metric_name="含税销售总额",
        source_dependency={"bind_entity": ["sales_order"]},
        calculation_rule={"calc_formula": "SUM(sales_order.amount_with_tax)"},
    )
    builder = PromptBuilder(
        _ScopedMetricStore(desired),
        lambda _query: [0.1],
        semantic_model_id=81,
        preferred_metric_codes=["annual_total_sales"],
    )

    knowledge = builder.retrieve("近一年活跃经销商在销售费森尤斯产品")

    assert [
        item.metadata["metric_code"] for item in knowledge["metrics"]
    ] == ["annual_total_sales"]


def test_unretrieved_department_filter_is_repaired_only_from_all_value_evidence():
    department = _entity("department", "科室", [{
        "attr_code": "dept_name",
        "attr_name": "科室名称",
        "field_mapping": "department.dept_name",
        "is_main_attribute": True,
    }])
    dealer = _entity("dealer", "经销商", [{
        "attr_code": "dealer_name",
        "attr_name": "经销商名称",
        "field_mapping": "dealer.dealer_name",
        "is_main_attribute": True,
    }])
    knowledge = {
        "entities": [department, dealer],
        "attributes": [],
        "relations": [],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl(
        dimensions=[{"name": "dealer.dealer_name", "attr": None}],
        filters=[{
            "field": "product_dept_relation.dept_name",
            "operator": "IN",
            "value": ["泌尿外科", "肾脏内科"],
        }],
    )

    def resolver(_model, _domain, candidates, value):
        assert value in {"泌尿外科", "肾脏内科"}
        assert "department.dept_name" in {
            candidate["field"] for candidate in candidates
        }
        return ["department.dept_name"]

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "我的产品适用于泌尿外科、肾脏内科，帮我推荐上海合适的经销商",
        81,
        205,
        exact_attribute_value_resolver=resolver,
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "department.dept_name",
        "operator": "IN",
        "value": ["泌尿外科", "肾脏内科"],
    }]
    assert not any(
        item.get("type") == "filter" for item in result["ambiguity"]
    )
    _validate_asl_output(normalized, knowledge, "泌尿外科、肾脏内科经销商")


def test_unretrieved_filter_never_executes_when_source_evidence_is_not_unique():
    department = _entity("department", "科室", [{
        "attr_code": "dept_name",
        "attr_name": "科室名称",
        "field_mapping": "department.dept_name",
    }])
    knowledge = {
        "entities": [department],
        "attributes": [],
        "relations": [],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl(filters=[{
        "field": "invented.department_name",
        "operator": "=",
        "value": "泌尿外科",
    }])

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查询泌尿外科",
        81,
        205,
        exact_attribute_value_resolver=lambda *_args: [],
    )
    result = json.loads(normalized)

    assert result["filters"] == []
    assert result["ambiguity"]
    assert "泌尿外科" in result["ambiguity"][0]["question"]
    _validate_asl_output(normalized, knowledge, "查询泌尿外科")


def test_contextual_product_noun_on_relation_key_uses_unique_catalog_value():
    product = _entity("product", "商品", [
        {
            "attr_code": "manufacturer_code",
            "attr_name": "厂家编码",
            "field_mapping": "product.manufacturer_code",
        },
        {
            "attr_code": "product_name",
            "attr_name": "商品名称",
            "field_mapping": "product.product_name",
            "is_main_attribute": True,
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
            "attr_name": "母厂牌",
            "field_mapping": "manufacturer.parent_brand",
            "description": "所属母品牌/集团品牌/厂牌",
        },
    ])
    relation = _relation(
        "manufacturer_produces_product",
        "manufacturer",
        "product",
        "manufacturer.manufacturer_code",
        "product.manufacturer_code",
    )
    knowledge = {
        "entities": [product, manufacturer],
        "attributes": [],
        "relations": [relation],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl(filters=[{
        "field": "product.manufacturer_code",
        "operator": "=",
        "value": "费森尤斯产品",
    }])

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "上海地区费森尤斯产品近一年销售趋势如何",
        81,
        205,
        exact_value_resolver=lambda *_args: [],
        exact_attribute_value_resolver=lambda *_args: [],
        catalog_value_resolver=lambda *_args: [{
            "entity_code": "manufacturer",
            "field": "manufacturer.parent_brand",
            "canonical_value": "费森尤斯",
            "match_type": "MENTION_CONTAINS_CANONICAL",
            "is_main_attribute": False,
        }],
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "manufacturer.parent_brand",
        "operator": "=",
        "value": "费森尤斯",
    }]
    assert not any(
        item.get("type") == "filter" for item in result["ambiguity"]
    )
    _validate_asl_output(normalized, knowledge, "费森尤斯产品")


def test_generic_contact_projection_expands_only_recalled_nearest_attributes():
    profile = _entity("dealer_profile", "经销商画像", [
        {
            "attr_code": "dealer_name",
            "attr_name": "经销商名称",
            "field_mapping": "dealer_result.dealer_name",
            "is_main_attribute": True,
        },
        {
            "attr_code": "email",
            "attr_name": "邮箱",
            "field_mapping": "dealer_result.email",
        },
        {
            "attr_code": "address",
            "attr_name": "地址",
            "field_mapping": "dealer_result.address",
        },
    ])
    knowledge = {
        "entities": [profile],
        "attributes": [],
        "relations": [],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl(dimensions=[
        {"name": "dealer_result.dealer_name", "attr": None},
        {"name": "dealer_result.contact_info", "attr": None},
    ])

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "提供上海地区BD品牌产品的经销商及联系方式",
        81,
        205,
    )
    result = json.loads(normalized)

    assert {item["name"] for item in result["dimensions"]} == {
        "dealer_result.dealer_name",
        "dealer_result.email",
        "dealer_result.address",
    }
    _validate_asl_output(normalized, knowledge, "经销商及联系方式")


def test_unretrieved_dealer_projection_uses_unique_recalled_main_attribute():
    """D01/W01 shape: a model-written label must not cause a validation 502."""
    profile = _entity("dealer_profile", "dealer profile", [
        {
            "attr_code": "dealer_name",
            "attr_name": "dealer name",
            "field_mapping": "dealer_result.dealer_name",
            "is_main_attribute": True,
        },
        {
            "attr_code": "business_scale",
            "attr_name": "business scale",
            "field_mapping": "dealer_result.business_scale",
        },
    ])
    knowledge = {
        "entities": [profile],
        "attributes": [],
        "relations": [],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl(dimensions=[{
        "name": "dealer_profile.company_name",
        "attr": None,
    }])
    ast["subject"] = {"entity": "dealer_profile"}

    normalized = _normalize_semantic_references(
        json.dumps(ast),
        knowledge,
        "recommend suitable dealers and show dealer profiles",
        81,
        205,
    )
    result = json.loads(normalized)

    assert result["dimensions"] == [{
        "name": "dealer_result.dealer_name",
        "attr": None,
        "level": None,
        "granularity": None,
    }]
    assert result["ambiguity"] == []
    _validate_asl_output(normalized, knowledge, "recommend suitable dealers")


def test_ambiguous_unretrieved_projection_becomes_clarification_not_502():
    profile = _entity("dealer_profile", "dealer profile", [
        {
            "attr_code": "dealer_name",
            "attr_name": "dealer name",
            "field_mapping": "dealer_result.dealer_name",
            "is_main_attribute": True,
        },
        {
            "attr_code": "company_name",
            "attr_name": "company name",
            "field_mapping": "dealer_result.company_name",
            "is_main_attribute": True,
        },
    ])
    knowledge = {
        "entities": [profile],
        "attributes": [],
        "relations": [],
        "metrics": [],
        "dimensions": [],
    }
    ast = _asl(dimensions=[{
        "name": "dealer_profile.profile_label",
        "attr": None,
    }])
    ast["subject"] = {"entity": "dealer_profile"}

    normalized = _normalize_semantic_references(
        json.dumps(ast),
        knowledge,
        "show profile",
        81,
        205,
    )
    result = json.loads(normalized)

    assert result["dimensions"] == []
    assert result["ambiguity"][0]["type"] == "dimension"
    assert set(result["ambiguity"][0]["candidates"]) == {
        "dealer_result.company_name",
        "dealer_result.dealer_name",
    }
    _validate_asl_output(normalized, knowledge, "show profile")


def test_brand_literal_on_product_name_moves_to_unique_related_catalog_field():
    """Q09 shape: manufacturer brand is not a product-name substring."""
    product = _entity("product", "product", [
        {
            "attr_code": "product_name",
            "attr_name": "product name",
            "field_mapping": "product.product_name",
            "is_main_attribute": True,
        },
        {
            "attr_code": "manufacturer_code",
            "attr_name": "manufacturer code",
            "field_mapping": "product.manufacturer_code",
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
    ])
    knowledge = {
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
    ast = _asl(
        dimensions=[{"name": "product.product_name", "attr": None}],
        filters=[{
            "field": "product.product_name",
            "operator": "LIKE",
            "value": "%Fresenius%",
        }],
    )
    ast["subject"] = {"entity": "product"}

    def catalog_resolver(_model, _domain, candidates, value):
        assert value == "Fresenius"
        assert "manufacturer.parent_brand" in {
            candidate["field"] for candidate in candidates
        }
        return [{
            "entity_code": "manufacturer",
            "field": "manufacturer.parent_brand",
            "canonical_value": "Fresenius",
            "match_type": "EXACT",
            "is_main_attribute": False,
        }]

    normalized = _normalize_semantic_references(
        json.dumps(ast),
        knowledge,
        "monthly sales of Fresenius products",
        81,
        205,
        exact_attribute_value_resolver=lambda *_args: [],
        catalog_value_resolver=catalog_resolver,
    )
    result = json.loads(normalized)

    assert result["filters"] == [{
        "field": "manufacturer.parent_brand",
        "operator": "=",
        "value": "Fresenius",
    }]
    assert result["ambiguity"] == []
    _validate_asl_output(normalized, knowledge, "Fresenius products")


def test_explicit_month_contract_overrides_model_day_granularity():
    metric = _result(
        "metric:annual_total_sales",
        "metric",
        metric_code="annual_total_sales",
        metric_name="含税销售总额",
        source_dependency={"bind_entity": ["sales_order"]},
        time_caliber={"time_anchor": "sales_order.created_date"},
    )
    dimension = _result(
        "dimension:transaction_date",
        "dimension",
        dim_code="transaction_date",
        dim_name="交易日期",
        dim_type="时间维度",
        granularity_support=["day", "week", "month", "quarter", "year"],
        bind_metrics=["annual_total_sales"],
        bind_entities=[{
            "attr": "date-attribute-id",
            "mappingTable": "sales_order",
            "mappingColumn": "created_date",
        }],
    )
    knowledge = {
        "entities": [],
        "attributes": [],
        "relations": [],
        "metrics": [metric],
        "dimensions": [dimension],
    }
    ast = _asl(
        metrics=[{"name": "annual_total_sales", "alias": "含税销售总额"}],
        dimensions=[{
            "name": "transaction_date",
            "attr": "date-attribute-id",
            "granularity": "day",
        }],
        time_context={
            "type": "custom",
            "start": "2025-10-17",
            "end": "2025-12-30",
            "unit": "month",
            "anchor": "sales_order.created_date",
        },
    )

    normalized = _normalize_explicit_time_granularity(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "查看2025年10月17日至2025年12月30日该产品含税销售总额按月趋势，"
        "执行要求：必须按月分组返回。",
    )
    result = json.loads(normalized)

    assert result["dimensions"][0]["granularity"] == "month"
    assert result["time_context"]["unit"] == "month"
