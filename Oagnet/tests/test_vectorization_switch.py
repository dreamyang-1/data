"""Attribute values obey the database switch before reading or embedding."""
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

import mysql_tool


def definition(flag=1, **overrides):
    return {
        "entity_code": "product", "entity_name": "商品",
        "attr_code": "product_name", "attr_name": "商品名称",
        "mapping_table": "product", "mapping_column": "product_name",
        "data_type": "varchar", "vectorization": flag,
        "data_source_id": 7, "db_type": "mysql", "host": "offline",
        "port": 3306, "username": "offline", "db_name": "offline",
        **overrides,
    }


@pytest.mark.parametrize("flag", [0, "0", " 0 ", False, None, "", 2, -1, "false", [], {}])
@pytest.mark.parametrize("mode", [None, "vector", "hybrid"])
def test_off_switch_cannot_be_overridden(flag, mode):
    assert mysql_tool.entity_value_vectorization_decision(definition(
        flag, search_mode=mode, is_main_attribute=1, query_output=1
    )) == (False, "VECTORIZATION_DISABLED")


@pytest.mark.parametrize("flag", [1, "1", " 1 ", True])
def test_enabled_text_values_and_audit_agree(monkeypatch, flag):
    monkeypatch.setattr(mysql_tool, "_entity_attribute_vector_definitions",
                        lambda *_: [definition(flag)])
    audit = mysql_tool.load_entity_attribute_vector_policy_audit(5, 9)
    assert audit["included_attributes"][0]["vectorization"] is True
    assert not audit["excluded_attributes"]


def test_existing_exact_only_and_nontext_policies_are_preserved():
    assert mysql_tool.entity_value_vectorization_decision(definition(
        1, attr_code="product_code"
    )) == (False, "IDENTIFIER_EXACT_ONLY")
    assert mysql_tool.entity_value_vectorization_decision(definition(
        1, data_type="decimal"
    )) == (False, "NON_TEXT_ATTRIBUTE")
    assert mysql_tool.entity_value_vectorization_decision(definition(
        1, search_mode="exact"
    )) == (False, "SEARCH_MODE_EXACT")


@pytest.fixture
def source(monkeypatch):
    rows = [definition(1), definition("0", attr_code="specification",
                                    mapping_column="specification")]
    monkeypatch.setattr(mysql_tool, "_entity_attribute_vector_definitions", lambda *_: rows)
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("测试商品",)]
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(mysql_tool.pymysql, "connect", connect)
    return rows, connect, cursor


def test_disabled_columns_are_not_read_and_audit_explains_why(source):
    _, connect, cursor = source
    result = mysql_tool.load_complete_entity_attribute_vector_source(5, 9)
    connect.assert_called_once()
    assert "`product_name`" in cursor.execute.call_args.args[0]
    assert "specification" not in cursor.execute.call_args.args[0]
    assert [r["attr_code"] for r in result] == ["product_name"]
    audit = mysql_tool.load_entity_attribute_vector_policy_audit(5, 9)
    assert audit["excluded_attributes"][0]["vectorization"] is False
    assert audit["excluded_attributes"][0]["reason"] == "VECTORIZATION_DISABLED"


def test_legacy_entrypoint_uses_same_governed_source(monkeypatch, source):
    query = MagicMock(return_value=[{"id": 9}])
    monkeypatch.setattr(mysql_tool, "_query", query)
    result = mysql_tool.load_entity_attribute_vector_source(5, 9)
    assert [r["attr_code"] for r in result] == ["product_name"]
    query.assert_called_once()  # scope check only; no ungoverned staging-table read


@pytest.mark.parametrize("path", ["sync", "rebuild"])
def test_api_filters_before_embedding_and_clears_disabled_old_values(monkeypatch, source, path):
    import api
    from fastapi.testclient import TestClient

    class Store:
        persist_dir = "offline"

        def __init__(self):
            self.records = {}

        def get_ids_by_where(self, where):
            assert where == {"$and": [
                {"type": "entity_attribute_value"},
                {"semantic_model_id": 5}, {"business_domain_id": 9},
            ]}
            return list(self.records)

        def add(self, records):
            self.records.update({r.id: r for r in records})

        def delete_by_ids(self, ids):
            for key in ids:
                del self.records[key]

    @contextmanager
    def lock(*_):
        yield

    store = Store()
    embed = MagicMock(side_effect=lambda texts: [[1., 0.] for _ in texts])
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(api, "embed_documents", embed)
    monkeypatch.setattr(api, "mysql_advisory_lock", lock)
    monkeypatch.setattr(api, "_entity_sync_results", {})
    payload = {"semantic_model_id": 5, "business_domain_id": 9}
    if path == "sync":
        payload["event_id"] = "switch-on"
    client = TestClient(api.app)
    first = client.post("/vector/entity-attributes/" + path, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["indexed_rows"] == 1
    assert first.json()["excluded_attributes"][0]["vectorization"] is False
    assert all(r.metadata["attr_code"] == "product_name" for r in store.records.values())
    rows, connect, _ = source
    rows[0]["vectorization"] = "0"
    connect.reset_mock()
    embed.reset_mock()
    if path == "sync":
        payload["event_id"] = "switch-off"  # config changes require a new event
    second = client.post("/vector/entity-attributes/" + path, json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "CLEARED"
    assert second.json()["deleted_rows"] == 1
    assert not store.records
    connect.assert_not_called()
    embed.assert_not_called()
