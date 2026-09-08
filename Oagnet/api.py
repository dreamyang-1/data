"""FastAPI 服务 - 提供向量库重建与自然语言转 DSL 接口。

启动:
    uvicorn api:app --host 0.0.0.0 --port 8021 --reload
"""
from __future__ import annotations

import threading
import uuid
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

from asl_contract import ASLValidationError, IntentASLContract
from scope_contract import CONTRACT_VERSION, normalize_domains, require_candidate_scope, semantic_record_types

from capacity_control import (
    AslCapacityController,
    AslCapacityExhausted,
    AslGenerationDeadlineExceeded,
    AslUpstreamRateLimited,
    build_asl_capacity_policy,
)
from embedding import embed_documents, embed_query
# 复用 agent 模块的 store 实例，确保 clear/rebuild 后引用一致
from agent import main, store as _store
from config import (
    DAILY_JOB_LEASE_SECONDS,
    ENTITY_SYNC_WAIT_SECONDS,
    LLM_MAX_CONCURRENCY,
    LLM_QUEUE_TIMEOUT_SECONDS,
    LLM_TIMEOUT_SECONDS,
    OAGNET_HOST,
    OAGNET_PORT,
    VECTOR_STORE_BACKEND,
)
from daily_job_store import DailyJobStoreError, daily_job_store
from logger import logger
from mysql_tool import mysql_advisory_lock, normalize_catalog_text
from vector_store import (
    rebuild_index_by_scope,
    replace_daily_table_index,
    replace_entity_attribute_index,
)

_daily_sync_lock = threading.Lock()
_entity_state_lock = threading.Lock()
_entity_sync_results: dict[tuple[int, int, str], dict] = {}
_last_entity_sync: dict[tuple[int, int], dict] = {}
_entity_sync_inflight: dict[tuple[int, int, str], threading.Event] = {}
_ASL_REQUEST_DEADLINE_SECONDS = 85
_asl_capacity = AslCapacityController(
    build_asl_capacity_policy(
        max_concurrency=LLM_MAX_CONCURRENCY,
        queue_timeout_seconds=LLM_QUEUE_TIMEOUT_SECONDS,
        llm_timeout_seconds=LLM_TIMEOUT_SECONDS,
        request_deadline_seconds=_ASL_REQUEST_DEADLINE_SECONDS,
    )
)

StrictPositiveInt = Annotated[StrictInt, Field(gt=0)]

app = FastAPI(title="Oagnet API", version="1.6.0")


@app.exception_handler(HTTPException)
async def http_exception_with_success_flag(
    _request: Request, exc: HTTPException
) -> JSONResponse:
    """Keep FastAPI's existing detail contract and add a stable failure flag."""
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder({"success": False, "detail": exc.detail}),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_with_success_flag(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({
            "success": False,
            "code": ('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED'
                     if any('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED' in str(e.get('msg', '')) for e in exc.errors())
                     else 'REQUEST_SCOPE_INVALID'
                     if any(any(part in {'semantic_model_id', 'business_domain_id', 'business_domain_ids'}
                                for part in e.get('loc', ())) or 'REQUEST_SCOPE_INVALID' in str(e.get('msg', ''))
                            for e in exc.errors()) else 'REQUEST_INVALID'),
            "detail": exc.errors(),
        }),
    )


@app.get("/", include_in_schema=False)
def service_info():
    """根路径返回服务入口，避免浏览器访问时误以为404代表未启动。"""
    return {
        "service": "Oagnet API",
        "status": "UP",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "vector_backend": VECTOR_STORE_BACKEND,
    }


@app.get("/vector/health")
def vector_health():
    """Report vector readiness without exposing connection credentials."""
    try:
        if hasattr(_store, "health_check"):
            return {"success": True, **_store.health_check()}
        return {
            "success": True,
            "backend": VECTOR_STORE_BACKEND,
            "healthy": True,
            "collection": getattr(_store, "collection_name", None),
        }
    except Exception as exc:
        logger.exception("vector backend health check failed")
        raise HTTPException(
            status_code=503,
            detail={"code": "VECTOR_BACKEND_UNAVAILABLE", "message": str(exc)},
        ) from exc

# ------------------------------------------------------------------
# 1. 重建向量库
# ------------------------------------------------------------------

class RebuildRequest(BaseModel):
    semantic_model_id: StrictPositiveInt
    business_domain_id: StrictPositiveInt | None = None


class RebuildResponse(BaseModel):
    success: bool
    message: str
    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)


