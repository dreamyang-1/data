import json
from types import SimpleNamespace

import pytest
import agent

from agent import (
    _apply_intent_asl_contract,
    _contract_filter_candidates,
    _validate_asl_output,
    _validate_intent_asl_contract,
)
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


def test_geographic_contract_filter_prefers_query_object_relation_path():
    knowledge = {
        "entities": [
            _entity("dealer", "经销商", "dealer.dealer_name", "经销商名称"),
            _entity("hospital", "医院", "hospital.business_city", "业务城市"),
            _entity("city", "城市", "dim_city.city_name", "城市名称"),
        ],
        "dimensions": [SimpleNamespace(metadata={
            "dim_code": "city",
            "dim_name": "城市",
            "synonyms": ["市", "地级市"],
            "bind_entities": [{
                "entity": "city",
                "mappingTable": "dim_city",
                "mappingColumn": "city_name",
            }],
        })],
        "relations": [
            SimpleNamespace(metadata={
                "parent": "dealer",
                "target_entity": "city",
                "join_key": {
                    "source_field": "dealer.city_id",
                    "target_field": "dim_city.city_id",
                },
            }),
            SimpleNamespace(metadata={
                "parent": "hospital",
                "target_entity": "city",
                "join_key": {
                    "source_field": "hospital.city_id",
                    "target_field": "dim_city.city_id",
                },
            }),
        ],
    }
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "经销商",
        "metric_required": True,
        "required_metric_codes": ["cooperating_hospital_count"],
        "required_projections": [],
        "required_groupings": ["经销商"],
        "filters": [{"field": "业务城市", "operator": "EQ", "value": "上海市"}],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "cooperating_hospital_count"}]
    ast["dimensions"] = [{"name": "dealer.dealer_name"}]
    ast["filters"] = [{
        "field": "hospital.business_city", "operator": "=", "value": "上海市",
    }]

    repaired, _repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract,
    )
    repaired_ast = json.loads(repaired)

    assert repaired_ast["filters"] == [{
        "field": "dim_city.city_name", "operator": "=", "value": "上海市",
    }]
    _validate_intent_asl_contract(repaired, contract, knowledge)


@pytest.mark.parametrize(
    "query_object,projection,entity_code,entity_name,field,attr_name",
    (
        ("经销商", "经销商名称", "dealer", "经销商", "dealer.dealer_name", "经销商名称"),
        ("医院", "医院名称", "hospital", "医院", "hospital.hospital_name", "医院名称"),
        ("商品", "商品名称", "product", "商品", "product.product_name", "商品名称"),
        ("厂家", "厂家名称", "manufacturer", "厂家", "manufacturer.manufacturer_name", "厂家名称"),
        ("商品", "商品规格", "product", "商品", "product.specification", "商品规格"),
    ),
)
def test_detail_projection_is_repaired_from_recalled_metadata(
    query_object, projection, entity_code, entity_name, field, attr_name
):
    knowledge = {"entities": [_entity(entity_code, entity_name, field, attr_name)]}
    contract = {
        "version": "1.0",
        "intent": "DETAIL_QUERY",
        "query_object": query_object,
        "metric_required": False,
        "required_metrics": [],
        "required_metric_codes": [],
        "required_projections": [projection],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }

    repaired, repairs = _apply_intent_asl_contract(_detail_ast(entity_code), knowledge, contract)
    ast = json.loads(repaired)

    assert [item["name"] for item in ast["dimensions"]] == [field]
    assert repairs == [{
        "type": "ADD_REQUIRED_PROJECTION",
        "semantic_label": projection,
        "resolved_field": field,
        "source": "RECALLED_SEMANTIC_METADATA",
    }]
    validated = _validate_asl_output(repaired, knowledge, required_metric_codes=[])
    _validate_intent_asl_contract(validated, contract)


def test_detail_projection_missing_has_precise_structured_error():
    contract = {
        "version": "1.0",
        "intent": "DETAIL_QUERY",
        "query_object": "经销商",
        "metric_required": False,
        "required_metrics": [],
        "required_metric_codes": [],
        "required_projections": ["经销商名称"],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }
    with pytest.raises(ASLValidationError) as exc:
        _apply_intent_asl_contract(_detail_ast("dealer"), {}, contract)

    assert exc.value.code == "ASL_DETAIL_PROJECTION_MISSING"
    assert exc.value.field == "dimensions"
    assert exc.value.code != "ASL_METRIC_SELECTION_INVALID"


