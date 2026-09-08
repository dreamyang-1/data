"""Chroma 向量存储 - 基于 ChromaDB 的轻量级持久化向量库。

设计原则（对齐 `向量化说明.md`）:
  - 仅将"需向量化的字段"拼接成 text 入向量索引
  - 其他所有业务字段作为标量 metadata 存储（dict/list 序列化为 JSON 字符串）
  - 重建索引时全量删除再写入，保证元数据与索引严格一致

数据源: MySQL（新表结构 semantic_model_entity_type 等）
存储路径: ./vector_store/chroma/

知识源类型:
  - 业务域 DSL（实体 / 属性 / 关系 / 指标 / 维度 / 枚举）—— 按语义建模 × 业务域隔离
  - 物理表元信息（semantic_model_table + semantic_model_field）—— 按数据源隔离
"""
from __future__ import annotations

import json
import hashlib
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import chromadb
from chromadb.config import Settings
from dimension_scope import project_dimension_to_domain

# 默认持久化目录
DEFAULT_PERSIST_DIR = Path(__file__).parent / "vector_store" / "chroma"
DEFAULT_COLLECTION = "dsl_knowledge"
ENTITY_ATTRIBUTE_VALUE_TYPE = "entity_attribute_value"
SEMANTIC_RECORD_TYPES = {
    "entity", "attribute", "relation", "dimension", "enum", "metric",
    "scoped_dimension", "scoped_enum",
}
PHYSICAL_RECORD_TYPES = {"table", "field"}
DAILY_TABLE_VECTOR_TYPE = "daily_table_row"

_UNICODE_DASH_TRANSLATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2212": "-", "\ufe58": "-", "\ufe63": "-",
    "\uff0d": "-",
})


def normalize_vector_text(value: object) -> str:
    """Use one punctuation form for embedding and stable record identities."""
    return str(value or "").strip().translate(_UNICODE_DASH_TRANSLATION)


# ============================ 数据结构 ============================

@dataclass
class VectorRecord:
    """一条向量记录（用于入库的标准化结构）"""
    id: str                              # 全局唯一ID: <type>:<code>
    text: str                            # 向量化文本（仅含需向量化的字段）
    vector: list[float]                  # 嵌入向量
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata 包含除向量化字段外的所有业务字段（标量存储）
    #   type: entity | attribute | relation | metric | dimension | enum
    #   code: 业务编码
    #   name: 业务名称（用于检索结果展示）
    #   ... 其他原始字段原样保留


@dataclass
class SearchResult:
    """单条召回结果"""
    id: str
    score: float                         # 相似度（cosine，越大越相似）
    text: str
    metadata: dict[str, Any]


# ============================ 向量存储 ============================

class ChromaVectorStore:
    """基于 ChromaDB 的向量存储"""

    def __init__(
        self,
        persist_dir: Path = DEFAULT_PERSIST_DIR,
        collection_name: str = DEFAULT_COLLECTION,
    ):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.collection_name = collection_name
        self._client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},  # cosine 距离
        )

    # -------- 基础操作 --------

    def add(self, records: Sequence[VectorRecord]) -> None:
        """批量写入记录（已存在 id 会 upsert）"""
        if not records:
            return
        self._collection.upsert(
            ids=[r.id for r in records],
            embeddings=[r.vector for r in records],
            documents=[r.text for r in records],
            metadatas=[self._serialize_meta(r.metadata) for r in records],
        )

    def search(
        self,
        query_vector: list[float],
        top_k: int = 8,
        where: dict | None = None,
    ) -> list[SearchResult]:
        """向量检索，可按 metadata 过滤

        Args:
            query_vector: 查询向量
            top_k: 召回数量
            where: chroma where 过滤条件，如 {"type": "metric"}
        """
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_vector],
            # Chroma safely returns fewer rows when the collection (or the
            # metadata filter) contains less than top_k, including an empty
            # collection. Avoid a separate native count() call for every
            # semantic sub-search.
            "n_results": top_k,
        }
        if where:
            kwargs["where"] = where
        res = self._collection.query(**kwargs)
        return self._format_results(res)

    def count(self) -> int:
        return self._collection.count()

    def clear(self) -> None:
        """清空集合（删除后重建空集合）"""
        self._client.delete_collection(self.collection_name)
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def delete_by_where(self, where: dict) -> None:
        """按 metadata 过滤条件删除记录"""
        self._collection.delete(where=where)

    def delete_by_ids(self, ids: Sequence[str]) -> None:
        """按记录 ID 删除；空列表不执行。"""
        if ids:
            self._collection.delete(ids=list(ids))

    def get_ids_by_where(self, where: dict) -> list[str]:
        """获取符合 metadata 过滤条件的全部记录 ID。"""
        res = self._collection.get(where=where, include=[])
        return list(res.get("ids") or []) if res else []

    def get_by_where(self, where: dict) -> list[SearchResult]:
        """Deterministically load metadata records without vector re-ranking.

        This is intentionally separate from :meth:`search`: callers use it to
        complete an already recalled relationship path, never as independent
        semantic evidence.  Completed records therefore carry a neutral score.
        """
        res = self._collection.get(
            where=where,
            include=["documents", "metadatas"],
        )
        if not res:
            return []
        ids = list(res.get("ids") or [])
        documents = list(res.get("documents") or [])
        metadatas = list(res.get("metadatas") or [])
        return [
            SearchResult(
                id=str(record_id),
                score=0.0,
                text=str(document or ""),
                metadata=self._deserialize_meta(metadata or {}),
            )
            for record_id, document, metadata in zip(ids, documents, metadatas)
        ]

    def count_by_where(self, where: dict) -> int:
        """按 where 条件统计记录数"""
        res = self._collection.get(where=where, include=[])
        return len(res["ids"]) if res and res.get("ids") else 0

    def rebuild(self, records: Sequence[VectorRecord]) -> None:
        """全量删除重建索引"""
        self.clear()
        self.add(records)

    # -------- 序列化辅助 --------

    @staticmethod
    def _serialize_meta(meta: dict) -> dict:
        """dict/list 嵌套结构序列化为字符串，兼容 chroma metadata 限制"""
        out = {}
        for k, v in meta.items():
            if isinstance(v, (dict, list)):
                out[k] = json.dumps(v, ensure_ascii=False)
            else:
                out[k] = v
        return out

    @staticmethod
    def _deserialize_meta(meta: dict) -> dict:
        """反向解析"""
        out = {}
        for k, v in meta.items():
            if isinstance(v, str) and v.startswith(("[", "{")):
                try:
                    out[k] = json.loads(v)
                except json.JSONDecodeError:
                    out[k] = v
            else:
                out[k] = v
        return out

    def _format_results(self, res: dict) -> list[SearchResult]:
        results: list[SearchResult] = []
        if not res or not res.get("ids"):
            return results
        ids_list = res["ids"][0]
        docs_list = res.get("documents", [[]])[0]
        metas_list = res.get("metadatas", [[]])[0]
        dists_list = res.get("distances", [[]])[0]
        for _id, doc, meta, dist in zip(ids_list, docs_list, metas_list, dists_list):
            # chroma cosine: distance 越小越相似，转换成 score（越大越相似）
            score = 1.0 - dist
            results.append(SearchResult(
                id=_id,
                score=float(score),
                text=doc or "",
                metadata=self._deserialize_meta(meta or {}),
            ))
        return results