@app.post("/vector/rebuild", response_model=RebuildResponse)
def vector_rebuild(req: RebuildRequest):
    """按作用域重建向量索引（只清并写入指定 sm/bd 的数据，不影响其他作用域）"""
    try:
        # Model-wide and per-domain rebuilds touch shared dimension records.
        # Use one model-level lock so these two forms cannot race each other.
        lock_name = f"oagnet:dsl:{req.semantic_model_id}"
        with mysql_advisory_lock(lock_name[:64]):
            stats = rebuild_index_by_scope(
                _store,
                embed_documents,
                semantic_model_id=req.semantic_model_id,
                business_domain_id=req.business_domain_id,
            )
        logger.info(
            f"向量库按作用域重建: sm={req.semantic_model_id}, bd={req.business_domain_id}, {stats}"
        )
        return RebuildResponse(
            success=True,
            message="向量库重建成功",
            total=stats.get("total", 0),
            by_type=stats.get("by_type", {}),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "VECTOR_SOURCE_INVALID", "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "VECTOR_REBUILD_BUSY", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception("vector rebuild failed")
        raise HTTPException(
            status_code=500,
            detail={"code": "VECTOR_REBUILD_FAILED", "message": "vector rebuild failed"},
        ) from exc


# ------------------------------------------------------------------
# 1.1 实体属性值按语义作用域覆盖向量化
# ------------------------------------------------------------------

class EntityAttributeVectorRequest(BaseModel):
    semantic_model_id: StrictPositiveInt
    business_domain_id: StrictPositiveInt


class EntityAttributeCount(BaseModel):
    entity_code: str
    attr_code: str
    indexed_rows: int = 0


class EntityAttributePolicyItem(BaseModel):
    entity_code: str
    entity_name: str = ""
    attr_code: str
    attr_name: str = ""
    mapping_table: str = ""
    mapping_column: str = ""
    vectorization: bool = False
    is_main_attribute: bool = False
    reason: str


class EntityAttributeVectorResponse(BaseModel):
    success: bool
    status: str = "SUCCEEDED"
    code: str = "ENTITY_ATTRIBUTES_INDEXED"
    message: str
    semantic_model_id: int
    business_domain_id: int
    received_rows: int = 0
    indexed_rows: int = 0
    overwritten_rows: int = 0
    deleted_rows: int = 0
    by_attribute: list[EntityAttributeCount] = Field(default_factory=list)
    included_attributes: list[EntityAttributePolicyItem] = Field(default_factory=list)
    excluded_attributes: list[EntityAttributePolicyItem] = Field(default_factory=list)


class EntityAttributeSyncRequest(EntityAttributeVectorRequest):
    event_id: str = Field(
        min_length=1,
        max_length=128,
        description="数据侧本次更新批次/事件的唯一ID；重试时必须保持不变",
    )

    @field_validator("event_id")
    @classmethod
    def normalize_event_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("event_id 不能为空")
        return normalized


class EntityAttributeSyncResponse(EntityAttributeVectorResponse):
    event_id: str
    completed_at: str


class EntityAttributeSearchRequest(BaseModel):
    semantic_model_id: StrictPositiveInt
    business_domain_id: StrictPositiveInt | None = Field(
        default=None,
        gt=0,
        description="可选；为空时在整个语义模型的全部业务域中检索",
    )
    business_domain_ids: list[StrictPositiveInt] = Field(
        default_factory=list,
        max_length=50,
        description="可选；当前支持空数组或一个不同业务域。多个显式域会被拒绝；单数字段用于兼容旧调用",
    )
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized

    @field_validator("business_domain_ids", mode="before")
    @classmethod
    def normalize_business_domain_ids(cls, value):
        if not isinstance(value, list):
            raise ValueError("business_domain_ids must be an array")
        if any(type(item) is not int or item <= 0 for item in value):
            raise ValueError("business_domain_ids must contain positive integers")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def reconcile_business_domain_scope(self):
        self.business_domain_ids = normalize_domains(
            self.business_domain_id,
            self.business_domain_ids if 'business_domain_ids' in self.model_fields_set else None,
        )
        return self


class EntityAttributeSearchMatch(BaseModel):
    record_id: str
    score: float = Field(ge=0.0, le=1.0)
    entity_name: str
    entity_alias: list[str] = Field(default_factory=list)
    attribute_name: str
    attribute_code: str
    attribute_value: str
    business_domain_id: int = Field(gt=0)
    entity_description: str | None = None
    attribute_description: str | None = None


class EntityAttributeSearchResponse(BaseModel):
    success: bool
    semantic_model_id: int
    business_domain_id: int | None
    business_domain_ids: list[int] = Field(default_factory=list)
    query: str
    matches: list[EntityAttributeSearchMatch]


class SemanticDisplayCandidate(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=100)
    slot: Literal["metric", "entity", "dimension", "field", "filter"]
    value: str = Field(min_length=1, max_length=300)
    field_name: str | None = Field(default=None, max_length=300)

    @field_validator("candidate_id", "value")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("semantic display candidate must not be empty")
        return normalized


class SemanticDisplayResolveRequest(BaseModel):
    semantic_model_id: StrictPositiveInt
    business_domain_ids: list[StrictPositiveInt] = Field(default_factory=list, max_length=50)
    candidates: list[SemanticDisplayCandidate] = Field(max_length=100)

    @field_validator('business_domain_ids')
    @classmethod
    def supported_domains(cls, values):
        return normalize_domains(business_domain_ids=values)


class SemanticDisplayMatch(BaseModel):
    semantic_model_id: int
    business_domain_id: int | None
    candidate_id: str
    slot: Literal["metric", "entity", "dimension", "field", "filter"]
    input_value: str
    canonical_name: str
    canonical_value: str | None = None
    canonical_code: str | None = None
    parent_name: str | None = None
    record_id: str
    score: float = Field(ge=0.0, le=1.0)


class SemanticDisplayResolveResponse(BaseModel):
    scope_contract_version: Literal['1.0'] = CONTRACT_VERSION
    success: bool
    semantic_model_id: int
    business_domain_ids: list[int] = Field(default_factory=list)
    matches: list[SemanticDisplayMatch]


def _semantic_term_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        decoded = __import__("json").loads(text)
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item).strip()]
    except (TypeError, ValueError):
        pass
    return [item.strip() for item in re.split(r"[,，;/；|]", text) if item.strip()]


def _semantic_display_key(value: Any) -> str:
    return re.sub(r"[\s,，.。;；:：()（）\[\]{}'\"“”‘’_\-]+", "", str(value or "")).casefold()


def _semantic_filter_field_matches(field_name: str | None, metadata: dict[str, Any]) -> bool:
    """Bind a filter value to its requested semantic attribute.

    Values such as ``1`` occur in many business columns.  A vector hit is not
    valid unless its attribute name/code also agrees with the source filter.
    Older callers may omit the hint and retain the previous exact-value
    behaviour for API compatibility.
    """
    if not field_name:
        return True
    expected = _semantic_display_key(field_name)
    terms = {
        _semantic_display_key(metadata.get(key))
        for key in ("attr_name", "attr_code", "source_field", "field_mapping")
        if _semantic_display_key(metadata.get(key))
    }
    if not expected or not terms:
        return False
    return expected in terms or any(
        len(term) >= 2 and (expected in term or term in expected)
        for term in terms
    )


_SEMANTIC_DISPLAY_SPECS = {
    "metric": ("metric", "metric_name", "metric_code", ("metric_name", "metric_code", "synonyms")),
    "entity": ("entity", "entity_name", "entity_code", ("entity_name", "entity_code", "entity_alias")),
    "dimension": ("dimension", "dim_name", "dim_code", ("dim_name", "dim_code", "synonyms")),
    "field": ("attribute", "attr_name", "attr_code", ("attr_name", "attr_code")),
    "filter": ("entity_attribute_value", "attr_name", "attr_code", ("attr_value",)),
}


