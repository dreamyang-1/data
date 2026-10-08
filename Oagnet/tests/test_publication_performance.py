"""Publication speed and correctness contracts; all sources/stores are offline."""
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import api
import mysql_tool as mysql
import physical_catalog_sync as physical
import publication_vectors as reuse
import vector_store as vectors
from test_physical_catalog_sync import Store, loaders, sources
from test_vectorization_switch import definition


class InventoryStore(Store):
    embedding_dim = 2

    def __init__(self, records=()):
        super().__init__(records)
        self.inventories = []
        self.written = []

    def get_catalog_inventory(self, where):
        self.inventories.append(deepcopy(where))
        return super().get_catalog_inventory(where)

    def add(self, records):
        self.written.extend(r.id for r in records)
        super().add(records)


class Embed:
    def __init__(self):
        self.texts = []
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        self.texts.extend(texts)
        return [[1., .5] for _ in texts]


def record(rid="a", **metadata):
    return vectors.VectorRecord(rid, "same name", [], {
        "type": "attribute", "semantic_model_id": 900, "business_domain_id": 901,
        "parent": "dealer", "attr_code": "name", "field_mapping": "dealer.name", **metadata})


def seed(store, records):
    embed = Embed()
    optimizer = reuse.PublicationVectors(store)
    optimizer.assign(records, embed)
    optimizer.write(records)
    if hasattr(store, "written"):
        store.written.clear()
    return embed


def test_unchanged_content_reuses_vectors_and_skips_writes():
    store = InventoryStore()
    seed(store, [record()])
    embed = Embed()
    optimizer = reuse.PublicationVectors(store)
    current = optimizer.assign([record()], embed)
    optimizer.write(current)
    assert embed.texts == [] and store.written == []
    assert current[0].vector == [1., .5] and optimizer.reused == 1


@pytest.mark.parametrize("change", [
    {"semantic_model_id": 910}, {"business_domain_id": 911}, {"parent": "hospital"},
    {"attr_code": "other_name"}, {"data_source_id": 92}, {"field_mapping": "hospital.name"},
    {"entity_id": 123}, {"attribute_id": 456}, {"source_table": "other"},
    {"source_field": "other"}, {"type": "field"},
])
def test_same_name_never_reuses_across_ownership(change):
    store = InventoryStore()
    seed(store, [record()])
    embed = Embed()
    reuse.PublicationVectors(store).assign([record(**change)], embed)
    assert len(embed.texts) == 1


def test_metadata_only_change_keeps_vector_but_updates_metadata():
    store = InventoryStore()
    seed(store, [record()])
    current, embed = [record(query_output=1)], Embed()
    optimizer = reuse.PublicationVectors(store)
    optimizer.assign(current, embed)
    optimizer.write(current)
    assert not embed.texts and store.written == ["a"]
    assert store.records["a"].metadata["query_output"] == 1


@pytest.mark.parametrize("fault", ["text", "signature", "dimension", "nan", "inf", "bool", "string", "legacy"])
def test_changed_or_invalid_vectors_are_regenerated(fault):
    store = InventoryStore()
    seed(store, [record()])
    old = store.records["a"]
    if fault == "text": old.text = "different"
    elif fault == "signature": old.metadata[reuse.SIGNATURE_KEY] = "old-model"
    elif fault == "legacy": old.metadata.pop(reuse.SIGNATURE_KEY)
    else:
        old.vector = {"dimension": [1.], "nan": [float("nan"), 1.],
                      "inf": [float("inf"), 1.], "bool": [True, 1.], "string": ["1", "2"]}[fault]
    embed = Embed()
    reuse.PublicationVectors(store).assign([record()], embed)
    assert len(embed.texts) == 1


def test_provider_model_dimension_and_revision_invalidate_signature(monkeypatch):
    before = reuse._signature()
    for name, value in [("BASE_URL", "https://offline.invalid"),
                        ("EMBEDDING_MODEL", "new-model"), ("EMBEDDING_DIM", 3)]:
        with monkeypatch.context() as changes:
            changes.setattr(reuse, name, value)
            assert reuse._signature() != before
    monkeypatch.setenv("OAGNET_EMBEDDING_REVISION", "2")
    assert reuse._signature() != before


