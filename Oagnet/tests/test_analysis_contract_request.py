import json

import pytest
from pydantic import ValidationError

import agent
from api import QueryRequest
from vector_store import SearchResult


CONTRACT = {
    "operator": "price_volume_decomposition",
    "required_columns": {
        "period_role": ["period_role", "期间角色"],
        "price": ["price", "单价"],
        "quantity": ["quantity", "销量"],
    },
    "minimum_rows": 2,
    "maximum_rows": 2,
}

EXPLORATION = {
    "minimum_numeric_metrics": 1, "maximum_numeric_metrics": 3,
    "minimum_dimensions": 1, "maximum_dimensions": 3,
    "prefer_time_dimension": True,
    "result_grain": "AGGREGATED_ANALYSIS_READY",
}


def _metric(code: str, name: str, synonyms: list[str] | None = None) -> SearchResult:
    return SearchResult(
        id=f"metric:{code}",
        score=0.5,
        text=name,
        metadata={
            "metric_code": code,
            "metric_name": name,
            "synonyms": synonyms or [],
            "type": "metric",
        },
    )


def test_entity_value_vector_records_authorize_their_governed_source_field():
    hit = SearchResult(
        id="value:brand",
        score=0.9,
        text="brand",
        metadata={
            "type": "entity_attribute_value",
            "semantic_model_id": 81,
            "business_domain_id": 205,
            "source_field": "products.brand_name",
        },
    )
    knowledge = {
        "entity_attribute_values": [],
        "_ambiguity_candidates": {"entity_attribute_value": [hit]},
    }

    assert "products.brand_name" in agent._known_physical_fields(knowledge)


def test_missing_asl_filter_field_gets_second_pass_exact_vector_proof():
    hit = SearchResult(
        id="value:brand",
        score=0.0,
        text="brand",
        metadata={
            "type": "entity_attribute_value",
            "semantic_model_id": 81,
            "business_domain_id": 205,
            "source_field": "products.brand_name",
        },
    )

    class Store:
        def find_exact(self, where):
            assert {"source_field": "products.brand_name"} in where["$and"]
            return [hit]

    content = json.dumps({
        "metrics": [],
        "dimensions": [],
        "filters": [{
            "field": "products.brand_name",
            "operator": "=",
            "value": "Acme",
        }],
        "time_context": None,
        "sort": None,
        "having": [],
    })
    knowledge = {"_vector_authorized_fields": []}

    agent._verify_missing_asl_fields_in_vector_store(
        content, knowledge, Store(), 81, [205]
    )

    assert knowledge["_vector_authorized_fields"] == ["products.brand_name"]
    agent._validate_vector_grounded_asl(content, knowledge)


def test_second_pass_vector_proof_does_not_accept_a_different_scope():
    hit = SearchResult(
        id="value:brand",
        score=0.0,
        text="brand",
        metadata={
            "type": "entity_attribute_value",
            "semantic_model_id": 81,
            "business_domain_id": 999,
            "source_field": "products.brand_name",
        },
    )

    class Store:
        def find_exact(self, _where):
            return [hit]

    content = json.dumps({
        "metrics": [],
        "dimensions": [],
        "filters": [{"field": "products.brand_name", "operator": "=", "value": "Acme"}],
        "time_context": None,
        "sort": None,
        "having": [],
    })
    knowledge = {"_vector_authorized_fields": []}

    agent._verify_missing_asl_fields_in_vector_store(
        content, knowledge, Store(), 81, [205]
    )

    assert knowledge["_vector_authorized_fields"] == []
    with pytest.raises(ValueError, match="filter field"):
        agent._validate_vector_grounded_asl(content, knowledge)


def test_query_request_accepts_matching_analysis_contract():
    request = QueryRequest(
        query="分析量价因素", semantic_model_id=6,
        analysis_operator="price_volume_decomposition",
        result_contract=CONTRACT,
    )
    assert request.result_contract["operator"] == request.analysis_operator


