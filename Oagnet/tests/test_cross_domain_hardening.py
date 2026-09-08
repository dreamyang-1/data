import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api
import mysql_tool
from agent import _metric_exact_codes, _validate_asl_output


def test_bare_relationship_verb_is_not_an_exact_metric_mention():
    knowledge = {
        "metrics": [type("Item", (), {"metadata": {
            "metric_code": "annual_total_sales",
            "metric_name": "含税销售总额",
            "synonyms": ["销售", "销售额"],
        }})()],
    }

    assert _metric_exact_codes(
        knowledge, "查询最近一年销售过费森尤斯产品的经销商名单"
    ) == set()
    assert _metric_exact_codes(knowledge, "查询最近一年销售额") == {
        "annual_total_sales"
    }
from prompt_build import PromptBuilder
from vector_store import (
    SearchResult,
    build_records_from_tables,
    rebuild_index_by_scope,
    rebuild_index_for_tables,
)


def _search_result(kind: str, *, score: float, **metadata) -> SearchResult:
    return SearchResult(
        id=f"{kind}:{metadata.get('metric_code') or metadata.get('entity_name') or 'x'}",
        score=score,
        text="candidate",
        metadata={"type": kind, **metadata},
    )


def _query_result_bundle(
    result: str = '{"version":"2.0"}',
    requested_domains: list[int] | None = None,
) -> dict:
    signature = "sha256:" + ("0" * 64)
    domains = requested_domains or []
    return {
        "result": result,
        "semantic_evidence": {
            "evidence_version": "1.0",
            "producer": "OAGNET",
            "semantic_model_id": 6,
            "requested_business_domain_ids": domains,
            "resolved_business_domain_ids": domains,
            "selected_metrics": [],
            "asl_signature": signature,
            "evidence_fingerprint": signature,
        },
    }


class CandidateStore:
    def __init__(self, by_type):
        self.by_type = by_type
        self.calls = []

    def count(self):
        return sum(len(items) for items in self.by_type.values())

    def search(self, _vector, top_k, where):
        self.calls.append((top_k, where))
        type_name = next(
            clause["type"]
            for clause in where.get("$and", [where])
            if "type" in clause
        )
        return list(self.by_type.get(type_name, []))[:top_k]


def test_semantic_model_wide_retrieval_prioritizes_exact_metric_synonym():
    store = CandidateStore({
        "metric": [
            _search_result(
                "metric",
                score=0.91,
                metric_code="total_product_amount",
                metric_name="商品总金额",
                synonyms=[],
            ),
            _search_result(
                "metric",
                score=0.42,
                metric_code="actual_payment_amount",
                metric_name="实付金额",
                synonyms=["销售额", "GMV"],
            ),
            _search_result(
                "metric",
                score=0.41,
                metric_code="actual_payment_amount",
                metric_name="实付金额",
                synonyms=["销售额", "GMV"],
            ),
        ],
    })
    builder = PromptBuilder(
        store,
        lambda _query: [0.1, 0.2],
        top_k=2,
        semantic_model_id=6,
        business_domain_id=None,
    )

    knowledge = builder.retrieve("分析销售额下降原因")

    assert knowledge["metrics"][0].metadata["metric_code"] == "actual_payment_amount"
    assert sum(
        item.metadata.get("metric_code") == "actual_payment_amount"
        for item in knowledge["metrics"]
    ) == 1
    assert all("business_domain_id" not in str(where) for _, where in store.calls)
    assert all(top_k >= 12 for top_k, _ in store.calls)


def test_explicit_multi_domain_retrieval_is_not_widened_to_whole_model():
    store = CandidateStore({})
    with pytest.raises(ValueError, match="EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED"):
        PromptBuilder(store, lambda _: pytest.fail("embedding must not run"),
                      semantic_model_id=6, business_domain_ids=[7, 9, 7])
    assert store.calls == []