@app.post(
    "/vector/semantic-elements/resolve",
    response_model=SemanticDisplayResolveResponse,
)
def semantic_display_elements_resolve(
    req: SemanticDisplayResolveRequest,
) -> SemanticDisplayResolveResponse:
    """Resolve display-only intent slots against the current vector snapshot.

    Vector similarity is used for retrieval only. A value is returned solely
    when it also exactly matches a canonical name/code/value or registered
    alias after harmless punctuation normalization. This keeps ungrounded LLM
    candidates out of user-visible intent diagnostics.
    """
    matches: list[SemanticDisplayMatch] = []
    domain_ids = req.business_domain_ids
    for candidate in req.candidates:
        record_type, name_key, code_key, term_keys = _SEMANTIC_DISPLAY_SPECS[candidate.slot]
        clauses: list[dict[str, Any]] = [
            {"semantic_model_id": req.semantic_model_id},
            {"type": semantic_record_types(record_type, domain_ids)},
        ]
        if req.business_domain_ids:
            clauses.append({"business_domain_id": {"$in": domain_ids}})
        where = {"$and": clauses}
        results = _store.search(embed_query(candidate.value), top_k=20, where=where)
        if candidate.slot == "filter" and hasattr(_store, "find_exact"):
            exact_where = {
                "$and": [*clauses, {"canonical_value": candidate.value.strip()}]
            }
            by_id = {item.id: item for item in results}
            for item in _store.find_exact(exact_where):
                item.score = 1.0
                by_id[item.id] = item
            results = sorted(
                by_id.values(), key=lambda item: item.score, reverse=True
            )[:20]
        input_key = _semantic_display_key(candidate.value)
        exact: list[SemanticDisplayMatch] = []
        exact_specificity: list[int] = []
        for result in results:
            metadata = result.metadata or {}
            try:
                require_candidate_scope(metadata, req.semantic_model_id, domain_ids)
            except ValueError as exc:
                raise HTTPException(502, detail={'code': 'SEMANTIC_SCOPE_MISMATCH'}) from exc
            if (
                candidate.slot == "filter"
                and not _semantic_filter_field_matches(candidate.field_name, metadata)
            ):
                continue
            terms: list[str] = []
            for key in term_keys:
                terms.extend(_semantic_term_values(metadata.get(key)))
            term_keys_normalized = {
                _semantic_display_key(term) for term in terms if _semantic_display_key(term)
            }
            literal_match = input_key in term_keys_normalized
            match_specificity = len(input_key) if literal_match else 0
            if candidate.slot == "metric" and not literal_match:
                # Intent models sometimes keep only a generic suffix such as
                # ``覆盖率``.  DataAnalysis also supplies the bounded original
                # question as a metric-context candidate; accept only a
                # complete registered metric name/alias contained in that
                # context.  Deliberately do not accept the inverse direction,
                # otherwise the generic suffix would again masquerade as a
                # canonical catalog term.
                contextual_terms = [
                    term_key
                    for term_key in term_keys_normalized
                    if len(term_key) >= 2 and term_key in input_key
                ]
                literal_match = bool(contextual_terms)
                match_specificity = max(
                    (len(term_key) for term_key in contextual_terms),
                    default=0,
                )
            if candidate.slot == "filter" and not literal_match and len(input_key) >= 2:
                literal_match = any(
                    input_key in term_key or term_key in input_key
                    for term_key in term_keys_normalized
                )
            if not input_key or not literal_match:
                continue
            canonical_name = str(metadata.get(name_key) or "").strip()
            canonical_code = str(metadata.get(code_key) or "").strip() or None
            canonical_value = (
                str(metadata.get("attr_value") or "").strip() or None
                if candidate.slot == "filter"
                else None
            )
            if not canonical_name or (candidate.slot == "filter" and not canonical_value):
                continue
            exact.append(SemanticDisplayMatch(
                semantic_model_id=metadata['semantic_model_id'],
                business_domain_id=metadata.get('business_domain_id'),
                candidate_id=candidate.candidate_id,
                slot=candidate.slot,
                input_value=candidate.value,
                canonical_name=canonical_name,
                canonical_value=canonical_value,
                canonical_code=canonical_code,
                parent_name=str(
                    metadata.get("entity_name") or metadata.get("parent_name") or ""
                ).strip() or None,
                record_id=result.id,
                score=max(0.0, min(1.0, float(result.score))),
            ))
            exact_specificity.append(match_specificity)
        if candidate.slot == "metric" and exact_specificity:
            # A shorter alias can be nested inside the intended compound
            # metric (for example ``医院覆盖`` inside ``区域医院覆盖率``).
            # Keep only the longest registered lexical proof before applying
            # the ordinary multi-canonical ambiguity gate.
            strongest = max(exact_specificity)
            exact = [
                item
                for item, specificity in zip(exact, exact_specificity)
                if specificity == strongest
            ]
        canonical_keys = {
            (
                item.canonical_code or "",
                item.canonical_name,
                item.canonical_value or "",
                item.parent_name or "",
            )
            for item in exact
        }
        # Multiple canonical records for one surface form are ambiguous. They
        # are intentionally omitted here and handled by the clarification path.
        if len(canonical_keys) == 1 and exact:
            matches.append(max(exact, key=lambda item: item.score))
    return SemanticDisplayResolveResponse(
        success=True,
        semantic_model_id=req.semantic_model_id,
        business_domain_ids=domain_ids,
        matches=matches,
    )


def _entity_aliases(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    try:
        import json
        decoded = json.loads(text)
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item).strip()]
    except (TypeError, ValueError):
        pass
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]


