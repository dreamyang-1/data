from __future__ import annotations

import pytest

from mysql_tool import _should_vectorize_entity_value
from vector_store import (
    MilvusVectorStore,
    VectorRecord,
    build_table_field_scope_filter,
)


class FakeMilvusClient:
    def __init__(self):
        self.upserts: list[tuple[str, list[dict]]] = []

    def upsert(self, *, collection_name, data, timeout):
        self.upserts.append((collection_name, data))


class FakeQueryIterator:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.closed = False

    def next(self):
        return next(self.batches, [])

    def close(self):
        self.closed = True


class FakePagedMilvusClient(FakeMilvusClient):
    def __init__(self):
        super().__init__()
        self.iterator = FakeQueryIterator([
            [{"record_id": "entity-attr-value:1"}],
            [{"record_id": "entity-attr-value:2"}],
            [],
        ])
        self.deletes = []

    def query_iterator(self, **kwargs):
        self.query_kwargs = kwargs
        return self.iterator

    def delete(self, *, collection_name, ids, timeout):
        self.deletes.append((collection_name, list(ids)))


def bare_store() -> MilvusVectorStore:
    store = object.__new__(MilvusVectorStore)
    store.timeout = 10
    store._client = FakeMilvusClient()
    store._collections = {
        "semantic": "semantic",
        "entity_value": "values",
        "physical": "physical",
        "daily": "daily",
    }
    return store


def record(record_id: str, kind: str) -> VectorRecord:
    return VectorRecord(
        id=record_id,
        text=record_id,
        vector=[0.1, 0.2],
        metadata={"type": kind, "semantic_model_id": 81, "business_domain_id": 205},
    )


def test_add_routes_knowledge_families_to_isolated_collections():
    store = bare_store()
    store.add([
        record("entity", "entity"),
        record("value", "entity_attribute_value"),
        record("field", "field"),
        record("daily", "daily_table_row"),
    ])
    assert {name for name, _ in store._client.upserts} == {
        "semantic", "values", "physical", "daily",
    }


def test_filter_compiler_supports_scoped_boolean_filters():
    expression = MilvusVectorStore._compile_filter({
        "$and": [
            {"type": "metric"},
            {"semantic_model_id": 81},
            {"business_domain_id": {"$in": [205, -1]}},
        ]
    })
    assert 'type == "metric"' in expression
    assert "semantic_model_id == 81" in expression
    assert "business_domain_id in [205,-1]" in expression


def test_filter_compiler_rejects_unregistered_fields():
    with pytest.raises(ValueError, match="unsupported Milvus filter field"):
        MilvusVectorStore._compile_filter({"password": "secret"})


def test_physical_scope_filter_is_type_isolated():
    where = build_table_field_scope_filter(58, 81)
    assert where["$and"][0] == {"type": {"$in": ["field", "table"]}}


@pytest.mark.parametrize(
    ("attr_code", "attr_name", "main", "vectorization", "data_type", "expected"),
    [
        ("city_name", "城市名称", False, 1, "varchar", True),
        ("dealer_name", "经销商名称", True, 0, "varchar", False),
        ("dealer_name", "经销商名称", True, 1, "varchar", True),
        ("specification", "规格型号", False, 1, "varchar", True),
        ("new_searchable_text", "新文本属性", False, 1, "text", True),
        ("unpublished_text", "未发布属性", False, 0, "varchar", False),
        ("order_key", "订单号", True, 1, "varchar", False),
        ("dealer_code", "经销商编码", True, 1, "varchar", False),
        ("created_date", "订单日期", True, 1, "datetime", False),
        ("amount_with_tax", "含税金额", False, 1, "decimal", False),
    ],
)
def test_entity_value_vectorization_policy(
    attr_code, attr_name, main, vectorization, data_type, expected
):
    assert _should_vectorize_entity_value({
        "attr_code": attr_code,
        "attr_name": attr_name,
        "is_main_attribute": main,
        "vectorization": vectorization,
        "data_type": data_type,
    }) is expected


def test_query_rows_uses_iterator_without_16384_row_cap():
    store = bare_store()
    store._client = FakePagedMilvusClient()

    ids = store.get_ids_by_where({"type": "entity_attribute_value"})

    assert ids == ["entity-attr-value:1", "entity-attr-value:2"]
    assert store._client.query_kwargs["batch_size"] == 500
    assert store._client.iterator.closed is True


def test_entity_value_deletes_are_routed_and_batched():
    store = bare_store()
    store._client = FakePagedMilvusClient()
    ids = [f"entity-attr-value:{index}" for index in range(1201)]

    store.delete_by_ids(ids)

    assert [name for name, _ in store._client.deletes] == [
        "values", "values", "values",
    ]
    assert [len(batch) for _, batch in store._client.deletes] == [500, 500, 201]