def test_asl_rejects_omission_of_explicit_metric_and_unsafe_having():
    actual = _search_result(
        "metric",
        score=0.7,
        metric_code="actual_payment_amount",
        metric_name="实付金额",
        synonyms=["销售额"],
    )
    product = _search_result(
        "metric",
        score=0.9,
        metric_code="total_product_amount",
        metric_name="商品总金额",
        synonyms=[],
    )
    base = {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "ent_order"},
        "metrics": [{"name": "total_product_amount", "alias": "销售额"}],
        "dimensions": [],
        "filters": [],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }
    with pytest.raises(ValueError, match="explicitly named"):
        _validate_asl_output(
            json.dumps(base, ensure_ascii=False),
            {
                "metrics": [actual, product],
                "dimensions": [],
                "entities": [_search_result(
                    "entity", score=0.8, entity_code="ent_order", entity_name="订单"
                )],
            },
            "查询销售额",
        )

    base["metrics"] = [{"name": "actual_payment_amount", "alias": "销售额"}]
    base["having"] = ["SUM(order_info.pay_amount) > 0; DROP TABLE order_info"]
    with pytest.raises(ValueError, match="safe aggregate"):
        _validate_asl_output(
            json.dumps(base, ensure_ascii=False),
            {
                "metrics": [actual],
                "dimensions": [],
                "entities": [_search_result(
                    "entity", score=0.8, entity_code="ent_order", entity_name="订单"
                )],
            },
            "查询销售额",
        )


def test_exact_metric_detection_does_not_double_count_nested_synonyms():
    sales = _search_result(
        "metric",
        score=0.8,
        metric_code="sales_amount",
        metric_name="销售额",
        synonyms=[],
    )
    product_sales = _search_result(
        "metric",
        score=0.8,
        metric_code="product_sales_amount",
        metric_name="商品销售额",
        synonyms=[],
    )
    knowledge = {"metrics": [sales, product_sales]}

    assert _metric_exact_codes(knowledge, "查询商品销售额") == {
        "product_sales_amount"
    }
    assert _metric_exact_codes(knowledge, "对比销售额与商品销售额") == {
        "sales_amount",
        "product_sales_amount",
    }

class RebuildStore:
    persist_dir = Path("unused")

    def __init__(self):
        self.added = []
        self.deleted = []

    def get_ids_by_where(self, _where):
        return ["sm6_bd7:entity:stale"]

    def add(self, records):
        self.added = list(records)

    def delete_by_ids(self, ids):
        self.deleted = list(ids)


def _dsl(domain_id: int, entity_code: str, metric_code: str):
    return {
        "semantic_model": {"id": 6, "name": "电商模型", "code": "mall"},
        "business_domain": {"id": domain_id, "name": f"domain-{domain_id}"},
        "entities": [{
            "entity_code": entity_code,
            "entity_name": entity_code,
            "attributes": [],
            "relations": [],
        }],
        "metrics": [{"metric_code": metric_code, "metric_name": metric_code}],
        "dimensions": [{"dim_code": "statistical_date", "dim_name": "统计日期"}],
    }


def test_semantic_model_wide_rebuild_aggregates_all_business_domains():
    store = RebuildStore()
    docs = {7: _dsl(7, "ent_order", "sales"), 10: _dsl(10, "ent_refund", "refund")}
    with patch("mysql_tool.get_business_domains", return_value=[{"id": 7}, {"id": 10}]), patch(
        "mysql_tool.get_dsl_by_scope", side_effect=lambda _sm, bd: docs[bd]
    ):
        stats = rebuild_index_by_scope(
            store,
            lambda texts: [[float(index), 1.0] for index, _ in enumerate(texts)],
            semantic_model_id=6,
            business_domain_id=None,
        )

    ids = {record.id for record in store.added}
    assert "sm6_bd7:entity:ent_order" in ids
    assert "sm6_bd10:entity:ent_refund" in ids
    assert "sm6_bd7:metric:sales" in ids
    assert "sm6_bd10:metric:refund" in ids
    assert len([item for item in ids if item == "sm6:dim:statistical_date"]) == 1
    assert len(stats["by_scope"]) == 2


def test_dsl_embedding_count_mismatch_preserves_old_index():
    store = RebuildStore()
    with patch("mysql_tool.get_dsl_by_scope", return_value=_dsl(7, "ent_order", "sales")):
        with pytest.raises(RuntimeError, match="Embedding 返回数量不一致"):
            rebuild_index_by_scope(store, lambda _texts: [], 6, 7)
    assert store.added == []
    assert store.deleted == []