def test_exploration_maps_unknown_code_only_by_exact_unique_metric_name():
    content = json.dumps({
        "version": "2.0",
        "metrics": [{"name": "model_quantity", "alias": "销售总数量"}],
        "dimensions": [{"name": "statistical_date", "granularity": "month"}],
        "sort": {
            "field_type": "metric", "field": "model_quantity",
            "direction": "desc",
        },
        "ambiguity": [{"type": "metric", "question": "确认指标", "candidates": []}],
    }, ensure_ascii=False)

    normalized = agent._normalize_exploration_metrics(
        content,
        {"metrics": [_metric("quantity_metric", "销售总数量")]},
        EXPLORATION,
    )
    ast = json.loads(normalized)

    assert ast["metrics"] == [{
        "name": "quantity_metric",
        "alias": "销售总数量",
    }]
    assert ast["sort"]["field"] == "quantity_metric"
    assert ast["ambiguity"] == []


def test_exploration_preserves_known_metric_and_bounds_metric_count():
    content = json.dumps({
        "metrics": [
            {"name": "annual_total_sales", "alias": "销售额"},
            {"name": "unknown_metric", "alias": "未知"},
        ],
        "ambiguity": [],
    }, ensure_ascii=False)

    normalized = agent._normalize_exploration_metrics(
        content,
        {
            "metrics": [
                _metric("annual_total_sales", "含税销售总额"),
                _metric("order_count", "订单笔数"),
            ]
        },
        {**EXPLORATION, "minimum_numeric_metrics": 2, "maximum_numeric_metrics": 2},
    )
    ast = json.loads(normalized)

    assert [item["name"] for item in ast["metrics"]] == ["annual_total_sales"]
    assert ast["ambiguity"][-1]["reason_code"] == (
        "UNRESOLVED_EXPLORATION_METRIC"
    )
    assert "unknown_metric" in ast["ambiguity"][-1]["question"]


def test_exploration_does_not_hide_unknown_metric_when_minimum_is_already_met():
    content = json.dumps({
        "metrics": [
            {"name": "order_count", "alias": "订单数"},
            {"name": "sales_total_quantity", "alias": "销售总数量"},
        ],
        "ambiguity": [],
    }, ensure_ascii=False)

    normalized = agent._normalize_exploration_metrics(
        content,
        {
            "metrics": [
                _metric("order_count", "订单数"),
                _metric("cooperating_hospital_count", "合作医院数"),
            ]
        },
        EXPLORATION,
    )
    ast = json.loads(normalized)

    assert [item["name"] for item in ast["metrics"]] == ["order_count"]
    assert ast["ambiguity"][-1]["reason_code"] == (
        "UNRESOLVED_EXPLORATION_METRIC"
    )
    assert "sales_total_quantity" in ast["ambiguity"][-1]["question"]


def test_exploration_keeps_known_sales_quantity_even_if_recalled_last():
    content = json.dumps({
        "metrics": [{"name": "sales_total_quantity", "alias": "销售总数量"}],
        "ambiguity": [],
    }, ensure_ascii=False)

    normalized = agent._normalize_exploration_metrics(
        content,
        {
            "metrics": [
                _metric("cooperating_hospital_count", "合作医院数"),
                _metric("sales_total_quantity", "销售总数量", ["销量"]),
            ]
        },
        EXPLORATION,
    )
    ast = json.loads(normalized)

    assert [item["name"] for item in ast["metrics"]] == [
        "sales_total_quantity"
    ]
    assert ast["ambiguity"] == []


def test_exploration_shared_synonym_requires_clarification():
    content = json.dumps({
        "metrics": [{"name": "model_sales", "alias": "业务量"}],
        "ambiguity": [],
    }, ensure_ascii=False)

    normalized = agent._normalize_exploration_metrics(
        content,
        {
            "metrics": [
                _metric("sales_amount", "销售额", ["业务量"]),
                _metric("sales_quantity", "销售数量", ["业务量"]),
            ]
        },
        EXPLORATION,
    )
    ast = json.loads(normalized)

    assert ast["metrics"] == []
    assert ast["ambiguity"][-1]["reason_code"] == (
        "UNRESOLVED_EXPLORATION_METRIC"
    )
    assert ast["ambiguity"][-1]["candidates"] == [
        "sales_amount（销售额）", "sales_quantity（销售数量）",
    ]