def test_metricless_attribute_detail_uses_registered_subject_event_time(monkeypatch):
    knowledge = {
        "entities": [SimpleNamespace(metadata={
            "entity_code": "device_inspection_data",
            "entity_name": "设备检测数据",
            "business_domain_id": 217,
            "attributes": json.dumps([
                {
                    "attr_code": "detection_value",
                    "attr_name": "检测值",
                    "field_mapping": "device_daily.detection_value",
                },
                {
                    "attr_code": "detection_unit",
                    "attr_name": "检测单位",
                    "field_mapping": "device_daily.detection_unit",
                },
            ], ensure_ascii=False),
        })],
        "attributes": [],
    }
    ast = json.loads(_detail_ast("device_inspection_data"))
    ast["ambiguity"] = [{
        "type": "metric",
        "question": "指标目录为空，是否需要补充指标？",
        "candidates": [],
    }]
    contract = {
        "version": "1.0",
        "intent": "DETAIL_QUERY",
        "query_object": "设备检测数据",
        "metric_required": False,
        "required_metrics": [],
        "required_metric_codes": [],
        "required_projections": ["检测值", "检测单位"],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
        "time_policy": "REQUIRED",
        "canonical_time_range": {
            "start": "2026-07-02", "end": "2026-09-02",
        },
    }
    monkeypatch.setattr(agent, "get_table_field_by_scope", lambda *_: {
        "tables": [{
            "table_name": "device_daily",
            "semantic_model_id": 85,
            "fields": [
                {"field_name": "create_time", "data_type": "DATETIME"},
                {"field_name": "data_date", "data_type": "DATE"},
                {"field_name": "collection_time", "data_type": "DATETIME"},
            ],
        }],
    })

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract, 85, 217
    )
    result = json.loads(repaired)

    assert result["metrics"] == []
    assert result["ambiguity"] == []
    assert [item["name"] for item in result["dimensions"]] == [
        "device_daily.detection_value", "device_daily.detection_unit",
        "device_daily.collection_time",
    ]
    assert result["time_context"] == {
        "type": "custom",
        "start": "2026-07-02",
        "end": "2026-09-02",
        "value": None,
        "unit": "day",
        "anchor": "device_daily.collection_time",
    }
    assert result["sort"] == {
        "field": "device_daily.collection_time",
        "field_type": "field",
        "direction": "ASC",
    }
    assert any(
        item.get("type") == "REMOVE_IRRELEVANT_METRIC_AMBIGUITY"
        for item in repairs
    )
    assert any(
        item.get("type") == "BIND_CANONICAL_TIME_RANGE"
        and item.get("source") == "MYSQL_SEMANTIC_FIELD_REGISTRY"
        for item in repairs
    )
    assert any(
        item.get("type") == "ADD_TIME_CONTEXT_PROJECTION"
        for item in repairs
    )
    validated = _validate_asl_output(repaired, knowledge, required_metric_codes=[])
    _validate_intent_asl_contract(validated, contract, knowledge)


def test_unique_logical_dimension_wins_over_multiple_equivalent_physical_fields():
    knowledge = {
        "dimensions": [SimpleNamespace(metadata={
            "dim_code": "dealer",
            "dim_name": "经销商名称",
            "synonyms": json.dumps(["经销商", "合作经销商"], ensure_ascii=False),
        })],
        "entities": [
            _entity("dealer", "经销商", "dealer.dealer_name", "经销商名称"),
            _entity("dealer_result", "经销商画像", "dealer_result.dealer_name", "经销商名称"),
        ],
    }
    contract = {
        "intent": "DETAIL_QUERY", "query_object": "经销商",
        "metric_required": False, "required_projections": ["经销商名称"],
        "required_metric_codes": [], "filters": [], "negative_filters": [],
        "sorting": None, "time_dimension_required": False,
    }
    repaired, _ = _apply_intent_asl_contract(_detail_ast("dealer"), knowledge, contract)
    assert json.loads(repaired)["dimensions"] == [{
        "name": "dealer", "attr": None, "level": None, "granularity": None,
    }]


