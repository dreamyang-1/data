import json

import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.analysis.contracts import (
    contract_for_request,
    explicit_object_comparison_scope,
    validate_contract,
)
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent, TrustedIdentity
from app.services.orchestrator import DataAnalysisOrchestrator


IDENTITY = TrustedIdentity(tenant_id="t", user_id="u")


def analysis_request(intent: PrimaryIntent, question: str) -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="c", application_id="app", tenant_id="t", user_id="u",
        original_question=question, primary_intent=intent,
    )


class StubClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def post(self, base_url, path, payload, **kwargs):
        self.calls.append((base_url, path, payload, kwargs))
        return next(self.responses)


def test_contract_detects_missing_ambiguous_and_non_numeric_roles():
    contract = contract_for_request(analysis_request(
        PrimaryIntent.ROOT_CAUSE_ANALYSIS, "做销售额量价分析"
    ))
    assert contract is not None
    violation = validate_contract(
        contract,
        ["period_role", "avg_price", "unit_price", "quantity"],
        [{"period_role": "BASE", "avg_price": "bad", "unit_price": 10, "quantity": 2}],
    )

    assert violation.ambiguous_roles["price"] == ["avg_price", "unit_price"]
    assert violation.row_count_error is not None


def test_funnel_contract_prefers_exact_stage_over_stage_order():
    contract = contract_for_request(analysis_request(
        PrimaryIntent.COMPOSITION_ANALYSIS, "分析访问支付漏斗"
    ))
    assert contract is not None
    violation = validate_contract(contract, ["stage_order", "stage", "count"], [
        {"stage_order": 1, "stage": "访问", "count": 10},
        {"stage_order": 2, "stage": "支付", "count": 5},
    ])
    assert violation.valid


def test_multidimensional_ratio_contract_requires_canonical_additive_inputs():
    contract = contract_for_request(analysis_request(
        PrimaryIntent.ROOT_CAUSE_ANALYSIS, "从各维度分析转化率下降原因"
    ))
    assert contract is not None
    assert contract.operator == "multidimensional_ratio_attribution"
    assert set(contract.required_columns) == {
        "dimension_name", "element_value", "baseline_numerator",
        "baseline_denominator", "current_numerator", "current_denominator",
    }
    rows = [{
        "dimension_name": "区域", "element_value": "华东",
        "baseline_numerator": 10, "baseline_denominator": 20,
        "current_numerator": 8, "current_denominator": 20,
    } for _ in range(4)]
    assert validate_contract(contract, list(contract.required_columns), rows).valid


def test_explicit_object_comparison_scope_uses_the_dimension_name_filter():
    request = analysis_request(PrimaryIntent.COMPARISON_ANALYSIS, "比较三个合作方")
    request.comparison_type = "对象间比较"
    request.dimensions = ["合作方"]
    request.filters = [
        {"field": "业务状态", "operator": "IN", "value": ["正常", "观察"]},
        {
            "field": "合作方名称",
            "operator": "IN",
            "value": ["甲", "乙", "丙"],
        },
    ]

    scope = explicit_object_comparison_scope(request)

    assert scope is not None
    assert scope.filter_field == "合作方名称"
    assert scope.requested_objects == ("甲", "乙", "丙")
    assert scope.minimum_returned_objects == 2


def test_period_comparison_does_not_create_an_object_scope_from_in_filters():
    request = analysis_request(PrimaryIntent.COMPARISON_ANALYSIS, "比较本月和上月")
    request.comparison_type = "环比"
    request.dimensions = ["月份"]
    request.filters = [{
        "field": "月份名称", "operator": "IN", "value": ["本月", "上月"]
    }]

    assert explicit_object_comparison_scope(request) is None