def test_query_request_accepts_multidimensional_attribution_contract():
    contract = {
        "operator": "multidimensional_attribution",
        "required_columns": {
            "dimension_name": ["dimension_name"],
            "element_value": ["element_value"],
            "baseline": ["baseline"],
            "current": ["current"],
        },
        "minimum_rows": 4,
    }
    request = QueryRequest(
        query="从各维度分析销售额下降原因",
        semantic_model_id=6,
        analysis_operator="multidimensional_attribution",
        result_contract=contract,
    )

    assert request.analysis_operator == "multidimensional_attribution"


def test_query_request_rejects_partial_or_mismatched_contract():
    with pytest.raises(ValidationError):
        QueryRequest(
            query="分析量价因素", semantic_model_id=6,
            analysis_operator="price_volume_decomposition",
        )


def test_query_request_validates_bounded_exploration_requirements():
    request = QueryRequest(
        query="全面分析销售数据", semantic_model_id=6,
        exploration_requirements=EXPLORATION,
    )
    assert request.exploration_requirements["maximum_numeric_metrics"] == 3
    with pytest.raises(ValidationError):
        QueryRequest(
            query="全面分析销售数据", semantic_model_id=6,
            exploration_requirements={**EXPLORATION, "maximum_numeric_metrics": 20},
        )
    with pytest.raises(ValidationError):
        QueryRequest(
            query="分析量价因素", semantic_model_id=6,
            analysis_operator="price_volume_decomposition",
            result_contract={**CONTRACT, "operator": "funnel_conversion_analysis"},
        )


def test_analysis_contract_does_not_pollute_semantic_retrieval(monkeypatch):
    observed = {}

    class Builder:
        last_knowledge = {}

        def __init__(self, *_args, **_kwargs):
            pass

        def build(self, retrieval_query):
            observed["retrieval_query"] = retrieval_query
            return "system"

    class ModelAgent:
        def invoke(self, payload):
            observed["execution_query"] = payload["messages"]
            return {"messages": [type("Message", (), {"content": '{"version":"2.0"}'})()]}

    monkeypatch.setattr(agent, "PromptBuilder", Builder)
    monkeypatch.setattr(agent, "create_deep_agent", lambda **_kwargs: ModelAgent())
    monkeypatch.setattr(agent, "_normalize_semantic_references", lambda content, *_args: content)
    monkeypatch.setattr(agent, "_normalize_dynamic_subject", lambda content, *_args: content)
    monkeypatch.setattr(agent, "_validate_vector_grounded_asl", lambda *_args: None)
    monkeypatch.setattr(agent, "_validate_asl_output", lambda content, *_args: content)

    result = agent.main(
        "分析最近两个月销售额的量价因素",
        retrieval_query="最近两个月销售额",
        store=object(),
        semantic_model_id=6,
        analysis_operator="price_volume_decomposition",
        result_contract=CONTRACT,
    )

    assert observed["retrieval_query"] == "最近两个月销售额"
    assert "price_volume_decomposition" not in observed["retrieval_query"]
    assert "price_volume_decomposition" in observed["execution_query"]
    assert json.loads(result)["analysis_contract"]["producer"] == "OAGNET"


