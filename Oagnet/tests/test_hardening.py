import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api
from agent import _strip_code_fence, _validate_asl_output
from mysql_tool import _data_source_scope_clause, _row_to_relation_dict
from vector_store import SearchResult, rebuild_index_by_scope


def _result(kind: str, code_key: str, code: str) -> SearchResult:
    return SearchResult(
        id=f"{kind}:{code}",
        score=0.9,
        text=code,
        metadata={"type": kind, code_key: code},
    )


def _asl(**updates):
    value = {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "order"},
        "metrics": [{"name": "sales", "alias": "销售额"}],
        "dimensions": [{"name": "stat_date", "granularity": "month"}],
        "filters": [],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }
    value.update(updates)
    return value


def test_model_json_extractor_accepts_one_object_after_explanation():
    assert json.loads(_strip_code_fence('说明如下：\n{"version":"2.0"}\n完成')) == {
        "version": "2.0"
    }


def test_asl_validation_accepts_only_retrieved_semantic_codes():
    knowledge = {
        "metrics": [_result("metric", "metric_code", "sales")],
        "dimensions": [_result("dimension", "dim_code", "stat_date")],
        "entities": [_result("entity", "entity_code", "order")],
    }
    result = json.loads(_validate_asl_output(json.dumps(_asl()), knowledge))
    assert result["metrics"][0]["name"] == "sales"


def test_caller_bound_metric_ignores_overlapping_unselected_synonyms():
    selected = _result("metric", "metric_code", "cooperating_dealer_count")
    selected.metadata.update({
        "metric_name": "已合作经销商数",
        "synonyms": ["合作经销商数", "合作"],
    })
    unrelated = _result("metric", "metric_code", "cooperating_hospital_count")
    unrelated.metadata.update({
        "metric_name": "已合作医院数",
        "synonyms": ["合作医院数", "合作"],
    })
    knowledge = {
        "metrics": [selected, unrelated],
        "dimensions": [_result("dimension", "dim_code", "stat_date")],
        "entities": [_result("entity", "entity_code", "order")],
    }
    asl = _asl(metrics=[{
        "name": "cooperating_dealer_count",
        "alias": "已合作经销商数",
    }])
    result = json.loads(_validate_asl_output(
        json.dumps(asl, ensure_ascii=False),
        knowledge,
        "统计某商品已合作经销商数",
        ["cooperating_dealer_count"],
    ))
    assert result["metrics"] == [{
        "name": "cooperating_dealer_count",
        "alias": "已合作经销商数",
    }]


@pytest.mark.parametrize(
    "sort",
    [
        {
            "field": "dealer_cumulative_sales_amount",
            "direction": "DESC",
            "field_type": "metric",
        },
        {
            "field": "unprojected_dimension",
            "direction": "ASC",
            "field_type": "dimension",
        },
    ],
)
def test_sort_must_reference_a_selected_metric_or_projected_dimension(sort):
    knowledge = {
        "metrics": [_result("metric", "metric_code", "sales")],
        "dimensions": [
            _result("dimension", "dim_code", "stat_date"),
            _result("dimension", "dim_code", "unprojected_dimension"),
        ],
        "entities": [_result("entity", "entity_code", "order")],
    }

    with pytest.raises(ValueError, match="sort field must be a selected"):
        _validate_asl_output(json.dumps(_asl(sort=sort)), knowledge)


@pytest.mark.parametrize(
    "updates",
    [
        {"subject": {"entity": "invented_entity"}},
        {"metrics": [{"name": "invented_metric"}]},
        {"metrics": [{"name": "sales"}, {"name": "sales"}]},
        {"metrics": [{"name": "sales", "time_anchor": "orders.secret"}]},
        {"dimensions": [{"name": "invented_dimension"}]},
        {"dimensions": [{"name": "stat_date"}, {"name": "stat_date"}]},
        {"dimensions": [{"name": "private_table.secret"}]},
        {"dimensions": [{"name": "stat_date", "attr": "invented"}]},
        {"dimensions": [{"name": "stat_date", "granularity": "decade"}]},
        {"filters": [{"field": "unsafe", "operator": "=", "value": 1}]},
        {"filters": [{"field": "orders.id", "operator": "DROP", "value": 1}]},
        {"limit": 0},
    ],
)
def test_asl_validation_rejects_hallucinated_or_unsafe_output(updates):
    knowledge = {
        "metrics": [_result("metric", "metric_code", "sales")],
        "dimensions": [_result("dimension", "dim_code", "stat_date")],
        "entities": [_result("entity", "entity_code", "order")],
    }
    with pytest.raises(ValueError):
        _validate_asl_output(json.dumps(_asl(**updates)), knowledge)


def test_asl_physical_fields_must_come_from_retrieved_entity_metadata():
    entity = _result("entity", "entity_code", "order")
    entity.metadata["attributes"] = [
        {"attr_code": "id", "field_mapping": "orders.id"},
    ]
    knowledge = {
        "metrics": [_result("metric", "metric_code", "sales")],
        "dimensions": [_result("dimension", "dim_code", "stat_date")],
        "entities": [entity],
    }
    valid = _asl(filters=[{"field": "orders.id", "operator": "=", "value": 1}])
    assert json.loads(_validate_asl_output(json.dumps(valid), knowledge))["filters"]

    invalid = _asl(filters=[{"field": "orders.secret", "operator": "=", "value": 1}])
    with pytest.raises(ValueError, match="not retrieved"):
        _validate_asl_output(json.dumps(invalid), knowledge)