def test_large_snapshot_uses_bounded_inventories_and_reuses_all():
    store = InventoryStore()
    seed(store, [record(str(i)) for i in range(1000)])
    embed = Embed()
    optimizer = reuse.PublicationVectors(store)
    optimizer.assign([record(str(i)) for i in range(1000)], embed)
    assert embed.calls == 0 and optimizer.reused == 1000
    assert all(len(where["$and"][0]["record_id"]["$in"]) <= 256 for where in store.inventories)
    assert all(where["$and"][1] == {"semantic_model_id": {"$in": [900]}} for where in store.inventories)


def test_signature_missing_or_unknown_store_dimension_cannot_guess_reuse():
    store = Store()
    seed(store, [record()])
    embed = Embed()
    reuse.PublicationVectors(store).assign([record()], embed)
    assert len(embed.texts) == 1


def test_full_existing_publish_api_warm_and_changed_runs(monkeypatch):
    tables, attrs, doc = sources()
    loaders(monkeypatch, tables, attrs, doc)
    store, embed = InventoryStore(), Embed()
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(api, "embed_documents", embed)
    monkeypatch.setattr(api, "mysql_advisory_lock", lambda *a: nullcontext())
    monkeypatch.setattr(api, "consistent_catalog_read", nullcontext)
    client = TestClient(api.app)
    payload = {"semantic_model_id": 900, "business_domain_id": 901}
    first = client.post("/vector/rebuild", json=payload)
    assert first.status_code == 200 and len(embed.texts) == 4
    before = deepcopy(store.records)
    embed.texts.clear(); store.written.clear()
    second = client.post("/vector/rebuild", json=payload)
    assert second.json() == first.json()
    assert not embed.texts and not store.written and store.records == before
    # Same-name enum content changes both physical field and semantic attribute.
    tables["tables"][0]["fields"][0]["enum_values"].append({"code": "9", "name": "新增"})
    third = client.post("/vector/rebuild", json=payload)
    assert third.status_code == 200 and len(embed.texts) == 2
    # Parent table/entity metadata embeds the child definitions, so update their
    # metadata too, without regenerating their unchanged embedding texts.
    assert set(store.written) == {"ds90:field:sales.channel", "sm900_bd901:attr:sales.channel",
                                  "ds90:table:sales", "sm900_bd901:entity:sales"}
    assert set(third.json()) == {"success", "message", "total", "by_type"}


def test_entity_values_add_delete_and_empty_keep_complete_snapshot(monkeypatch):
    rows = [{"id": 1, "semantic_model_id": 900, "business_domain_id": 901,
             "entity_code": "dealer", "entity_name": "经销商", "attr_name": "名称",
             "attr_code": "name", "attr_value": "甲", "data_source_id": 90}]
    monkeypatch.setattr(mysql, "load_complete_entity_attribute_vector_source", lambda *_: deepcopy(rows))
    monkeypatch.setattr(mysql, "load_entity_attribute_vector_policy_audit", lambda *_: {
        "included_attributes": [], "excluded_attributes": []})
    store, embed = InventoryStore(), Embed()
    run = lambda: vectors.replace_entity_attribute_index(store, embed, semantic_model_id=900, business_domain_id=901)
    assert run()["indexed_rows"] == 1
    embed.texts.clear(); store.written.clear()
    assert run()["indexed_rows"] == 1 and not embed.texts and not store.written
    rows.append(dict(rows[0], id=2, attr_value="乙"))
    assert run()["indexed_rows"] == 2 and len(embed.texts) == 1
    embed.texts.clear()
    rows.pop(0)
    assert run()["deleted_rows"] == 1 and not embed.texts
    assert len(store.records) == 1
    rows.clear()
    assert run()["status"] == "CLEARED" and not store.records