def test_intent_asl_contract_is_authoritative_in_model_prompt(monkeypatch):
    observed = {}
    contract = {
        "intent": "DETAIL_QUERY",
        "query_object": "科室",
        "metric_required": False,
        "required_projections": ["使用科室"],
        "filters": [{"field": "产品名称", "operator": "EQ", "value": "Prismaflex M60 set"}],
    }

    class Builder:
        last_knowledge = {}

        def __init__(self, *_args, **_kwargs):
            pass

        def build(self, retrieval_query):
            observed["retrieval_query"] = retrieval_query
            return "base semantic prompt"

    class ModelAgent:
        def invoke(self, payload):
            observed["execution_query"] = payload["messages"]
            return {"messages": [type("Message", (), {"content": '{"version":"2.0"}'})()]}

    def create_agent(**kwargs):
        observed["system_prompt"] = kwargs["system_prompt"]
        return ModelAgent()

    monkeypatch.setattr(agent, "PromptBuilder", Builder)
    monkeypatch.setattr(agent, "create_deep_agent", create_agent)
    monkeypatch.setattr(agent, "_normalize_semantic_references", lambda content, *_args: content)
    monkeypatch.setattr(agent, "_normalize_dynamic_subject", lambda content, *_args: content)
    monkeypatch.setattr(agent, "_validate_vector_grounded_asl", lambda *_args: None)
    monkeypatch.setattr(agent, "_validate_asl_output", lambda content, *_args: content)
    monkeypatch.setattr(agent, "_apply_intent_asl_contract", lambda content, *_args: (content, []))
    monkeypatch.setattr(agent, "_validate_intent_asl_contract", lambda *_args, **_kwargs: None)

    agent.main(
        "请提供百特Prismaflex M60 set使用科室。",
        retrieval_query="请提供百特Prismaflex M60 set使用科室。",
        store=object(),
        semantic_model_id=81,
        intent_asl_contract=contract,
    )

    assert observed["retrieval_query"] == "请提供百特Prismaflex M60 set使用科室。"
    assert "Caller-owned Intent-ASL contract" in observed["system_prompt"]
    assert "Do not change its intent, query_object" in observed["system_prompt"]
    assert "intent_asl_contract=" in observed["execution_query"]


def test_execution_constraints_do_not_pollute_semantic_date_validation(monkeypatch):
    observed = {}

    class Builder:
        last_knowledge = {}

        def __init__(self, *_args, **_kwargs):
            pass

        def build(self, retrieval_query):
            observed["retrieval_query"] = retrieval_query
            return "system"

    class ModelAgent:
        def invoke(self, _payload):
            return {"messages": [type("Message", (), {
                "content": '{"version":"2.0"}',
            })()]}

    def normalize(content, _knowledge, user_query, *_args, **_kwargs):
        observed["normalization_query"] = user_query
        return content

    def validate(content, _knowledge, user_query):
        observed["validation_query"] = user_query
        return content

    monkeypatch.setattr(agent, "PromptBuilder", Builder)
    monkeypatch.setattr(agent, "create_deep_agent", lambda **_kwargs: ModelAgent())
    monkeypatch.setattr(agent, "_normalize_semantic_references", normalize)
    monkeypatch.setattr(agent, "_normalize_dynamic_subject", lambda value, *_args: value)
    monkeypatch.setattr(agent, "_validate_vector_grounded_asl", lambda *_args: None)
    monkeypatch.setattr(agent, "_validate_asl_output", validate)

    agent.main(
        "查询累计销售额\n执行要求：不得擅自添加最近一年、本年时间过滤。",
        retrieval_query="查询累计销售额",
        store=object(),
        semantic_model_id=81,
    )

    assert observed == {
        "retrieval_query": "查询累计销售额",
        "normalization_query": "查询累计销售额",
        "validation_query": "查询累计销售额",
    }


def test_exploration_requirements_are_enforced_after_clean_retrieval(monkeypatch):
    observed = {}

    class Builder:
        def __init__(self, *_args, **_kwargs):
            self.last_knowledge = {
                "metrics": [_metric("sales", "销售额")],
                "dimensions": [],
            }
        def build(self, retrieval_query):
            observed["retrieval_query"] = retrieval_query
            return "system"

    content = json.dumps({
        "version": "2.0", "metrics": [{"name": "sales"}],
        "dimensions": [{"name": "month", "granularity": "month"}],
        "time_context": {"type": "range"}, "ambiguity": [],
    })
    model_agent = type("ModelAgent", (), {"invoke": lambda _self, _payload: {
        "messages": [type("Message", (), {"content": content})()]
    }})()
    monkeypatch.setattr(agent, "PromptBuilder", Builder)
    monkeypatch.setattr(agent, "create_deep_agent", lambda **_kwargs: model_agent)
    monkeypatch.setattr(agent, "_normalize_semantic_references", lambda value, *_args: value)
    monkeypatch.setattr(agent, "_normalize_dynamic_subject", lambda value, *_args: value)
    monkeypatch.setattr(agent, "_validate_vector_grounded_asl", lambda *_args: None)
    monkeypatch.setattr(agent, "_validate_asl_output", lambda value, *_args: value)

    result = agent.main(
        "全面分析销售数据", store=object(), semantic_model_id=6,
        exploration_requirements=EXPLORATION,
    )
    parsed = json.loads(result)
    assert observed["retrieval_query"] == "全面分析销售数据"
    assert parsed["analysis_exploration"]["requirements"] == EXPLORATION