class MilvusVectorStore:
    """Milvus-backed store with hard collection isolation by knowledge family.

    The public methods intentionally mirror :class:`ChromaVectorStore` so the
    semantic planner does not depend on a storage vendor.  Structure rebuilds,
    entity-value snapshots, physical catalog data and daily rows live in
    separate collections and therefore cannot delete one another.
    """

    _FILTER_FIELDS = {
        "record_id", "type", "semantic_model_id", "business_domain_id",
        "data_source_id", "scope_key", "entity_code", "entity_name",
        "parent", "attr_code", "attr_name", "source_table", "source_id",
        "source_field", "canonical_value", "snapshot_version", "content_hash",
    }

    def __init__(self) -> None:
        from pymilvus import MilvusClient
        from config import (
            EMBEDDING_DIM,
            MILVUS_DATABASE,
            MILVUS_DAILY_COLLECTION,
            MILVUS_ENTITY_VALUE_COLLECTION,
            MILVUS_HOST,
            MILVUS_PASSWORD,
            MILVUS_PHYSICAL_COLLECTION,
            MILVUS_PORT,
            MILVUS_SEMANTIC_COLLECTION,
            MILVUS_TIMEOUT_SECONDS,
            MILVUS_USER,
        )

        self.embedding_dim = EMBEDDING_DIM
        self.timeout = MILVUS_TIMEOUT_SECONDS
        self.persist_dir = Path("milvus")
        self.collection_name = MILVUS_SEMANTIC_COLLECTION
        self._collections = {
            "semantic": self._safe_collection_name(MILVUS_SEMANTIC_COLLECTION),
            "entity_value": self._safe_collection_name(MILVUS_ENTITY_VALUE_COLLECTION),
            "physical": self._safe_collection_name(MILVUS_PHYSICAL_COLLECTION),
            "daily": self._safe_collection_name(MILVUS_DAILY_COLLECTION),
        }
        uri = f"http://{MILVUS_HOST}:{MILVUS_PORT}"
        kwargs: dict[str, Any] = {"uri": uri, "db_name": MILVUS_DATABASE}
        if MILVUS_USER:
            kwargs["user"] = MILVUS_USER
            kwargs["password"] = MILVUS_PASSWORD
        self._client = MilvusClient(**kwargs)
        self.ensure_schema()

    @staticmethod
    def _safe_collection_name(value: str) -> str:
        name = re.sub(r"[^0-9A-Za-z_]", "_", str(value or "").strip())
        if not name:
            raise RuntimeError("Milvus collection name is empty")
        if name[0].isdigit():
            name = "v_" + name
        return name[:255]

    def ensure_schema(self) -> None:
        from pymilvus import DataType, MilvusClient

        for collection in dict.fromkeys(self._collections.values()):
            if self._client.has_collection(collection_name=collection):
                description = self._client.describe_collection(collection_name=collection)
                fields = {field["name"]: field for field in description.get("fields", [])}
                vector = fields.get("embedding")
                params = (vector or {}).get("params") or {}
                actual_dim = int(params.get("dim") or 0)
                if not vector or actual_dim != self.embedding_dim:
                    raise RuntimeError(
                        f"Milvus collection {collection} has incompatible embedding dim "
                        f"{actual_dim}; expected {self.embedding_dim}"
                    )
                for field_name, max_length in (
                    ("source_field", 256), ("canonical_value", 4096),
                ):
                    if field_name not in fields:
                        self._client.add_collection_field(
                            collection_name=collection,
                            field_name=field_name,
                            data_type=DataType.VARCHAR,
                            max_length=max_length,
                            nullable=True,
                        )
                self._client.load_collection(collection_name=collection)
                continue

            schema = MilvusClient.create_schema(
                auto_id=False, enable_dynamic_field=False,
            )
            schema.add_field("record_id", DataType.VARCHAR, is_primary=True, max_length=512)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self.embedding_dim)
            schema.add_field("text", DataType.VARCHAR, max_length=65535)
            schema.add_field("type", DataType.VARCHAR, max_length=64)
            schema.add_field("semantic_model_id", DataType.INT64)
            schema.add_field("business_domain_id", DataType.INT64)
            schema.add_field("data_source_id", DataType.INT64)
            schema.add_field("scope_key", DataType.VARCHAR, max_length=128)
            schema.add_field("entity_code", DataType.VARCHAR, max_length=256)
            schema.add_field("entity_name", DataType.VARCHAR, max_length=512)
            schema.add_field("parent", DataType.VARCHAR, max_length=256)
            schema.add_field("attr_code", DataType.VARCHAR, max_length=256)
            schema.add_field("attr_name", DataType.VARCHAR, max_length=512)
            schema.add_field("source_table", DataType.VARCHAR, max_length=256)
            schema.add_field("source_field", DataType.VARCHAR, max_length=256, nullable=True)
            schema.add_field("source_id", DataType.VARCHAR, max_length=512)
            schema.add_field("canonical_value", DataType.VARCHAR, max_length=4096, nullable=True)
            schema.add_field("snapshot_version", DataType.VARCHAR, max_length=128)
            schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
            schema.add_field("metadata", DataType.JSON)

            indexes = self._client.prepare_index_params()
            indexes.add_index(
                field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE",
            )
            for field_name in (
                "type", "semantic_model_id", "business_domain_id", "data_source_id",
                "scope_key", "snapshot_version", "entity_code", "attr_code",
                "source_table",
            ):
                indexes.add_index(field_name=field_name, index_type="AUTOINDEX")
            self._client.create_collection(
                collection_name=collection, schema=schema, index_params=indexes,
            )
            self._client.load_collection(collection_name=collection)

    @staticmethod
    def _family_for_type(record_type: str) -> str:
        if record_type in SEMANTIC_RECORD_TYPES:
            return "semantic"
        if record_type == ENTITY_ATTRIBUTE_VALUE_TYPE:
            return "entity_value"
        if record_type in PHYSICAL_RECORD_TYPES:
            return "physical"
        if record_type == DAILY_TABLE_VECTOR_TYPE:
            return "daily"
        raise ValueError(f"unsupported vector record type: {record_type}")

    @staticmethod
    def _metadata_scalar(metadata: dict, key: str, default: Any) -> Any:
        value = metadata.get(key, default)
        if value is None:
            return default
        return value

    def _to_row(self, record: VectorRecord) -> dict:
        metadata = dict(record.metadata)
        record_type = str(metadata.get("type") or "")
        sm = int(metadata.get("semantic_model_id") or -1)
        bd = int(metadata.get("business_domain_id") or -1)
        ds = int(metadata.get("data_source_id") or -1)
        scope_key = str(metadata.get("scope_key") or f"sm:{sm}:bd:{bd}:ds:{ds}")
        content_hash = str(metadata.get("content_hash") or hashlib.sha256(
            record.text.encode("utf-8")
        ).hexdigest())
        return {
            "record_id": str(record.id)[:512],
            "embedding": list(record.vector),
            "text": str(record.text)[:65535],
            "type": record_type[:64],
            "semantic_model_id": sm,
            "business_domain_id": bd,
            "data_source_id": ds,
            "scope_key": scope_key[:128],
            "entity_code": str(metadata.get("entity_code") or metadata.get("parent") or "")[:256],
            "entity_name": str(metadata.get("entity_name") or metadata.get("parent_name") or "")[:512],
            "parent": str(metadata.get("parent") or "")[:256],
            "attr_code": str(metadata.get("attr_code") or "")[:256],
            "attr_name": str(metadata.get("attr_name") or "")[:512],
            "source_table": str(metadata.get("source_table") or "")[:256],
            "source_field": str(metadata.get("source_field") or "")[:256] or None,
            "source_id": str(metadata.get("source_id") or "")[:512],
            "canonical_value": str(
                metadata.get("canonical_value") or metadata.get("attr_value") or ""
            )[:4096] or None,
            "snapshot_version": str(metadata.get("snapshot_version") or "legacy")[:128],
            "content_hash": content_hash[:64],
            "metadata": self._json_safe(metadata),
        }

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): MilvusVectorStore._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [MilvusVectorStore._json_safe(v) for v in value]
        return str(value)

    def add(self, records: Sequence[VectorRecord]) -> None:
        grouped: dict[str, list[dict]] = {}
        for record in records:
            record_type = str(record.metadata.get("type") or "")
            family = self._family_for_type(record_type)
            grouped.setdefault(family, []).append(self._to_row(record))
        for family, rows in grouped.items():
            collection = self._collections[family]
            for start in range(0, len(rows), 500):
                self._client.upsert(
                    collection_name=collection,
                    data=rows[start:start + 500],
                    timeout=self.timeout,
                )

    @classmethod
    def _literal(cls, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return json.dumps(str(value), ensure_ascii=False)

    @classmethod
    def _compile_filter(cls, where: dict | None) -> str:
        if not where:
            return "record_id != \"\""
        if "$and" in where:
            return " and ".join(
                f"({cls._compile_filter(item)})" for item in where["$and"]
            )
        if "$or" in where:
            return " or ".join(
                f"({cls._compile_filter(item)})" for item in where["$or"]
            )
        expressions: list[str] = []
        aliases = {"record_id": "record_id", "type": "type"}
        for raw_field, condition in where.items():
            field = aliases.get(raw_field, raw_field)
            if field not in cls._FILTER_FIELDS:
                raise ValueError(f"unsupported Milvus filter field: {raw_field}")
            if isinstance(condition, dict):
                if set(condition) != {"$in"}:
                    raise ValueError(f"unsupported Milvus filter operator for {raw_field}")
                values = condition["$in"]
                expressions.append(
                    f"{field} in [{','.join(cls._literal(v) for v in values)}]"
                )
            else:
                expressions.append(f"{field} == {cls._literal(condition)}")
        return " and ".join(expressions) or "record_id != \"\""

    @staticmethod
    def _types_in_filter(where: Any) -> set[str]:
        if not isinstance(where, dict):
            return set()
        found: set[str] = set()
        for key, value in where.items():
            if key == "type":
                if isinstance(value, dict) and "$in" in value:
                    found.update(map(str, value["$in"]))
                elif not isinstance(value, dict):
                    found.add(str(value))
            elif key in {"$and", "$or"} and isinstance(value, list):
                for item in value:
                    found.update(MilvusVectorStore._types_in_filter(item))
        return found

    def _candidate_families(self, where: dict | None) -> list[str]:
        types = self._types_in_filter(where)
        if not types:
            return list(self._collections)
        return list(dict.fromkeys(self._family_for_type(item) for item in types))

    @staticmethod
    def _from_hit(hit: dict) -> SearchResult:
        entity = hit.get("entity") or {}
        metadata = dict(entity.get("metadata") or {})
        return SearchResult(
            id=str(entity.get("record_id") or hit.get("id") or ""),
            score=float(hit.get("distance") or 0.0),
            text=str(entity.get("text") or ""),
            metadata=metadata,
        )

    def search(self, query_vector: list[float], top_k: int = 8,
               where: dict | None = None) -> list[SearchResult]:
        expression = self._compile_filter(where)
        output_fields = ["record_id", "text", "metadata"]
        combined: list[SearchResult] = []
        for family in self._candidate_families(where):
            result = self._client.search(
                collection_name=self._collections[family],
                data=[query_vector],
                anns_field="embedding",
                filter=expression,
                limit=top_k,
                output_fields=output_fields,
                search_params={"metric_type": "COSINE", "params": {}},
                timeout=self.timeout,
            )
            combined.extend(self._from_hit(hit) for hit in (result[0] if result else []))
        combined.sort(key=lambda item: item.score, reverse=True)
        return combined[:top_k]

    def _query_rows(self, where: dict | None, output_fields: list[str]) -> list[dict]:
        expression = self._compile_filter(where)
        rows: list[dict] = []
        for family in self._candidate_families(where):
            collection = self._collections[family]
            query_iterator = getattr(self._client, "query_iterator", None)
            if not callable(query_iterator):
                # Compatibility path for older clients and lightweight test
                # doubles. Production Milvus clients use the iterator below so
                # stale-id discovery is never silently capped at 16,384 rows.
                rows.extend(self._client.query(
                    collection_name=collection,
                    filter=expression,
                    output_fields=output_fields,
                    limit=16384,
                    timeout=self.timeout,
                ))
                continue
            iterator = query_iterator(
                collection_name=collection,
                filter=expression,
                output_fields=output_fields,
                batch_size=500,
            )
            try:
                while True:
                    batch = iterator.next()
                    if not batch:
                        break
                    rows.extend(batch)
            finally:
                iterator.close()
        return rows

    def get_ids_by_where(self, where: dict) -> list[str]:
        return [str(row["record_id"]) for row in self._query_rows(where, ["record_id"])]

    def get_by_where(self, where: dict) -> list[SearchResult]:
        return [SearchResult(
            id=str(row.get("record_id") or ""), score=0.0,
            text=str(row.get("text") or ""), metadata=dict(row.get("metadata") or {}),
        ) for row in self._query_rows(where, ["record_id", "text", "metadata"])]

    def find_exact(self, where: dict) -> list[SearchResult]:
        """Return exact scalar matches from explicit Milvus fields."""
        return self.get_by_where(where)

    def count_by_where(self, where: dict) -> int:
        return len(self.get_ids_by_where(where))

    def count(self) -> int:
        return sum(self.count_by_where({"type": {"$in": list(types)}}) for types in (
            SEMANTIC_RECORD_TYPES, {ENTITY_ATTRIBUTE_VALUE_TYPE},
            PHYSICAL_RECORD_TYPES, {DAILY_TABLE_VECTOR_TYPE},
        ))

    def delete_by_where(self, where: dict) -> None:
        expression = self._compile_filter(where)
        for family in self._candidate_families(where):
            self._client.delete(
                collection_name=self._collections[family], filter=expression,
                timeout=self.timeout,
            )

    def delete_by_ids(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        grouped: dict[str, list[str]] = {}
        broadcast: list[str] = []
        for item in dict.fromkeys(str(item) for item in ids):
            if item.startswith("entity-attr-value:"):
                grouped.setdefault("entity_value", []).append(item)
            elif item.startswith("daily:"):
                grouped.setdefault("daily", []).append(item)
            elif item.startswith("ds"):
                grouped.setdefault("physical", []).append(item)
            elif item.startswith("sm"):
                grouped.setdefault("semantic", []).append(item)
            else:
                broadcast.append(item)
        if broadcast:
            for family in self._collections:
                grouped.setdefault(family, []).extend(broadcast)
        for family, family_ids in grouped.items():
            collection = self._collections[family]
            for start in range(0, len(family_ids), 500):
                self._client.delete(
                    collection_name=collection,
                    ids=family_ids[start:start + 500],
                    timeout=self.timeout,
                )

    def clear(self) -> None:
        """Clear only semantic definitions; other collection families survive."""
        self.delete_by_where({"type": {"$in": sorted(SEMANTIC_RECORD_TYPES)}})

    def rebuild(self, records: Sequence[VectorRecord]) -> None:
        if any(str(record.metadata.get("type")) not in SEMANTIC_RECORD_TYPES
               for record in records):
            raise ValueError("rebuild() accepts semantic catalog records only")
        self.clear()
        self.add(records)

    def health_check(self) -> dict[str, Any]:
        return {
            "backend": "milvus",
            "healthy": all(
                self._client.has_collection(collection_name=name)
                for name in self._collections.values()
            ),
            "collections": dict(self._collections),
            "embedding_dim": self.embedding_dim,
        }


def create_vector_store():
    """Create the configured backend without exposing connection credentials."""
    from config import VECTOR_STORE_BACKEND

    if VECTOR_STORE_BACKEND == "milvus":
        return MilvusVectorStore()
    if VECTOR_STORE_BACKEND == "chroma":
        return ChromaVectorStore()
    raise RuntimeError(f"unsupported VECTOR_STORE_BACKEND: {VECTOR_STORE_BACKEND}")


# ============================ 向量化文本构建（仅含向量化字段） ============================

def _join_parts(*parts: Any) -> str:
    """拼接非空文本片段，使用 ' / ' 分隔"""
    flat = []
    for p in parts:
        if not p:
            continue
        if isinstance(p, (list, tuple)):
            flat.extend([str(x) for x in p if x])
        else:
            flat.append(str(p))
    return " / ".join(flat)


def _build_entity_text(entity: dict) -> str:
    """实体向量化字段: entity_name + entity_alias + business_domain + description"""
    biz_def = entity.get("business_definition") or {}
    return _join_parts(
        entity.get("entity_name"),
        entity.get("entity_alias"),
        entity.get("business_domain"),
        biz_def.get("description"),
    )


def _build_attr_text(attr: dict) -> str:
    """属性向量化字段: attr_name + description + data_type"""
    return _join_parts(
        attr.get("attr_name"),
        attr.get("description"),
        attr.get("data_type"),
    )


def _build_relation_text(rel: dict) -> str:
    """关系向量化字段: relation_name + description + target_entity"""
    return _join_parts(
        rel.get("relation_name"),
        rel.get("description"),
        rel.get("target_entity"),
    )


def _build_dim_text(dim: dict) -> str:
    """维度向量化字段: dim_name + synonyms + dim_description + special_rules"""
    biz_def = dim.get("business_definition") or {}
    special_rules = dim.get("special_rules") or []
    special_descs = [r.get("desc") for r in special_rules if isinstance(r, dict)]
    return _join_parts(
        dim.get("dim_name"),
        dim.get("synonyms"),
        biz_def.get("description"),
        special_descs,
    )


def _build_enum_text(enum: dict) -> str:
    """枚举值向量化字段: name"""
    return _join_parts(enum.get("name"))


def _build_metric_text(metric: dict) -> str:
    """指标向量化字段:
    metric_name + synonyms + business_domain
    + business_definition.description + application_scenes
    + calculation_rule.calc_formula + global_filters.desc
    """
    biz_def = metric.get("business_definition") or {}
    calc_rule = metric.get("calculation_rule") or {}
    filter_descs = [f.get("desc") for f in calc_rule.get("global_filters") or []]
    return _join_parts(
        metric.get("metric_name"),
        metric.get("synonyms"),
        metric.get("business_domain"),
        biz_def.get("description"),
        biz_def.get("application_scenes"),
        calc_rule.get("calc_formula"),
        filter_descs,
    )


# ============================ MySQL → 向量记录构建 ============================

def load_all_dsl() -> list[dict]:
    """从 MySQL 全量加载所有语义建模下所有业务域的 DSL（用于一次性构建全量索引）。

    遍历 semantic_model → semantic_model_business_domain → 各业务数据，
    每个业务域生成一份 DSL 文档（带 semantic_model_id 和 business_domain_id）。

    Returns:
        list[dict]: 每个元素是一个作用域的 DSL 文档
        {
            "semantic_model": {id, name, code},
            "business_domain": {id, name, code} | None,
            "entities": [...],
            "metrics": [...],
            "dimensions": [...],
        }
    """
    from mysql_tool import (
        get_business_domains,
        get_dsl_by_scope,
        get_semantic_models,
    )

    docs: list[dict] = []
    for sm in get_semantic_models():
        sm_id = sm["id"]
        for bd in get_business_domains(sm_id):
            doc = get_dsl_by_scope(sm_id, bd["id"])
            docs.append(doc)
    return docs


def build_records_from_dsl(doc: dict, embed_fn) -> list[VectorRecord]:
    """从单个作用域 DSL 文档构建向量记录，metadata 注入 semantic_model_id / business_domain_id。

    Args:
        doc: load_all_dsl() 中单个作用域的 DSL 文档
        embed_fn: 批量嵌入函数 list[str] -> list[list[float]]

    Note:
        维度/枚举本作用域内属于"跨业务域共享"，但仍写入 semantic_model_id 和
        business_domain_id（值为 -1 表示跨域共享），以便 chroma where 查询
        （chroma 不支持 None 作为 where 值）。
    """
    sm_id = doc["semantic_model"]["id"]
    sm_name = doc["semantic_model"]["name"]
    bd_id = doc["business_domain"]["id"] if doc["business_domain"] else -1
    bd_name = doc["business_domain"]["name"] if doc["business_domain"] else "(跨域共享)"

    pending: list[tuple[str, dict]] = []

    for entity in doc["entities"]:
        pending.append((_build_entity_text(entity), {"kind": "entity", "obj": entity}))
        for attr in entity.get("attributes") or []:
            pending.append((_build_attr_text(attr), {
                "kind": "attribute",
                "obj": attr,
                "entity_code": entity.get("entity_code"),
                "entity_name": entity.get("entity_name"),
            }))
        for rel in entity.get("relations") or []:
            pending.append((_build_relation_text(rel), {
                "kind": "relation",
                "obj": rel,
                "entity_code": entity.get("entity_code"),
                "entity_name": entity.get("entity_name"),
            }))

    for dim in doc["dimensions"]:
        pending.append((_build_dim_text(dim), {"kind": "dimension", "obj": dim}))
        for enum in dim.get("enum_list") or []:
            pending.append((_build_enum_text(enum), {
                "kind": "enum",
                "obj": enum,
                "dim_code": dim.get("dim_code"),
                "dim_name": dim.get("dim_name"),
            }))
        # Publish only ownership-proven projections. Explicit retrieval never
        # adds shared domain -1; the shared original remains model-wide only.
        scoped_dim = project_dimension_to_domain(dim, doc)
        if scoped_dim is not None:
            pending.append((_build_dim_text(scoped_dim), {
                "kind": "dimension", "obj": scoped_dim, "domain_projection": True,
            }))
            for enum in scoped_dim.get("enum_list") or []:
                pending.append((_build_enum_text(enum), {
                    "kind": "enum", "obj": enum, "domain_projection": True,
                    "dim_code": scoped_dim.get("dim_code"),
                    "dim_name": scoped_dim.get("dim_name"),
                }))

    for metric in doc["metrics"]:
        pending.append((_build_metric_text(metric), {"kind": "metric", "obj": metric}))

    texts = [t for t, _ in pending]
    vectors = embed_fn(texts) if texts else []
    if len(vectors) != len(pending):
        raise RuntimeError(
            "Embedding 返回数量不一致: "
            f"expected={len(pending)}, actual={len(vectors)}"
        )

    # 实体/属性/关系/指标 ID 带 sm+bd 前缀；
    # 维度/枚举 ID 只带 sm 前缀（跨业务域共享），同一 sm 下多次 upsert 会去重
    bd_scope_prefix = f"sm{sm_id}_bd{bd_id}" if bd_id != -1 else f"sm{sm_id}"
    sm_only_prefix = f"sm{sm_id}"

    records: list[VectorRecord] = []
    for (text, ctx), vec in zip(pending, vectors):
        kind = ctx["kind"]
        obj = ctx["obj"]

        if kind == "entity":
            rid = f"{bd_scope_prefix}:entity:{obj.get('entity_code') or uuid.uuid4().hex[:8]}"
            meta = {**obj, "type": "entity"}
            scope_bd_id = bd_id
            scope_bd_name = bd_name
        elif kind == "attribute":
            parent = ctx.get("entity_code") or "unknown"
            attr_code = obj.get("attr_code") or uuid.uuid4().hex[:8]
            rid = f"{bd_scope_prefix}:attr:{parent}.{attr_code}"
            meta = {
                **obj,
                "type": "attribute",
                "parent": ctx.get("entity_code"),
                "parent_name": ctx.get("entity_name"),
            }
            scope_bd_id = bd_id
            scope_bd_name = bd_name
        elif kind == "relation":
            parent = ctx.get("entity_code") or "unknown"
            rel_code = obj.get("relation_code") or uuid.uuid4().hex[:8]
            rid = f"{bd_scope_prefix}:relation:{parent}.{rel_code}"
            meta = {
                **obj,
                "type": "relation",
                "parent": ctx.get("entity_code"),
                "parent_name": ctx.get("entity_name"),
            }
            scope_bd_id = bd_id
            scope_bd_name = bd_name
        elif kind == "dimension":
            # 维度跨业务域共享：ID 只带 sm 前缀，business_domain_id 标记为 -1
            rid = f"{sm_only_prefix}:dim:{obj.get('dim_code') or uuid.uuid4().hex[:8]}"
            meta = {**obj, "type": "dimension"}
            scope_bd_id = -1
            scope_bd_name = "(跨域共享)"
        elif kind == "enum":
            parent = ctx.get("dim_code") or "unknown"
            enum_code = obj.get("code") or uuid.uuid4().hex[:8]
            rid = f"{sm_only_prefix}:enum:{parent}.{enum_code}"
            meta = {
                **obj,
                "type": "enum",
                "parent": ctx.get("dim_code"),
                "parent_name": ctx.get("dim_name"),
            }
            scope_bd_id = -1
            scope_bd_name = "(跨域共享)"
        elif kind == "metric":
            rid = f"{bd_scope_prefix}:metric:{obj.get('metric_code') or uuid.uuid4().hex[:8]}"
            meta = {**obj, "type": "metric"}
            scope_bd_id = bd_id
            scope_bd_name = bd_name
        else:
            continue

        if ctx.get("domain_projection"):
            rid = rid.replace(sm_only_prefix + ":", bd_scope_prefix + ":", 1)
            scope_bd_id, scope_bd_name = bd_id, bd_name
            meta["scope_projection_version"] = "dimension-domain-v1"
            meta["type"] = "scoped_" + kind

        # 注入作用域元数据（检索时用于 where 过滤）
        meta["semantic_model_id"] = sm_id
        meta["semantic_model_name"] = sm_name
        meta["business_domain_id"] = scope_bd_id
        meta["business_domain_name"] = scope_bd_name

        meta = {k: v for k, v in meta.items() if v is not None}
        records.append(VectorRecord(id=rid, text=text, vector=vec, metadata=meta))

    # 按 id 去重（同一作用域内可能因数据源重复行导致 rid 冲突，保留首份）
    deduped: dict[str, VectorRecord] = {}
    for r in records:
        if r.id not in deduped:
            deduped[r.id] = r
    return list(deduped.values())


def build_scope_filter(semantic_model_id: int, business_domain_id: int = None) -> dict:
    """Apply an exact domain; shared records are accessible only in MODEL_WIDE."""
    from scope_contract import scope_filter
    return scope_filter(semantic_model_id, business_domain_id)


# ============================ 索引重建入口 ============================

def rebuild_index(store: ChromaVectorStore, embed_fn) -> dict:
    """从 MySQL 全量重建向量索引（先清空再写入）

    遍历所有 semantic_model × business_domain 作用域，逐个构建记录后批量写入。
    metadata 中带 semantic_model_id 和 business_domain_id，检索时按作用域过滤。

    Args:
        store: 向量存储实例
        embed_fn: 批量嵌入函数 list[str] -> list[list[float]]

    Returns:
        统计信息 dict
    """
    docs = load_all_dsl()

    # 按 ID 去重：维度/枚举跨业务域共享，同一 sm 下 ID 相同，只保留首份
    deduped: dict[str, VectorRecord] = {}
    scope_stats: list[dict] = []
    for doc in docs:
        records = build_records_from_dsl(doc, embed_fn)
        scope_stats.append({
            "semantic_model": doc["semantic_model"]["name"],
            "business_domain": doc["business_domain"]["name"] if doc["business_domain"] else None,
            "records": len(records),
            "dimension_projection_candidates": len(doc["dimensions"]),
            "dimension_projections_published": sum(r.metadata.get("type") == "scoped_dimension" for r in records),
        })
        for r in records:
            if r.id not in deduped:
                deduped[r.id] = r

    all_records = list(deduped.values())
    # Replace only governed semantic definitions.  Entity values, physical
    # catalog records and daily data are independent knowledge families and
    # must survive a semantic-model publication.
    store.delete_by_where({"type": {"$in": sorted(SEMANTIC_RECORD_TYPES)}})
    store.add(all_records)

    type_counts: dict[str, int] = {}
    for r in all_records:
        t = r.metadata.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "total": len(all_records),
        "by_type": type_counts,
        "by_scope": scope_stats,
        "persist_dir": str(store.persist_dir),
    }


def rebuild_index_by_scope(
    store: ChromaVectorStore,
    embed_fn,
    semantic_model_id: int,
    business_domain_id: int = None,
) -> dict:
    """按作用域增量重建向量索引（只处理指定 sm/bd，不影响其他作用域）

    - 指定 business_domain_id：删除该 bd 下的实体/属性/关系/指标，再写入；
      同时 upsert 该 sm 下的维度/枚举（跨域共享，ID 带 sm 前缀，自动去重）
    - business_domain_id=None：删除该 sm 下所有记录，再写入该 sm 下全部业务域数据

    若该作用域无历史记录则跳过删除步骤，直接写入。

    Args:
        store: 向量存储实例
        embed_fn: 批量嵌入函数 list[str] -> list[list[float]]
        semantic_model_id: 语义建模 ID
        business_domain_id: 业务域 ID（可选）

    Returns:
        统计信息 dict（含 deleted 字段表示本次删除的历史记录数）
    """
    from mysql_tool import get_business_domains, get_dsl_by_scope

    # 1. 构造作用域 where 条件（需与 build_scope_filter 查询条件保持一致，
    #    维度/枚举 business_domain_id=-1 为跨域共享，删除时也要一并清理）
    if business_domain_id is None:
        scope_where = {
            "$and": [
                {"type": {"$in": sorted(SEMANTIC_RECORD_TYPES)}},
                {"semantic_model_id": semantic_model_id},
            ]
        }
    else:
        scope_where = {
            "$and": [
                {"type": {"$in": sorted(SEMANTIC_RECORD_TYPES)}},
                {"semantic_model_id": semantic_model_id},
                {"business_domain_id": {"$in": [business_domain_id, -1]}},
            ]
        }

    # 2. 先完整拉取、校验并生成新向量。任何上游失败都不会破坏旧索引。
    # business_domain_id=None 表示重建整个语义模型，不能直接调用
    # get_dsl_by_scope(sm, None)：该兼容接口在 None 场景不返回实体，并会把所有
    # 指标错误标成跨域共享。这里必须逐业务域加载后合并。
    if business_domain_id is None:
        domains = get_business_domains(semantic_model_id)
        if domains:
            docs = [
                get_dsl_by_scope(semantic_model_id, int(domain["id"]))
                for domain in domains
            ]
        else:
            # 同时完成 semantic_model 存在性校验，并兼容尚未创建业务域、但已有
            # 全局维度/指标的模型。
            docs = [get_dsl_by_scope(semantic_model_id, None)]
    else:
        docs = [get_dsl_by_scope(semantic_model_id, business_domain_id)]

    deduped: dict[str, VectorRecord] = {}
    scope_stats: list[dict[str, Any]] = []
    for doc in docs:
        scope_records = build_records_from_dsl(doc, embed_fn)
        scope_stats.append({
            "business_domain_id": (
                doc["business_domain"]["id"] if doc.get("business_domain") else None
            ),
            "business_domain": (
                doc["business_domain"]["name"] if doc.get("business_domain") else None
            ),
            "records": len(scope_records),
            "dimension_projection_candidates": len(doc["dimensions"]),
            "dimension_projections_published": sum(r.metadata.get("type") == "scoped_dimension" for r in scope_records),
        })
        for record in scope_records:
            deduped.setdefault(record.id, record)
    records = list(deduped.values())
    previous_ids = set(store.get_ids_by_where(scope_where))
    current_ids = {record.id for record in records}

    # 3. upsert 新快照成功后，再删除新快照中已不存在的旧记录。
    store.add(records)
    stale_ids = sorted(previous_ids - current_ids)
    store.delete_by_ids(stale_ids)

    type_counts: dict[str, int] = {}
    for r in records:
        t = r.metadata.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "total": len(records),
        "by_type": type_counts,
        "deleted": len(stale_ids),
        "semantic_model": docs[0]["semantic_model"]["name"],
        "business_domain": (
            docs[0]["business_domain"]["name"]
            if business_domain_id is not None and docs[0]["business_domain"]
            else None
        ),
        "by_scope": scope_stats,
        "persist_dir": str(store.persist_dir),
    }