@pytest.mark.parametrize("fault", ["embedding", "inventory"])
def test_preparation_failure_does_not_delete_old_values(monkeypatch, fault):
    store = InventoryStore([record("old")])
    rows = [{"id": 1, "semantic_model_id": 900, "business_domain_id": 901,
             "entity_name": "经销商", "attr_name": "名称", "attr_code": "name", "attr_value": "甲"}]
    monkeypatch.setattr(mysql, "load_complete_entity_attribute_vector_source", lambda *_: rows)
    monkeypatch.setattr(mysql, "load_entity_attribute_vector_policy_audit", lambda *_: {
        "included_attributes": [], "excluded_attributes": []})
    embed = Embed()
    if fault == "inventory":
        monkeypatch.setattr(store, "get_catalog_inventory", MagicMock(side_effect=RuntimeError("offline")))
    else:
        embed = lambda texts: []
    with pytest.raises(RuntimeError):
        vectors.replace_entity_attribute_index(store, embed, semantic_model_id=900, business_domain_id=901)
    assert "old" in store.records and not store.deleted and not store.written


def source_setup(monkeypatch, definitions):
    monkeypatch.setattr(mysql, "_entity_attribute_vector_definitions", lambda *_: definitions)
    connect = MagicMock(side_effect=lambda **kw: MagicMock())
    monkeypatch.setattr(mysql.pymysql, "connect", connect)
    return connect


def test_connections_and_shared_physical_reads_reused_without_merging_owners(monkeypatch):
    definitions = [definition(), definition(attr_code="spec", mapping_column="spec"),
                   definition(entity_code="hospital", entity_name="医院")]
    connect = source_setup(monkeypatch, definitions)
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [("其他",)]
    connect.side_effect = None; connect.return_value = connection
    rows = mysql.load_complete_entity_attribute_vector_source(900, 901)
    assert connect.call_count == 1 and connection.close.call_count == 1
    assert connection.cursor.return_value.__enter__.return_value.execute.call_count == 2
    assert {(r["entity_code"], r["attr_code"]) for r in rows} == {
        ("product", "product_name"), ("product", "spec"), ("hospital", "product_name")}


@pytest.mark.parametrize("difference", [{"data_source_id": 8}, {"db_name": "other"},
                                       {"username": "other"}, {"password": "other"}])
def test_different_datasources_or_credentials_never_share_connections(monkeypatch, difference):
    connect = source_setup(monkeypatch, [definition(), definition(attr_code="other", **difference)])
    mysql.load_complete_entity_attribute_vector_source(900, 901)
    assert connect.call_count == 2


def test_connection_cleanup_on_read_failure(monkeypatch):
    connect = source_setup(monkeypatch, [definition()])
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("offline")
    connect.side_effect = None; connect.return_value = connection
    with pytest.raises(RuntimeError): mysql.load_complete_entity_attribute_vector_source(900, 901)
    connection.close.assert_called_once()


def test_stage_log_has_duration_failure_and_no_exception_business_text(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(reuse, "logger", log)
    with pytest.raises(RuntimeError):
        with reuse.publication_stage("read", model=900, domain=901):
            raise RuntimeError("business value secret")
    args = log.info.call_args.args
    assert "FAILED" in args and isinstance(args[-1], int)
    assert "secret" not in str(args)


def test_chroma_inventory_returns_vectors_using_ids_not_metadata_filter():
    store = object.__new__(vectors.ChromaVectorStore)
    store._collection = MagicMock()
    store._collection.get.return_value = {"ids": ["a"], "documents": ["text"],
        "metadatas": [{"semantic_model_id": 900}], "embeddings": [[1., .5]]}
    inventory = store.get_catalog_inventory({"$and": [
        {"record_id": {"$in": ["a"]}}, {"semantic_model_id": {"$in": [900]}}]})
    assert inventory[0].vector == [1., .5]
    kwargs = store._collection.get.call_args.kwargs
    assert kwargs["ids"] == ["a"] and "record_id" not in str(kwargs["where"])