def test_asl_validation_accepts_metric_free_detail_projection():
    entity = _result("entity", "entity_code", "supplier")
    entity.metadata["attributes"] = [
        {"attr_code": "supplier_name", "field_mapping": "supplier_info.supplier_name"},
    ]
    knowledge = {"metrics": [], "dimensions": [], "entities": [entity]}
    detail = _asl(
        subject={"entity": "supplier"},
        metrics=[],
        dimensions=[{"name": "supplier_info.supplier_name"}],
    )

    result = json.loads(_validate_asl_output(json.dumps(detail), knowledge))

    assert result["metrics"] == []
    assert result["dimensions"] == [{"name": "supplier_info.supplier_name"}]


def test_authoritative_empty_metric_set_allows_derived_count_detail_projection():
    entity = _result("entity", "entity_code", "dealer")
    entity.metadata["attributes"] = [
        {"attr_code": "dealer_name", "field_mapping": "dealer.dealer_name"},
    ]
    metric = _result("metric", "metric_code", "cooperating_dealer_count")
    metric.metadata["metric_name"] = "已合作经销商数"
    metric.metadata["synonyms"] = ["合作经销商"]
    knowledge = {"metrics": [metric], "dimensions": [], "entities": [entity]}
    detail = _asl(
        subject={"entity": "dealer"},
        metrics=[],
        dimensions=[{"name": "dealer.dealer_name"}],
    )

    result = json.loads(_validate_asl_output(
        json.dumps(detail), knowledge, "合作经销商名单", []
    ))

    assert result["metrics"] == []


def test_asl_validation_still_rejects_empty_query_shape():
    knowledge = {"metrics": [], "dimensions": [], "entities": []}
    empty = _asl(subject={}, metrics=[], dimensions=[])

    with pytest.raises(ValueError, match="detail projection"):
        _validate_asl_output(json.dumps(empty), knowledge)


def test_scope_rebuild_failure_preserves_existing_index():
    class Store:
        persist_dir = Path("unused")

        def get_ids_by_where(self, _where):
            raise AssertionError("old index must not be touched before embedding succeeds")

        def add(self, _records):
            raise AssertionError("must not write a partial snapshot")

        def delete_by_ids(self, _ids):
            raise AssertionError("must not delete the old snapshot")

    doc = {
        "semantic_model": {"id": 6, "name": "model"},
        "business_domain": {"id": 7, "name": "sales"},
        "entities": [{
            "entity_code": "order",
            "entity_name": "订单",
            "attributes": [],
            "relations": [],
        }],
        "metrics": [],
        "dimensions": [],
    }
    with patch("mysql_tool.get_dsl_by_scope", return_value=doc):
        with pytest.raises(RuntimeError, match="embedding failed"):
            rebuild_index_by_scope(
                Store(),
                lambda _texts: (_ for _ in ()).throw(RuntimeError("embedding failed")),
                6,
                7,
            )


def test_agent_query_validation_and_sanitized_model_failure():
    client = TestClient(api.app)
    invalid = client.post(
        "/agent/query",
        json={"query": "   ", "semantic_model_id": 0, "business_domain_id": -1},
    )
    assert invalid.status_code == 422
    assert invalid.json()["success"] is False

    with patch("api.main", side_effect=ValueError("private model output")):
        failed = client.post(
            "/agent/query",
            json={"query": "查询销售额", "semantic_model_id": 6, "business_domain_id": 7},
        )
    assert failed.status_code == 502
    assert failed.json()["success"] is False
    assert "private model output" not in json.dumps(failed.json())


def test_source_config_contains_no_embedded_credentials():
    text = (Path(__file__).parents[1] / "config.py").read_text(encoding="utf-8")
    assert "sk-" not in text
    assert "youotech_" not in text


def test_relation_metadata_uses_semantic_code_and_normalized_physical_join_keys():
    value = _row_to_relation_dict({
        "code": "refund_order",
        "name": "关联",
        "target_entity_type_id": "opaque-uuid",
        "target_entity_code": "ent_order",
        "source_table_column_name": "refund_record-order_id",
        "target_table_column_name": "order_info.order_id",
    })

    assert value["target_entity"] == "ent_order"
    assert value["join_key"] == {
        "source_field": "refund_record.order_id",
        "target_field": "order_info.order_id",
    }


def test_table_metadata_scope_keeps_globally_unbound_rows_and_qualifies_alias():
    where, args = _data_source_scope_clause(
        semantic_model_id=6,
        data_source_id=2,
        alias="t",
    )

    assert "t.is_deleted = 0" in where
    assert "t.semantic_model_id = %s OR t.semantic_model_id IS NULL" in where
    assert "t.data_source_id = %s" in where
    assert args == [6, 2]