def test_selected_query_object_attribute_wins_over_overlapping_global_dimension():
    knowledge = {
        "dimensions": [SimpleNamespace(metadata={
            "dim_code": "department",
            "dim_name": "科室",
            "synonyms": json.dumps(["主要适用科室"], ensure_ascii=False),
        })],
        "entities": [
            _entity(
                "product",
                "商品",
                "product.main_department",
                "主要适用科室",
            ),
            _entity(
                "department",
                "科室",
                "department.dept_name",
                "科室名称",
            ),
        ],
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["metrics"] = [{"name": "order_count"}]
    ast["dimensions"] = [{
        "name": "product.main_department",
        "attr": None,
        "level": None,
        "granularity": None,
    }]
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "商品",
        "metric_required": True,
        "required_metrics": ["订单笔数"],
        "required_metric_codes": ["order_count"],
        "required_projections": [],
        "required_groupings": ["主要适用科室"],
        "semantic_entity_mentions": [],
        "filters": [],
        "negative_filters": [],
        "sorting": {"required": True, "direction": "DESC", "limit": None},
        "time_policy": "OPTIONAL",
    }

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract,
    )

    assert json.loads(repaired)["dimensions"] == ast["dimensions"]
    assert not any(
        item.get("type") in {
            "ADD_REQUIRED_GROUPING", "REMOVE_UNCONTRACTED_GROUPINGS",
        }
        for item in repairs
    )
    _validate_intent_asl_contract(repaired, contract, knowledge)


def test_detail_contract_removes_model_invented_metric_without_weakening_projection_gate():
    knowledge = {
        "entities": [_entity("dealer", "经销商", "dealer.dealer_name", "经销商名称")]
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["metrics"] = [{"name": "invented_metric"}]
    contract = {
        "intent": "DETAIL_QUERY", "query_object": "经销商",
        "metric_required": False, "required_projections": ["经销商名称"],
        "required_metric_codes": [], "filters": [], "negative_filters": [],
        "sorting": None, "time_dimension_required": False,
    }
    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract
    )
    result = json.loads(repaired)
    assert result["metrics"] == []
    assert result["dimensions"][0]["name"] == "dealer.dealer_name"
    assert [item["type"] for item in repairs] == [
        "REMOVE_UNREQUESTED_METRICS", "ADD_REQUIRED_PROJECTION",
    ]


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


def test_required_filter_is_repaired_from_recalled_semantic_metadata():
    knowledge = {
        "entities": [
            _entity("dealer", "经销商", "dealer.dealer_name", "经销商名称"),
            _entity("product", "商品", "product.product_name", "商品名称"),
        ],
    }
    contract = {
        "intent": "DETAIL_QUERY", "query_object": "经销商",
        "metric_required": False, "required_projections": ["经销商名称"],
        "required_metric_codes": [],
        "filters": [{"field": "商品名称", "operator": "EQ", "value": "A产品"}],
        "negative_filters": [], "sorting": None,
        "time_dimension_required": False,
    }

    repaired, repairs = _apply_intent_asl_contract(
        _detail_ast("dealer"), knowledge, contract,
    )
    ast = json.loads(repaired)

    assert ast["filters"] == [{
        "field": "product.product_name", "operator": "=", "value": "A产品",
    }]
    assert any(item["type"] == "ADD_REQUIRED_FILTER" for item in repairs)
    validated = _validate_asl_output(repaired, knowledge, required_metric_codes=[])
    _validate_intent_asl_contract(validated, contract, knowledge)


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


def test_required_negative_filter_preserves_exclusion_polarity():
    knowledge = {
        "entities": [
            _entity("dealer", "经销商", "dealer.dealer_name", "经销商名称"),
            _entity(
                "manufacturer", "厂家", "manufacturer.manufacturer_name", "厂家名称",
            ),
        ],
    }
    contract = {
        "intent": "DETAIL_QUERY", "query_object": "经销商",
        "metric_required": False, "required_projections": ["经销商名称"],
        "required_metric_codes": [], "filters": [],
        "negative_filters": [
            {"field": "厂家名称", "operator": "NE", "value": "B厂家"},
        ],
        "sorting": None, "time_dimension_required": False,
    }

    repaired, repairs = _apply_intent_asl_contract(
        _detail_ast("dealer"), knowledge, contract,
    )
    assert json.loads(repaired)["filters"] == [{
        "field": "manufacturer.manufacturer_name",
        "operator": "!=",
        "value": "B厂家",
    }]
    assert any(item["type"] == "ADD_REQUIRED_NEGATIVE_FILTER" for item in repairs)


def test_filter_repair_refuses_ambiguous_metadata_mapping():
    knowledge = {
        "entities": [
            _entity("dealer", "经销商", "dealer.dealer_name", "经销商名称"),
            _entity("product", "商品", "product.product_name", "商品名称"),
            _entity("order", "订单商品", "sales_order.product_name", "商品名称"),
        ],
    }
    contract = {
        "intent": "DETAIL_QUERY", "query_object": "经销商",
        "metric_required": False, "required_projections": ["经销商名称"],
        "required_metric_codes": [],
        "filters": [{"field": "商品名称", "operator": "EQ", "value": "A产品"}],
        "negative_filters": [], "sorting": None,
        "time_dimension_required": False,
    }

    with pytest.raises(ASLValidationError) as exc:
        _apply_intent_asl_contract(_detail_ast("dealer"), knowledge, contract)
    assert exc.value.code == "ASL_FILTER_INVALID"
    assert len(exc.value.details["candidates"]) == 2