@app.post(
    "/vector/entity-attributes/rebuild",
    response_model=EntityAttributeVectorResponse,
)
def entity_attribute_vector_rebuild(req: EntityAttributeVectorRequest):
    """覆盖指定语义模型/业务域的实体属性值向量，并供问题改写检索。"""
    try:
        lock_name = f"oagnet:entity-att:{req.semantic_model_id}:{req.business_domain_id}"
        with mysql_advisory_lock(lock_name[:64]):
            stats = replace_entity_attribute_index(
                _store,
                embed_documents,
                semantic_model_id=req.semantic_model_id,
                business_domain_id=req.business_domain_id,
            )
        logger.info(
            "实体属性值向量重建成功: sm=%s, bd=%s, received=%s, indexed=%s",
            req.semantic_model_id,
            req.business_domain_id,
            stats["received_rows"],
            stats["indexed_rows"],
        )
        return EntityAttributeVectorResponse(
            success=True,
            status=stats.get("status", "SUCCEEDED"),
            code=stats.get("code", "ENTITY_ATTRIBUTES_INDEXED"),
            message=(
                "当前业务域没有配置实体属性，无需更新向量；已有向量保持不变"
                if stats.get("status") == "SKIPPED"
                else (
                    "当前业务域实体属性已清空，陈旧向量已删除"
                    if stats.get("status") == "CLEARED"
                    else "实体属性值向量重建成功"
                )
            ),
            **{k: stats[k] for k in (
                "semantic_model_id", "business_domain_id", "received_rows",
                "indexed_rows", "overwritten_rows", "deleted_rows",
            )},
            by_attribute=stats.get("by_attribute", []),
            included_attributes=stats.get("included_attributes", []),
            excluded_attributes=stats.get("excluded_attributes", []),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "ENTITY_ATTRIBUTE_SOURCE_INVALID", "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "ENTITY_ATTRIBUTE_REBUILD_BUSY", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception("实体属性值向量重建失败")
        raise HTTPException(
            status_code=500,
            detail={"code": "ENTITY_ATTRIBUTE_REBUILD_FAILED", "message": "实体属性值向量重建失败"},
        ) from exc


@app.post(
    "/vector/entity-attributes/search",
    response_model=EntityAttributeSearchResponse,
)
def entity_attribute_vector_search(req: EntityAttributeSearchRequest):
    """检索实体别名及属性值；业务域为空时覆盖整个语义模型。"""
    try:
        clauses = [
            {"type": "entity_attribute_value"},
            {"semantic_model_id": req.semantic_model_id},
        ]
        if len(req.business_domain_ids) == 1:
            clauses.append({"business_domain_id": req.business_domain_ids[0]})
        elif req.business_domain_ids:
            clauses.append({"business_domain_id": {"$in": req.business_domain_ids}})
        where = {"$and": clauses}
        normalized_query = normalize_catalog_text(req.query)
        # Exact canonical values are authoritative and must not be displaced by
        # approximate nearest neighbours (notably short province/city names).
        exact_where = {
            "$and": [*clauses, {"canonical_value": normalized_query}]
        }
        exact = (
            _store.find_exact(exact_where)
            if hasattr(_store, "find_exact")
            else []
        )
        approximate = _store.search(
            embed_query(normalized_query), top_k=max(req.top_k, 20), where=where
        )
        # Milvus filters scalar columns but returns a separate metadata JSON.
        # Validate both result paths before merging/ranking or echoing scope.
        for item in [*exact, *approximate]:
            require_candidate_scope(item.metadata, req.semantic_model_id, req.business_domain_ids)
        by_id = {item.id: item for item in approximate}
        for item in exact:
            item.score = 1.0
            by_id[item.id] = item
        results = sorted(
            by_id.values(), key=lambda item: item.score, reverse=True
        )[:req.top_k]
        matches: list[EntityAttributeSearchMatch] = []
        for item in results:
            score = max(0.0, min(1.0, float(item.score)))
            if score < req.score_threshold:
                continue
            metadata = item.metadata
            matches.append(EntityAttributeSearchMatch(
                record_id=item.id,
                score=score,
                entity_name=str(metadata.get("entity_name") or ""),
                entity_alias=_entity_aliases(metadata.get("entity_alias")),
                attribute_name=str(metadata.get("attr_name") or ""),
                attribute_code=str(metadata.get("attr_code") or ""),
                attribute_value=str(metadata.get("attr_value") or ""),
                business_domain_id=int(metadata["business_domain_id"]),
                entity_description=(str(metadata["entity_description"]) if metadata.get("entity_description") else None),
                attribute_description=(str(metadata["attr_description"]) if metadata.get("attr_description") else None),
            ))
        return EntityAttributeSearchResponse(
            success=True,
            semantic_model_id=req.semantic_model_id,
            business_domain_id=req.business_domain_id,
            business_domain_ids=req.business_domain_ids,
            query=req.query,
            matches=matches,
        )
    except ValueError as exc:
        if str(exc).startswith('SEMANTIC_SCOPE_MISMATCH'):
            raise HTTPException(status_code=502, detail={
                'code': 'SEMANTIC_SCOPE_MISMATCH',
                'message': 'Entity attribute candidate scope could not be verified',
            }) from exc
        raise HTTPException(status_code=503, detail={
            'code': 'ENTITY_ATTRIBUTE_SEARCH_FAILED', 'message': 'Entity attribute search unavailable',
        }) from exc
    except Exception as exc:
        logger.exception(
            "实体属性值检索失败: sm=%s, bds=%s",
            req.semantic_model_id,
            req.business_domain_ids or None,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "ENTITY_ATTRIBUTE_SEARCH_FAILED", "message": "实体属性检索暂时不可用"},
        ) from exc


