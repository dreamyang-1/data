from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

try:
    import chromadb  # noqa: F401
except ImportError:
    chromadb_stub = types.ModuleType("chromadb")
    chromadb_stub.PersistentClient = object
    chromadb_config_stub = types.ModuleType("chromadb.config")
    chromadb_config_stub.Settings = object
    sys.modules["chromadb"] = chromadb_stub
    sys.modules["chromadb.config"] = chromadb_config_stub

from vector_store import (
    VectorRecord,
    build_records_from_entity_attributes,
    replace_entity_attribute_index,
)


class FakeStore:
    persist_dir = "fake"

    def __init__(self, old_ids=()):
        self.old_ids = list(old_ids)
        self.added: list[VectorRecord] = []
        self.deleted: list[str] = []

    def get_ids_by_where(self, where):
        self.where = where
        return self.old_ids

    def add(self, records):
        self.added = list(records)

    def delete_by_ids(self, ids):
        self.deleted.extend(ids)


def fake_embed(texts):
    return [[float(i), 1.0] for i, _ in enumerate(texts)]


def source_row(row_id=1):
    return {
        "id": row_id,
        "semantic_model_id": 5,
        "business_domain_id": 9,
        "entity_name": "门店",
        "entity_alias": "店铺,网点",
        "entity_description": "销售门店",
        "attr_name": "所在区域",
        "attr_code": "region_code",
        "attr_description": "门店所属区域",
        "attr_value": "华东",
    }


class EntityAttributeVectorTests(unittest.TestCase):
    def test_builds_scoped_record_with_value(self):
        record = build_records_from_entity_attributes([source_row()], fake_embed)[0]
        self.assertEqual("entity_attribute_value", record.metadata["type"])
        self.assertEqual(5, record.metadata["semantic_model_id"])
        self.assertEqual(9, record.metadata["business_domain_id"])
        self.assertIn("属性值: 华东", record.text)
        self.assertIn("实体别名: 店铺,网点", record.text)
        self.assertEqual("店铺,网点", record.metadata["entity_alias"])

    def test_rejects_missing_required_business_field(self):
        row = source_row()
        row["attr_code"] = None
        with self.assertRaisesRegex(ValueError, "attr_code"):
            build_records_from_entity_attributes([row], fake_embed)

    def test_scope_replace_preserves_other_scopes_and_deletes_stale(self):
        expected = build_records_from_entity_attributes([source_row()], fake_embed)[0]
        stale = "entity-attr-value:5:9:old"
        store = FakeStore((expected.id, stale))
        fake_mysql = types.SimpleNamespace(
            load_entity_attribute_vector_source=lambda *_: [source_row()]
        )
        with patch.dict(sys.modules, {"mysql_tool": fake_mysql}):
            stats = replace_entity_attribute_index(
                store, fake_embed, semantic_model_id=5, business_domain_id=9
            )
        self.assertEqual([stale], store.deleted)
        self.assertEqual(1, stats["indexed_rows"])
        self.assertEqual([{
            "entity_code": "",
            "attr_code": "region_code",
            "indexed_rows": 1,
        }], stats["by_attribute"])

    def test_empty_authoritative_scope_clears_stale_vectors(self):
        store = FakeStore(("old",))
        fake_mysql = types.SimpleNamespace(
            load_entity_attribute_vector_source=lambda *_: []
        )
        with patch.dict(sys.modules, {"mysql_tool": fake_mysql}):
            stats = replace_entity_attribute_index(
                store, fake_embed, semantic_model_id=5, business_domain_id=9
            )
        self.assertEqual("CLEARED", stats["status"])
        self.assertEqual("ENTITY_ATTRIBUTES_CLEARED", stats["code"])
        self.assertEqual(0, stats["indexed_rows"])
        self.assertEqual([], store.added)
        self.assertEqual(["old"], store.deleted)

    def test_never_configured_empty_scope_is_a_noop(self):
        store = FakeStore()
        fake_mysql = types.SimpleNamespace(
            load_entity_attribute_vector_source=lambda *_: []
        )
        with patch.dict(sys.modules, {"mysql_tool": fake_mysql}):
            stats = replace_entity_attribute_index(
                store, fake_embed, semantic_model_id=5, business_domain_id=9
            )
        self.assertEqual("SKIPPED", stats["status"])
        self.assertEqual("NO_ENTITY_ATTRIBUTES", stats["code"])
        self.assertEqual([], store.deleted)


if __name__ == "__main__":
    unittest.main()