# ============================ 物理表 / 字段 向量化（数据源作用域） ============================
#
# 作用域维度：data_source_id（数据源隔离）；semantic_model_id 可空作为附加过滤。
# 与业务域 DSL 互不影响：metadata.type 为 "table" / "field"，避免与 entity/attribute 冲突。
# 字段记录通过 parent=table_name 关联到所属表（参考 attribute 通过 parent=entity_code 关联）。

def _build_table_text(table: dict) -> str:
    """表向量化字段: table_name + description + comment"""
    return _join_parts(
        table.get("table_name"),
        table.get("description"),
        table.get("comment"),
    )


def _build_field_text(field: dict) -> str:
    """字段向量化字段: field_name + data_type + comment + description + enum_values"""
    return _join_parts(
        field.get("field_name"),
        field.get("data_type"),
        field.get("comment"),
        field.get("description"),
        field.get("enum_values"),
    )


def build_records_from_tables(doc: dict, embed_fn) -> list[VectorRecord]:
    """从数据源作用域文档构建向量记录，metadata 注入 data_source_id。

    Args:
        doc: get_table_field_by_scope() / load_all_table_fields() 中单个作用域文档
        embed_fn: 批量嵌入函数 list[str] -> list[list[float]]

    Note:
        - 表 ID: `ds{ds_id}:table:{table_name}`
        - 字段 ID: `ds{ds_id}:field:{table_name}.{field_name}`
        - semantic_model_id 为 None 时记为 -1（chroma 不支持 None 作为 where 值）
        - 字段通过 parent/parent_name 关联到表（参考 attribute 模式）
    """
    ds_id = doc["scope"]["data_source_id"]
    if ds_id is None:
        # 没有数据源归属的记录无法稳定去重，跳过向量化
        return []
    sm_id = doc["scope"]["semantic_model_id"]
    sm_id_meta = -1 if sm_id is None else sm_id

    pending: list[tuple[str, dict]] = []
    for table in doc["tables"]:
        pending.append((_build_table_text(table), {"kind": "table", "obj": table}))
        for field in table.get("fields") or []:
            pending.append((_build_field_text(field), {
                "kind": "field",
                "obj": field,
                "table_name": table.get("table_name"),
            }))

    texts = [t for t, _ in pending]
    vectors = embed_fn(texts) if texts else []
    if len(vectors) != len(pending):
        raise RuntimeError(
            "Embedding 返回数量不一致: "
            f"expected={len(pending)}, actual={len(vectors)}"
        )

    records: list[VectorRecord] = []
    for (text, ctx), vec in zip(pending, vectors):
        kind = ctx["kind"]
        obj = ctx["obj"]
        table_name = obj.get("table_name") if kind == "table" else ctx.get("table_name")

        if kind == "table":
            rid = f"ds{ds_id}:table:{table_name or uuid.uuid4().hex[:8]}"
            meta = {**obj, "type": "table"}
        elif kind == "field":
            parent = ctx.get("table_name") or "unknown"
            field_name = obj.get("field_name") or uuid.uuid4().hex[:8]
            rid = f"ds{ds_id}:field:{parent}.{field_name}"
            meta = {
                **obj,
                "type": "field",
                "parent": ctx.get("table_name"),
                "parent_name": ctx.get("table_name"),
            }
        else:
            continue

        # 注入作用域元数据（检索时用于 where 过滤）
        meta["data_source_id"] = ds_id
        meta["semantic_model_id"] = sm_id_meta

        meta = {k: v for k, v in meta.items() if v is not None}
        records.append(VectorRecord(id=rid, text=text, vector=vec, metadata=meta))

    # 按 id 去重（同一作用域内 table_name + field_name 重复时保留首份）
    deduped: dict[str, VectorRecord] = {}
    for r in records:
        if r.id not in deduped:
            deduped[r.id] = r
    return list(deduped.values())


