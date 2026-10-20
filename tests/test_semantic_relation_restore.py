"""Relationship candidates survive deletion/recreation without using deleted rows."""
import pytest

from app.services.semantic_recommend import SemanticRecommendService
from test_semantic_recommend import base_reader, field, FakeGateway


@pytest.mark.parametrize("key", ["customer_code", "customer_no", "customer_key", "customer_id"])
def test_recreate_entity_restores_business_key_without_saved_relation(key):
    reader = base_reader(fields={1: [field(11, key)], 2: [field(21, key)]}, relations=[])
    service = SemanticRecommendService(reader, None)
    before = service.recommend_relations(81, "et-1", "et-2")
    reader._entities[0]["id"] = "recreated"
    after = service.recommend_relations(81, "recreated", "et-2")
    assert after == before
    assert len(after) == 1
    assert after[0]["fieldPairs"][0]["sourceColumnName"] == key
    assert after[0]["cardinality"] == ""
    assert "待确认" in after[0]["semanticDescription"] or "需确认" in after[0]["semanticDescription"]
    assert not after[0]["fromExisting"]


@pytest.mark.parametrize("key", ["name", "status", "id", "amount"])
def test_common_non_business_keys_do_not_create_relationships(key):
    reader = base_reader(fields={1: [field(11, key)], 2: [field(21, key)]}, relations=[])
    assert SemanticRecommendService(reader, None).recommend_relations(81, "et-1", "et-2") == []


def test_incompatible_types_and_cross_source_are_not_join_candidates():
    reader = base_reader(fields={
        1: [field(11, "customer_code", ftype="varchar(40)")],
        2: [field(21, "customer_code", ftype="bigint")],
    }, relations=[])
    service = SemanticRecommendService(reader, None)
    assert service.recommend_relations(81, "et-1", "et-2") == []
    reader._fields[2][0]["type"] = "varchar(20)"
    assert len(service.recommend_relations(81, "et-1", "et-2")) == 1
    reader._entities[1]["data_source_id"] = 59
    reader._tables[1]["data_source_id"] = 59
    assert service.recommend_relations(81, "et-1", "et-2") == []


def test_legacy_bare_field_resolves_in_its_entity_and_reverses():
    reader = base_reader()
    reader._relations = [reader._relations[0]]
    reader._relations[0]["source_table_column_name"] = "id"
    reader._relations[0]["target_table_column_name"] = "customer_id"
    service = SemanticRecommendService(reader, None)
    existing = service.recommend_relations(81, "et-2", "et-1")[0]
    assert existing["cardinality"] == "N:1"
    pair = existing["fieldPairs"][0]
    assert (pair["sourceTableName"], pair["sourceColumnName"]) == ("sales_order", "customer_id")
    assert (pair["targetTableName"], pair["targetColumnName"]) == ("customer", "id")
    reader._relations[0]["target_table_column_name"] = "unknown_code"
    unresolved = service.recommend_relations(81, "et-2", "et-1")[0]["fieldPairs"][0]
    assert unresolved["sourceTableName"] is None
    assert unresolved["sourceColumnName"] is None


def test_business_id_takes_precedence_over_surrogate_id():
    reader = base_reader(fields={
        1: [field(11, "id"), field(12, "customer_id")],
        2: [field(21, "id"), field(22, "customer_id")],
    }, relations=[])
    candidates = SemanticRecommendService(reader, None).recommend_relations(81, "et-1", "et-2")
    assert len(candidates) == 1
    assert candidates[0]["fieldPairs"][0]["sourceColumnName"] == "customer_id"
    assert candidates[0]["cardinality"] == ""


def test_llm_cannot_replace_unknown_cardinality_with_unproved_claim():
    reader = base_reader(fields={1: [field(11, "customer_code")], 2: [field(21, "customer_code")]}, relations=[])
    gateway = FakeGateway('[{"candidate":1,"relationName":"拥有","semanticDescription":"一个客户有多个订单"}]')
    candidate = SemanticRecommendService(reader, gateway).recommend_relations(81, "et-1", "et-2")[0]
    assert candidate["cardinality"] == ""
    assert "需确认" in candidate["semanticDescription"]


def test_alternative_foreign_keys_are_separate_candidates_not_composite_join():
    reader = base_reader(relations=[])
    reader._fields[2].append(field(23, "customerbilling_id"))
    candidates = SemanticRecommendService(reader, None).recommend_relations(81, "et-1", "et-2")
    assert len(candidates) == 2
    assert all(len(c["fieldPairs"]) == 1 for c in candidates)


def test_sub_table_candidates_require_current_entity_mapping():
    reader = base_reader(fields={
        1: [field(11, "customer_code")],
        2: [field(21, "order_no")],
        3: [field(31, "customer_code")],
    }, relations=[])
    service = SemanticRecommendService(reader, None)
    assert service.recommend_relations(81, "et-1", "et-2") == []
    mappings = [{"entity_type_id": "et-2", "sub_table_name": "log_record"}]
    reader.sub_table_mappings = lambda model: mappings
    candidate = service.recommend_relations(81, "et-1", "et-2")[0]
    assert candidate["fieldPairs"][0]["targetTableName"] == "log_record"
    reader._entities[1]["id"] = "recreated"
    assert service.recommend_relations(81, "et-1", "recreated") == []
    mappings[0]["entity_type_id"] = "recreated"
    assert service.recommend_relations(81, "et-1", "recreated")[0] == candidate


def test_sub_table_reader_is_scoped_and_filters_deleted_rows(monkeypatch):
    from app.services.semantic_recommend import PymysqlMetadataReader
    reader = PymysqlMetadataReader(host="h", port=3306, user="u", password="p", database="d")
    captured = []
    monkeypatch.setattr(reader, "_fetch", lambda sql, params: captured.append((sql, params)) or [])
    assert reader.sub_table_mappings(81) == []
    sql, params = captured[0]
    assert "semantic_model_id = %s" in sql and "is_deleted = 0" in sql
    assert params == (81,)