def _table_doc():
    return {
        "scope": {"semantic_model_id": None, "data_source_id": 2},
        "tables": [{
            "table_id": 1,
            "table_name": "order_info",
            "description": "订单表",
            "fields": [{
                "field_id": 11,
                "field_name": "pay_amount",
                "data_type": "decimal",
                "table_id": 1,
            }],
        }],
    }


def test_table_embedding_count_mismatch_preserves_old_index():
    with pytest.raises(RuntimeError, match="Embedding 返回数量不一致"):
        build_records_from_tables(_table_doc(), lambda _texts: [])

    store = RebuildStore()
    with patch("mysql_tool.load_all_table_fields", return_value=[_table_doc()]):
        with pytest.raises(RuntimeError, match="Embedding 返回数量不一致"):
            rebuild_index_for_tables(store, lambda _texts: [])
    assert store.added == []
    assert store.deleted == []


def test_entity_attribute_search_allows_semantic_model_wide_scope():
    result = _search_result(
        "entity_attribute_value",
        score=0.93,
        semantic_model_id=6,
        business_domain_id=9,
        entity_name="商品",
        attr_name="商品名称",
        attr_code="goods_name",
        attr_value="无线蓝牙耳机",
    )
    with patch("api.embed_query", return_value=[0.1]), patch.object(
        api._store, "search", return_value=[result]
    ) as search:
        response = TestClient(api.app).post(
            "/vector/entity-attributes/search",
            json={"query": "蓝牙耳机", "semantic_model_id": 6},
        )

    assert response.status_code == 200
    assert response.json()["business_domain_id"] is None
    assert response.json()["matches"][0]["business_domain_id"] == 9
    where = search.call_args.kwargs["where"]
    assert {"semantic_model_id": 6} in where["$and"]
    assert not any("business_domain_id" in item for item in where["$and"])