def test_filter_repair_uses_fact_subject_to_disambiguate_shared_business_role():
    knowledge = {
        "entities": [
            _entity("sales_order", "销售订单", "sales_order.business_city", "业务城市"),
            _entity("hospital", "医院", "hospital.business_city", "业务城市"),
        ],
    }
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "经销商",
        "metric_required": True,
        "required_metric_codes": [],
        "required_groupings": [],
        "required_projections": [],
        "filters": [{"field": "业务城市", "operator": "EQ", "value": "上海市"}],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }

    repaired, repairs = _apply_intent_asl_contract(
        _detail_ast("sales_order"), knowledge, contract,
    )

    assert json.loads(repaired)["filters"] == [{
        "field": "sales_order.business_city", "operator": "=", "value": "上海市",
    }]
    assert any(
        item["type"] == "ADD_REQUIRED_FILTER"
        and item["resolved_field"] == "sales_order.business_city"
        for item in repairs
    )


def test_filter_contract_uses_current_source_value_to_break_name_field_tie(
    monkeypatch,
):
    knowledge = {
        "entities": [
            _entity("dealer", "经销商", "dealer.dealer_name", "名称"),
            _entity("company", "公司", "company.company_name", "名称"),
        ],
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["dimensions"] = [{
        "name": "dealer.dealer_name",
        "granularity": None,
    }]
    ast["filters"] = [{
        "field": "legacy.dealer_name",
        "operator": "=",
        "value": "杭州琅骏医疗科技有限公司",
    }]
    ast["ambiguity"] = [{
        "type": "filter",
        "question": "经销商名称=杭州琅骏医疗科技有限公司未召回到字段",
        "candidates": ["销售公司"],
    }]
    contract = {
        "intent": "DETAIL_QUERY",
        "query_object": "dealer",
        "metric_required": False,
        "required_projections": [],
        "required_metric_codes": [],
        "filters": [{
            "field": "名称",
            "operator": "EQ",
            "value": "杭州琅骏医疗科技有限公司",
        }],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }
    published = [
        {"entity_code": "dealer", "field": "dealer.dealer_name"},
        {"entity_code": "company", "field": "company.company_name"},
    ]
    monkeypatch.setattr(
        agent, "load_published_entity_attribute_candidates", lambda *_args: published,
    )
    monkeypatch.setattr(
        agent,
        "resolve_exact_entity_attribute_value_fields",
        lambda _model, _domains, candidates, _literal: [
            item["field"]
            for item in candidates
            if item["field"] == "dealer.dealer_name"
        ],
    )

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        contract,
        semantic_model_id=81,
    )

    assert json.loads(repaired)["filters"] == [{
        "field": "dealer.dealer_name",
        "operator": "=",
        "value": "杭州琅骏医疗科技有限公司",
    }]
    assert any(item["type"] == "ADD_REQUIRED_FILTER" for item in repairs)
    assert json.loads(repaired)["ambiguity"] == []
    _validate_asl_output(repaired, knowledge, required_metric_codes=[])


def test_filter_contract_prefers_unique_published_main_attribute_for_value_tie(
    monkeypatch,
):
    knowledge = {"entities": []}
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "annual_total_sales"}]
    ast["filters"] = []
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "sales_order",
        "metric_required": True,
        "required_metric_codes": ["annual_total_sales"],
        "required_projections": [],
        "required_groupings": [],
        "filters": [{"field": "地区", "operator": "EQ", "value": "四川省"}],
        "negative_filters": [],
        "sorting": None,
        "time_dimension_required": False,
    }
    published = [
        {
            "entity_code": "province",
            "field": "dim_province.province_name",
            "is_main_attribute": True,
        },
        {
            "entity_code": "all_hospital",
            "field": "all_hospital.province",
            "is_main_attribute": False,
        },
    ]
    monkeypatch.setattr(
        agent, "load_published_entity_attribute_candidates", lambda *_args: published,
    )
    monkeypatch.setattr(
        agent,
        "resolve_exact_entity_attribute_value_fields",
        lambda _model, _domains, candidates, _literal: [
            item["field"] for item in candidates
        ],
    )

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        contract,
        semantic_model_id=81,
    )

    assert json.loads(repaired)["filters"] == [{
        "field": "dim_province.province_name",
        "operator": "=",
        "value": "四川省",
    }]
    assert any(item["type"] == "ADD_REQUIRED_FILTER" for item in repairs)