@app.post(
    "/vector/entity-attributes/sync",
    response_model=EntityAttributeSyncResponse,
)
def entity_attribute_vector_sync(req: EntityAttributeSyncRequest):
    """同步读取实体属性表、生成向量并覆盖索引，完成后才返回。"""
    scope = (req.semantic_model_id, req.business_domain_id)
    event_key = (*scope, req.event_id)
    with _entity_state_lock:
        cached = _entity_sync_results.get(event_key)
        if cached is not None:
            return EntityAttributeSyncResponse(**cached)
        completion = _entity_sync_inflight.get(event_key)
        is_owner = completion is None
        if completion is None:
            completion = threading.Event()
            _entity_sync_inflight[event_key] = completion

    # Concurrent retries for the same event share the first execution instead of
    # racing for the MySQL lock and returning a misleading 409.
    if not is_owner:
        if not completion.wait(ENTITY_SYNC_WAIT_SECONDS):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ENTITY_ATTRIBUTE_SYNC_IN_PROGRESS",
                    "message": "相同 event_id 的同步仍在执行，请稍后重试",
                },
            )
        with _entity_state_lock:
            cached = _entity_sync_results.get(event_key)
        if cached is not None:
            return EntityAttributeSyncResponse(**cached)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ENTITY_ATTRIBUTE_SYNC_RETRY_REQUIRED",
                "message": "相同 event_id 的上一次同步未成功，请重试",
            },
        )
    try:
        lock_name = f"oagnet:entity-att:{req.semantic_model_id}:{req.business_domain_id}"
        with mysql_advisory_lock(lock_name[:64]):
            stats = replace_entity_attribute_index(
                _store,
                embed_documents,
                semantic_model_id=req.semantic_model_id,
                business_domain_id=req.business_domain_id,
            )
        result = {
            "success": True,
            "status": stats.get("status", "SUCCEEDED"),
            "code": stats.get("code", "ENTITY_ATTRIBUTES_INDEXED"),
            "message": (
                "当前业务域没有配置实体属性，无需更新向量；已有向量保持不变"
                if stats.get("status") == "SKIPPED"
                else (
                    "当前业务域实体属性已清空，陈旧向量已删除"
                    if stats.get("status") == "CLEARED"
                    else "实体属性值读取、向量化和索引覆盖完成"
                )
            ),
            "event_id": req.event_id,
            "completed_at": datetime.now().astimezone().isoformat(),
            **{key: stats[key] for key in (
                "semantic_model_id", "business_domain_id", "received_rows",
                "indexed_rows", "overwritten_rows", "deleted_rows",
            )},
            "by_attribute": stats.get("by_attribute", []),
            "included_attributes": stats.get("included_attributes", []),
            "excluded_attributes": stats.get("excluded_attributes", []),
        }
        with _entity_state_lock:
            _entity_sync_results[event_key] = result
            _last_entity_sync[scope] = result
            while len(_entity_sync_results) > 500:
                _entity_sync_results.pop(next(iter(_entity_sync_results)))
        return EntityAttributeSyncResponse(**result)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "ENTITY_ATTRIBUTE_SOURCE_INVALID", "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "ENTITY_ATTRIBUTE_SYNC_BUSY", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception("实体属性值同步失败，旧索引已保留")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ENTITY_ATTRIBUTE_SYNC_FAILED",
                "message": "数据读取、校验或向量化失败，旧索引已保留",
            },
        ) from exc
    finally:
        with _entity_state_lock:
            marker = _entity_sync_inflight.pop(event_key, None)
            if marker is not None:
                marker.set()


@app.get("/vector/entity-attributes/status")
def entity_attribute_sync_status(
    semantic_model_id: int,
    business_domain_id: int,
):
    if semantic_model_id <= 0 or business_domain_id <= 0:
        raise HTTPException(status_code=422, detail="作用域ID必须为正整数")
    scope = (semantic_model_id, business_domain_id)
    with _entity_state_lock:
        return {
            "trigger_mode": "api_only",
            "execution_mode": "synchronous",
            "source_table": "published_entity_attributes",
            "semantic_model_id": semantic_model_id,
            "business_domain_id": business_domain_id,
            "active_job_id": None,
            "running": False,
            "last_success": _last_entity_sync.get(scope),
        }
# ------------------------------------------------------------------
# 2. 每日业务表全量覆盖向量同步
# ------------------------------------------------------------------

class DailyTableSyncAcceptedResponse(BaseModel):
    accepted: bool
    message: str
    job_id: str
    status: str
    source_table: str
    accepted_at: str
    event_id: str | None = None


class DailyTableSyncRequest(BaseModel):
    """由数据端提供本次需要全量覆盖向量索引的表结构。"""

    source_table: str = Field(
        min_length=1,
        max_length=64,
        description="MySQL 源表名，仅支持普通标识符，不支持库名、点号或 SQL 片段",
    )
    id_field: str = Field(
        min_length=1,
        max_length=64,
        description="能够唯一标识一行数据且非空的主键/唯一键字段",
    )
    text_fields: list[str] = Field(
        min_length=1,
        max_length=32,
        description="按顺序拼接后生成向量的文本字段",
    )
    metadata_fields: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="随向量保存、用于结果展示或过滤的标量字段",
    )
    event_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="数据侧事件唯一ID；同一事件重试时保持不变，可获得幂等结果",
    )

    @field_validator("source_table", "id_field")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("仅允许英文字母、数字和下划线，且不能以数字开头")
        return value

    @field_validator("text_fields", "metadata_fields")
    @classmethod
    def validate_field_list(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            field_name = value.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name):
                raise ValueError(f"非法字段名: {value!r}")
            if field_name in normalized:
                raise ValueError(f"字段重复: {field_name}")
            normalized.append(field_name)
        return normalized

    @field_validator("event_id")
    @classmethod
    def normalize_daily_event_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("event_id 不能为空")
        return normalized

class DailyTableSyncJobResponse(BaseModel):
    job_id: str
    status: str
    source_table: str
    event_id: str | None = None
    accepted_at: str
    started_at: str | None = None
    completed_at: str | None = None
    result: dict | None = None
    error: dict | None = None


def _run_daily_table_sync(trigger: str, source: DailyTableSyncRequest) -> dict:
    """串行执行一次由数据端触发的同步。"""
    if not _daily_sync_lock.acquire(blocking=False):
        raise RuntimeError("每日向量同步正在执行，请勿重复提交")
    try:
        lock_name = f"oagnet:daily-vector:{source.source_table}"
        with mysql_advisory_lock(lock_name[:64]):
            stats = replace_daily_table_index(
                _store,
                embed_documents,
                table_name=source.source_table,
                id_field=source.id_field,
                text_fields=source.text_fields,
                metadata_fields=source.metadata_fields,
            )
        result = {
            "success": True,
            "message": "已完成表内容读取和全量向量覆盖",
            **stats,
            "trigger": trigger,
            "completed_at": datetime.now().astimezone().isoformat(),
        }
        # The vector snapshot is already committed at this point. A temporary
        # Redis outage must not relabel the successful index update as failed.
        try:
            daily_job_store.set_last_success(result)
        except DailyJobStoreError:
            logger.exception("Failed to persist last successful daily vector result")
        logger.info("每日业务表向量同步成功: %s", result)
        return result
    finally:
        _daily_sync_lock.release()