def test_entity_attribute_search_honors_explicit_multi_domain_scope():
    with patch("api.embed_query", return_value=[0.1]), patch.object(
        api._store, "search", return_value=[]
    ) as search:
        response = TestClient(api.app).post(
            "/vector/entity-attributes/search",
            json={
                "query": "蓝牙耳机",
                "semantic_model_id": 6,
                "business_domain_ids": [7, 9, 7],
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED"
    search.assert_not_called()


def test_entity_resolution_uses_business_domain_ownership_for_legacy_rows():
    calls = []

    def query(sql, args=None):
        calls.append((sql, args))
        return [{"code": "ent_order"}]

    with patch.object(mysql_tool, "_query", side_effect=query):
        assert mysql_tool.resolve_entity_code_by_table(8, None, "order_info") == "ent_order"

    sql, args = calls[0]
    assert "JOIN semantic_model_business_domain" in sql
    assert "b.semantic_model_id = %s" in sql
    assert args == (8, "order_info")


def test_entity_resolution_supports_explicit_multi_domain_scope():
    calls = []

    def query(sql, args=None):
        calls.append((sql, args))
        return [{"code": "ent_order"}]

    with patch.object(mysql_tool, "_query", side_effect=query):
        assert mysql_tool.resolve_entity_code_by_table(8, [7, 9], "order_info") == "ent_order"

    sql, args = calls[0]
    assert "e.business_domain_id IN (%s, %s)" in sql
    assert args == (8, "order_info", 7, 9)


def test_agent_query_passes_null_domain_for_auto_routing():
    with patch("api.main", return_value=_query_result_bundle()) as main:
        response = TestClient(api.app).post(
            "/agent/query",
            json={"query": "查询销售额", "semantic_model_id": 6},
        )
    assert response.status_code == 200
    assert main.call_args.kwargs["business_domain_id"] is None
    assert main.call_args.kwargs["business_domain_ids"] == []
    assert main.call_args.kwargs["include_evidence"] is True
    assert response.json()["business_domain_selection_mode"] == "AUTO"
    assert response.json()["semantic_evidence"]["producer"] == "OAGNET"


def test_agent_query_passes_validated_metric_binding_to_main():
    with patch("api.main", return_value=_query_result_bundle()) as main:
        response = TestClient(api.app).post(
            "/agent/query",
            json={
                "query": "查询销售额", "semantic_model_id": 6,
                "metric_ids": ["6:sales_amount"],
            },
        )
    assert response.status_code == 200
    assert main.call_args.kwargs["preferred_metric_codes"] == ["sales_amount"]


def test_metricless_intent_contract_is_authoritative_for_detail_query():
    """A governed empty metric set must not fall back to lexical metric guessing."""
    with patch("api.main", return_value=_query_result_bundle()) as main:
        response = TestClient(api.app).post(
            "/agent/query",
            json={
                "query": "查询A产品合作的经销商名单",
                "retrieval_query": "经销商 商品名称 合作关系 销售语义候选",
                "semantic_model_id": 6,
                "metric_ids": [],
                "intent_asl_contract": {
                    "version": "1.0",
                    "intent": "DETAIL_QUERY",
                    "query_object": "经销商",
                    "metric_required": False,
                    "required_metrics": [],
                    "required_metric_codes": [],
                    "required_projections": ["经销商名称"],
                    "projection_mode": "DISTINCT",
                    "filters": [
                        {"field": "商品名称", "operator": "EQ", "value": "A产品"}
                    ],
                    "negative_filters": [],
                    "sorting": None,
                    "time_dimension_required": False,
                    "time_policy": "FORBIDDEN",
                },
            },
        )

    assert response.status_code == 200
    assert main.call_args.kwargs["preferred_metric_codes"] == []
    assert main.call_args.kwargs["metric_selection_authoritative"] is True


def test_agent_query_rejects_metric_binding_from_another_model():
    response = TestClient(api.app).post(
        "/agent/query",
        json={
            "query": "查询销售额", "semantic_model_id": 6,
            "metric_ids": ["9:sales_amount"],
        },
    )
    assert response.status_code == 422


def test_agent_query_supports_legacy_single_and_explicit_multi_domain_scope():
    client = TestClient(api.app)
    with patch(
        "api.main",
        side_effect=lambda *_args, **kwargs: _query_result_bundle(
            requested_domains=kwargs["business_domain_ids"]
        ),
    ) as main:
        legacy = client.post(
            "/agent/query",
            json={
                "query": "查询销售额",
                "semantic_model_id": 6,
                "business_domain_id": 7,
            },
        )
        multi = client.post(
            "/agent/query",
            json={
                "query": "分析销售与退款",
                "semantic_model_id": 6,
                "business_domain_ids": [7, 10, 7],
            },
        )

    assert legacy.status_code == 200
    assert legacy.json()["business_domain_ids"] == [7]
    assert legacy.json()["business_domain_selection_mode"] == "EXPLICIT"
    assert multi.status_code == 422
    assert multi.json()["code"] == "EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED"
    assert main.call_count == 1
    assert main.call_args.kwargs["business_domain_ids"] == [7]


def test_agent_query_rejects_conflicting_domain_contracts():
    response = TestClient(api.app).post(
        "/agent/query",
        json={
            "query": "查询销售额",
            "semantic_model_id": 6,
            "business_domain_id": 7,
            "business_domain_ids": [7, 10],
        },
    )

    assert response.status_code == 422
    assert response.json()["success"] is False


def test_vector_rebuild_uses_model_level_lock_for_all_domain_forms():
    lock_names = []

    @contextmanager
    def lock(name):
        lock_names.append(name)
        yield

    stats = {"total": 0, "by_type": {}}
    with patch("api.mysql_advisory_lock", side_effect=lock), patch(
        "api.rebuild_index_by_scope", return_value=stats
    ):
        client = TestClient(api.app)
        scoped = client.post(
            "/vector/rebuild",
            json={"semantic_model_id": 6, "business_domain_id": 7},
        )
        whole = client.post(
            "/vector/rebuild",
            json={"semantic_model_id": 6},
        )

    assert scoped.status_code == 200
    assert whole.status_code == 200
    assert lock_names == ["oagnet:dsl:6", "oagnet:dsl:6"]