def test_contract_removes_unrequested_main_identity_filter_from_asl_draft(
    monkeypatch,
):
    knowledge = {
        "entities": [
            _entity(
                "sales_order", "销售订单",
                "sales_order.business_province", "业务省份",
            ),
            _entity(
                "manufacturer", "厂家",
                "manufacturer.manufacturer_name", "厂家名称",
            ),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "annual_total_sales"}]
    ast["filters"] = [
        {
            "field": "sales_order.business_province",
            "operator": "=",
            "value": "国外",
        },
        {
            "field": "manufacturer.manufacturer_name",
            "operator": "=",
            "value": "Unrelated Example Manufacturer",
        },
    ]
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "sales_order",
        "metric_required": True,
        "required_metric_codes": ["annual_total_sales"],
        "required_projections": [],
        "required_groupings": [],
        "filters": [{
            "field": "业务省份",
            "operator": "EQ",
            "value": "国外",
        }],
        "negative_filters": [],
        "semantic_entity_mentions": [],
        "sorting": None,
        "time_dimension_required": False,
    }
    monkeypatch.setattr(
        agent,
        "load_published_entity_attribute_candidates",
        lambda *_args: [
            {
                "entity_code": "sales_order",
                "field": "sales_order.business_province",
                "is_main_attribute": False,
            },
            {
                "entity_code": "manufacturer",
                "field": "manufacturer.manufacturer_name",
                "is_main_attribute": True,
            },
        ],
    )

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        contract,
        semantic_model_id=81,
    )

    assert json.loads(repaired)["filters"] == [{
        "field": "sales_order.business_province",
        "operator": "=",
        "value": "国外",
    }]
    assert any(
        item["type"] == "REMOVE_UNCONTRACTED_IDENTITY_FILTERS"
        for item in repairs
    )


