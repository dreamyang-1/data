from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

# 纯逻辑测试不实例化 Chroma；仅在确实未安装依赖时提供轻量桩。
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
    build_records_from_daily_table,
    replace_daily_table_index,
)


class FakeStore:
    def __init__(self, old_ids=()):
        self.persist_dir = "fake"
        self.old_ids = list(old_ids)
        self.added: list[VectorRecord] = []
        self.deleted: list[str] = []

    def get_ids_by_where(self, _where):
        return list(self.old_ids)

    def add(self, records):
        self.added = list(records)

    def delete_by_ids(self, ids):
        self.deleted.extend(ids)


def fake_embed(texts):
    return [[float(index), 1.0] for index, _ in enumerate(texts)]


class DailyVectorSyncTests(unittest.TestCase):
    def test_builds_stable_records(self):
        rows = [
            {"id": 1, "title": "门店", "content": "上海第一门店", "updated_at": "now"},
            {"id": 2, "title": "渠道", "content": "线上渠道", "updated_at": "now"},
        ]
        records = build_records_from_daily_table(
            rows,
            fake_embed,
            table_name="daily_vector_source",
            id_field="id",
            text_fields=("title", "content"),
            metadata_fields=("updated_at",),
        )
        self.assertEqual(2, len(records))
        self.assertEqual("门店\n上海第一门店", records[0].text)
        self.assertEqual("daily_table_row", records[0].metadata["type"])
        self.assertEqual("1", records[0].metadata["source_id"])
        self.assertNotEqual(records[0].id, records[1].id)

    def test_rejects_duplicate_primary_key(self):
        with self.assertRaisesRegex(ValueError, "主键重复"):
            build_records_from_daily_table(
                [{"id": 1, "content": "a"}, {"id": 1, "content": "b"}],
                fake_embed,
                table_name="daily_vector_source",
                id_field="id",
                text_fields=("content",),
            )

    def test_rejects_empty_vector_text(self):
        with self.assertRaisesRegex(ValueError, "向量字段均为空"):
            build_records_from_daily_table(
                [{"id": 1, "content": None}],
                fake_embed,
                table_name="daily_vector_source",
                id_field="id",
                text_fields=("content",),
            )

    def test_rejects_embedding_count_mismatch(self):
        with self.assertRaisesRegex(RuntimeError, "Embedding 返回数量不一致"):
            build_records_from_daily_table(
                [{"id": 1, "content": "a"}],
                lambda _texts: [],
                table_name="daily_vector_source",
                id_field="id",
                text_fields=("content",),
            )

    def test_replace_upserts_before_deleting_stale_ids(self):
        rows = [{"id": 1, "title": "新标题", "content": "新内容"}]
        expected = build_records_from_daily_table(
            rows,
            fake_embed,
            table_name="daily_vector_source",
            id_field="id",
            text_fields=("title", "content"),
        )[0]
        stale_id = "daily:daily_vector_source:stale"
        store = FakeStore(old_ids=(expected.id, stale_id))
        fake_mysql = types.SimpleNamespace(load_daily_vector_source=lambda **_: rows)
        with patch.dict(sys.modules, {"mysql_tool": fake_mysql}):
            stats = replace_daily_table_index(
                store,
                fake_embed,
                table_name="daily_vector_source",
                id_field="id",
                text_fields=("title", "content"),
            )
        self.assertEqual([stale_id], store.deleted)
        self.assertEqual(1, stats["received_rows"])
        self.assertEqual(1, stats["overwritten_rows"])
        self.assertEqual(1, stats["deleted_rows"])

    def test_empty_source_keeps_old_index(self):
        store = FakeStore(old_ids=("old",))
        fake_mysql = types.SimpleNamespace(load_daily_vector_source=lambda **_: [])
        with patch.dict(sys.modules, {"mysql_tool": fake_mysql}):
            with self.assertRaisesRegex(ValueError, "为空"):
                replace_daily_table_index(
                    store,
                    fake_embed,
                    table_name="daily_vector_source",
                    id_field="id",
                    text_fields=("content",),
                )
        self.assertEqual([], store.added)
        self.assertEqual([], store.deleted)


if __name__ == "__main__":
    unittest.main()