def build_table_field_scope_filter(
    data_source_id: int,
    semantic_model_id: int = None,
) -> dict:
    """构建物理表/字段检索的 Chroma where 过滤条件。

    - 必须指定 data_source_id（主隔离维度）
    - semantic_model_id 可选；指定时匹配该 sm 或 -1（未绑定具体语义建模的物理表）

    Args:
        data_source_id: 数据源 ID（必填）
        semantic_model_id: 语义建模 ID（可选）

    Returns:
        chroma where 字典
    """
    if semantic_model_id is None:
        return {
            "$and": [
                {"type": {"$in": sorted(PHYSICAL_RECORD_TYPES)}},
                {"data_source_id": data_source_id},
            ]
        }
    return {
        "$and": [
            {"type": {"$in": sorted(PHYSICAL_RECORD_TYPES)}},
            {"data_source_id": data_source_id},
            {"semantic_model_id": {"$in": [semantic_model_id, -1]}},
        ]
    }


def rebuild_index_for_tables(store: ChromaVectorStore, embed_fn) -> dict:
    """从 MySQL 全量重建物理表/字段向量索引（先清空 table/field 类型记录再写入）。

    遍历所有 data_source 作用域，逐个构建记录后批量写入。
    metadata 中带 data_source_id，检索时按作用域过滤。

    Args:
        store: 向量存储实例
        embed_fn: 批量嵌入函数 list[str] -> list[list[float]]

    Returns:
        统计信息 dict
    """
    from mysql_tool import load_all_table_fields

    docs = load_all_table_fields()

    # 先构建完整新快照，数据库读取或 Embedding 失败时不触碰旧索引。
    # 按 ID 去重：同 data_source 下表名唯一，单作用域内已去重。
    deduped: dict[str, VectorRecord] = {}
    scope_stats: list[dict] = []
    for doc in docs:
        records = build_records_from_tables(doc, embed_fn)
        scope_stats.append({
            "data_source_id": doc["scope"]["data_source_id"],
            "semantic_model_id": doc["scope"]["semantic_model_id"],
            "records": len(records),
        })
        for r in records:
            if r.id not in deduped:
                deduped[r.id] = r

    all_records = list(deduped.values())
    scope_where = {"type": {"$in": ["table", "field"]}}
    previous_ids = set(store.get_ids_by_where(scope_where))
    current_ids = {record.id for record in all_records}
    store.add(all_records)
    stale_ids = sorted(previous_ids - current_ids)
    store.delete_by_ids(stale_ids)

    type_counts: dict[str, int] = {}
    for r in all_records:
        t = r.metadata.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "total": len(all_records),
        "by_type": type_counts,
        "deleted": len(stale_ids),
        "by_scope": scope_stats,
        "persist_dir": str(store.persist_dir),
    }


