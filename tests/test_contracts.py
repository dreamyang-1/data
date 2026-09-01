"""契约一致性测试：保证 contracts.py 既是文档又是可执行校验。

覆盖三类不变量：
1. 严格模式：契约模型拒绝未知字段（防止服务端偷偷加字段）。
2. 必填字段：契约模型在缺关键字段时报错（防止服务端漏字段）。
3. 契约注册表与适配器端点对齐：CONTRACTS 列出的端点都有对应模型。
"""
import pytest
from pydantic import ValidationError

from app.adapters.contracts import (
    CONTRACTS,
    AuthorizeResponse,
    CompileQueryResponse,
    ErrorPayload,
    ExecuteQueryResponse,
    LineageResponse,
    MetricDefinitionResponse,
    ResolveMetricsResponse,
    VerifyMetricResponse,
)
from app.adapters.http import (
    HttpKnowledgeAdapter,
    HttpPolicyAdapter,
    HttpQueryAdapter,
    HttpSemanticAdapter,
)


# ---------------------------------------------------------------------------
# 1. 严格模式：拒绝未知字段
# ---------------------------------------------------------------------------


def _valid_authorize_payload():
    return {"allowed": True, "policy_ref": "p-1"}


def _valid_resolve_payload():
    return {
        "metrics": [
            {
                "input": "销售额",
                "metric_id": "metric.sales_amount",
                "version": "v1",
                "canonical_name": "销售额",
                "unit": "元",
            }
        ]
    }


def _valid_definition_payload():
    return {
        "metric_id": "metric.sales_amount",
        "version": "v1",
        "name": "销售额",
        "definition": "订单实付金额合计",
        "unit": "元",
    }


def _valid_lineage_payload():
    return {
        "metric_id": "metric.sales_amount",
        "version": "v1",
        "business_lineage": ["销售域"],
    }


def _valid_execute_payload():
    return {
        "columns": ["metric_id"],
        "rows": [],
        "snapshot_id": "snap-1",
        "data_as_of": "2026-08-19T10:00:00+00:00",
    }


def _valid_verify_payload():
    return {
        "metric_id": "metric.sales_amount",
        "version": "v1",
        "verified": True,
    }


def _valid_compile_payload():
    return {
        "metric_ids": ["metric.sales_amount"],
        "limit": 100,
    }


@pytest.mark.parametrize(
    "model, payload",
    [
        (AuthorizeResponse, _valid_authorize_payload()),
        (ResolveMetricsResponse, _valid_resolve_payload()),
        (MetricDefinitionResponse, _valid_definition_payload()),
        (LineageResponse, _valid_lineage_payload()),
        (ExecuteQueryResponse, _valid_execute_payload()),
        (VerifyMetricResponse, _valid_verify_payload()),
        (CompileQueryResponse, _valid_compile_payload()),
    ],
)
def test_contract_rejects_unknown_field(model, payload):
    payload["__unknown_field__"] = "should-be-rejected"
    with pytest.raises(ValidationError):
        model.model_validate(payload)


# ---------------------------------------------------------------------------
# 2. 必填字段：缺关键字段时报错
# ---------------------------------------------------------------------------


def test_authorize_requires_allowed_and_policy_ref():
    with pytest.raises(ValidationError):
        AuthorizeResponse.model_validate({"allowed": True})  # 缺 policy_ref


def test_resolve_requires_metrics_list():
    with pytest.raises(ValidationError):
        ResolveMetricsResponse.model_validate({})


def test_execute_requires_snapshot_id():
    payload = _valid_execute_payload()
    del payload["snapshot_id"]
    with pytest.raises(ValidationError):
        ExecuteQueryResponse.model_validate(payload)


def test_verify_requires_verified_flag():
    payload = _valid_verify_payload()
    del payload["verified"]
    with pytest.raises(ValidationError):
        VerifyMetricResponse.model_validate(payload)


def test_error_payload_requires_error_code():
    with pytest.raises(ValidationError):
        ErrorPayload.model_validate({"message": "oops"})


# ---------------------------------------------------------------------------
# 3. 契约注册表与适配器端点对齐
# ---------------------------------------------------------------------------


def test_contracts_registry_covers_all_adapters():
    """CONTRACTS 注册表应包含五个服务的契约。"""
    assert set(CONTRACTS.keys()) == {"semantic", "policy", "query", "knowledge", "analysis"}


def test_semantic_adapter_endpoints_match_registry():
    """HttpSemanticAdapter 实际调用的端点应在 CONTRACTS['semantic'] 中有对应模型。"""
    expected = {
        "POST /v1/metrics:resolve",
        "GET /v1/metrics/{id}/definition",
        "POST /v1/metrics/{id}/lineage",
    }
    assert set(CONTRACTS["semantic"].keys()) == expected


def test_policy_adapter_endpoints_match_registry():
    assert set(CONTRACTS["policy"].keys()) == {"POST /v1/authorize"}


def test_query_adapter_endpoints_match_registry():
    assert set(CONTRACTS["query"].keys()) == {
        "POST /v1/query:compile",
        "POST /v1/query:execute",
    }


def test_knowledge_adapter_endpoints_match_registry():
    assert set(CONTRACTS["knowledge"].keys()) == {"POST /v1/knowledge:verify"}


def test_analysis_adapter_endpoints_match_registry():
    assert set(CONTRACTS["analysis"].keys()) == {"POST /v1/analysis:run"}


# ---------------------------------------------------------------------------
# 4. 契约响应能被适配器内部模型正确转换
# ---------------------------------------------------------------------------


def test_authorize_response_maps_to_internal_tuple():
    """AuthorizeResponse 字段应能完整映射到适配器返回的 (allowed, policy_ref)。"""
    parsed = AuthorizeResponse.model_validate(_valid_authorize_payload())
    assert (parsed.allowed, parsed.policy_ref) == (True, "p-1")