def _process_daily_table_sync_job(job_id: str) -> None:
    """后台执行同步，并记录可查询的最终状态。"""
    try:
        job = daily_job_store.get(job_id)
        if job is None:
            logger.error("Daily vector job state expired before execution: job_id=%s", job_id)
            return
        job["status"] = "RUNNING"
        job["started_at"] = datetime.now().astimezone().isoformat()
        daily_job_store.save(job)
    except DailyJobStoreError:
        logger.exception("Unable to load/start daily vector job: job_id=%s", job_id)
        return

    stop_heartbeat = threading.Event()

    def renew_lease() -> None:
        interval = max(1, DAILY_JOB_LEASE_SECONDS // 3)
        while not stop_heartbeat.wait(interval):
            try:
                if not daily_job_store.renew(job_id):
                    logger.error("Daily vector job lost its Redis lease: job_id=%s", job_id)
                    return
            except DailyJobStoreError:
                logger.exception("Failed to renew daily vector job lease: job_id=%s", job_id)

    heartbeat = threading.Thread(target=renew_lease, daemon=True)
    heartbeat.start()
    try:
        source = DailyTableSyncRequest(**job["source_config"])
        result = _run_daily_table_sync("api", source)
        job["status"] = "SUCCEEDED"
        job["result"] = result
        job["completed_at"] = datetime.now().astimezone().isoformat()
        try:
            daily_job_store.save(job)
        except DailyJobStoreError:
            logger.exception("Failed to persist completed daily vector job: job_id=%s", job_id)
    except Exception as exc:
        logger.exception("每日业务表向量同步失败，旧索引已保留")
        job["status"] = "FAILED"
        job["error"] = {
            "code": "DAILY_VECTOR_SYNC_FAILED",
            "message": "表读取或向量化失败，旧索引已保留",
        }
        job["completed_at"] = datetime.now().astimezone().isoformat()
        try:
            daily_job_store.save(job)
        except DailyJobStoreError:
            logger.exception("Failed to persist failed daily vector job: job_id=%s", job_id)
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=1)
        try:
            daily_job_store.release(job_id)
        except DailyJobStoreError:
            logger.exception("Failed to release daily vector job lease: job_id=%s", job_id)


@app.post(
    "/vector/daily-table/sync",
    response_model=DailyTableSyncAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def daily_table_sync(req: DailyTableSyncRequest, background_tasks: BackgroundTasks):
    """接收数据端同步信号并立即返回，读取和向量化在后台执行。"""
    accepted_at = datetime.now().astimezone().isoformat()
    # With event_id, retries map to one deterministic job across processes and
    # service restarts. Without it, preserve the original one-request-one-job API.
    job_id = (
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"oagnet:{req.source_table}:{req.event_id}"))
        if req.event_id
        else str(uuid.uuid4())
    )
    source_config = req.model_dump(exclude_none=True)

    try:
        existing = daily_job_store.get(job_id) if req.event_id else None
    except DailyJobStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DAILY_JOB_STORE_UNAVAILABLE",
                "message": "任务状态服务不可用",
            },
        ) from exc
    if existing is not None:
        if existing.get("source_config") != source_config:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "DAILY_VECTOR_SYNC_IDEMPOTENCY_CONFLICT",
                    "message": "相同 event_id 对应的同步参数不一致",
                    "job_id": job_id,
                },
            )
        return DailyTableSyncAcceptedResponse(
            accepted=True,
            message="相同 event_id 已接收，返回原任务状态",
            job_id=job_id,
            status=str(existing["status"]),
            source_table=str(existing["source_table"]),
            accepted_at=str(existing["accepted_at"]),
            event_id=req.event_id,
        )

    job = {
        "job_id": job_id,
        "status": "QUEUED",
        "source_table": req.source_table,
        "event_id": req.event_id,
        "source_config": source_config,
        "accepted_at": accepted_at,
        "started_at": None,
        "completed_at": None,
        "result": None,
        "error": None,
    }

    try:
        created = daily_job_store.create(job)
    except DailyJobStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DAILY_JOB_STORE_UNAVAILABLE", "message": "任务状态服务不可用"},
        ) from exc
    if not created:
        try:
            active_job_id = daily_job_store.active_job_id()
            active = daily_job_store.get(active_job_id) if active_job_id else None
        except DailyJobStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "DAILY_JOB_STORE_UNAVAILABLE",
                    "message": "任务状态服务不可用",
                },
            ) from exc
        # Same idempotent request may have won the Redis race in another worker.
        if req.event_id and active_job_id == job_id and active is not None:
            return DailyTableSyncAcceptedResponse(
                accepted=True,
                message="相同 event_id 已接收，返回原任务状态",
                job_id=job_id,
                status=str(active["status"]),
                source_table=str(active["source_table"]),
                accepted_at=str(active["accepted_at"]),
                event_id=req.event_id,
            )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DAILY_VECTOR_SYNC_ALREADY_RUNNING",
                "message": "已有同步任务正在排队或执行",
                "job_id": active_job_id,
            },
        )

    background_tasks.add_task(_process_daily_table_sync_job, job_id)
    return DailyTableSyncAcceptedResponse(
        accepted=True,
        message="已接收同步请求，正在后台读取表并执行向量化",
        job_id=job_id,
        status="QUEUED",
        source_table=req.source_table,
        accepted_at=accepted_at,
        event_id=req.event_id,
    )


@app.get(
    "/vector/daily-table/jobs/{job_id}",
    response_model=DailyTableSyncJobResponse,
)
def daily_table_job_status(job_id: str):
    try:
        persisted_job = daily_job_store.get(job_id)
    except DailyJobStoreError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "DAILY_JOB_STORE_UNAVAILABLE"}
        ) from exc
    if persisted_job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "DAILY_VECTOR_SYNC_JOB_NOT_FOUND",
                "message": "同步任务不存在或状态已过期",
            },
        )
    if persisted_job.get("status") in {"QUEUED", "RUNNING"}:
        try:
            active_job_id = daily_job_store.active_job_id()
        except DailyJobStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "DAILY_JOB_STORE_UNAVAILABLE",
                    "message": "任务状态服务不可用",
                },
            ) from exc
        if active_job_id != job_id:
            persisted_job["status"] = "FAILED"
            persisted_job["error"] = {
                "code": "DAILY_VECTOR_SYNC_INTERRUPTED",
                "message": "服务中断或任务租约已过期，请重新提交同步任务",
            }
            persisted_job["completed_at"] = datetime.now().astimezone().isoformat()
            try:
                daily_job_store.save(persisted_job)
            except DailyJobStoreError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "DAILY_JOB_STORE_UNAVAILABLE",
                        "message": "任务状态服务不可用",
                    },
                ) from exc
    return DailyTableSyncJobResponse(**persisted_job)