def test_structured_reference_is_vector_normalized_and_unmatched_items_are_removed():
    knowledge = {
        "metrics": [_metric("sales", "销售额", ["含税销售额"])],
        "entities": [SearchResult(
            id="entity:dealer", score=0.9, text="经销商",
            metadata={
                "type": "entity", "entity_code": "dealer", "entity_name": "经销商",
                "attributes": [{
                    "attr_code": "dealer_name", "attr_name": "经销商名称",
                    "field_mapping": {"mappingTable": "dealer", "mappingColumn": "dealer_name"},
                    "is_main_attribute": True,
                }],
            },
        )],
        "attributes": [], "dimensions": [], "relations": [],
    }
    reference = {
        "primary_intent": "DETAIL_QUERY",
        "entity": "经销商",
        "metrics": [
            {"input": "含税销售额", "canonical_name": None, "metric_id": None},
            {"input": "不存在指标", "canonical_name": None, "metric_id": None},
        ],
        "fields": ["经销商名称", "不存在字段"],
        "dimensions": [],
        "filters": [
            {"field": "经销商名称", "operator": "EQ", "value": "甲公司"},
            {"field": "不存在字段", "operator": "EQ", "value": "噪声"},
        ],
        "operators": [], "time_range": None,
    }

    normalized, dropped = agent._normalize_structured_reference(reference, knowledge)

    assert normalized["entity"] == "dealer"
    assert normalized["metrics"] == ["sales"]
    assert normalized["fields"] == ["dealer.dealer_name"]
    assert normalized["filters"] == [{
        "field": "dealer.dealer_name", "operator": "EQ", "value": "甲公司",
    }]
    assert {item["value"] for item in dropped} == {"不存在指标", "不存在字段"}


def test_vector_grounding_rejects_field_authorized_only_by_non_vector_fallback():
    asl = json.dumps({
        "version": "2.0", "intent": "query", "subject": {}, "metrics": [],
        "dimensions": [{"name": "published.only_field"}], "filters": [],
        "time_context": None, "sort": None, "limit": None, "having": [],
        "ambiguity": [],
    }, ensure_ascii=False)
    knowledge = {
        "metrics": [], "dimensions": [], "entities": [],
        "_published_authorized_fields": ["published.only_field"],
        "_vector_authorized_fields": [],
    }

    with pytest.raises(ValueError, match="vector semantic scope"):
        agent._validate_vector_grounded_asl(asl, knowledge)


def test_exploration_normalizes_time_granularity_that_is_too_coarse():
    ast = {
        "dimensions": [{"name": "statistical_date", "granularity": "year"}],
        "time_context": {
            "type": "range", "start": "2026-02-27", "end": "2026-08-27", "unit": "year"
        },
    }

    agent._normalize_exploration_time_granularity(ast, "全面分析最近半年的销售趋势")

    assert ast["dimensions"][0]["granularity"] == "month"
    assert ast["time_context"]["unit"] == "month"


def test_exploration_preserves_explicit_year_granularity():
    ast = {
        "dimensions": [{"name": "statistical_date", "granularity": "year"}],
        "time_context": {
            "type": "range", "start": "2026-02-27", "end": "2026-08-27", "unit": "year"
        },
    }

    agent._normalize_exploration_time_granularity(ast, "按年分析最近半年的销售趋势")

    assert ast["dimensions"][0]["granularity"] == "year"