@pytest.mark.asyncio
async def test_http_adapter_injects_contract_and_rejects_wrong_result_shape():
    request = analysis_request(PrimaryIntent.ROOT_CAUSE_ANALYSIS, "分析销售额量价因素")
    contract = contract_for_request(request)
    assert contract is not None
    asl = {
        "version": "2.0", "metrics": [], "dimensions": [], "ambiguity": [],
        "analysis_contract": {
            "contract_version": "1.0", "analysis_operator": contract.operator,
            "result_contract": contract.model_dump(mode="json"), "producer": "OAGNET",
        },
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT period, sales FROM t"},
        {
            "success": True, "sql": "SELECT period, sales FROM t",
            "columns": ["period", "sales"],
            "data": [{"period": "base", "sales": 100}], "row_count": 1,
            "analysis_contract": {
                "contract_version": "1.0", "operator": contract.operator,
                "contract_satisfied": False, "producer": "SQL_TRANSLATOR",
                "violations": {
                    "missing_roles": ["period_role", "price", "quantity"],
                    "ambiguous_roles": {}, "invalid_numeric_roles": [],
                    "row_count_error": "expected at least 2 rows, got 1",
                },
            },
        },
    ])

    with pytest.raises(AdapterError) as captured:
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
            request, IDENTITY, semantic_model_id=6, business_domain_id=None
        )

    assert captured.value.code == "ANALYSIS_RESULT_CONTRACT_INVALID"
    assert set(captured.value.details["violations"]["missing_roles"]) == {
        "period_role", "price", "quantity"
    }
    assert client.calls[0][2]["query"] == request.original_question
    assert client.calls[0][2]["analysis_operator"] == "price_volume_decomposition"
    assert client.calls[1][2]["analysis_contract"]["operator"] == "price_volume_decomposition"
    assert client.calls[2][2]["analysis_contract"]["operator"] == "price_volume_decomposition"


@pytest.mark.asyncio
async def test_retry_feedback_is_appended_to_oagnet_query():
    request = analysis_request(PrimaryIntent.COMPOSITION_ANALYSIS, "查看转化漏斗")
    request.assumptions.append(
        'ANALYSIS_CONTRACT_RETRY:{"missing_roles":["stage_order"]}'
    )
    client = StubClient([{
        "success": False,
    }])

    with pytest.raises(AdapterError):
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
            request, IDENTITY, semantic_model_id=6, business_domain_id=None
        )

    query = client.calls[0][2]["query"]
    assert "上一次查询结果未通过契约校验" in query
    assert "stage_order" in query


def test_contract_violations_become_structured_business_requirements():
    error = AdapterError(
        "ANALYSIS_RESULT_CONTRACT_INVALID", "invalid",
        details={"violations": {
            "missing_roles": ["price"],
            "ambiguous_roles": {"quantity": ["销量", "数量"]},
            "invalid_numeric_roles": ["stage_order"],
            "row_count_error": "expected at least 2 rows, got 1",
        }},
    )

    requirements = DataAnalysisOrchestrator._analysis_contract_requirements(error)
    assert [item.code for item in requirements] == [
        "missing_analysis_column:price",
        "ambiguous_analysis_column:quantity",
        "invalid_analysis_numeric:stage_order",
        "analysis_row_count_invalid",
    ]
    assert all(item.category == "DATA" for item in requirements)


@pytest.mark.asyncio
async def test_open_report_sends_and_verifies_exploration_requirements():
    request = analysis_request(PrimaryIntent.REPORT_GENERATION, "全面分析这批经营数据")
    requirements = {
        "minimum_numeric_metrics": 1, "maximum_numeric_metrics": 3,
        "minimum_dimensions": 1, "maximum_dimensions": 3,
        "prefer_time_dimension": True,
        "result_grain": "AGGREGATED_ANALYSIS_READY",
    }
    asl = {
        "version": "2.0", "metrics": [{"name": "sales"}],
        "dimensions": [{"name": "month", "granularity": "month"}],
        "ambiguity": [],
        "analysis_exploration": {
            "contract_version": "1.0", "requirements": requirements,
            "producer": "OAGNET",
        },
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT month, sales FROM t"},
        {"success": True, "sql": "SELECT month, sales FROM t",
         "columns": ["month", "sales"],
         "data": [{"month": "2026-07-01", "sales": 10}], "row_count": 1},
    ])
    result = await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        request, IDENTITY, semantic_model_id=6, business_domain_id=None
    )
    assert client.calls[0][2]["exploration_requirements"] == requirements
    assert result.dataset.rows[0]["sales"] == 10