@app.get("/vector/daily-table/status")
def daily_table_status():
    try:
        persisted_active_id = daily_job_store.active_job_id()
        persisted_active = (
            daily_job_store.get(persisted_active_id) if persisted_active_id else None
        )
        persisted_last_success = daily_job_store.last_success()
    except DailyJobStoreError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "DAILY_JOB_STORE_UNAVAILABLE"}
        ) from exc
    return {
        "trigger_mode": "api_only",
        "execution_mode": "async_background",
        "state_backend": "redis",
        "source_table": persisted_active.get("source_table") if persisted_active else None,
        "active_job_id": persisted_active_id,
        "running": persisted_active_id is not None,
        "last_success": persisted_last_success,
    }


# ------------------------------------------------------------------
# 3. 自然语言 → DSL
# ------------------------------------------------------------------

class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    retrieval_query: str | None = Field(default=None, min_length=1, max_length=4000)
    semantic_model_id: StrictPositiveInt
    business_domain_id: StrictPositiveInt | None = None
    business_domain_ids: list[StrictPositiveInt] = Field(
        default_factory=list,
        max_length=50,
        description="可选；空数组为 MODEL_WIDE，单元素为严格显式域，多个不同业务域会被拒绝",
    )
    metric_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="调用方已解析的指标ID，格式 semantic_model_id:metric_code",
    )
    metricless_projection: bool = Field(
        default=False,
        description="Caller converted a derived count into a governed distinct-detail projection",
    )
    intent_asl_contract: IntentASLContract | None = Field(
        default=None,
        description="Caller-owned intent/query-shape contract using semantic labels",
    )
    analysis_operator: Literal[
        "dimension_contribution_decomposition",
        "price_volume_decomposition",
        "funnel_conversion_analysis",
        "structural_share_shift",
        "multidimensional_attribution",
        "multidimensional_ratio_attribution",
    ] | None = None
    result_contract: dict | None = Field(
        default=None,
        description="数据分析算子要求的结构化结果契约",
    )
    exploration_requirements: dict | None = Field(
        default=None,
        description="开放式分析要求的受限指标、维度和结果粒度",
    )

    @field_validator("query", "retrieval_query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("query text must not be blank")
        return value

    @field_validator("business_domain_ids", mode="before")
    @classmethod
    def normalize_business_domain_ids(cls, value):
        if not isinstance(value, list):
            raise ValueError("business_domain_ids must be an array")
        if any(type(item) is not int or item <= 0 for item in value):
            raise ValueError("business_domain_ids must contain positive integers")
        return list(dict.fromkeys(value))

    @field_validator("metric_ids")
    @classmethod
    def validate_metric_ids(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value))
        if any(not re.fullmatch(r"[1-9]\d*:[A-Za-z0-9_.-]{1,128}", item) for item in normalized):
            raise ValueError("metric_ids must use semantic_model_id:metric_code format")
        return normalized

    @model_validator(mode="after")
    def reconcile_business_domain_scope(self):
        self.business_domain_ids = normalize_domains(
            self.business_domain_id,
            self.business_domain_ids if 'business_domain_ids' in self.model_fields_set else None,
        )
        if any(int(item.split(":", 1)[0]) != self.semantic_model_id for item in self.metric_ids):
            raise ValueError("metric_ids conflict with semantic_model_id")
        if self.metricless_projection and self.metric_ids:
            raise ValueError("metricless_projection cannot be combined with metric_ids")
        if (self.analysis_operator is None) != (self.result_contract is None):
            raise ValueError("analysis_operator and result_contract must be provided together")
        if self.result_contract is not None:
            if self.result_contract.get("operator") != self.analysis_operator:
                raise ValueError("result_contract operator does not match analysis_operator")
            required = self.result_contract.get("required_columns")
            if not isinstance(required, dict) or not required:
                raise ValueError("result_contract.required_columns must be a non-empty object")
            if any(
                not isinstance(role, str) or not role
                or not isinstance(aliases, list) or not aliases
                or any(not isinstance(alias, str) or not alias for alias in aliases)
                for role, aliases in required.items()
            ):
                raise ValueError("result_contract required column aliases are invalid")
        if self.exploration_requirements is not None:
            allowed = {
                "minimum_numeric_metrics", "maximum_numeric_metrics",
                "minimum_dimensions", "maximum_dimensions",
                "prefer_time_dimension", "result_grain",
            }
            if set(self.exploration_requirements) != allowed:
                raise ValueError("exploration_requirements fields are invalid")
            for minimum, maximum in (
                ("minimum_numeric_metrics", "maximum_numeric_metrics"),
                ("minimum_dimensions", "maximum_dimensions"),
            ):
                low, high = self.exploration_requirements[minimum], self.exploration_requirements[maximum]
                if type(low) is not int or type(high) is not int or not 1 <= low <= high <= 3:
                    raise ValueError("exploration_requirements ranges must be between 1 and 3")
            if type(self.exploration_requirements["prefer_time_dimension"]) is not bool:
                raise ValueError("exploration_requirements prefer_time_dimension must be boolean")
            if self.exploration_requirements["result_grain"] != "AGGREGATED_ANALYSIS_READY":
                raise ValueError("exploration_requirements result_grain is invalid")
        return self


class MetricSemanticEvidence(BaseModel):
    canonical_code: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    semantic_model_id: StrictPositiveInt
    business_domain_id: StrictPositiveInt | None = None
    calculation_formula: str = Field(min_length=1)
    formula_source: Literal[
        "calculation_formula",
        "indicator_logic",
        "vector_calculation_rule",
    ]
    formula_signature: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    global_filters: Any = None
    metadata_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    metadata_source: Literal["MYSQL_SEMANTIC_LAYER", "VECTOR_INDEX_FALLBACK"]
    sql_verified: bool
    retrieval_record_id: str = Field(min_length=1)
    retrieval_score: float


class SemanticQueryEvidence(BaseModel):
    evidence_version: Literal["1.0"]
    producer: Literal["OAGNET"]
    semantic_model_id: StrictPositiveInt
    requested_business_domain_ids: list[StrictPositiveInt] = Field(default_factory=list)
    resolved_business_domain_ids: list[StrictPositiveInt] = Field(default_factory=list)
    selected_metrics: list[MetricSemanticEvidence] = Field(default_factory=list)
    asl_signature: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class QueryResponse(BaseModel):
    scope_contract_version: Literal['1.0'] = CONTRACT_VERSION
    success: bool
    query: str
    semantic_model_id: int
    business_domain_id: int | None
    business_domain_ids: list[int] = Field(default_factory=list)
    business_domain_selection_mode: Literal["AUTO", "EXPLICIT"]
    result: str
    semantic_evidence: SemanticQueryEvidence
    asl_contract: dict | None = None
    asl_validation: Literal["PASS"] | None = None
    asl_validation_error_code: str | None = None
    asl_repair: list[dict] = Field(default_factory=list)


