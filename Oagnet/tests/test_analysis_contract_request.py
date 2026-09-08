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
    monkeypatch.setattr(agent, "_validate_asl_output", lambda value, *_args: value)

    result = agent.main(
        "全面分析销售数据", store=object(), semantic_model_id=6,
        exploration_requirements=EXPLORATION,
    )
    parsed = json.loads(result)
    assert observed["retrieval_query"] == "全面分析销售数据"
    assert parsed["analysis_exploration"]["requirements"] == EXPLORATION


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