def rebuild_index_for_tables_by_scope(
    store: ChromaVectorStore,
    embed_fn,
    data_source_id: int,
    semantic_model_id: int = None,
) -> dict:
    """按数据源作用域增量重建物理表/字段向量索引。

    Args:
        store: 向量存储实例
        embed_fn: 批量嵌入函数 list[str] -> list[list[float]]
        data_source_id: 数据源 ID（必填）
        semantic_model_id: 语义建模 ID（可选，附加过滤）

    Returns:
        统计信息 dict（含 deleted 字段表示本次删除的历史记录数）
    """
    from mysql_tool import get_table_field_by_scope

    # 1. 构造作用域 where 条件
    scope_where = build_table_field_scope_filter(data_source_id, semantic_model_id)

    # 2. 先构建完整新快照，避免数据库或 Embedding 失败时丢失旧索引。
    doc = get_table_field_by_scope(semantic_model_id=semantic_model_id, data_source_id=data_source_id)
    records = build_records_from_tables(doc, embed_fn)
    previous_ids = set(store.get_ids_by_where(scope_where))
    current_ids = {record.id for record in records}
    store.add(records)
    stale_ids = sorted(previous_ids - current_ids)
    store.delete_by_ids(stale_ids)

    type_counts: dict[str, int] = {}
    for r in records:
        t = r.metadata.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "total": len(records),
        "by_type": type_counts,
        "deleted": len(stale_ids),
        "data_source_id": data_source_id,
        "semantic_model_id": semantic_model_id,
        "persist_dir": str(store.persist_dir),
    }