def _asl_validation_error_code(exc: ValueError) -> str:
    """Map internal validation reasons to stable, non-sensitive API codes."""
    if isinstance(exc, ASLValidationError):
        return exc.code
    reason = str(exc).casefold()
    if "asl must contain a metric, a detail projection" in reason:
        return "ASL_EMPTY_QUERY_PROJECTION"
    if "time_context anchor" in reason or "time_anchor" in reason:
        return "ASL_TIME_ANCHOR_INVALID"
    if any(marker in reason for marker in (
        "asl omitted metrics explicitly named",
        "metric selection does not match caller-bound metrics",
        "metric was not retrieved from semantic scope",
        "metric is duplicated",
        "metric name is required",
    )):
        return "ASL_METRIC_SELECTION_INVALID"
    if "filter" in reason:
        return "ASL_FILTER_INVALID"
    if "dimension" in reason or "detail projection" in reason:
        return "ASL_DIMENSION_INVALID"
    if "business-domain" in reason or "business domain" in reason:
        return "ASL_SCOPE_INVALID"
    return "ASL_OUTPUT_INVALID"


@app.post("/agent/query", response_model=QueryResponse)
def agent_query(req: QueryRequest):
    """自然语言提问生成 DSL，直接返回 agent 结果

    通过 semantic_model_id + 可选业务域参数限定检索作用域：
      - semantic_model_id: 必填，指定语义建模
      - business_domain_id: 可选，兼容旧版的单业务域字段
      - business_domain_ids: 可选；当前最多一个不同业务域
      - 两个业务域字段都为空时：MODEL_WIDE，仅在本轮模型内检索
    """
    try:
        generated = _asl_capacity.run(
            lambda: main(
                req.query,
                retrieval_query=req.retrieval_query,
                store=_store,
                semantic_model_id=req.semantic_model_id,
                business_domain_id=req.business_domain_id,
                business_domain_ids=req.business_domain_ids,
                preferred_metric_codes=[
                    item.split(":", 1)[1] for item in req.metric_ids
                ],
                # An explicit metricless Intent-ASL contract is authoritative too.
                # Without this branch, DETAIL / ENTITY / RELATION requests can be
                # reclassified as metric queries when the enriched retrieval text
                # happens to mention a metric synonym.
                metric_selection_authoritative=(
                    req.metricless_projection
                    or bool(req.metric_ids)
                    or (
                        req.intent_asl_contract is not None
                        and not req.intent_asl_contract.metric_required
                    )
                ),
                intent_asl_contract=(
                    req.intent_asl_contract.model_dump(mode="json")
                    if req.intent_asl_contract is not None else None
                ),
                analysis_operator=req.analysis_operator,
                result_contract=req.result_contract,
                exploration_requirements=req.exploration_requirements,
                include_evidence=True,
            )
        )
        if not isinstance(generated, dict):
            raise ValueError("Oagnet evidence result envelope is missing")
        result = generated.get("result")
        semantic_evidence = generated.get("semantic_evidence")
        if not isinstance(result, str) or not isinstance(semantic_evidence, dict):
            raise ValueError("Oagnet evidence result envelope is invalid")
        if semantic_evidence.get("semantic_model_id") != req.semantic_model_id:
            raise ValueError("Oagnet evidence semantic_model_id does not match request")
        if semantic_evidence.get("requested_business_domain_ids") != req.business_domain_ids:
            raise ValueError("Oagnet evidence business-domain scope does not match request")
    except AslCapacityExhausted as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "ASL_CAPACITY_EXHAUSTED",
                "message": "ASL generation is busy; retry this request shortly",
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except AslUpstreamRateLimited as exc:
        logger.warning(
            "ASL upstream rate limited: sm=%s, bds=%s, retry_after=%s",
            req.semantic_model_id,
            req.business_domain_ids or None,
            exc.retry_after_seconds,
        )
        raise HTTPException(
            status_code=429,
            detail={
                "code": "ASL_UPSTREAM_RATE_LIMITED",
                "message": "upstream ASL model is rate limited; retry later",
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except AslGenerationDeadlineExceeded as exc:
        logger.warning(
            "ASL request deadline exceeded: sm=%s, bds=%s, deadline=%ss",
            req.semantic_model_id,
            req.business_domain_ids or None,
            _ASL_REQUEST_DEADLINE_SECONDS,
        )
        raise HTTPException(
            status_code=504,
            detail={
                "code": "ASL_GENERATION_DEADLINE_EXCEEDED",
                "message": "ASL generation exceeded the bounded request deadline",
            },
        ) from exc
    except ValueError as exc:
        error_code = _asl_validation_error_code(exc)
        logger.warning(
            "ASL output rejected: sm=%s, bds=%s, code=%s, field=%s, reason=%s, details=%s",
            req.semantic_model_id,
            req.business_domain_ids or None,
            error_code,
            getattr(exc, "field", None),
            exc,
            getattr(exc, "details", None),
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": error_code,
                "message": "model output did not pass semantic/schema validation",
            },
        ) from exc
    except Exception as exc:
        logger.exception(
            "ASL generation failed: sm=%s, bds=%s",
            req.semantic_model_id,
            req.business_domain_ids or None,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "ASL_GENERATION_FAILED", "message": "ASL generation unavailable"},
        ) from exc
    logger.info(
        "ASL generated: sm=%s, bds=%s, query_chars=%s, result_chars=%s",
        req.semantic_model_id,
        req.business_domain_ids or None,
        len(req.query),
        len(result),
    )
    return {
        "success": True,
        "query": req.query,
        "semantic_model_id": req.semantic_model_id,
        "business_domain_id": req.business_domain_id,
        "business_domain_ids": req.business_domain_ids,
        "business_domain_selection_mode": "EXPLICIT" if req.business_domain_ids else "AUTO",
        "result": result,
        "semantic_evidence": semantic_evidence,
        "asl_contract": generated.get("asl_contract"),
        "asl_validation": generated.get("asl_validation"),
        "asl_validation_error_code": generated.get("asl_validation_error_code"),
        "asl_repair": generated.get("asl_repair") or [],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=OAGNET_HOST, port=OAGNET_PORT)