def test_contract_removes_unrequested_non_main_brand_filter():
    knowledge = {
        "entities": [
            _entity(
                "product", "商品",
                "product.product_name", "商品名称",
            ),
            _entity(
                "manufacturer", "厂家",
                "manufacturer.parent_brand", "母厂牌",
            ),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["dimensions"] = [{"name": "dealer.dealer_name"}]
    ast["filters"] = [
        {
            "field": "product.product_name",
            "operator": "=",
            "value": "空心纤维血液透析器",
        },
        {
            "field": "manufacturer.parent_brand",
            "operator": "=",
            "value": "费森尤斯",
        },
    ]
    contract = {
        "intent": "DETAIL_QUERY",
        "query_object": "经销商",
        "metric_required": False,
        "required_projections": [],
        "required_groupings": [],
        "filters": [{
            "field": "商品名称",
            "operator": "EQ",
            "value": "空心纤维血液透析器",
        }],
        "negative_filters": [],
        "forbidden_filters": [{
            "field": "母厂牌",
            "operator": "EQ",
            "value": "费森尤斯",
        }],
        "semantic_entity_mentions": [],
        "sorting": None,
        "time_dimension_required": False,
    }

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract,
    )
    result = json.loads(repaired)

    assert result["filters"] == [{
        "field": "product.product_name",
        "operator": "=",
        "value": "空心纤维血液透析器",
    }]
    assert any(
        item["type"] == "REMOVE_UNCONTRACTED_IDENTITY_FILTERS"
        for item in repairs
    )
    _validate_intent_asl_contract(repaired, contract, knowledge)


def test_distinct_entity_list_uses_time_as_filter_without_projecting_anchor():
    knowledge = {
        "entities": [
            _entity(
                "dealer", "经销商",
                "dealer.dealer_name", "经销商名称",
            ),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["dimensions"] = [{"name": "dealer.dealer_name"}]
    ast["time_context"] = {
        "type": "custom",
        "start": "2025-09-07",
        "end": "2026-09-07",
        "value": None,
        "unit": "day",
        "anchor": "sales_order.created_date",
    }
    contract = {
        "intent": "DETAIL_QUERY",
        "query_object": "经销商",
        "metric_required": False,
        "required_projections": ["经销商名称"],
        "required_groupings": [],
        "projection_mode": "DISTINCT",
        "filters": [],
        "negative_filters": [],
        "forbidden_filters": [],
        "semantic_entity_mentions": [],
        "sorting": None,
        "time_dimension_required": False,
        "time_policy": "REQUIRED",
        "canonical_time_range": {
            "start": "2025-09-07", "end": "2026-09-07",
        },
    }

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract,
    )
    result = json.loads(repaired)

    assert [item["name"] for item in result["dimensions"]] == [
        "dealer.dealer_name",
    ]
    assert result["sort"] is None
    assert result["time_context"]["anchor"] == "sales_order.created_date"
    assert not any(
        item["type"] in {"ADD_TIME_CONTEXT_PROJECTION", "ADD_TIME_CONTEXT_SORT"}
        for item in repairs
    )
    _validate_intent_asl_contract(repaired, contract, knowledge)


def test_relationship_contract_binds_transaction_fact_anchor():
    knowledge = {
        "entities": [
            _entity(
                "sales_order", "销售订单",
                "sales_order.order_key", "订单标识",
            ),
            _entity(
                "dealer", "经销商",
                "dealer.dealer_name", "经销商名称",
            ),
        ],
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["dimensions"] = [{
        "name": "dealer.dealer_name",
        "granularity": None,
    }]
    contract = {
        "intent": "DETAIL_QUERY",
        "query_object": "dealer",
        "metric_required": False,
        "required_metric_codes": [],
        "required_projections": ["经销商名称"],
        "required_groupings": [],
        "projection_mode": "DISTINCT",
        "relationship_anchor": "销售订单",
        "filters": [],
        "negative_filters": [],
        "semantic_entity_mentions": [],
        "sorting": None,
        "time_dimension_required": False,
    }

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract,
    )

    assert json.loads(repaired)["subject"] == {"entity": "sales_order"}
    assert any(item["type"] == "BIND_RELATIONSHIP_ANCHOR" for item in repairs)
    _validate_intent_asl_contract(repaired, contract, knowledge)


def test_metric_relationship_contract_uses_physical_grouping_from_fact_anchor():
    knowledge = {
        "entities": [
            _entity(
                "sales_order", "销售订单",
                "sales_order.order_key", "订单标识",
            ),
            _entity(
                "dealer", "经销商",
                "dealer.dealer_name", "经销商名称",
            ),
        ],
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["metrics"] = [{"name": "annual_total_sales"}]
    ast["dimensions"] = [{
        "name": "dealer",
        "attr": "stale-binding",
        "level": None,
        "granularity": None,
    }]
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "dealer",
        "metric_required": True,
        "required_metric_codes": ["annual_total_sales"],
        "required_projections": [],
        "required_groupings": ["经销商"],
        "relationship_anchor": "销售订单",
        "filters": [],
        "negative_filters": [],
        "semantic_entity_mentions": [],
        "sorting": {"required": True, "direction": "DESC", "limit": None},
        "time_dimension_required": False,
    }

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract,
    )
    repaired_ast = json.loads(repaired)

    assert repaired_ast["subject"] == {"entity": "sales_order"}
    assert repaired_ast["dimensions"] == [{
        "name": "dealer.dealer_name",
        "attr": None,
        "level": None,
        "granularity": None,
    }]
    assert any(item["type"] == "BIND_RELATIONSHIP_ANCHOR" for item in repairs)
    assert any(item["type"] == "ADD_REQUIRED_GROUPING" for item in repairs)
    _validate_intent_asl_contract(repaired, contract, knowledge)


def test_detail_contract_removes_default_time_and_nonrequested_projection():
    knowledge = {
        "entities": [
            _entity("dealer", "经销商", "dealer.dealer_name", "经销商名称"),
            _entity("product", "商品", "product.product_name", "商品名称"),
        ],
    }
    ast = json.loads(_detail_ast("dealer"))
    ast["dimensions"] = [
        {"name": "product.product_name", "granularity": None},
    ]
    ast["time_context"] = {
        "type": "range", "start": "2025-09-01", "end": "2026-09-01",
        "unit": "day", "anchor": "sales_order.created_date",
    }
    ast["ambiguity"] = [{"type": "time_anchor", "message": "ambiguous"}]
    contract = {
        "intent": "DETAIL_QUERY", "query_object": "经销商",
        "metric_required": False, "required_projections": ["经销商名称"],
        "required_metric_codes": [], "filters": [], "negative_filters": [],
        "sorting": None, "time_dimension_required": False,
        "time_policy": "FORBIDDEN",
    }

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract,
    )
    result = json.loads(repaired)

    assert [item["name"] for item in result["dimensions"]] == ["dealer.dealer_name"]
    assert result["time_context"] is None
    assert result["ambiguity"] == []
    assert {item["type"] for item in repairs} >= {
        "ADD_REQUIRED_PROJECTION",
        "REMOVE_UNREQUESTED_PROJECTIONS",
        "REMOVE_DEFAULT_TIME_SCOPE",
    }