# ============================ 每日业务表全量向量同步 ============================

def _daily_value(value: Any) -> str:
    """将数据库值稳定转换为可向量化/可存 metadata 的文本。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def build_records_from_entity_attributes(
    rows: Sequence[dict],
    embed_fn,
) -> list[VectorRecord]:
    """Convert a governed entity-value snapshot into isolated vector records."""
    pending: list[tuple[str, dict, str]] = []
    seen: set[str] = set()
    required = ("id", "semantic_model_id", "business_domain_id", "entity_name", "attr_name", "attr_code", "attr_value")
    for row_number, row in enumerate(rows, start=1):
        missing = [name for name in required if row.get(name) is None or not str(row.get(name)).strip()]
        if missing:
            raise ValueError(f"entity value snapshot row {row_number} is missing: {missing}")
        source_id = str(row["id"]).strip()
        entity_code = str(row.get("entity_code") or row.get("entity_name") or "").strip()
        normalized_attribute_value = normalize_vector_text(row["attr_value"])
        canonical_identity = "\x1f".join((
            str(row["semantic_model_id"]), str(row["business_domain_id"]),
            entity_code, str(row["attr_code"]).strip(),
            normalized_attribute_value.casefold(),
        ))
        stable_digest = hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()
        record_id = f"entity-attr-value:{stable_digest}"
        if record_id in seen:
            raise ValueError(f"entity value snapshot source id is duplicated: {source_id}")
        seen.add(record_id)
        text = "\n".join(filter(None, (
            f"实体名称: {_daily_value(row.get('entity_name')).strip()}",
            f"实体别名: {_daily_value(row.get('entity_alias')).strip()}" if row.get("entity_alias") else "",
            f"实体描述: {_daily_value(row.get('entity_description')).strip()}" if row.get("entity_description") else "",
            f"属性名称: {_daily_value(row.get('attr_name')).strip()}",
            f"属性编码: {_daily_value(row.get('attr_code')).strip()}",
            f"属性描述: {_daily_value(row.get('attr_description')).strip()}" if row.get("attr_description") else "",
            f"属性值: {normalized_attribute_value}",
        )))
        metadata = {
            "type": ENTITY_ATTRIBUTE_VALUE_TYPE,
            "source_table": "published_entity_attributes",
            "source_id": source_id,
            "semantic_model_id": int(row["semantic_model_id"]),
            "business_domain_id": int(row["business_domain_id"]),
            "data_source_id": int(row.get("data_source_id") or -1),
            "entity_code": entity_code,
            "entity_name": _daily_value(row["entity_name"]),
            "attr_name": _daily_value(row["attr_name"]),
            "attr_code": _daily_value(row["attr_code"]),
            "attr_value": _daily_value(row["attr_value"]),
            "canonical_value": normalized_attribute_value,
            "source_field": _daily_value(row.get("source_field")),
            "snapshot_version": _daily_value(row.get("snapshot_version") or "legacy"),
        }
        if row.get("source_table"):
            metadata["source_table"] = _daily_value(row["source_table"])
        for optional in ("entity_alias", "entity_description", "attr_description", "create_time", "update_time"):
            if row.get(optional) is not None:
                metadata[optional] = _daily_value(row[optional])
        pending.append((text, metadata, record_id))

    vectors = embed_fn([text for text, _, _ in pending]) if pending else []
    if len(vectors) != len(pending):
        raise RuntimeError(f"Embedding 返回数量不一致: expected={len(pending)}, actual={len(vectors)}")
    return [VectorRecord(id=rid, text=text, vector=vec, metadata=meta)
            for (text, meta, rid), vec in zip(pending, vectors)]


def replace_entity_attribute_index(
    store: ChromaVectorStore,
    embed_fn,
    *,
    semantic_model_id: int,
    business_domain_id: int,
) -> dict:
    """按语义模型/业务域安全全量覆盖实体属性值索引。"""
    import mysql_tool

    loader = getattr(
        mysql_tool,
        "load_complete_entity_attribute_vector_source",
        mysql_tool.load_entity_attribute_vector_source,
    )
    rows = loader(semantic_model_id, business_domain_id)
    policy_loader = getattr(
        mysql_tool, "load_entity_attribute_vector_policy_audit", None
    )
    policy_audit = (
        policy_loader(semantic_model_id, business_domain_id)
        if callable(policy_loader)
        else {"included_attributes": [], "excluded_attributes": []}
    )
    per_attribute = Counter(
        (str(row.get("entity_code") or ""), str(row.get("attr_code") or ""))
        for row in rows
    )
    by_attribute = [
        {
            "entity_code": entity_code,
            "attr_code": attr_code,
            "indexed_rows": count,
        }
        for (entity_code, attr_code), count in sorted(per_attribute.items())
    ]
    snapshot_version = hashlib.sha256(
        "\n".join(sorted(str(row["id"]) for row in rows)).encode("utf-8")
    ).hexdigest()[:24]
    for row in rows:
        row["snapshot_version"] = snapshot_version
    scope_where = {"$and": [
        {"type": ENTITY_ATTRIBUTE_VALUE_TYPE},
        {"semantic_model_id": semantic_model_id},
        {"business_domain_id": business_domain_id},
    ]}
    previous_ids = set(store.get_ids_by_where(scope_where))
    if not rows:
        # 空作用域是一个合法的权威快照。若从未入过库则直接 SKIPPED；若旧索引
        # 仍有记录，说明数据侧已删除全部配置，必须清理陈旧向量，避免问题改写
        # 继续命中已经删除的别名/属性值。
        stale_ids = sorted(previous_ids)
        store.delete_by_ids(stale_ids)
        return {
            "source_table": "published_entity_attributes",
            "semantic_model_id": semantic_model_id,
            "business_domain_id": business_domain_id,
            "status": "CLEARED" if stale_ids else "SKIPPED",
            "code": (
                "ENTITY_ATTRIBUTES_CLEARED"
                if stale_ids
                else "NO_ENTITY_ATTRIBUTES"
            ),
            "received_rows": 0,
            "indexed_rows": 0,
            "overwritten_rows": 0,
            "deleted_rows": len(stale_ids),
            "by_attribute": by_attribute,
            "included_attributes": policy_audit["included_attributes"],
            "excluded_attributes": policy_audit["excluded_attributes"],
            "persist_dir": str(store.persist_dir),
        }
    records = build_records_from_entity_attributes(rows, embed_fn)
    current_ids = {record.id for record in records}
    store.add(records)
    stale_ids = sorted(previous_ids - current_ids)
    store.delete_by_ids(stale_ids)
    return {
        "source_table": "published_entity_attributes",
        "semantic_model_id": semantic_model_id,
        "business_domain_id": business_domain_id,
        "status": "SUCCEEDED",
        "code": "ENTITY_ATTRIBUTES_INDEXED",
        "received_rows": len(rows),
        "indexed_rows": len(records),
        "overwritten_rows": len(previous_ids & current_ids),
        "deleted_rows": len(stale_ids),
        "by_attribute": by_attribute,
        "included_attributes": policy_audit["included_attributes"],
        "excluded_attributes": policy_audit["excluded_attributes"],
        "persist_dir": str(store.persist_dir),
    }


def build_records_from_daily_table(
    rows: Sequence[dict],
    embed_fn,
    *,
    table_name: str,
    id_field: str,
    text_fields: Sequence[str],
    metadata_fields: Sequence[str] = (),
) -> list[VectorRecord]:
    """把每日源表行转换为稳定 ID 的向量记录。"""
    pending: list[tuple[str, dict, str]] = []
    seen_ids: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        source_id = row.get(id_field)
        if source_id is None or not str(source_id).strip():
            raise ValueError(f"{table_name} 第 {row_number} 行缺少主键字段 {id_field}")
        raw_id = str(source_id).strip()
        digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()
        record_id = f"daily:{table_name}:{digest}"
        if record_id in seen_ids:
            raise ValueError(f"{table_name} 主键重复: {raw_id}")
        seen_ids.add(record_id)

        text_parts = [_daily_value(row.get(field)).strip() for field in text_fields]
        text = "\n".join(part for part in text_parts if part)
        if not text:
            raise ValueError(
                f"{table_name} 主键 {raw_id} 的向量字段均为空: {list(text_fields)}"
            )
        metadata = {
            "type": DAILY_TABLE_VECTOR_TYPE,
            "source_table": table_name,
            "source_id": raw_id,
        }
        for field_name in metadata_fields:
            value = row.get(field_name)
            if value is not None:
                metadata[field_name] = _daily_value(value)
        pending.append((text, metadata, record_id))

    texts = [text for text, _, _ in pending]
    vectors = embed_fn(texts) if texts else []
    if len(vectors) != len(pending):
        raise RuntimeError(
            f"Embedding 返回数量不一致: expected={len(pending)}, actual={len(vectors)}"
        )
    return [
        VectorRecord(id=record_id, text=text, vector=vector, metadata=metadata)
        for (text, metadata, record_id), vector in zip(pending, vectors)
    ]


def replace_daily_table_index(
    store: ChromaVectorStore,
    embed_fn,
    *,
    table_name: str,
    id_field: str,
    text_fields: Sequence[str],
    metadata_fields: Sequence[str] = (),
) -> dict:
    """读取指定表并全量覆盖其向量记录。

    安全顺序：读取并校验全部源数据 → 生成全部向量 → upsert 新快照 → 删除旧快照中
    已不存在的 ID。读取、校验或 Embedding 失败时不会删除旧索引。空表默认拒绝覆盖，
    防止上游异常把有效索引误清空。
    """
    from mysql_tool import load_daily_vector_source

    rows = load_daily_vector_source(
        table_name=table_name,
        id_field=id_field,
        text_fields=tuple(text_fields),
        metadata_fields=tuple(metadata_fields),
    )
    if not rows:
        raise ValueError(f"每日同步源表 {table_name} 为空，已保留原向量数据")

    records = build_records_from_daily_table(
        rows,
        embed_fn,
        table_name=table_name,
        id_field=id_field,
        text_fields=text_fields,
        metadata_fields=metadata_fields,
    )
    scope_where = {
        "$and": [
            {"type": DAILY_TABLE_VECTOR_TYPE},
            {"source_table": table_name},
        ]
    }
    previous_ids = set(store.get_ids_by_where(scope_where))
    current_ids = {record.id for record in records}

    # 先 upsert 完整的新快照；成功后才清理本次已不存在的旧行。
    store.add(records)
    stale_ids = sorted(previous_ids - current_ids)
    store.delete_by_ids(stale_ids)

    return {
        "source_table": table_name,
        "received_rows": len(rows),
        "indexed_rows": len(records),
        "overwritten_rows": len(previous_ids & current_ids),
        "deleted_rows": len(stale_ids),
        "persist_dir": str(store.persist_dir),
    }


if __name__ == "__main__":
    from embedding import embed_documents, embed_query

    print("=" * 60)
    print("构建 Chroma 向量索引（按 语义建模 × 业务域 隔离）")
    print("=" * 60)
    store = ChromaVectorStore()

    stats = rebuild_index(store, embed_documents)
    print(f"索引构建完成: {json.dumps(stats, ensure_ascii=False, indent=2)}")

    print()
    print("=" * 60)
    print("检索测试（限定 sm=6 商超业务数据, bd=7 销售交易域）")
    print("=" * 60)
    queries = [
        "今年小程序渠道的支付GMV是多少？",
        "各渠道的订单量",
        "退款金额",
    ]
    where = build_scope_filter(semantic_model_id=6, business_domain_id=7)
    print(f"过滤条件: {where}\n")
    for q in queries:
        print(f"[Query] {q}")
        vec = embed_query(q)
        results = store.search(vec, top_k=5, where=where)
        for r in results:
            name = (
                r.metadata.get("name")
                or r.metadata.get("entity_name")
                or r.metadata.get("metric_name")
                or r.metadata.get("dim_name")
                or ""
            )
            bd = r.metadata.get("business_domain_name") or "(跨域)"
            print(f"  score={r.score:.4f}  {r.id:50s}  {bd:10s}  {name}")
        print()
