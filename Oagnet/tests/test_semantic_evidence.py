import hashlib
import inspect
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api
import mysql_tool
from agent import _build_semantic_evidence, main
from vector_store import SearchResult


def _metric_result(
    *,
    code: str = "actual_payment_amount",
    name: str = "向量旧名称",
    domain_id: int = 7,
    formula: str = "SUM(order_info.vector_amount)",
) -> SearchResult:
    return SearchResult(
        id=f"sm6_bd{domain_id}:metric:{code}",
        score=0.93,
        text="销售额 实付金额",
        metadata={
            "type": "metric",
            "semantic_model_id": 6,
            "business_domain_id": domain_id,
            "metric_code": code,
            "metric_name": name,
            "calculation_rule": {
                "calc_formula": formula,
                "global_filters": ["order_info.status = 'PAID'"],
            },
        },
    )


def _asl(*codes: str) -> str:
    return json.dumps(
        {"metrics": [{"name": code} for code in codes]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_semantic_evidence_prefers_authoritative_sql_metric_metadata():
    validated = _asl("actual_payment_amount")
    knowledge = {"metrics": [_metric_result()]}

    def sql_loader(model_id, metric_codes, domain_ids):
        assert model_id == 6
        assert metric_codes == ["actual_payment_amount"]
        assert domain_ids == [7]
        return [{
            "id": 101,
            "semantic_model_id": 6,
            "business_domain_id": 7,
            "indicator_code": "actual_payment_amount",
            "indicator_name": "销售额",
            "calculation_formula": "  SUM(order_info.pay_amount)  ",
            "indicator_logic": None,
            "global_filters": '["order_info.order_status = 1"]',
        }]

    evidence = _build_semantic_evidence(
        validated,
        knowledge,
        semantic_model_id=6,
        requested_business_domain_ids=[7],
        sql_loader=sql_loader,
    )

    metric = evidence["selected_metrics"][0]
    assert evidence["producer"] == "OAGNET"
    assert evidence["semantic_model_id"] == 6
    assert evidence["requested_business_domain_ids"] == [7]
    assert evidence["resolved_business_domain_ids"] == [7]
    assert metric["canonical_code"] == "actual_payment_amount"
    assert metric["canonical_name"] == "销售额"
    assert metric["calculation_formula"] == "SUM(order_info.pay_amount)"
    assert metric["global_filters"] == ["order_info.order_status = 1"]
    assert metric["metadata_source"] == "MYSQL_SEMANTIC_LAYER"
    assert metric["sql_verified"] is True
    assert metric["retrieval_record_id"] == "sm6_bd7:metric:actual_payment_amount"
    expected = "sha256:" + hashlib.sha256(
        b"SUM(order_info.pay_amount)"
    ).hexdigest()
    assert metric["formula_signature"] == expected
    assert metric["metadata_fingerprint"].startswith("sha256:")
    assert evidence["asl_signature"].startswith("sha256:")
    assert evidence["evidence_fingerprint"].startswith("sha256:")


def test_semantic_evidence_marks_vector_fallback_when_sql_is_unavailable():
    evidence = _build_semantic_evidence(
        _asl("actual_payment_amount"),
        {"metrics": [_metric_result(name="实付金额")]},
        semantic_model_id=6,
        requested_business_domain_ids=[],
        sql_loader=lambda *_args: (_ for _ in ()).throw(ConnectionError("offline")),
    )

    metric = evidence["selected_metrics"][0]
    assert metric["canonical_name"] == "实付金额"
    assert metric["calculation_formula"] == "SUM(order_info.vector_amount)"
    assert metric["metadata_source"] == "VECTOR_INDEX_FALLBACK"
    assert metric["sql_verified"] is False
    assert evidence["resolved_business_domain_ids"] == [7]


def test_semantic_evidence_does_not_hide_non_operational_sql_errors():
    with pytest.raises(ValueError, match="broken semantic query"):
        _build_semantic_evidence(
            _asl("actual_payment_amount"),
            {"metrics": [_metric_result()]},
            semantic_model_id=6,
            requested_business_domain_ids=[],
            sql_loader=lambda *_args: (_ for _ in ()).throw(
                ValueError("broken semantic query")
            ),
        )


def test_semantic_evidence_rejects_stale_vector_metric_when_sql_is_reachable():
    with pytest.raises(ValueError, match="missing from current SQL semantic metadata"):
        _build_semantic_evidence(
            _asl("actual_payment_amount"),
            {"metrics": [_metric_result()]},
            semantic_model_id=6,
            requested_business_domain_ids=[],
            sql_loader=lambda *_args: [],
        )


def test_semantic_evidence_uses_vector_domain_to_disambiguate_duplicate_codes():
    rows = [
        {
            "id": 1,
            "business_domain_id": 7,
            "indicator_code": "shared_metric",
            "indicator_name": "销售域指标",
            "calculation_formula": "SUM(sales.amount)",
        },
        {
            "id": 2,
            "business_domain_id": 10,
            "indicator_code": "shared_metric",
            "indicator_name": "售后域指标",
            "calculation_formula": "SUM(refund.amount)",
        },
    ]
    result = _metric_result(
        code="shared_metric",
        name="向量指标",
        domain_id=10,
        formula="SUM(refund.old_amount)",
    )
    evidence = _build_semantic_evidence(
        _asl("shared_metric"),
        {"metrics": [result]},
        6,
        [],
        sql_loader=lambda *_args: rows,
    )

    metric = evidence["selected_metrics"][0]
    assert metric["business_domain_id"] == 10
    assert metric["canonical_name"] == "售后域指标"
    assert evidence["resolved_business_domain_ids"] == [10]


def test_semantic_evidence_rejects_metric_outside_explicit_domain_scope():
    with pytest.raises(ValueError, match="outside requested business domains"):
        _build_semantic_evidence(
            _asl("actual_payment_amount"),
            {"metrics": [_metric_result(domain_id=10)]},
            6,
            [7],
            sql_loader=lambda *_args: [{
                "id": 1,
                "business_domain_id": 10,
                "indicator_code": "actual_payment_amount",
                "indicator_name": "销售额",
                "calculation_formula": "SUM(order_info.pay_amount)",
            }],
        )


def test_semantic_evidence_for_clarification_does_not_invent_auto_domains():
    called = False

    def sql_loader(*_args):
        nonlocal called
        called = True
        return []

    evidence = _build_semantic_evidence(
        _asl(),
        {"metrics": []},
        6,
        [],
        sql_loader=sql_loader,
    )
    assert called is False
    assert evidence["selected_metrics"] == []
    assert evidence["resolved_business_domain_ids"] == []


def test_metric_evidence_sql_is_parameterized_and_keeps_explicit_domain_scope():
    with patch.object(mysql_tool, "_query", return_value=[]) as query:
        rows = mysql_tool.get_metric_evidence(
            6,
            ["actual_payment_amount", "refund_amount"],
            [7, 10, 7],
        )

    assert rows == []
    sql, args = query.call_args.args
    assert "indicator_code IN (%s, %s)" in sql
    assert "business_domain_id IN (%s, %s)" in sql
    assert "actual_payment_amount" not in sql
    assert args == (6, "actual_payment_amount", "refund_amount", 7, 10)


def test_query_response_openapi_requires_oagnet_semantic_evidence():
    schema = api.app.openapi()["components"]["schemas"]["QueryResponse"]
    assert "semantic_evidence" in schema["properties"]
    assert "semantic_evidence" in schema["required"]
    evidence_schema = api.app.openapi()["components"]["schemas"]["SemanticQueryEvidence"]
    assert "selected_metrics" in evidence_schema["properties"]
    assert "resolved_business_domain_ids" in evidence_schema["properties"]


def test_agent_query_returns_metric_evidence_without_changing_result_field():
    signature = "sha256:" + ("a" * 64)
    bundle = {
        "result": '{"version":"2.0"}',
        "semantic_evidence": {
            "evidence_version": "1.0",
            "producer": "OAGNET",
            "semantic_model_id": 6,
            "requested_business_domain_ids": [7],
            "resolved_business_domain_ids": [7],
            "selected_metrics": [{
                "canonical_code": "actual_payment_amount",
                "canonical_name": "销售额",
                "semantic_model_id": 6,
                "business_domain_id": 7,
                "calculation_formula": "SUM(order_info.pay_amount)",
                "formula_source": "calculation_formula",
                "formula_signature": signature,
                "global_filters": [],
                "metadata_fingerprint": signature,
                "metadata_source": "MYSQL_SEMANTIC_LAYER",
                "sql_verified": True,
                "retrieval_record_id": "sm6_bd7:metric:actual_payment_amount",
                "retrieval_score": 0.91,
            }],
            "asl_signature": signature,
            "evidence_fingerprint": signature,
        },
    }
    with patch("api.main", return_value=bundle) as generated:
        response = TestClient(api.app).post(
            "/agent/query",
            json={
                "query": "查询销售额",
                "semantic_model_id": 6,
                "business_domain_id": 7,
            },
        )

    assert response.status_code == 200
    assert response.json()["result"] == bundle["result"]
    assert response.json()["semantic_evidence"]["selected_metrics"][0][
        "canonical_code"
    ] == "actual_payment_amount"
    assert generated.call_args.kwargs["include_evidence"] is True


def test_agent_main_keeps_legacy_string_mode_by_default():
    assert inspect.signature(main).parameters["include_evidence"].default is False