def test_metric_contract_repairs_required_grouping_from_semantic_metadata():
    knowledge = {
        "entities": [
            _entity("dealer", "经销商", "dealer.dealer_name", "经销商名称"),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "annual_total_sales", "alias": "整体业务规模"}]
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "经销商",
        "metric_required": True,
        "required_metrics": ["整体业务规模"],
        "required_metric_codes": ["annual_total_sales"],
        "required_projections": [],
        "required_groupings": ["经销商"],
        "filters": [],
        "negative_filters": [],
        "sorting": {"required": True, "direction": "DESC", "limit": None},
        "time_dimension_required": False,
        "time_policy": "OPTIONAL",
    }
    ast["sort"] = {
        "field_type": "metric", "field": "annual_total_sales", "direction": "DESC",
    }

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False), knowledge, contract,
    )
    result = json.loads(repaired)

    assert [item["name"] for item in result["dimensions"]] == [
        "dealer.dealer_name",
    ]
    assert any(item["type"] == "ADD_REQUIRED_GROUPING" for item in repairs)
    _validate_intent_asl_contract(repaired, contract, knowledge)


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


def test_untyped_entity_mention_is_bound_to_unique_source_catalog_value(monkeypatch):
    knowledge = {
        "entities": [
            _entity("product", "product", "product.product_name", "product name"),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_quantity"}]
    ast["ambiguity"] = [{
        "type": "filter",
        "question": "heart lung system has multiple catalog candidates",
        "candidates": [
            "product.product_name=heart-lung system",
            "product.product_name=heart-lung system disposable kit",
        ],
    }]
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "sales order",
        "metric_required": True,
        "required_metrics": ["sales quantity"],
        "required_metric_codes": ["sales_total_quantity"],
        "required_projections": [],
        "required_groupings": [],
        "semantic_entity_mentions": ["heart lung system"],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_policy": "OPTIONAL",
    }
    monkeypatch.setattr(
        agent, "resolve_exact_entity_attribute_value_fields", lambda *_args: [],
    )
    monkeypatch.setattr(
        agent,
        "resolve_entity_attribute_catalog_matches",
        lambda *_args: [
            {
                "field": "product.product_name",
                "canonical_value": "heart-lung system",
                "match_type": "ORDERED_SUBSEQUENCE",
            },
            {
                "field": "product.product_name",
                "canonical_value": "heart-lung system disposable kit",
                "match_type": "ORDERED_SUBSEQUENCE",
            },
        ],
    )

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast), knowledge, contract, semantic_model_id=81,
        domain_scope=205,
    )

    assert json.loads(repaired)["filters"] == [{
        "field": "product.product_name",
        "operator": "=",
        "value": "heart-lung system",
    }]
    assert json.loads(repaired)["ambiguity"] == []
    assert repairs[-1]["type"] == "ADD_SOURCE_RESOLVED_ENTITY_FILTER"


def test_untyped_entity_mention_whitespace_variant_reuses_role_bound_filter(
    monkeypatch,
):
    knowledge = {
        "entities": [
            _entity("manufacturer", "厂家", "manufacturer.manufacturer_name", "厂家名称"),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_quantity"}]
    ast["filters"] = [{
        "field": "manufacturer.manufacturer_name",
        "operator": "=",
        "value": "Intuitive Surgical,Inc.直观医疗公司",
    }]
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "sales order",
        "metric_required": True,
        "required_metrics": ["sales quantity"],
        "required_metric_codes": ["sales_total_quantity"],
        "required_projections": [],
        "required_groupings": [],
        "semantic_entity_mentions": [
            "IntuitiveSurgical,Inc.直观医疗公司",
        ],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_policy": "OPTIONAL",
    }
    monkeypatch.setattr(
        agent,
        "resolve_exact_entity_attribute_value_fields",
        lambda *_args: pytest.fail("bound whitespace variant must not be resolved twice"),
    )

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        contract,
        semantic_model_id=81,
        domain_scope=205,
    )

    assert json.loads(repaired)["filters"] == ast["filters"]
    assert repairs == []


def test_untyped_entity_mention_searches_all_catalog_chunks(monkeypatch):
    knowledge = {
        "entities": [
            _entity(
                f"entity_{index:02d}",
                f"entity {index:02d}",
                f"table_{index:02d}.display_name",
                "display name",
            )
            for index in range(40)
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_quantity"}]
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "sales order",
        "metric_required": True,
        "required_metrics": ["sales quantity"],
        "required_metric_codes": ["sales_total_quantity"],
        "required_projections": [],
        "required_groupings": [],
        "semantic_entity_mentions": ["Fresenius"],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_policy": "OPTIONAL",
    }
    chunk_sizes = []
    catalog_chunk_sizes = []

    def resolve_exact(_model_id, _domain_scope, candidates, _literal):
        chunk_sizes.append(len(candidates))
        return [
            item["field"]
            for item in candidates
            if item["field"] == "table_39.display_name"
        ]

    monkeypatch.setattr(
        agent, "resolve_exact_entity_attribute_value_fields", resolve_exact,
    )

    def resolve_catalog(_model_id, _domain_scope, candidates, _literal):
        catalog_chunk_sizes.append(len(candidates))
        return [{
            "field": item["field"],
            "canonical_value": "Fresenius Medical Care",
            "match_type": "EXACT",
        } for item in candidates if item["field"] == "table_39.display_name"]

    monkeypatch.setattr(
        agent,
        "resolve_entity_attribute_catalog_matches",
        resolve_catalog,
    )

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast), knowledge, contract, semantic_model_id=81,
        domain_scope=205,
    )

    assert chunk_sizes == [32, 8]
    assert catalog_chunk_sizes == [32, 8]
    assert json.loads(repaired)["filters"] == [{
        "field": "table_39.display_name",
        "operator": "=",
        "value": "Fresenius Medical Care",
    }]
    assert repairs[-1]["type"] == "ADD_SOURCE_RESOLVED_ENTITY_FILTER"


def test_untyped_entity_mention_falls_back_to_current_published_attributes(
    monkeypatch,
):
    knowledge = {
        "entities": [
            _entity("product", "product", "product.product_name", "product name"),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_quantity"}]
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "sales order",
        "metric_required": True,
        "required_metrics": ["sales quantity"],
        "required_metric_codes": ["sales_total_quantity"],
        "required_projections": [],
        "required_groupings": [],
        "semantic_entity_mentions": ["Fresenius"],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_policy": "OPTIONAL",
    }
    monkeypatch.setattr(
        agent,
        "load_published_entity_attribute_candidates",
        lambda *_args: [{
            "entity_code": "brand",
            "business_domain_id": 205,
            "field": "brand.brand_name",
        }],
    )

    def resolve_exact(_model_id, _domain_scope, candidates, _literal):
        return [
            item["field"]
            for item in candidates
            if item["field"] == "brand.brand_name"
        ]

    monkeypatch.setattr(
        agent, "resolve_exact_entity_attribute_value_fields", resolve_exact,
    )
    monkeypatch.setattr(
        agent, "resolve_entity_attribute_catalog_matches", lambda *_args: [],
    )

    repaired, repairs = _apply_intent_asl_contract(
        json.dumps(ast), knowledge, contract, semantic_model_id=81,
        domain_scope=205,
    )

    payload = json.loads(repaired)
    assert payload["filters"] == [{
        "field": "brand.brand_name",
        "operator": "=",
        "value": "Fresenius",
    }]
    assert any(
        item["type"] == "ADD_SOURCE_RESOLVED_ENTITY_FILTER"
        for item in repairs
    )
def test_untyped_entity_mention_fails_closed_when_source_value_is_not_unique(
    monkeypatch,
):
    knowledge = {
        "entities": [
            _entity("product", "product", "product.product_name", "product name"),
        ],
    }
    ast = json.loads(_detail_ast("sales_order"))
    ast["metrics"] = [{"name": "sales_total_quantity"}]
    contract = {
        "intent": "METRIC_QUERY",
        "query_object": "sales order",
        "metric_required": True,
        "required_metrics": ["sales quantity"],
        "required_metric_codes": ["sales_total_quantity"],
        "required_projections": [],
        "required_groupings": [],
        "semantic_entity_mentions": ["device"],
        "filters": [],
        "negative_filters": [],
        "sorting": None,
        "time_policy": "OPTIONAL",
    }
    monkeypatch.setattr(
        agent, "resolve_exact_entity_attribute_value_fields", lambda *_args: [],
    )
    monkeypatch.setattr(
        agent,
        "resolve_entity_attribute_catalog_matches",
        lambda *_args: [
            {
                "field": "product.product_name",
                "canonical_value": "device A",
                "match_type": "ORDERED_SUBSEQUENCE",
            },
            {
                "field": "product.product_name",
                "canonical_value": "device B",
                "match_type": "ORDERED_SUBSEQUENCE",
            },
        ],
    )

    with pytest.raises(ASLValidationError) as exc:
        _apply_intent_asl_contract(
            json.dumps(ast), knowledge, contract, semantic_model_id=81,
            domain_scope=205,
        )

    assert exc.value.code == "ASL_ENTITY_MENTION_UNRESOLVED"
