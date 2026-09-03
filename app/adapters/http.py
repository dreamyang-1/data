from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import re
import time
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.adapters.base import AdapterBundle, AdapterError, MetricDiscovery
from app.analysis.contracts import (
    contract_for_request,
    ordered_entity_metric_ranking_request,
    validate_contract,
)
from app.domain.models import ExplorationQueryRequirements
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    DataQueryResult,
    Dataset,
    EvidenceItem,
    KnowledgeContext,
    KnowledgeDocument,
    MetricRef,
    PrimaryIntent,
    TrustedIdentity,
)
from app.services.knowledge_retrieval import (
    KnowledgeSearchCache,
    normalize_and_deduplicate_hits,
)
from app.services.progress import emit_progress
from app.services.intent_asl_contract import (
    build_intent_asl_contract,
    validate_intent_asl_contract_completeness,
    validate_intent_asl_contract_definition,
)
from app.services.relationship_projection import (
    requires_distinct_relationship_projection,
)
from app.tools.database_load import DatabaseLoadError, build_database_load_tool
from app.adapters.semantic_query import CompositeSemanticQueryTool


# These codes describe a deterministic semantic/query contract rejection.  A
# transport-level 5xx wrapper must not make the HTTP client submit the exact
# same read-only plan several times.  The orchestrator may still perform its
# single evidence-enriched semantic re-plan for the explicitly whitelisted
# repairable subset.
_NON_RETRYABLE_UPSTREAM_CODES = frozenset({
    "ASL_ENTITY_MENTION_UNRESOLVED",
    "ASL_FILTER_INVALID",
    "ASL_REQUIRED_FILTER_MISSING",
    "ASL_REQUIRED_DIMENSION_MISSING",
    "ASL_DETAIL_FIELDS_INCOMPLETE",
    "ASL_DETAIL_PROJECTION_MISSING",
    "ASL_GROUPING_DIMENSION_MISSING",
    "ASL_DIMENSION_INVALID",
    "ASL_UNREQUESTED_DIMENSION",
    "SQL_QUERY_ENTITY_ALIGNMENT_FAILED",
    "SQL_QUERY_FILTER_OPERATOR_FAILED",
    "SQL_RELATIONSHIP_GRAPH_INCOMPLETE",
})


def _compact_progress_value(value: Any, limit: int = 800) -> str:
    """Render auditable tool I/O without flooding the SSE stream."""
    if isinstance(value, str):
        rendered = re.sub(r"\s+", " ", value).strip()
    else:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    if len(rendered) <= limit:
        return rendered
    omitted = len(rendered) - limit
    return f"{rendered[:limit]}…（已省略 {omitted} 字符）"


class PlatformHttpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _headers(
        self,
        identity: TrustedIdentity | None = None,
        *,
        application_id: str | None = None,
        idempotency_key: str | None = None,
        bearer_token: str | None = None,
    ) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        elif self.settings.platform_api_key:
            headers["Authorization"] = f"Bearer {self.settings.platform_api_key.get_secret_value()}"
        if identity:
            headers["X-Roles"] = ",".join(identity.roles)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def post(
        self,
        base_url: str,
        path: str,
        payload: dict[str, Any],
        *,
        identity: TrustedIdentity | None = None,
        application_id: str | None = None,
        idempotency_key: str | None = None,
        retryable: bool = True,
        timeout: float | None = None,
        bearer_token: str | None = None,
    ) -> Any:
        return await self._request(
            "POST", base_url, path, payload, identity=identity,
            application_id=application_id, idempotency_key=idempotency_key,
            retryable=retryable, timeout=timeout, bearer_token=bearer_token
        )

    async def get(
        self,
        base_url: str,
        path: str,
        *,
        identity: TrustedIdentity | None = None,
        application_id: str | None = None,
        timeout: float | None = None,
        bearer_token: str | None = None,
    ) -> Any:
        return await self._request(
            "GET", base_url, path, None, identity=identity,
            application_id=application_id, retryable=True, timeout=timeout,
            bearer_token=bearer_token
        )

    async def health(self, base_url: str) -> bool:
        try:
            await self.get(base_url, "/health", timeout=2.0)
            return True
        except Exception:  # health is deliberately non-throwing
            return False

    async def openapi_has_paths(
        self, base_url: str, required_paths: list[str]
    ) -> bool:
        """Probe the API contract, with a safe invalid-body fallback.

        Some legacy Spring services expose neither OpenAPI nor /health. For
        those only, a deliberately invalid POST may prove that a route exists
        (400/401/403/415/422) without executing a business query.
        """
        try:
            async with httpx.AsyncClient(
                base_url=base_url.rstrip("/"), timeout=2.0
            ) as client:
                response = await client.get("/openapi.json", headers=self._headers())
                if response.status_code == 200:
                    schema = response.json()
                    paths = schema.get("paths") if isinstance(schema, dict) else None
                    return isinstance(paths, dict) and all(
                        path in paths for path in required_paths
                    )
                # An advertised OpenAPI endpoint that fails must not be hidden by
                # a fallback probe. 404/405 means this legacy service has none.
                if response.status_code not in {404, 405}:
                    return False
                for path in required_paths:
                    probe = await client.post(
                        path, json={}, headers=self._headers()
                    )
                    if probe.status_code == 404 or probe.status_code >= 500:
                        return False
                return True
        except Exception:
            return False

    async def _request(
        self,
        method: str,
        base_url: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        identity: TrustedIdentity | None = None,
        application_id: str | None = None,
        idempotency_key: str | None = None,
        retryable: bool,
        timeout: float | None = None,
        bearer_token: str | None = None,
    ) -> Any:
        attempts = self.settings.http_max_retries + 1 if retryable else 1
        last_error: Exception | None = None
        last_adapter_error: AdapterError | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    base_url=base_url.rstrip("/"),
                    timeout=timeout or self.settings.request_timeout_seconds,
                ) as client:
                    response = await client.request(
                        method,
                        path,
                        json=payload if method != "GET" else None,
                        headers=self._headers(
                            identity,
                            application_id=application_id,
                            idempotency_key=idempotency_key,
                            bearer_token=bearer_token,
                        ),
                    )
                if response.status_code in {401, 403}:
                    raise AdapterError(
                        "DEPENDENCY_FORBIDDEN",
                        "dependency rejected identity",
                        status_code=response.status_code,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    upstream_code = self._upstream_error_code(response)
                    if upstream_code in _NON_RETRYABLE_UPSTREAM_CODES:
                        raise AdapterError(
                            "DEPENDENCY_CONTRACT_REJECTED",
                            "dependency rejected the semantic/query contract",
                            retryable=False,
                            status_code=response.status_code,
                            upstream_code=upstream_code,
                            details={"path": path},
                        )
                    effective_retryable = bool(
                        retryable
                        and upstream_code not in _NON_RETRYABLE_UPSTREAM_CODES
                    )
                    last_adapter_error = AdapterError(
                        "DEPENDENCY_UNAVAILABLE",
                        f"dependency returned HTTP {response.status_code}",
                        retryable=effective_retryable,
                        status_code=response.status_code,
                        upstream_code=upstream_code,
                        details={"path": path},
                    )
                    if effective_retryable and attempt + 1 < attempts:
                        await asyncio.sleep(
                            self.settings.http_retry_backoff_seconds * (2**attempt)
                        )
                        continue
                    raise last_adapter_error
                if response.is_error:
                    upstream_code = self._upstream_error_code(response)
                    raise AdapterError(
                        "DEPENDENCY_CONTRACT_REJECTED",
                        f"dependency returned HTTP {response.status_code}",
                        status_code=response.status_code,
                        upstream_code=upstream_code,
                        details={"path": path},
                    )
                return response.json()
            except AdapterError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(self.settings.http_retry_backoff_seconds * (2**attempt))
        if last_adapter_error is not None:
            raise last_adapter_error
        raise AdapterError(
            "DEPENDENCY_UNAVAILABLE", "platform dependency unavailable", retryable=True
        ) from last_error

    @staticmethod
    def _upstream_error_code(response: httpx.Response) -> str:
        """Extract a stable, non-sensitive code from flat or FastAPI error bodies."""
        try:
            body = response.json()
        except ValueError:
            body = None
        candidates: list[Any] = []
        if isinstance(body, dict):
            candidates.extend((body.get("error_code"), body.get("code")))
            detail = body.get("detail")
            if isinstance(detail, dict):
                candidates.extend((detail.get("error_code"), detail.get("code")))
        for value in candidates:
            if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", value):
                return value
        return f"HTTP_{response.status_code}"


class HttpSemanticAdapter:
    def __init__(self, settings: Settings, client: PlatformHttpClient) -> None:
        self.settings = settings
        self.client = client

    async def health(self) -> bool:
        return await self.client.openapi_has_paths(
            self.settings.semantic_base_url,
            [
                self.settings.semantic_resolve_path,
                self.settings.semantic_definition_path,
                self.settings.semantic_lineage_path,
            ],
        )

    async def resolve_metrics(
        self, request: CanonicalAnalysisRequest, semantic_model_id: int | None
    ) -> list[MetricRef]:
        identity = TrustedIdentity(
            tenant_id=request.tenant_id, user_id=request.user_id, roles=[]
        )
        data = await self.client.post(
            self.settings.semantic_base_url,
            self.settings.semantic_resolve_path,
            {
                "schema_version": request.schema_version,
                "tenant_id": request.tenant_id,
                "semantic_model_id": semantic_model_id,
                "metric_names": [metric.input for metric in request.metrics],
            },
            identity=identity,
            application_id=request.application_id,
        )
        resolution_status = data.get("status")
        if resolution_status in {"NOT_FOUND", "AMBIGUOUS"}:
            return []
        if resolution_status != "RESOLVED" or not isinstance(data.get("metrics"), list):
            raise AdapterError(
                "SEMANTIC_RESPONSE_INVALID",
                "semantic service did not return a resolved metric list",
            )
        try:
            metrics = [MetricRef.model_validate(item) for item in data["metrics"]]
        except Exception as exc:
            raise AdapterError("SEMANTIC_RESPONSE_INVALID", "invalid semantic response") from exc
        expected_prefix = f"{semantic_model_id}:"
        if semantic_model_id is None or any(
            not metric.metric_id or not metric.metric_id.startswith(expected_prefix)
            for metric in metrics
        ):
            raise AdapterError(
                "SEMANTIC_RESPONSE_INVALID",
                "semantic service returned an unscoped metric binding",
            )
        return metrics

    async def definition(self, metric: MetricRef) -> EvidenceItem:
        path = self.settings.semantic_definition_path.format(
            metric_id=metric.metric_id, version=metric.version
        )
        data = await self.client.get(
            self.settings.semantic_base_url,
            path,
        )
        return EvidenceItem(
            evidence_id=f"definition:{metric.metric_id}:{metric.version}",
            kind="METRIC_DEFINITION",
            source_ref=path,
            payload=data,
        )

    async def lineage(self, metric: MetricRef, identity: TrustedIdentity) -> EvidenceItem:
        path = self.settings.semantic_lineage_path.format(metric_id=metric.metric_id)
        data = await self.client.post(
            self.settings.semantic_base_url,
            path,
            {"version": metric.version},
            identity=identity,
        )
        return EvidenceItem(
            evidence_id=f"lineage:{metric.metric_id}:{metric.version}",
            kind="DATA_LINEAGE",
            source_ref=path,
            payload=data,
        )


class HttpKnowledgeAdapter:
    def __init__(
        self,
        settings: Settings,
        client: PlatformHttpClient,
        cache: KnowledgeSearchCache | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.cache = cache

    async def health(self) -> bool:
        return await self.client.health(self.settings.knowledge_base_url)

    async def _search(
        self,
        *,
        queries: list[str],
        scope: list[str],
        identity: TrustedIdentity,
        application_id: str | None = None,
    ) -> Any:
        payload = {
            "query": queries,
            "knowledge_base_name": sorted(set(scope)),
            "top_k": self.settings.knowledge_recall_top_k,
            "score_threshold": self.settings.knowledge_score_threshold,
            "file_name": "",
            "metadata": {},
            "retrieval_method": self.settings.knowledge_retrieval_method,
            "RRF_weight": -1.0,
            "label_list": [],
            "source_list": [],
            "search_filename": True,
            "max_hops": 1,
            "knowledge_graph_identifier": [],
            "cache_schema_version": "knowledge-search-v1",
            "cache_index_version": self.settings.knowledge_index_version,
            "cache_tenant_id": identity.tenant_id,
            "cache_user_id": identity.user_id,
            "cache_roles": sorted(set(identity.roles)),
            "cache_application_id": application_id or "",
        }
        if self.cache is not None:
            cached = await self.cache.get(payload)
            if cached is not None:
                return cached
        fill_lock_token: str | None = None
        if self.cache is not None:
            fill_lock_token = await self.cache.acquire_fill_lock(payload)
            if fill_lock_token is None:
                cached = await self.cache.wait_for_value(payload)
                if cached is not None:
                    return cached
                # The first owner may have failed or its lease may have expired.
                # Compete for ownership once more instead of letting every waiter
                # hit Milvus simultaneously. If a healthy owner still exists we
                # fail open after the bounded wait to preserve availability.
                fill_lock_token = await self.cache.acquire_fill_lock(payload)
                if fill_lock_token is None:
                    cached = await self.cache.wait_for_value(payload)
                    if cached is not None:
                        return cached
        renewal_task: asyncio.Task | None = None
        if self.cache is not None and fill_lock_token:
            renewal_task = asyncio.create_task(
                self._renew_fill_lock(payload, fill_lock_token),
                name="knowledge-cache-lock-renewal",
            )
        upstream_payload = {
            key: value for key, value in payload.items()
            if not key.startswith("cache_")
        }
        try:
            data = await self.client.post(
                self.settings.knowledge_base_url,
                self.settings.knowledge_base_search_path,
                upstream_payload,
                identity=identity,
                application_id=application_id,
                timeout=self.settings.knowledge_base_timeout_seconds,
                bearer_token=(
                    self.settings.knowledge_base_api_key.get_secret_value()
                    if self.settings.knowledge_base_api_key
                    else None
                ),
            )
            if self.cache is not None:
                await self.cache.put(payload, data)
            return data
        finally:
            if renewal_task is not None:
                renewal_task.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal_task
            if self.cache is not None and fill_lock_token is not None:
                await self.cache.release_fill_lock(payload, fill_lock_token)

    async def _renew_fill_lock(self, payload: dict[str, Any], token: str) -> None:
        interval = max(0.5, self.settings.knowledge_cache_lock_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if self.cache is None or not await self.cache.renew_fill_lock(payload, token):
                return

    async def retrieve_analysis_context(
        self,
        request: CanonicalAnalysisRequest,
        dataset: Dataset,
        identity: TrustedIdentity,
    ) -> KnowledgeContext:
        # Scope is supplied by the trusted application binding. Empty is an
        # explicit "no knowledge base" decision, not permission to use a global
        # default configured for another application.
        scope = list(request.knowledge_base_names)
        if not scope:
            raise AdapterError("KNOWLEDGE_SCOPE_MISSING", "analysis knowledge bases not configured")
        metric_names = "、".join(m.canonical_name or m.input for m in request.metrics) or "未指定指标"
        query_text = (
            f"分析类型：{request.primary_intent.value}；用户问题：{request.original_question}；"
            f"指标：{metric_names}；查询结果字段：{', '.join(dataset.columns)}；"
            f"结果行数：{dataset.row_count}。请检索相关业务口径、影响因素、分析方法和已知事件。"
        )
        subqueries = [query_text]
        if request.metrics:
            subqueries.append(
                f"{'、'.join(m.canonical_name or m.input for m in request.metrics)} "
                f"{request.primary_intent.value} 业务事件 影响因素"
            )
        data = await self._search(
            queries=subqueries[:3],
            scope=scope,
            identity=identity,
            application_id=request.application_id,
        )
        hits = normalize_and_deduplicate_hits(
            data,
            allowed_knowledge_bases=scope,
            final_top_k=self.settings.analysis_knowledge_top_k,
            excerpt_chars=self.settings.analysis_knowledge_excerpt_chars,
            score_type=self.settings.knowledge_score_type,
            min_normalized_relevance=self.settings.knowledge_min_normalized_relevance,
            max_per_source=self.settings.knowledge_max_per_source,
        )
        documents: list[KnowledgeDocument] = []
        for hit in hits:
            documents.append(KnowledgeDocument(
                content=hit.content,
                source=hit.source,
                block_id=hit.block_id,
                kb_name=hit.kb_name,
                score=hit.raw_score,
                normalized_relevance=hit.normalized_relevance,
                retrieval_rank=hit.retrieval_rank,
            ))
        return KnowledgeContext(query=query_text, documents=documents)


class HttpDataRetrievalAdapter:
    """Calls the existing platform-owned Oagnet and SQL translation services."""

    def __init__(self, settings: Settings, client: PlatformHttpClient) -> None:
        self.settings = settings
        self.client = client
        # Cache only validated query plans. Results are never cached, so every
        # request still reads current business data from the SQL service.
        self._asl_plan_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def health(self) -> bool:
        # Only the ASL generation and SQL execution routes are core query
        # dependencies. Entity-vector rewriting is deliberately excluded here:
        # it enriches accuracy but can safely degrade to the original question.
        # Treating it as core would incorrectly take the whole agent out of
        # readiness whenever the optional rewrite endpoint is being refreshed.
        results = await asyncio.gather(
            self.client.openapi_has_paths(
                self.settings.asl_generator_base_url,
                [self.settings.asl_generator_path],
            ),
            self.client.openapi_has_paths(
                self.settings.sql_translator_base_url,
                [
                    self.settings.sql_translate_path,
                    self.settings.sql_execute_path,
                ],
            ),
        )
        return all(results)

    async def rewrite_health(self) -> bool:
        """Report the optional entity normalization contract separately."""
        return await self.client.openapi_has_paths(
            self.settings.asl_generator_base_url,
            [self.settings.entity_attribute_search_path],
        )

    @staticmethod
    def _semantic_evidence_fingerprint(evidence: dict[str, Any]) -> str:
        unsigned = {
            key: value for key, value in evidence.items()
            if key != "evidence_fingerprint"
        }
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_time_independent_snapshot_metric(
        asl: dict[str, Any], selected_metrics: list[dict[str, Any]]
    ) -> bool:
        """Recognize a schema-backed master-data count without naming a domain.

        The decision is intentionally conservative: every selected metric must
        be a distinct count over the ASL subject's own table, the live planner
        must omit time_context, and no fact/event table marker may occur. This
        keeps ordinary sales/order metrics on their configured time policy.
        """
        if asl.get("time_context") is not None or not selected_metrics:
            return False
        subject = asl.get("subject")
        subject_code = (
            str(subject.get("entity") or "")
            if isinstance(subject, dict)
            else str(subject or "")
        ).strip().lower()
        if not subject_code:
            return False
        fact_markers = re.compile(
            r"(?:sales|order|transaction|payment|refund|流水|销售|订单|交易|退款)",
            re.I,
        )
        for item in selected_metrics:
            formula = str(item.get("calculation_formula") or "")
            if fact_markers.search(formula):
                return False
            distinct = re.search(
                r"COUNT\s*\(\s*DISTINCT\s+([A-Za-z_]\w*)\.[A-Za-z_]\w*\s*\)",
                formula,
                re.I,
            )
            if distinct is None or distinct.group(1).lower() != subject_code:
                return False
        return True

    async def discover_metrics(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> MetricDiscovery:
        """Discover missing metrics against Oagnet's current SQL-verified view.

        This preflight exists only to avoid asking the user for a metric that
        was already expressed in natural language but published after this
        process started. It does not reuse the returned ASL for execution.
        """
        if semantic_model_id is None:
            return MetricDiscovery(metrics=[])
        query = request.rewritten_question or request.original_question
        generated = await self.client.post(
            self.settings.asl_generator_base_url,
            self.settings.asl_generator_path,
            {
                "query": query,
                "retrieval_query": query,
                "semantic_model_id": semantic_model_id,
                "business_domain_id": business_domain_id,
                "business_domain_ids": list(request.business_domain_ids),
                "metric_ids": [],
                "metricless_projection": False,
                "intent_asl_contract": None,
                "analysis_operator": None,
                "result_contract": None,
                "exploration_requirements": None,
            },
            identity=identity,
            application_id=request.application_id,
            idempotency_key=f"{request.request_id}:metric-discovery",
            retryable=True,
            timeout=self.settings.asl_generation_timeout_seconds,
        )
        if generated.get("success") is not True:
            return MetricDiscovery(metrics=[])
        raw_asl = generated.get("result")
        evidence = generated.get("semantic_evidence")
        if not isinstance(raw_asl, str) or not isinstance(evidence, dict):
            raise AdapterError(
                "SEMANTIC_DISCOVERY_INVALID",
                "metric discovery omitted its semantic evidence",
            )
        if (
            evidence.get("producer") != "OAGNET"
            or evidence.get("semantic_model_id") != semantic_model_id
            or evidence.get("evidence_version") != "1.0"
            or evidence.get("asl_signature")
            != "sha256:" + hashlib.sha256(raw_asl.encode("utf-8")).hexdigest()
            or evidence.get("evidence_fingerprint")
            != self._semantic_evidence_fingerprint(evidence)
        ):
            raise AdapterError(
                "SEMANTIC_DISCOVERY_INVALID",
                "metric discovery evidence did not match the current ASL snapshot",
            )
        asl = self._json_object(raw_asl, "SEMANTIC_DISCOVERY_INVALID")
        selected = evidence.get("selected_metrics")
        if not isinstance(selected, list):
            raise AdapterError(
                "SEMANTIC_DISCOVERY_INVALID",
                "metric discovery evidence has an invalid metric list",
            )
        metric_codes = [
            str(item.get("name"))
            for item in (asl.get("metrics") or [])
            if isinstance(item, dict) and item.get("name")
        ]
        evidence_codes: list[str] = []
        metrics: list[MetricRef] = []
        for item in selected:
            if not isinstance(item, dict):
                raise AdapterError(
                    "SEMANTIC_DISCOVERY_INVALID",
                    "metric discovery evidence contains an invalid item",
                )
            code = str(item.get("canonical_code") or "").strip()
            name = str(item.get("canonical_name") or "").strip()
            if (
                not code
                or not name
                or item.get("semantic_model_id") != semantic_model_id
                or item.get("metadata_source") != "MYSQL_SEMANTIC_LAYER"
                or item.get("sql_verified") is not True
            ):
                # Never turn a stale vector-only hit into an authoritative
                # caller binding. The ordinary clarification path stays open.
                return MetricDiscovery(metrics=[])
            evidence_codes.append(code)
            metrics.append(MetricRef(
                input=name,
                metric_id=f"{semantic_model_id}:{code}",
                version="current",
                canonical_name=name,
            ))
        if (
            not metrics
            or len(set(evidence_codes)) != len(evidence_codes)
            or metric_codes != evidence_codes
        ):
            return MetricDiscovery(metrics=[])
        subject = asl.get("subject")
        subject_code = (
            str(subject.get("entity") or "").strip()
            if isinstance(subject, dict)
            else str(subject or "").strip()
        ) or None
        discovered_filters = tuple(
            dict(item) for item in (asl.get("filters") or [])
            if isinstance(item, dict)
        )
        discovered_dimensions = tuple(
            str(item.get("name") or "").strip()
            for item in (asl.get("dimensions") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        )
        return MetricDiscovery(
            metrics=metrics,
            time_independent_snapshot=self._is_time_independent_snapshot_metric(
                asl, selected
            ),
            evidence_fingerprint=str(evidence.get("evidence_fingerprint")),
            subject=subject_code,
            filters=discovered_filters,
            dimensions=discovered_dimensions,
        )

    async def discover_attribute_details(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> MetricDiscovery:
        """Resolve a raw entity-attribute request without inventing a metric.

        Oagnet remains the semantic owner: it selects only published entity
        attributes and proves literal filters against the registered source
        catalog. This preflight only changes the canonical query shape after
        that validated, current-snapshot plan contains a real detail projection.
        """
        if semantic_model_id is None:
            return MetricDiscovery(metrics=[])
        query = request.rewritten_question or request.original_question
        generated = await self.client.post(
            self.settings.asl_generator_base_url,
            self.settings.asl_generator_path,
            {
                "query": query,
                "retrieval_query": query,
                "semantic_model_id": semantic_model_id,
                "business_domain_id": business_domain_id,
                "business_domain_ids": list(request.business_domain_ids),
                "metric_ids": [],
                "metricless_projection": True,
                "intent_asl_contract": None,
                "analysis_operator": None,
                "result_contract": None,
                "exploration_requirements": None,
            },
            identity=identity,
            application_id=request.application_id,
            idempotency_key=f"{request.request_id}:attribute-detail-discovery",
            retryable=True,
            timeout=self.settings.asl_generation_timeout_seconds,
        )
        if generated.get("success") is not True:
            return MetricDiscovery(metrics=[])
        raw_asl = generated.get("result")
        evidence = generated.get("semantic_evidence")
        if not isinstance(raw_asl, str) or not isinstance(evidence, dict):
            return MetricDiscovery(metrics=[])
        if (
            evidence.get("producer") != "OAGNET"
            or evidence.get("semantic_model_id") != semantic_model_id
            or evidence.get("evidence_version") != "1.0"
            or evidence.get("asl_signature")
            != "sha256:" + hashlib.sha256(raw_asl.encode("utf-8")).hexdigest()
            or evidence.get("evidence_fingerprint")
            != self._semantic_evidence_fingerprint(evidence)
        ):
            raise AdapterError(
                "SEMANTIC_DISCOVERY_INVALID",
                "attribute discovery evidence did not match the current ASL snapshot",
            )
        asl = self._json_object(raw_asl, "SEMANTIC_DISCOVERY_INVALID")
        if asl.get("metrics"):
            return MetricDiscovery(metrics=[])
        ambiguities = [
            item for item in (asl.get("ambiguity") or [])
            if isinstance(item, dict)
        ]
        # An empty metric catalog is immaterial for an explicitly metricless
        # attribute projection. Any other ambiguity still blocks conversion.
        if any(str(item.get("type") or "") != "metric" for item in ambiguities):
            return MetricDiscovery(metrics=[])
        subject = asl.get("subject")
        subject_code = (
            str(subject.get("entity") or "").strip()
            if isinstance(subject, dict)
            else ""
        )
        dimensions = tuple(dict.fromkeys(
            str(item.get("name") or "").strip().rsplit(".", 1)[-1]
            for item in (asl.get("dimensions") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ))
        if not subject_code or not dimensions:
            return MetricDiscovery(metrics=[])

        filters: list[dict[str, Any]] = []
        for repair in generated.get("asl_repair") or []:
            if (
                not isinstance(repair, dict)
                or repair.get("type") != "ADD_SOURCE_RESOLVED_ENTITY_FILTER"
            ):
                continue
            field = str(repair.get("resolved_field") or "").strip()
            value = repair.get("canonical_value")
            if field and value not in (None, ""):
                filters.append({"field": field, "operator": "=", "value": value})
        for item in asl.get("filters") or []:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            operator = str(item.get("operator") or "").upper()
            value = item.get("value")
            candidate = {"field": field, "operator": "=", "value": value}
            if (
                field
                and "." in field
                and operator in {"=", "EQ"}
                and value not in (None, "")
                and candidate not in filters
            ):
                filters.append(candidate)
        return MetricDiscovery(
            metrics=[],
            evidence_fingerprint=str(evidence.get("evidence_fingerprint") or "") or None,
            subject=subject_code,
            filters=tuple(filters),
            dimensions=dimensions,
        )

    @staticmethod
    def _constraint_field_family(value: str) -> str | None:
        normalized = value.lower()
        # Ordered from compound/specific concepts to generic entities.  A
        # current semantic field such as ``product_category.product_type``
        # contains both "product" and "category"; returning ambiguity here
        # makes valid metadata updates look like an entity mismatch.
        families = (
            ("department", ("科室", "部门", "department", "dept")),
            ("category", ("商品分类", "产品分类", "商品品类", "分类", "类别", "品类", "类目", "category", "class")),
            ("brand", ("商品品牌", "品牌", "brand")),
            ("manufacturer", ("厂家", "制造商", "厂商", "manufacturer", "maker", "producer")),
            ("region", ("地区", "区域", "省份", "城市", "region", "province", "city")),
            ("supplier", ("供应商", "经销商", "supplier", "dealer", "vendor")),
            ("hospital", ("医院", "hospital")),
            ("customer", ("客户", "会员", "customer", "member")),
            ("store", ("门店", "店铺", "store", "shop")),
            ("product", ("商品", "产品", "货品", "product", "goods", "sku")),
        )
        return next(
            (
                family for family, aliases in families
                if any(alias in normalized for alias in aliases)
            ),
            None,
        )

    @classmethod
    def _semantic_filter_retrieval_terms(
        cls,
        request: CanonicalAnalysisRequest,
        *,
        semantic_model_id: int | None,
    ) -> str:
        """Return role-specific recall anchors for caller-owned filters.

        A literal such as a full hospital or bilingual manufacturer name can
        dominate vector recall and crowd out the registered name attribute.
        The Intent-ASL contract then contains the right business filter but the
        planner has no recalled field with which to realize it.  Add semantic
        role/name anchors after all shape-specific retrieval rewrites.  Model
        81's currently published identifiers are included only as recall hints;
        Oagnet must still prove them against live metadata and source values.
        """
        families = {
            cls._constraint_field_family(str(item.get("field") or ""))
            for item in request.filters
            if isinstance(item, dict)
        }
        terms: list[str] = []
        if "hospital" in families:
            terms.extend(("医院主数据", "医院名称", "医疗机构名称"))
            if semantic_model_id == 81:
                terms.extend(("hospital_name", "hospital.hospital_name"))
        if "manufacturer" in families:
            terms.extend((
                "厂家主数据", "厂家名称", "制造商名称", "厂家标准名称",
            ))
            if semantic_model_id == 81:
                terms.extend((
                    "manufacturer_name", "standard_name",
                    "manufacturer.manufacturer_name",
                    "manufacturer.standard_name",
                ))
        if (
            "DEPARTMENT_GRAIN=PRODUCT_MAIN_DEPARTMENT_COMBINATION"
            in request.assumptions
        ):
            terms.extend((
                "主要适用科室", "商品主要适用科室", "原始科室组合",
            ))
            if semantic_model_id == 81:
                terms.extend(("main_department", "product.main_department"))
        return " ".join(dict.fromkeys(terms))

    @classmethod
    def _constraint_field_matches(cls, expected: str, actual: str) -> bool:
        expected_normalized = expected.strip().lower()
        actual_normalized = actual.strip().lower()
        if expected_normalized == actual_normalized:
            return True
        if actual_normalized.rsplit(".", 1)[-1] == expected_normalized.rsplit(".", 1)[-1]:
            return True
        expected_family = cls._constraint_field_family(expected_normalized)
        if (
            expected_family is None
            or expected_family != cls._constraint_field_family(actual_normalized)
        ):
            return False

        def stable_key_kind(value: str) -> str:
            field = value.rsplit(".", 1)[-1]
            match = re.search(r"(?:^|_)(id|code)$", field)
            return match.group(1) if match else "display"

        # A same-family name field is not equivalent to a code/id join key.
        # Keeping the key kind aligned prevents a valid predecessor code set
        # from being copied into a display-name filter (or vice versa).
        return stable_key_kind(expected_normalized) == stable_key_kind(actual_normalized)

    @classmethod
    def _validate_dependency_constraints(
        cls,
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
    ) -> None:
        """Fail before SQL translation unless every DAG value set is in ASL.

        The validation intentionally accepts only exact ``IN`` (or a single
        equality) filters.  LIKE/contains/ranges and an empty filter list cannot
        prove that the downstream query stayed inside its predecessor result.
        """
        if not request.dependency_constraints:
            return
        filters = asl.get("filters")
        if not isinstance(filters, list):
            raise AdapterError(
                "ASL_DEPENDENCY_CONSTRAINT_MISSING",
                "ASL omitted the required predecessor-result filters",
            )
        candidates = [item for item in filters if isinstance(item, dict)]
        missing: list[dict[str, Any]] = []
        for constraint in request.dependency_constraints:
            expected_values = constraint.values
            matched = False
            for item in candidates:
                field = item.get("field") or item.get("name")
                if not isinstance(field, str) or not cls._constraint_field_matches(
                    constraint.source_column, field
                ):
                    continue
                operator = str(item.get("operator") or item.get("op") or "").strip().upper()
                value = item.get("values") if "values" in item else item.get("value")
                if operator in {"IN", "IN_LIST"} and isinstance(value, list):
                    if len(value) == len(expected_values) and all(
                        any(candidate == expected for candidate in value)
                        for expected in expected_values
                    ):
                        matched = True
                        break
                elif operator in {"=", "EQ", "EQUAL", "EQUALS"} and len(expected_values) == 1:
                    if value == expected_values[0]:
                        matched = True
                        break
            if not matched:
                missing.append({
                    "source_task_id": constraint.source_task_id,
                    "source_dataset_id": constraint.source_dataset_id,
                    "source_column": constraint.source_column,
                    "value_count": len(expected_values),
                    "value_fingerprint": constraint.value_fingerprint,
                })
        if missing:
            raise AdapterError(
                "ASL_DEPENDENCY_CONSTRAINT_MISSING",
                "ASL did not preserve one or more exact predecessor-result filters",
                details={"missing_constraints": missing},
            )

    async def query(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> DataQueryResult:
        if semantic_model_id is None:
            raise AdapterError(
                "SEMANTIC_CONTEXT_MISSING",
                "semantic_model_id is required",
            )

        metric_definitions = await self._current_metric_definitions(request, identity)
        metric_definition_fingerprints = [
            hashlib.sha256(json.dumps(
                definition,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            for definition in metric_definitions
        ]
        intent_asl_contract: dict[str, Any] | None = build_intent_asl_contract(request)
        contract_definition_errors = validate_intent_asl_contract_definition(
            intent_asl_contract
        )
        if contract_definition_errors:
            if request.turn_admission is not None:
                raise AdapterError(
                    "INTENT_ASL_CONTRACT_INVALID",
                    "canonical request produced an invalid Intent-ASL contract",
                    details={"errors": contract_definition_errors},
                )
            # Direct adapter callers and older internal tests may intentionally
            # provide a partial request.  They retain the legacy path; admitted
            # user turns always have to satisfy the full contract above.
            intent_asl_contract = None
        contract_completeness_errors = (
            validate_intent_asl_contract_completeness(
                intent_asl_contract,
                request,
            )
            if intent_asl_contract is not None
            else []
        )
        if contract_completeness_errors:
            raise AdapterError(
                "INTENT_ASL_CONTRACT_INCOMPLETE",
                "Intent-ASL contract lost one or more explicit current-turn requirements",
                details={"errors": contract_completeness_errors},
            )

        semantic_query = request.rewritten_question or request.original_question
        # Keep equivalent temporal grouping word orders away from the semantic
        # planner's entity-alignment boundary.  Some callers naturally write
        # “按月请计算”/“请按月查询”; both are the same governed grouped query as
        # “按月统计”.  This normalization changes neither literals nor scope.
        semantic_query = re.sub(
            r"^(?:请)?按(日|天|周|月|季度|年)(?:请)?(?:计算|查询|汇总)",
            lambda match: f"按{match.group(1)}统计",
            semantic_query,
            count=1,
        )
        if semantic_model_id == 81 and "经销商等级" in semantic_query:
            # Model 81 has dealer type and profile/status attributes, but no
            # governed field whose business meaning is "dealer level". Do not
            # let this optional unavailable column block the valid amount/count
            # portion of a compound query. The response explicitly discloses
            # the omitted field instead of silently inventing a mapping.
            semantic_query = re.sub(
                r"(?:并|且|以及|同时)?(?:关联|显示|返回|带上)?经销商等级",
                "",
                semantic_query,
            )
            if "UNAVAILABLE_REQUESTED_FIELD=经销商等级" not in request.assumptions:
                request.assumptions.append("UNAVAILABLE_REQUESTED_FIELD=经销商等级")
        if request.primary_intent == PrimaryIntent.DETAIL_QUERY:
            semantic_query = self._detail_semantic_query(request, semantic_query)
        asl_query = semantic_query
        retrieval_query = semantic_query
        if request.filters:
            asl_query += (
                "\n调用方已确认的强制筛选条件："
                + json.dumps(request.filters, ensure_ascii=False, separators=(",", ":"))
                + "。这些条件必须逐项出现在ASL filters中，字段可映射为已注册的语义字段，"
                "但值、运算方向和业务含义不得省略、放宽或替换。"
            )
        required_non_null_names = [
            value.split("=", 1)[1]
            for value in request.assumptions
            if value.startswith("REQUIRED_NAME_NON_NULL=")
            and value.split("=", 1)[1].strip()
        ]
        if required_non_null_names:
            asl_query += (
                "\n名单主名称完整性要求："
                + "、".join(dict.fromkeys(required_non_null_names))
                + "必须使用当前语义层注册的对应名称属性投影，并在ASL filters中保留"
                "“名称 != 空字符串”约束；该约束通过SQL三值逻辑同时排除NULL和空字符串，"
                "不得把空名称作为名单成员返回。"
            )
        if request.dimensions:
            grouped_dimension_roles = self._required_grouped_dimension_roles(
                request
            )
            filter_only_roles = [
                str(value)
                for value in request.dimensions
                if str(value) not in grouped_dimension_roles
            ]
            asl_query += (
                "\n调用方识别出的语义维度角色："
                + json.dumps(request.dimensions, ensure_ascii=False, separators=(",", ":"))
                + "。必须基于当前语义模型最新注册的实体、维度及关系逐项解析这些角色，"
                "不得复用历史物理字段或应用侧静态表字段。需要分组或展示的角色为："
                + json.dumps(
                    grouped_dimension_roles,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "，必须保留在ASL dimensions中；仅限定查询范围的筛选角色为："
                + json.dumps(
                    filter_only_roles,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "，只需保留在ASL filters中，不得仅因调用方识别出该角色就强制加入"
                "dimensions或GROUP BY。"
            )
        if "NULL_DIMENSION_BUCKET=医院等级:未填写" in request.assumptions:
            asl_query += (
                "\n空维度保留契约：医院等级为空的记录也必须参与统计，"
                "使用“未填写”作为展示分组；不得添加医院等级 IS NOT NULL 筛选。"
            )
        if (
            any(
                self._constraint_field_family(str(value)) == "region"
                for value in request.dimensions
            )
            and any(
                isinstance(item, dict)
                and self._constraint_field_family(
                    str(item.get("field") or "")
                ) == "region"
                for item in request.filters
            )
        ):
            asl_query += (
                "\n行政区层级一致性要求：当省/地区筛选与城市等下级行政区分组同时出现时，"
                "两者必须沿当前语义层中同一结果对象、同一行政区层级关系解析；禁止按经销商"
                "城市分组却使用医院、客户或其他业务对象的省份字段进行筛选。"
            )
        if request.entity and any(
            isinstance(item, dict)
            and self._constraint_field_family(
                str(item.get("field") or "")
            )
            == "region"
            for item in request.filters
        ):
            asl_query += (
                f"\n结果对象位置口径：用户当前查询的结果对象是{request.entity}；在用户未"
                "明确说明订单发生地、医院所在地、客户所在地等其他位置口径时，城市、省份"
                f"等位置修饰词约束{request.entity}自身已注册的位置关系。必须沿当前语义层"
                f"从{request.entity}到行政区维度的关系选择规范显示字段；不得因为模型中"
                "同时存在其他业务对象的位置字段就要求用户二次确认，也不得切换到其他业务"
                "对象的位置分支。"
            )
        if request.time_range is not None:
            asl_query += (
                "\n调用方已确认的强制时间范围："
                f"{request.time_range.start.isoformat()} 至 "
                f"{(request.time_range.end_exclusive - timedelta(days=1)).isoformat()}（含首尾）；"
                "使用已注册的交易日期时间锚点，不得再次追问时间范围。"
            )
        if request.primary_intent == PrimaryIntent.REPORT_GENERATION:
            original_semantics = re.sub(
                r"\s+", " ", request.original_question
            ).strip()
            if (
                original_semantics
                and original_semantics not in retrieval_query
            ):
                # Canonical rendering carries the executable report contract,
                # while the user's original wording often carries the business
                # fact family (for example “在上海卖某产品”). Preserve that text
                # only for semantic recall; do not append it to ASL instructions.
                retrieval_query = f"{retrieval_query} {original_semantics}"
        if request.dependency_constraints:
            rendered_constraints = [
                {
                    "field": item.source_column,
                    "operator": "IN",
                    "values": item.values,
                    "source_dataset_id": item.source_dataset_id,
                }
                for item in request.dependency_constraints
            ]
            asl_query += (
                "\nDAG上游结果强制约束：以下每一项都必须生成为精确的IN过滤器；"
                "禁止省略、改成模糊匹配或无过滤执行："
                + json.dumps(rendered_constraints, ensure_ascii=False, separators=(",", ":"))
            )
        if (
            request.time_range is None
            and "TIME_SCOPE=ALL_TIME" in request.assumptions
        ):
            asl_query += (
                "\n执行要求：时间口径为截至业务数据水位的全部可用历史数据；"
                "不得擅自添加最近一年、本年或其他默认时间过滤。"
            )
        metric_window_is_definition = (
            request.time_range is None
            and any(
                value.startswith("METRIC_WINDOW_IS_DEFINITION=")
                for value in request.assumptions
            )
        )
        if metric_window_is_definition:
            asl_query += (
                "\n执行要求：指标名称中的近N日/周/月窗口是该预计算指标的定义，"
                "不是本次查询的日历时间范围；time_context必须为null，不得添加"
                "日期过滤、时间分组或为此连接交易事实表。"
            )
        analysis_contract = contract_for_request(request)
        exploration_requirements = (
            ExplorationQueryRequirements()
            if request.primary_intent == PrimaryIntent.REPORT_GENERATION
            else None
        )
        partner_recommendation = any(
            value.startswith("DEALER_RECOMMENDATION_DEFAULT_RANKING=")
            for value in request.assumptions
        )
        grouped_partner_metric = bool(
            request.primary_intent == PrimaryIntent.METRIC_QUERY
            and request.metrics
            and request.entity in {"经销商", "供应商"}
            and request.entity in request.dimensions
        )
        time_grouping_code = next(
            (
                value.split("=", 1)[1]
                for value in request.assumptions
                if value.startswith("DEFAULT_TIME_GRANULARITY=")
            ),
            None,
        )
        brand_name_filter = next(
            (
                item for item in request.filters
                if isinstance(item, dict)
                and any(
                    marker in str(item.get("field") or "").lower()
                    for marker in (
                        "品牌", "厂牌", "厂家", "制造商",
                        "brand", "manufacturer",
                    )
                )
            ),
            None,
        )
        transaction_activity = (
            "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
            in request.assumptions
        )
        sales_record_time_scope = self._detail_time_uses_sales_records(
            request, semantic_query
        )
        if sales_record_time_scope:
            # Time-qualified relationship and ranking requests must recall the
            # registered sales-fact date in the same semantic scope as their
            # product/partner joins. This applies beyond DETAIL_QUERY (for
            # example, an entity ranking filtered to partners with sales in a
            # period), so establish the recall contract before shape branches.
            retrieval_query = self._detail_retrieval_query(
                request, semantic_query
            )
            if not transaction_activity:
                asl_query += (
                    "\n时间范围限定的是销售事实记录，time_context 不能为空；"
                    "必须从本次召回且与结果实体连通的已注册销售交易时间维度中选择，"
                    "不得改用人员入职、主体创建或画像快照日期，也不得忽略时间范围。"
                )
        if transaction_activity:
            asl_query += (
                "\n资格口径：本次将请求时间范围内存在销售记录的对象定义为活跃对象；"
                "必须用已注册的销售交易时间维度限定事实记录，"
                "不得额外构造活跃等级、合作状态或最近一次订单阈值。"
            )
        if brand_name_filter is not None:
            retrieval_query = self._detail_retrieval_query(request, semantic_query)
            retrieval_query += (
                " 母品牌 厂家名称 parent_brand manufacturer_name "
                "manufacturer.parent_brand manufacturer.manufacturer_name"
            )
            asl_query += (
                "\n品牌筛选口径：调用方提供的是用户可见的品牌/厂家名称，不是关系键或编码。"
                "必须使用召回元数据中商品实体自身的品牌名称属性，或沿已注册厂家关系"
                "连接到厂家的名称属性进行精确名称过滤；禁止将名称值填入 *_code、ID、"
                "关系键字段，也不得要求用户提供内部编码。"
            )
        if analysis_contract is not None:
            retry_feedback = next(
                (
                    value.removeprefix("ANALYSIS_CONTRACT_RETRY:")
                    for value in request.assumptions
                    if value.startswith("ANALYSIS_CONTRACT_RETRY:")
                ),
                None,
            )
            if retry_feedback:
                asl_query += (
                    "\n上一次查询结果未通过契约校验，必须修正以下问题后重新生成ASL："
                    + retry_feedback
                )
        semantic_retry_feedback = next(
            (
                value.removeprefix("SEMANTIC_QUERY_RETRY:")
                for value in request.assumptions
                if value.startswith("SEMANTIC_QUERY_RETRY:")
            ),
            None,
        )
        if semantic_retry_feedback is not None:
            asl_query += (
                "\n语义规划重试要求：首次规划未能稳定完成实体/字段对齐。请重新读取本次"
                "召回的最新语义层实体、维度、指标和关系，以用户原文及调用方已确认的"
                "filters、dimensions、metrics为准重新生成ASL。不得复用首次SQL，不得"
                "发明字段或关系；只有当前语义层确实存在多个无法消解的候选时才返回"
                "ambiguity。首次反馈："
                + semantic_retry_feedback
            )
        if request.primary_intent == PrimaryIntent.DETAIL_QUERY and request.fields:
            detail_fields = "、".join(request.fields)
            asl_query += (
                "\n结果形状约束：这是行级明细查询，dimensions 必须完整投影："
                + detail_fields
                + "。所需属性可能位于与查询主体相关联的实体上；此时必须沿召回的"
                "语义关系路径投影相关实体的真实字段，不得用主体上的摘要字段、同名字段"
                "或其他结果列替代。metrics 必须为空，不得用聚合指标代替这些字段，"
                "也不得遗漏任一字段；"
                "仅使用召回元数据中存在的实体、物理字段和关系。"
            )
            if any(
                str(item.get("field") or "") in {"地区", "区域", "省份", "城市"}
                for item in request.filters
                if isinstance(item, dict)
            ) and not any(
                str(item.get("field") or "") in {"商品名称", "产品名称"}
                for item in request.filters
                if isinstance(item, dict)
            ):
                asl_query += (
                    "用户未明确说订单发生地或业务城市时，地区修饰词约束结果对象自身"
                    "已注册的位置字段，不得在等距的事实表业务城市字段之间重复追问。"
                )
            retrieval_query = self._detail_retrieval_query(request, semantic_query)
        elif partner_recommendation:
            # Recommendation metrics are commonly stored on a partner profile,
            # while eligibility filters live on product, department, hospital
            # and partner master entities.  Recall those semantic endpoints and
            # their registered bridge graph without leaking physical names or
            # execution-only instructions into vector search.
            retrieval_query = self._detail_retrieval_query(request, semantic_query)
            asl_query += (
                "\n执行要求：这是按经销商画像指标排名的对象推荐。"
                "必须保留经销商分组和主排名指标，其他画像指标作为返回列；"
                "若召回关系已通过桥接实体连通科室、商品、医院和经销商，"
                "直接沿注册路径执行，不得因为不是直连关系而追问。"
            )
        elif grouped_partner_metric:
            # The result measure usually comes from an order/fact entity while
            # brand, catalog and region filters live on related master data.
            # Use the same relationship-aware semantic recall as row detail,
            # but retain the aggregate execution shape.
            retrieval_query = self._detail_retrieval_query(request, semantic_query)
            asl_query += (
                f"\n执行要求：这是按{request.entity}分组的指标清单。"
                f"必须保留{request.entity}名称维度以及全部请求指标；"
                "城市、商品品牌、商品品类等用于限定范围时必须完整保留为filters，"
                "但不应仅因它们被识别为语义角色就加入dimensions或GROUP BY；只有"
                "用户明确要求分组或展示时才投影。不得把这些字段作为替代指标，也"
                "不得把聚合指标删除或退化为纯明细查询。若品牌和品类已经分别给出"
                "且用户未说具体商品，禁止再"
                "合成或添加商品名称筛选。"
            )
        elif (
            request.primary_intent == PrimaryIntent.METRIC_QUERY
            and time_grouping_code is not None
        ):
            grain_label = {
                "day": "日", "week": "周", "month": "月",
                "quarter": "季度", "year": "年",
            }.get(time_grouping_code, time_grouping_code)
            asl_query += (
                f"\n执行要求：这是按{grain_label}分组的指标统计查询，不是趋势分析。"
                "必须保留请求指标，并使用time_context.anchor对应的已注册交易日期"
                f"生成{grain_label}时间分组维度；返回各时间分组及指标值。"
                "不得只返回全区间汇总值，也不得生成趋势、涨跌或原因分析结论。"
            )
        elif ordered_entity_metric_ranking_request(request):
            asl_query += (
                "\n执行要求：这是按单一指标排序的实体集合，不是基期/当前期对比；"
                "必须按请求实体维度分组、返回该唯一指标，并按该指标降序排序。"
                "未指定TOP/BOTTOM数量时必须保留全部匹配实体，不得只返回两行。"
            )
        elif request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS:
            asl_query += (
                "\n执行要求：必须保留用户要求的对比维度并返回至少两个可比较分组；"
                "时间段对比按合适的时间粒度分组，不能只返回整个区间的一个汇总值。"
            )
        elif request.primary_intent == PrimaryIntent.TREND_ANALYSIS:
            explicit_granularity = next(
                (
                    value
                    for pattern, value in (
                        (r"按(?:日|天)(?:统计|汇总|分析|看|给我)?", "日"),
                        (r"按周(?:统计|汇总|分析|看|给我)?", "周"),
                        (r"按月(?:统计|汇总|分析|看|给我)?", "月"),
                        (r"按季度(?:统计|汇总|分析|看|给我)?", "季度"),
                        (r"按年(?:统计|汇总|分析|看|给我)?", "年"),
                    )
                    if re.search(pattern, request.original_question)
                ),
                None,
            )
            if explicit_granularity:
                asl_query += (
                    f"\n执行要求：用户明确要求按{explicit_granularity}分组，必须使用该时间粒度；"
                    "不得沿用上一轮或默认的其他时间粒度。时间维度必须使用"
                    "time_context.anchor对应的已注册交易日期维度，不能使用其他实体日期；"
                    "禁止给商品、经销商、医院等非时间维度设置granularity。"
                )
            elif (
                request.time_range is not None
                and (request.time_range.end_exclusive - request.time_range.start).days >= 60
            ):
                asl_query += (
                    "\n执行要求：当前趋势区间不少于60天且用户未明确粒度，默认按月分组返回；"
                    "时间维度必须使用time_context.anchor对应字段，不能使用其他实体日期。"
                )
        # A contextual rewrite deliberately omits unresolved replacement
        # literals from the executable sentence until their semantic role is
        # source-verified.  Keep those caller-owned literals in semantic recall,
        # otherwise attributes such as a newly published brand/manufacturer
        # dimension may never enter the current knowledge scope and cannot be
        # resolved.  These hints affect recall only; the ASL contract and live
        # source catalog still decide the field and canonical value.
        semantic_mentions = list(dict.fromkeys(
            value.strip()
            for value in request.semantic_entity_mentions
            if value.strip()
        ))
        if semantic_mentions:
            mention_text = " ".join(semantic_mentions)
            if mention_text not in retrieval_query:
                retrieval_query += f" {mention_text}"
            retrieval_query += (
                " 实体筛选值 身份属性 名称 品牌 母厂牌 生产厂家 "
                "商品名称 商品品类 产品分类 当前语义层维度及关系"
            )

        # Shape-specific branches above may rebuild ``retrieval_query`` (for
        # example grouped partner metrics). Manufacturer/brand filters still
        # need the current semantic model's registered name attributes in the
        # final recall query, so enrich the final value after all shape branches.
        if brand_name_filter is not None:
            manufacturer_recall = (
                " 母品牌 厂家名称 parent_brand manufacturer_name "
                "manufacturer.parent_brand manufacturer.manufacturer_name"
            )
            if "manufacturer.manufacturer_name" not in retrieval_query:
                retrieval_query += manufacturer_recall
        role_recall = self._semantic_filter_retrieval_terms(
            request,
            semantic_model_id=semantic_model_id,
        )
        if role_recall:
            retrieval_query += " " + role_recall
        if any(
            self._constraint_field_family(str(item.get("field") or ""))
            == "hospital"
            for item in request.filters
            if isinstance(item, dict)
        ):
            asl_query += (
                "\n实体过滤要求：医院全称必须绑定到当前语义模型已发布的医院主名称"
                "属性，并按源数据中的完整规范值过滤；不得缩短成末尾别名，也不得把"
                "医院名称误当成商品、品牌或厂家过滤。"
            )
        if any(
            self._constraint_field_family(str(item.get("field") or ""))
            == "manufacturer"
            for item in request.filters
            if isinstance(item, dict)
        ):
            asl_query += (
                "\n实体过滤要求：用户给出的是厂家法定名称，不是关系编码。"
                "名称可能同时包含中英文、空格和标点；必须使用当前召回的厂家名称或"
                "厂家标准名称属性进行源数据校验。多个名称变体若归属同一业务主体，"
                "不得要求用户改用编码。"
            )
        if (
            "DEPARTMENT_GRAIN=PRODUCT_MAIN_DEPARTMENT_COMBINATION"
            in request.assumptions
        ):
            asl_query += (
                "\n分组粒度要求：本次“科室”按商品主数据中已发布的“主要适用科室”"
                "原始组合值分组，不拆分为单个标准科室，不经过商品—科室多对多桥接；"
                "订单笔数仍使用已发布指标公式按销售订单事实唯一键去重。"
            )
        if request.dependency_constraints:
            dependency_fields = list(dict.fromkeys(
                item.source_column for item in request.dependency_constraints
            ))
            retrieval_query += (
                "\nInternal DAG semantic retrieval requirement: recall the exact "
                "fields below and their complete relation path to the requested "
                "result entity: "
                + ", ".join(dependency_fields)
            )
        asl_cache_key: str | None = None
        cacheable_asl = (
            not request.dependency_constraints
            and not request.semantic_entity_mentions
            # Without an upstream semantic-metadata version in the cache key,
            # a filtered or grouped plan can retain renamed dimensions and
            # relationship paths for several minutes.  Always re-resolve such
            # plans against the current model; scalar plans may still use the
            # bounded cache because metric-definition fingerprints are keyed.
            and not request.filters
            and not request.dimensions
            and analysis_contract is None
            and exploration_requirements is None
            and self.settings.asl_plan_cache_ttl_seconds > 0
            and self.settings.asl_plan_cache_max_items > 0
            and request.primary_intent in {
                PrimaryIntent.METRIC_QUERY, PrimaryIntent.DETAIL_QUERY,
            }
        )
        if cacheable_asl:
            asl_cache_key = hashlib.sha256(json.dumps({
                "query": asl_query,
                "retrieval_query": retrieval_query,
                "semantic_model_id": semantic_model_id,
                "business_domain_id": business_domain_id,
                "business_domain_ids": list(request.business_domain_ids),
                # Query-plan visibility can differ by caller. Keep cache reuse
                # within the same trusted identity and application boundary.
                "tenant_id": identity.tenant_id,
                "user_id": identity.user_id,
                "roles": sorted(identity.roles),
                "application_id": request.application_id,
                "metric_ids": [
                    metric.metric_id for metric in request.metrics
                    if metric.metric_id is not None
                ],
                "metric_definition_fingerprints": metric_definition_fingerprints,
                "intent_asl_contract": intent_asl_contract,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        intent_contract_confirmed = False
        asl_repairs: list[dict[str, Any]] = []
        asl = (
            None
            if request.dependency_constraints or request.semantic_entity_mentions
            else self._reuse_time_only_asl(request, semantic_model_id)
        )
        if asl is None and asl_cache_key is not None:
            cached = self._asl_plan_cache.get(asl_cache_key)
            if cached is not None:
                expires_at, cached_asl = cached
                if expires_at > time.monotonic():
                    asl = copy.deepcopy(cached_asl)
                    intent_contract_confirmed = intent_asl_contract is not None
                else:
                    self._asl_plan_cache.pop(asl_cache_key, None)
        if asl is None:
            generated = await self.client.post(
                self.settings.asl_generator_base_url,
                self.settings.asl_generator_path,
                {
                    "query": asl_query,
                    "retrieval_query": retrieval_query,
                    "semantic_model_id": semantic_model_id,
                    "business_domain_id": business_domain_id,
                    "business_domain_ids": list(request.business_domain_ids),
                    "metric_ids": [
                        metric.metric_id
                        for metric in request.metrics
                        if metric.metric_id is not None
                    ],
                    "metricless_projection": bool(
                        request.execution_contract_transform
                        == "RELATIONSHIP_COUNT_TO_DISTINCT_PROJECTION"
                    ),
                    "intent_asl_contract": intent_asl_contract,
                    "analysis_operator": (
                        analysis_contract.operator if analysis_contract is not None else None
                    ),
                    "result_contract": (
                        analysis_contract.model_dump(mode="json")
                        if analysis_contract is not None else None
                    ),
                    "exploration_requirements": (
                        exploration_requirements.model_dump(mode="json")
                        if exploration_requirements is not None else None
                    ),
                },
                identity=identity,
                application_id=request.application_id,
                idempotency_key=f"{request.request_id}:asl",
                # ASL generation is read-only and guarded by a stable
                # idempotency key. Transient connection resets are therefore
                # safe to retry; disabling retries made a single upstream TCP
                # reset surface directly as a failed user turn.
                retryable=True,
                timeout=self.settings.asl_generation_timeout_seconds,
            )
            if generated.get("success") is not True:
                raise AdapterError("ASL_GENERATION_FAILED", "ASL generator rejected request")
            contract_acknowledged = (
                "asl_validation" in generated or "asl_contract" in generated
            )
            if contract_acknowledged and (
                generated.get("asl_validation") != "PASS"
                or generated.get("asl_contract") != intent_asl_contract
            ):
                raise AdapterError(
                    "ASL_INTENT_CONTRACT_UNCONFIRMED",
                    "ASL generator did not confirm the caller-owned Intent-ASL contract",
                    details={
                        "validation": generated.get("asl_validation"),
                        "validation_error_code": generated.get(
                            "asl_validation_error_code"
                        ),
                    },
                )
            intent_contract_confirmed = contract_acknowledged
            asl_repairs = [
                dict(item)
                for item in generated.get("asl_repair") or []
                if isinstance(item, dict)
            ]
            requested_domains = list(request.business_domain_ids)
            if len(requested_domains) > 1:
                echoed_domains = generated.get("business_domain_ids")
                if echoed_domains != requested_domains:
                    raise AdapterError(
                        "ASL_SCOPE_INVALID",
                        "ASL generator did not confirm the requested multi-domain scope",
                    )
            asl = self._json_object(generated.get("result"), "ASL_RESPONSE_INVALID")
            if analysis_contract is not None:
                contract_envelope = asl.get("analysis_contract")
                if (
                    not isinstance(contract_envelope, dict)
                    or contract_envelope.get("producer") != "OAGNET"
                    or contract_envelope.get("analysis_operator") != analysis_contract.operator
                    or contract_envelope.get("result_contract")
                    != analysis_contract.model_dump(mode="json")
                ):
                    raise AdapterError(
                        "ASL_ANALYSIS_CONTRACT_UNCONFIRMED",
                        "Oagnet did not confirm the requested analysis contract",
                    )
            if exploration_requirements is not None:
                exploration_envelope = asl.get("analysis_exploration")
                if (
                    not isinstance(exploration_envelope, dict)
                    or exploration_envelope.get("producer") != "OAGNET"
                    or exploration_envelope.get("requirements")
                    != exploration_requirements.model_dump(mode="json")
                ):
                    raise AdapterError(
                        "ASL_EXPLORATION_REQUIREMENTS_UNCONFIRMED",
                        "Oagnet did not confirm the requested exploration requirements",
                    )
        self._remove_unrequested_metric_window_time(asl, request)
        self._repair_model81_trend_time(asl, request, semantic_model_id)
        self._bind_canonical_time_range(
            asl,
            request,
            time_policy=str(
                (intent_asl_contract or {}).get("time_policy") or "OPTIONAL"
            ),
        )
        self._repair_required_model81_dimensions(asl, request, semantic_model_id)
        self._deduplicate_relationship_identity_dimensions(asl)
        self._ensure_required_name_non_null_filters(asl, request)
        self._validate_semantic_entity_mentions(asl, request, asl_repairs)
        bound_metric_codes = {
            metric.metric_id.split(":", 1)[1]
            for metric in request.metrics
            if metric.metric_id and ":" in metric.metric_id
        }
        if bound_metric_codes:
            selected_metric_codes = {
                str(item.get("name"))
                for item in (asl.get("metrics") or [])
                if isinstance(item, dict) and item.get("name")
            }
            if selected_metric_codes != bound_metric_codes:
                # Metric resolution is the authoritative semantic binding. The
                # ASL model occasionally drops the second metric from an
                # otherwise valid multi-metric request. Repair that projection
                # deterministically instead of returning a misleading partial
                # result or an administrator-facing conflict message.
                if (
                    request.primary_intent == PrimaryIntent.METRIC_QUERY
                    and selected_metric_codes.issubset(bound_metric_codes)
                ):
                    asl["metrics"] = [
                        {
                            "name": metric.metric_id.split(":", 1)[1],
                            "alias": metric.canonical_name or metric.input,
                            "time_anchor": None,
                        }
                        for metric in request.metrics
                        if metric.metric_id and ":" in metric.metric_id
                    ]
                    sort = asl.get("sort")
                    if (
                        isinstance(sort, dict)
                        and str(sort.get("field_type") or "").lower() == "metric"
                        and str(sort.get("field") or "") not in bound_metric_codes
                    ):
                        sort["field"] = next(iter(bound_metric_codes))
                else:
                    raise AdapterError(
                        "ASL_METRIC_SELECTION_INVALID",
                        "ASL metrics differ from caller-bound semantic metrics",
                        details={
                            "expected_metric_codes": sorted(bound_metric_codes),
                            "selected_metric_codes": sorted(selected_metric_codes),
                        },
                    )
        ambiguities = asl.get("ambiguity") or []
        if not isinstance(ambiguities, list):
            raise AdapterError("ASL_RESPONSE_INVALID", "ASL ambiguity must be a list")
        if ambiguities:
            raise AdapterError(
                "ASL_AMBIGUOUS",
                "ASL generator returned business ambiguities",
                details=ambiguities,
            )
        self._validate_request_filters(
            asl,
            request,
            exact_operator_contract=intent_contract_confirmed,
        )
        self._validate_no_synthetic_product_filter(asl, request)
        self._validate_dependency_constraints(asl, request)
        if request.primary_intent == PrimaryIntent.DETAIL_QUERY:
            dimensions = asl.get("dimensions")
            if not isinstance(dimensions, list) or not dimensions:
                raise AdapterError(
                    "ASL_ANALYSIS_SHAPE_INVALID",
                    "detail ASL must project at least one registered field",
                )
            missing_fields = self._missing_detail_fields(request.fields, dimensions)
            if missing_fields:
                raise AdapterError(
                    "ASL_DETAIL_FIELDS_INCOMPLETE",
                    "detail ASL omitted one or more explicitly requested fields",
                    details={
                        "requested_fields": request.fields,
                        "missing_fields": missing_fields,
                        "projected_dimensions": dimensions,
                    },
                )
            # DETAIL_QUERY means row-level projection. Some model generations
            # additionally emit a generic metric such as “订单明细”, which turns
            # the query into GROUP BY aggregation and can introduce an unsafe
            # cross-table join. The intent selected by the orchestrator is the
            # authoritative execution shape, so aggregate metrics are removed.
            asl["metrics"] = []
            # Relationship lists are set-shaped even though the physical joins
            # may contain several paths to the same projected combination.  The
            # translator applies this before LIMIT; ordinary order/event detail
            # remains explicitly row-shaped.
            asl["projection_mode"] = (
                "DISTINCT"
                if requires_distinct_relationship_projection(request)
                else "ROWS"
            )
        elif grouped_partner_metric:
            dimensions = asl.get("dimensions")
            metrics = asl.get("metrics")
            if not isinstance(dimensions, list) or not dimensions:
                raise AdapterError(
                    "ASL_ANALYSIS_SHAPE_INVALID",
                    "grouped partner metric ASL must return the partner dimension",
                )
            if not isinstance(metrics, list) or not metrics:
                raise AdapterError(
                    "ASL_ANALYSIS_SHAPE_INVALID",
                    "grouped partner metric ASL must retain requested metrics",
                )
            self._validate_grouped_semantic_dimensions(asl, request)
        elif (
            request.primary_intent == PrimaryIntent.METRIC_QUERY
            and request.metrics
            and self._required_grouped_dimension_roles(request)
        ):
            self._validate_grouped_semantic_dimensions(asl, request)
        if ordered_entity_metric_ranking_request(request):
            sort = asl.get("sort")
            if not (
                isinstance(sort, dict)
                and str(sort.get("field_type") or "").lower() == "metric"
                and str(sort.get("direction") or "").upper() == "DESC"
            ):
                raise AdapterError(
                    "ASL_ANALYSIS_SHAPE_INVALID",
                    "ordered entity ranking ASL must sort the requested metric descending",
                )
            if asl.get("limit") is not None:
                raise AdapterError(
                    "ASL_ANALYSIS_SHAPE_INVALID",
                    "unbounded entity ranking ASL must not add a TOP/BOTTOM limit",
                )
        if request.primary_intent in {
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
        }:
            dimensions = asl.get("dimensions")
            if not isinstance(dimensions, list) or not dimensions:
                raise AdapterError(
                    "ASL_ANALYSIS_SHAPE_INVALID",
                    "analysis ASL must return a comparison/time grouping dimension",
                )
            if (
                request.primary_intent == PrimaryIntent.TREND_ANALYSIS
                and not any(self._is_temporal_asl_item(item) for item in dimensions)
            ):
                raise AdapterError(
                    "ASL_ANALYSIS_SHAPE_INVALID",
                    "trend ASL must group by a registered temporal dimension",
                )

        if asl_cache_key is not None:
            if len(self._asl_plan_cache) >= self.settings.asl_plan_cache_max_items:
                oldest_key = min(
                    self._asl_plan_cache,
                    key=lambda key: self._asl_plan_cache[key][0],
                )
                self._asl_plan_cache.pop(oldest_key, None)
            self._asl_plan_cache[asl_cache_key] = (
                time.monotonic() + self.settings.asl_plan_cache_ttl_seconds,
                copy.deepcopy(asl),
            )

        try:
            translated = await self.client.post(
                self.settings.sql_translator_base_url,
                self.settings.sql_translate_path,
                {
                    "asl": json.dumps(asl, ensure_ascii=False),
                    "modelId": str(semantic_model_id),
                    "analysis_contract": (
                        analysis_contract.model_dump(mode="json")
                        if analysis_contract is not None else None
                    ),
                },
                identity=identity,
                application_id=request.application_id,
                idempotency_key=(
                    f"{request.request_id}:sql-translate:"
                    f"{metric_definition_fingerprints[0][:16]}"
                    if metric_definition_fingerprints
                    else f"{request.request_id}:sql-translate"
                ),
                retryable=True,
            )
        except AdapterError as exc:
            if exc.status_code == 404:
                raise AdapterError(
                    "SQL_TRANSLATION_ENDPOINT_UNAVAILABLE",
                    "SQL translator has not deployed the split translation endpoint",
                    status_code=404,
                ) from exc
            if exc.status_code is not None and exc.status_code >= 500:
                raise AdapterError(
                    "SQL_TRANSLATION_FAILED",
                    "SQL translation service failed",
                    retryable=exc.retryable,
                    status_code=exc.status_code,
                    upstream_code=exc.upstream_code,
                    details=exc.details,
                ) from exc
            raise
        translated = self._unwrap_sql_response(translated)
        if translated.get("success") is not True:
            ambiguity = translated.get("ambiguity")
            if ambiguity:
                raise AdapterError(
                    "SQL_TRANSLATION_AMBIGUOUS",
                    "SQL translator returned business ambiguities",
                    details=ambiguity,
                )
            upstream_code = str(
                translated.get("code")
                or translated.get("error_code")
                or ""
            ) or None
            raise AdapterError(
                "SQL_TRANSLATION_FAILED",
                str(
                    translated.get("error")
                    or "SQL service rejected request"
                ),
                retryable=bool(translated.get("retryable")),
                upstream_code=upstream_code,
            )

        sql = translated.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise AdapterError(
                "SQL_RESPONSE_INVALID", "SQL translation response did not contain SQL"
            )
        sql = sql.strip()
        sql = self._apply_current_metric_formulas(sql, metric_definitions)
        self._validate_read_only_sql(sql)
        self._validate_sql_relationship_graph(sql)
        self._validate_query_to_sql_entity_alignment(request, sql)
        self._validate_geographic_hierarchy_alignment(asl, request, sql)
        metric_bindings = [
            {
                "用户指标": metric.input,
                "标准指标": metric.canonical_name or metric.input,
                "指标ID": metric.metric_id,
            }
            for metric in request.metrics
        ]
        query_shape = {
            "查询对象": (asl.get("subject") or {}).get("entity")
            if isinstance(asl.get("subject"), dict)
            else asl.get("subject"),
            "分组维度": asl.get("dimensions") or [],
            "排序": asl.get("sort"),
            "结果上限": asl.get("limit"),
        }
        await emit_progress(
            "SEMANTIC_QUERY_PLANNING",
            "COMPLETED",
            "工具：智能语义查询器；"
            f"输入：问题={_compact_progress_value(request.rewritten_question or request.original_question, 180)}，"
            f"语义模型={semantic_model_id}，"
            f"业务域={request.business_domain_ids or ([business_domain_id] if business_domain_id else [])}，"
            f"指标诉求={_compact_progress_value([metric.input for metric in request.metrics], 240)}，"
            f"筛选条件={_compact_progress_value(request.filters, 500)}；"
            f"输出：指标绑定={_compact_progress_value(metric_bindings, 500)}，"
            f"查询结构={_compact_progress_value(query_shape, 500)}，"
            f"只读 SQL=已生成（{len(sql)} 字符），安全校验=PASS。",
        )

        execute_payload: dict[str, Any] = {
            "sql": sql,
            "modelId": str(semantic_model_id),
        }
        if analysis_contract is not None:
            execute_payload["analysis_contract"] = analysis_contract.model_dump(mode="json")
        translated_data_source_id = (
            translated.get("dataSourceId") or translated.get("data_source_id")
        )
        selected_data_source_id: str | None = None
        if request.database_id is not None:
            try:
                selected = build_database_load_tool().load(request.database_id)
            except DatabaseLoadError as exc:
                raise AdapterError("DATABASE_ID_INVALID", str(exc)) from exc
            selected_data_source_id = selected.data_source_id
            if (
                translated_data_source_id is not None
                and str(translated_data_source_id) != selected_data_source_id
            ):
                raise AdapterError(
                    "DATA_SOURCE_SCOPE_MISMATCH",
                    "SQL translator selected a database outside the requested scope",
                    details={
                        "requested_database_id": selected_data_source_id,
                        "translated_data_source_id": str(translated_data_source_id),
                    },
                )
            execute_payload["dataSourceId"] = selected_data_source_id
        elif translated_data_source_id is not None:
            execute_payload["dataSourceId"] = translated_data_source_id
        await emit_progress(
            "SQL_EXECUTION",
            "RUNNING",
            "调用工具：SQL 执行服务。\n"
            f"输入：{_compact_progress_value(execute_payload, 1200)}。",
        )
        try:
            executed = await self.client.post(
                self.settings.sql_translator_base_url,
                self.settings.sql_execute_path,
                execute_payload,
                identity=identity,
                application_id=request.application_id,
                idempotency_key=f"{request.request_id}:sql-execute",
                # Database execution is not automatically retried: a timeout does
                # not prove that the upstream query was never started.
                retryable=False,
            )
        except AdapterError as exc:
            if exc.status_code == 404:
                raise AdapterError(
                    "SQL_EXECUTION_ENDPOINT_UNAVAILABLE",
                    "SQL translator has not deployed the split execution endpoint",
                    status_code=404,
                ) from exc
            if exc.status_code is not None and exc.status_code >= 500:
                raise AdapterError(
                    "SQL_EXECUTION_FAILED",
                    "SQL execution service failed",
                    retryable=exc.retryable,
                    status_code=exc.status_code,
                    upstream_code=exc.upstream_code,
                    details=exc.details,
                ) from exc
            raise
        raw = self._unwrap_sql_response(executed)
        if raw.get("success") is not True:
            raise AdapterError(
                "SQL_EXECUTION_FAILED",
                str(raw.get("error") or "query execution failed"),
                retryable=bool(raw.get("retryable")),
                upstream_code=str(raw.get("error_code") or raw.get("code") or "") or None,
            )
        if analysis_contract is not None:
            contract_proof = raw.get("analysis_contract")
            if not isinstance(contract_proof, dict):
                raise AdapterError(
                    "SQL_ANALYSIS_CONTRACT_UNCONFIRMED",
                    "SQL execution did not return analysis contract proof",
                )
            if (
                contract_proof.get("producer") != "SQL_TRANSLATOR"
                or contract_proof.get("operator") != analysis_contract.operator
            ):
                raise AdapterError(
                    "SQL_ANALYSIS_CONTRACT_UNCONFIRMED",
                    "SQL execution returned mismatched analysis contract proof",
                )
            if contract_proof.get("contract_satisfied") is not True:
                raise AdapterError(
                    "ANALYSIS_RESULT_CONTRACT_INVALID",
                    "SQL execution result does not satisfy analysis contract",
                    details={
                        "contract": analysis_contract.model_dump(mode="json"),
                        "violations": contract_proof.get("violations") or {},
                        "returned_columns": raw.get("columns") or [],
                        "returned_row_count": raw.get("row_count"),
                    },
                )
        await emit_progress(
            "SQL_EXECUTION",
            "COMPLETED",
            "SQL 执行服务调用完成。\n"
            f"输出：columns={_compact_progress_value(raw.get('columns') or [], 500)}；"
            f"row_count={raw.get('row_count', len(raw.get('rows') or []))}；"
            f"rows_preview={_compact_progress_value((raw.get('data') or raw.get('preview_data') or [])[:2], 1000)}。\n"
            "正在校验结果集契约和数据源范围。",
        )
        actual_data_source_id = str((raw.get("data_source") or {}).get("id") or "") or None
        if (
            selected_data_source_id is not None
            and actual_data_source_id != selected_data_source_id
        ):
            raise AdapterError(
                "DATA_SOURCE_SCOPE_MISMATCH",
                "SQL execution did not confirm the requested database",
                details={
                    "requested_database_id": selected_data_source_id,
                    "actual_data_source_id": actual_data_source_id,
                },
            )
        result_file_url = self._result_file_url(raw)
        dataset = self._dataset(
            raw,
            request_id=str(request.request_id),
            has_result_file=result_file_url is not None,
            distinct_projection=requires_distinct_relationship_projection(request),
        )
        if analysis_contract is not None and not dataset.truncated:
            violation = validate_contract(
                analysis_contract, dataset.columns, dataset.rows
            )
            if not violation.valid:
                raise AdapterError(
                    "ANALYSIS_RESULT_CONTRACT_INVALID",
                    "query result does not satisfy the selected analysis operator contract",
                    details={
                        "contract": analysis_contract.model_dump(mode="json"),
                        "violations": violation.as_dict(),
                        "returned_columns": dataset.columns,
                        "returned_row_count": dataset.row_count,
                    },
                )
        return DataQueryResult(
            asl=asl,
            sql=sql,
            dataset=dataset,
            data_source_id=actual_data_source_id,
            result_file_url=result_file_url,
        )

    async def _current_metric_definitions(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
    ) -> list[dict[str, Any]]:
        """Read the current published formula for every version-bound metric."""
        definitions: list[dict[str, Any]] = []
        for metric in request.metrics:
            if not metric.metric_id or not metric.version:
                continue
            path = self.settings.semantic_definition_path.format(
                metric_id=metric.metric_id,
                version=metric.version,
            )
            payload = await self.client.get(
                self.settings.semantic_base_url,
                path,
                identity=identity,
                application_id=request.application_id,
            )
            if not isinstance(payload, dict):
                raise AdapterError(
                    "METRIC_DEFINITION_INVALID",
                    "semantic service returned an invalid metric definition",
                )
            returned_id = str(payload.get("metric_id") or "")
            formula = str(
                payload.get("calculation_formula") or payload.get("formula") or ""
            ).strip()
            if returned_id != metric.metric_id or not formula:
                raise AdapterError(
                    "METRIC_DEFINITION_INVALID",
                    "semantic service did not return the requested published formula",
                    details={"metric_id": metric.metric_id},
                )
            definitions.append({
                "metric_id": metric.metric_id,
                "version": str(payload.get("version") or metric.version),
                "formula": formula,
                # Formula and global filters jointly define a governed metric.
                # Including both in the plan fingerprint prevents a published
                # filter change from reusing ASL generated from an older scope.
                "global_filters": (
                    payload.get("global_filters")
                    if isinstance(payload.get("global_filters"), list)
                    else []
                ),
            })
        return definitions

    @classmethod
    def _apply_current_metric_formulas(
        cls,
        sql: str,
        definitions: list[dict[str, Any]],
    ) -> str:
        """Reject or safely repair SQL produced from a stale metric snapshot.

        The only automatic rewrite is a same-table
        ``COUNT(DISTINCT table.field)`` field change. It covers identity-name
        to identity-code corrections without altering joins, filters, grouping,
        or another metric expression.
        """
        current = sql
        for definition in definitions:
            formula = definition["formula"].split("=", 1)[-1].strip()
            expected_match = re.fullmatch(
                r"COUNT\s*\(\s*DISTINCT\s+"
                r"([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*\)",
                formula,
                re.IGNORECASE,
            )
            if expected_match is None:
                expected_fields = set(re.findall(
                    r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*",
                    formula,
                ))
                normalized_sql = current.replace("`", "").lower()
                if any(field.lower() not in normalized_sql for field in expected_fields):
                    raise AdapterError(
                        "METRIC_FORMULA_STALE",
                        "SQL translation does not match the current published metric formula",
                        details={"metric_id": definition["metric_id"]},
                    )
                continue

            expected_field = expected_match.group(1)
            expected_table = expected_field.split(".", 1)[0].lower()
            count_pattern = re.compile(
                r"COUNT\s*\(\s*DISTINCT\s+"
                r"([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*\)",
                re.IGNORECASE,
            )
            candidates = [
                match for match in count_pattern.finditer(current)
                if match.group(1).split(".", 1)[0].lower() == expected_table
            ]
            if any(
                match.group(1).lower() == expected_field.lower()
                for match in candidates
            ):
                continue
            if len(candidates) != 1:
                raise AdapterError(
                    "METRIC_FORMULA_STALE",
                    "SQL translation does not match the current published metric formula",
                    details={"metric_id": definition["metric_id"]},
                )
            stale = candidates[0]
            current = current[:stale.start(1)] + expected_field + current[stale.end(1):]
        return current

    @classmethod
    def _validate_request_filters(
        cls,
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
        *,
        exact_operator_contract: bool = False,
    ) -> None:
        """Fail closed when generated ASL drops a caller-grounded filter value."""
        generated = [
            item for item in (asl.get("filters") or []) if isinstance(item, dict)
        ]
        negative_operators = {
            "NE", "!=", "<>", "NOT_EQ", "NOT EQ", "NOT IN", "NOT_IN", "EXCLUDE",
        }
        null_operators = {
            "IS_NOT_NULL", "IS NOT NULL", "NOT_NULL", "NOT NULL",
        }

        def is_non_null_constraint(item: dict[str, Any]) -> bool:
            operator = str(item.get("operator") or "").upper()
            if operator in null_operators:
                return True
            return (
                operator in negative_operators
                and item.get("value") is not None
                and str(item.get("value")).strip() == ""
            )
        missing: list[dict[str, Any]] = []
        for required in request.filters:
            if not isinstance(required, dict):
                continue
            required_operator = str(required.get("operator") or "EQ").upper()
            if required_operator in null_operators:
                expected_field = str(required.get("field") or "").strip()
                preserved = any(
                    is_non_null_constraint(item)
                    and not cls._missing_detail_fields(
                        [expected_field],
                        [{
                            key: item.get(key)
                            for key in ("field", "name", "attr", "alias")
                            if item.get(key) is not None
                        }],
                    )
                    for item in generated
                )
                if not preserved:
                    missing.append(required)
                continue
            raw_value = required.get("value")
            required_values = raw_value if isinstance(raw_value, list) else [raw_value]
            required_text = {
                str(value).strip().strip("%")
                for value in required_values
                if value not in (None, "")
            }
            if not required_text:
                continue
            required_operator = str(
                required.get("operator") or "EQ"
            ).upper().replace("_", " ")
            required_negative = required_operator in negative_operators
            preserved = False
            for item in generated:
                candidate_operator = str(
                    item.get("operator") or "EQ"
                ).upper().replace("_", " ")
                candidate_negative = candidate_operator in negative_operators
                if candidate_negative != required_negative:
                    continue
                candidate = item.get("value")
                candidate_values = candidate if isinstance(candidate, list) else [candidate]
                candidate_text = {
                    str(value).strip().strip("%")
                    for value in candidate_values
                    if value not in (None, "")
                }
                required_exact = required_operator in {
                    "EQ", "=", "EQUAL", "EQUALS", "NE", "!=", "<>", "NOT EQ",
                }
                candidate_exact = candidate_operator in {
                    "EQ", "=", "EQUAL", "EQUALS", "NE", "!=", "<>", "NOT EQ",
                }
                if exact_operator_contract and (
                    required_exact
                    and candidate_exact
                    and required_text == candidate_text
                ) or (
                    (not exact_operator_contract or not required_exact)
                    and all(
                        any(
                            required_value == candidate_value
                            or required_value in candidate_value
                            or candidate_value in required_value
                            for candidate_value in candidate_text
                        )
                        for required_value in required_text
                    )
                ):
                    preserved = True
                    break
            if not preserved:
                missing.append(required)
        if missing:
            raise AdapterError(
                "ASL_REQUIRED_FILTER_MISSING",
                "ASL did not preserve one or more caller-grounded filters",
                details={"missing_filters": missing},
            )
        required_name_fields = list(dict.fromkeys(
            value.split("=", 1)[1].strip()
            for value in request.assumptions
            if value.startswith("REQUIRED_NAME_NON_NULL=")
            and value.split("=", 1)[1].strip()
        ))
        missing_name_constraints = [
            expected_field
            for expected_field in required_name_fields
            if not any(
                is_non_null_constraint(item)
                and not cls._missing_detail_fields(
                    [expected_field],
                    [{
                        key: item.get(key)
                        for key in ("field", "name", "attr", "alias")
                        if item.get(key) is not None
                    }],
                )
                for item in generated
            )
        ]
        if missing_name_constraints:
            raise AdapterError(
                "ASL_REQUIRED_NAME_NON_NULL_MISSING",
                "ASL omitted a required master-name non-null constraint",
                details={"missing_name_fields": missing_name_constraints},
            )

    @staticmethod
    def _validate_semantic_entity_mentions(
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
        repairs: list[dict[str, Any]],
    ) -> None:
        """Require source-backed proof for every caller-owned untyped noun."""

        mentions = list(dict.fromkeys(
            value.strip()
            for value in request.semantic_entity_mentions
            if value.strip()
        ))
        if not mentions:
            return
        generated_filters = [
            item for item in asl.get("filters") or [] if isinstance(item, dict)
        ]
        unresolved: list[str] = []
        def literal_key(value: object) -> str:
            # Structured extraction may preserve or remove spaces inside the
            # same bilingual legal name.  Once the role-bound filter has been
            # source-validated, that formatting-only variant is already bound
            # and must not trigger a second untyped-entity lookup.
            return re.sub(r"\s+", "", str(value or "").strip()).casefold()

        for mention in mentions:
            direct = any(
                literal_key(str(item.get("value") or "").strip("%"))
                == literal_key(mention)
                for item in generated_filters
            )
            resolved = False
            for repair in repairs:
                if (
                    repair.get("type") != "ADD_SOURCE_RESOLVED_ENTITY_FILTER"
                    or str(repair.get("mention") or "").strip() != mention
                ):
                    continue
                field = str(repair.get("resolved_field") or "").strip()
                canonical = str(repair.get("canonical_value") or "").strip()
                resolved = bool(
                    field
                    and canonical
                    and any(
                        str(item.get("field") or "").strip() == field
                        and str(item.get("value") or "").strip().strip("%")
                        == canonical
                        for item in generated_filters
                    )
                )
                if resolved:
                    break
            if not direct and not resolved:
                unresolved.append(mention)
        if unresolved:
            raise AdapterError(
                "ASL_ENTITY_MENTION_UNRESOLVED",
                "ASL did not provide source-backed bindings for semantic entity mentions",
                details={"unresolved_mentions": unresolved},
            )

    @classmethod
    def _ensure_required_name_non_null_filters(
        cls,
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
    ) -> None:
        """Bind list-name completeness to the currently projected ASL field.

        The ASL model chooses the registered semantic attribute.  Once that
        projection is present, adding a non-null predicate on the exact same
        reference is deterministic and remains valid when physical schemas are
        republished.  No table or column name is guessed by the application.
        """

        required_fields = list(dict.fromkeys(
            value.split("=", 1)[1].strip()
            for value in request.assumptions
            if value.startswith("REQUIRED_NAME_NON_NULL=")
            and value.split("=", 1)[1].strip()
        ))
        if not required_fields:
            return
        filters = [
            item for item in (asl.get("filters") or []) if isinstance(item, dict)
        ]
        dimensions = [
            item for item in (asl.get("dimensions") or []) if isinstance(item, dict)
        ]
        null_operators = {
            "IS_NOT_NULL", "IS NOT NULL", "NOT_NULL", "NOT NULL",
        }

        negative_operators = {
            "NE", "!=", "<>", "NOT_EQ", "NOT IN", "NOT_IN", "EXCLUDE",
        }

        def is_non_null_constraint(item: dict[str, Any]) -> bool:
            operator = str(item.get("operator") or "").upper()
            if operator in null_operators:
                return True
            return (
                operator in negative_operators
                and item.get("value") is not None
                and str(item.get("value")).strip() == ""
            )
        for expected_field in required_fields:
            if any(
                is_non_null_constraint(item)
                and not cls._missing_detail_fields([expected_field], [item])
                for item in filters
            ):
                continue
            candidates = [
                item for item in dimensions
                if not cls._missing_detail_fields([expected_field], [item])
            ]
            if len(candidates) != 1:
                continue
            dimension = candidates[0]
            reference = next(
                (
                    str(dimension.get(key)).strip()
                    for key in ("name", "field")
                    if dimension.get(key) is not None
                    and str(dimension.get(key)).strip()
                ),
                None,
            )
            if reference is None:
                continue
            filters.append({
                "field": reference,
                "operator": "!=",
                "value": "",
            })
        asl["filters"] = filters

    @classmethod
    def _validate_no_synthetic_product_filter(
        cls,
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
    ) -> None:
        """Reject a product-name guess when brand + category fully define scope."""

        requested_families = {
            cls._constraint_field_family(str(item.get("field") or ""))
            for item in request.filters
            if isinstance(item, dict)
        }
        if not {"brand", "category"}.issubset(requested_families):
            return
        if "product" in requested_families:
            return
        synthetic = [
            item for item in (asl.get("filters") or [])
            if isinstance(item, dict)
            and cls._constraint_field_family(str(item.get("field") or "")) == "product"
        ]
        if synthetic:
            raise AdapterError(
                "ASL_SYNTHETIC_PRODUCT_FILTER",
                "ASL invented a product-name filter for an explicit brand/category scope",
                details={"unexpected_filters": synthetic},
            )

    @classmethod
    def _validate_grouped_semantic_dimensions(
        cls,
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
    ) -> None:
        """Require every requested role using current ASL semantic metadata."""

        dimensions = [
            item for item in (asl.get("dimensions") or [])
            if isinstance(item, dict)
        ]
        actual_references = [
            " ".join(
                str(item.get(key) or "")
                for key in ("name", "alias", "attr", "field")
            ).strip()
            for item in dimensions
        ]
        missing: list[str] = []
        required_dimensions = cls._required_grouped_dimension_roles(request)
        for expected in required_dimensions:
            expected_text = str(expected).strip()
            if not expected_text:
                continue
            preserved = any(
                cls._semantic_dimension_role_matches(expected_text, actual)
                for actual in actual_references
            )
            if not preserved:
                missing.append(expected_text)
        if missing:
            raise AdapterError(
                "ASL_REQUIRED_DIMENSION_MISSING",
                "ASL omitted one or more current semantic dimension roles",
                details={
                    "missing_dimensions": missing,
                    "required_grouped_dimensions": required_dimensions,
                    "projected_dimensions": dimensions,
                },
            )
        unexpected = [
            actual
            for actual in actual_references
            if actual
            and not any(
                cls._semantic_dimension_role_matches(expected, actual)
                for expected in required_dimensions
            )
        ]
        if unexpected and "STRICT_GROUPING_DIMENSIONS" in request.assumptions:
            raise AdapterError(
                "ASL_UNREQUESTED_DIMENSION",
                "ASL added grouping dimensions that change the requested grain",
                details={
                    "unexpected_dimensions": unexpected,
                    "required_grouped_dimensions": required_dimensions,
                    "projected_dimensions": dimensions,
                },
            )

    @classmethod
    def _semantic_dimension_role_matches(
        cls,
        expected: str,
        actual: str,
    ) -> bool:
        """Match a business dimension without collapsing city into province."""

        def is_hospital_level(value: str) -> bool:
            normalized = value.casefold()
            return any(
                marker in normalized
                for marker in (
                    "医院等级", "医院级别", "hospital_level", "hospitallevel",
                )
            )

        expected_hospital_level = is_hospital_level(expected)
        actual_hospital_level = is_hospital_level(actual)
        if expected_hospital_level or actual_hospital_level:
            return expected_hospital_level and actual_hospital_level

        if not cls._constraint_field_matches(expected, actual):
            return False
        if cls._constraint_field_family(expected) != "region":
            return True

        def geographic_level(value: str) -> str | None:
            normalized = value.casefold()
            levels = (
                ("province", ("province", "省份", "省级")),
                ("city", ("city", "城市", "市级")),
                ("district", ("district", "区县", "县级")),
                ("region", ("region", "地区", "区域", "地域")),
            )
            return next(
                (
                    level
                    for level, aliases in levels
                    if any(alias in normalized for alias in aliases)
                ),
                None,
            )

        expected_level = geographic_level(expected)
        actual_level = geographic_level(actual)
        return (
            expected_level is None
            or actual_level is None
            or expected_level == "region"
            or actual_level == "region"
            or expected_level == actual_level
        )

    @classmethod
    def _required_grouped_dimension_roles(
        cls,
        request: CanonicalAnalysisRequest,
    ) -> list[str]:
        """Separate result/group roles from dimensions that only constrain scope.

        Intent extraction intentionally records every semantic role mentioned
        by the user.  That does not mean every role belongs in ``GROUP BY``:
        in "Shanghai + brand + category dealer sales", dealer is the result
        group while the other three roles are fixed filters.  Keep the result
        entity and all non-filter roles; filter-only roles remain governed by
        the independent lossless-filter validator.
        """

        filter_fields = [
            str(item.get("field") or "").strip()
            for item in request.filters
            if isinstance(item, dict) and str(item.get("field") or "").strip()
        ]
        required: list[str] = []
        for value in request.dimensions:
            dimension = str(value).strip()
            if not dimension:
                continue
            is_result_entity = bool(
                request.entity
                and cls._semantic_dimension_role_matches(
                    str(request.entity), dimension
                )
            )
            is_filter_only = any(
                cls._semantic_dimension_role_matches(dimension, field)
                for field in filter_fields
            )
            if is_result_entity or not is_filter_only:
                required.append(dimension)
        return list(dict.fromkeys(required))

    @classmethod
    def _validate_geographic_hierarchy_alignment(
        cls,
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
        sql: str,
    ) -> None:
        """Reject province/city filters attached to another business branch.

        A generated query can group a dealer city while filtering the province
        reached through a hospital.  Both labels and the literal value look
        valid in isolation, so ordinary entity/value preservation checks pass,
        but the result answers a different question.  This guard uses only the
        current ASL fields and translated JOIN graph: a geographic filter must
        be directly connected to its geographic GROUP BY table, or connected
        through administrative tables only.  Physical names are never repaired
        or invented here; the semantic relationship service remains the source
        of truth and must regenerate/translate a consistent path.
        """

        if not any(
            cls._constraint_field_family(str(value)) == "region"
            for value in request.dimensions
        ):
            return

        geographic_filter_fields = [
            str(item.get("field") or "").strip().strip("`")
            for item in (asl.get("filters") or [])
            if isinstance(item, dict)
            and cls._constraint_field_family(
                str(item.get("field") or "")
            ) == "region"
            and "." in str(item.get("field") or "")
        ]
        if not geographic_filter_fields:
            return

        group_match = re.search(
            r"\bGROUP\s+BY\b(?P<body>.*?)(?=\bHAVING\b|\bORDER\s+BY\b|"
            r"\bLIMIT\b|$)",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        if group_match is None:
            return
        grouped_geographic_fields = [
            f"{table}.{field}"
            for table, field in re.findall(
                r"`?([A-Za-z_][A-Za-z0-9_]*)`?\."
                r"`?([A-Za-z_][A-Za-z0-9_]*)`?",
                group_match.group("body"),
            )
            if cls._constraint_field_family(f"{table}.{field}") == "region"
        ]
        if not grouped_geographic_fields:
            return

        graph: dict[str, set[str]] = {}
        for left, right in re.findall(
            r"`?([A-Za-z_][A-Za-z0-9_]*)`?\."
            r"`?[A-Za-z_][A-Za-z0-9_]*`?\s*=\s*"
            r"`?([A-Za-z_][A-Za-z0-9_]*)`?\."
            r"`?[A-Za-z_][A-Za-z0-9_]*`?",
            sql,
            re.IGNORECASE,
        ):
            if left == right:
                continue
            graph.setdefault(left, set()).add(right)
            graph.setdefault(right, set()).add(left)

        def shortest_path(source: str, target: str) -> list[str] | None:
            if source == target:
                return [source]
            frontier: list[list[str]] = [[source]]
            visited = {source}
            while frontier:
                path = frontier.pop(0)
                for candidate in sorted(graph.get(path[-1], set())):
                    if candidate in visited:
                        continue
                    next_path = [*path, candidate]
                    if candidate == target:
                        return next_path
                    visited.add(candidate)
                    frontier.append(next_path)
            return None

        mismatches: list[dict[str, Any]] = []
        for grouped_field in grouped_geographic_fields:
            grouped_table = grouped_field.split(".", 1)[0]
            for filter_field in geographic_filter_fields:
                filter_table = filter_field.split(".", 1)[0]
                path = shortest_path(grouped_table, filter_table)
                if path is None:
                    continue
                intermediate = path[1:-1]
                aligned = len(path) <= 2 or all(
                    cls._constraint_field_family(table) == "region"
                    for table in intermediate
                )
                if not aligned:
                    mismatches.append({
                        "grouped_field": grouped_field,
                        "filter_field": filter_field,
                        "join_path": path,
                    })
        if mismatches:
            raise AdapterError(
                "SQL_GEOGRAPHIC_HIERARCHY_MISMATCH",
                "SQL attached an administrative filter to a different business entity branch",
                details={"mismatches": mismatches},
            )

    @staticmethod
    def _validate_query_to_sql_entity_alignment(
        request: CanonicalAnalysisRequest,
        sql: str,
    ) -> dict[str, Any]:
        """Prove that current explicit entities survived and stale ones did not."""

        admission = request.turn_admission
        if admission is None:
            return {"status": "PASS", "checked_filters": 0}
        explicit_filter_slot = admission.current_turn_facts.explicit_slots.get(
            "filters"
        )
        explicit_filters = (
            explicit_filter_slot.value
            if explicit_filter_slot is not None
            and isinstance(explicit_filter_slot.value, list)
            else []
        )
        normalized_sql = sql.replace("`", "")
        folded_sql = normalized_sql.casefold()
        negative_operators = {
            "NE", "!=", "<>", "NOT_EQ", "NOT IN", "NOT_IN", "EXCLUDE",
        }
        missing: list[dict[str, Any]] = []
        polarity_errors: list[dict[str, Any]] = []
        operator_errors: list[dict[str, Any]] = []
        current_by_field: dict[str, set[str]] = {}
        for item in explicit_filters:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "")
            raw_values = item.get("value")
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            texts = {
                str(value).strip().strip("%")
                for value in values
                if value not in (None, "")
            }
            if field and texts:
                current_by_field.setdefault(field, set()).update(texts)
            for value in texts:
                aliases = HttpDataRetrievalAdapter._sql_entity_value_aliases(
                    field, value
                )
                if not any(alias.casefold() in folded_sql for alias in aliases):
                    missing.append({"field": field, "value": value})
                    continue
                if str(item.get("operator") or "EQ").upper() in negative_operators:
                    polarity_preserved = any(
                        re.search(
                            rf"(?:!=|<>|\bnot\s+(?:in|like)\b)[^;]{{0,160}}"
                            rf"['\"]?{re.escape(alias)}['\"]?",
                            normalized_sql,
                            flags=re.I,
                        )
                        is not None
                        for alias in aliases
                    )
                    if not polarity_preserved:
                        polarity_errors.append({"field": field, "value": value})
                elif str(item.get("operator") or "EQ").upper() in {
                    "EQ", "=", "EQUALS",
                }:
                    equality_preserved = any(
                        re.search(
                            rf"(?:(?<![<>!])=(?!=)\s*['\"]?{re.escape(alias)}['\"]?"
                            rf"|\bin\s*\([^)]*['\"]?{re.escape(alias)}['\"]?[^)]*\))",
                            normalized_sql,
                            flags=re.I,
                        )
                        is not None
                        for alias in aliases
                    )
                    if not equality_preserved:
                        operator_errors.append({
                            "field": field,
                            "value": value,
                            "expected_operator": "EQ",
                        })
        if missing:
            raise AdapterError(
                "SQL_QUERY_ENTITY_ALIGNMENT_FAILED",
                "SQL did not preserve one or more current-turn entities",
                details={"missing_entities": missing},
            )
        if polarity_errors:
            raise AdapterError(
                "SQL_QUERY_FILTER_POLARITY_FAILED",
                "SQL reversed or dropped a current negative filter",
                details={"filters": polarity_errors},
            )
        if operator_errors:
            raise AdapterError(
                "SQL_QUERY_FILTER_OPERATOR_FAILED",
                "SQL weakened an exact current filter into a different operator",
                details={"filters": operator_errors},
            )

        stale: list[dict[str, Any]] = []
        for item in admission.context_before.get("filters", []):
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "")
            raw_values = item.get("value")
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            for value in values:
                text = str(value or "").strip().strip("%")
                if (
                    text
                    and field in current_by_field
                    and not any(
                        HttpDataRetrievalAdapter._sql_entity_values_equivalent(
                            field, text, current
                        )
                        for current in current_by_field[field]
                    )
                    and any(
                        alias.casefold() in folded_sql
                        for alias in HttpDataRetrievalAdapter._sql_entity_value_aliases(
                            field, text
                        )
                    )
                ):
                    stale.append({"field": field, "value": text})
        if stale:
            raise AdapterError(
                "STALE_CONTEXT_CONFLICT",
                "SQL contains an entity replaced by the current turn",
                details={"stale_entities": stale},
            )
        return {
            "status": "PASS",
            "checked_filters": len(explicit_filters),
        }

    @staticmethod
    def _sql_entity_value_aliases(field: str, value: str) -> set[str]:
        """Return only field-safe aliases accepted by the SQL alignment gate.

        Users commonly append the generic object word ``产品`` or ``商品`` to
        a concrete product name, while the semantic layer stores the canonical
        name without that suffix.  Treat that one normalization as equivalent
        only for product-name fields.  Other entity families remain exact so a
        manufacturer, region, or negative filter cannot be weakened.
        """

        normalized = str(value).strip().strip("%")
        if not normalized:
            return set()
        aliases = {normalized}
        normalized_field = str(field).strip().casefold()
        is_product_field = (
            normalized_field in {"商品", "商品名称", "产品", "产品名称"}
            or "product" in normalized_field
        )
        is_hospital_level_field = (
            normalized_field in {"医院等级", "医院级别"}
            or "hospital_level" in normalized_field
            or "hospitallevel" in normalized_field
        )
        if is_product_field:
            for suffix in ("产品", "商品"):
                if not normalized.endswith(suffix):
                    continue
                canonical = normalized[: -len(suffix)].strip()
                # Never turn a specific entity requirement into a one-character
                # substring check (for example ``A产品`` -> ``A``).
                if len(canonical) >= 2:
                    aliases.add(canonical)
        if is_hospital_level_field and normalized.endswith("医院"):
            canonical = normalized[:-2].strip()
            if canonical:
                aliases.add(canonical)
        return aliases

    @staticmethod
    def _sql_entity_values_equivalent(field: str, left: str, right: str) -> bool:
        left_aliases = {
            value.casefold()
            for value in HttpDataRetrievalAdapter._sql_entity_value_aliases(
                field, left
            )
        }
        right_aliases = {
            value.casefold()
            for value in HttpDataRetrievalAdapter._sql_entity_value_aliases(
                field, right
            )
        }
        return bool(left_aliases & right_aliases)

    @staticmethod
    def _reuse_time_only_asl(
        request: CanonicalAnalysisRequest, semantic_model_id: int | None
    ) -> dict[str, Any] | None:
        if (
            "DETERMINISTIC_LIMIT_FAST_PATH" in request.assumptions
            and request.asl_template is not None
            and request.ranking_limit is not None
            and semantic_model_id is not None
        ):
            template = copy.deepcopy(request.asl_template)
            template["limit"] = request.ranking_limit
            # Preserve every previously grounded metric, filter, relation and
            # time anchor.  Only the presentation limit is allowed to change.
            return template
        if (
            "DETERMINISTIC_GRAIN_FAST_PATH" in request.assumptions
            and request.asl_template is not None
            and semantic_model_id is not None
        ):
            grain = next(
                (
                    value.split("=", 1)[1]
                    for value in request.assumptions
                    if value.startswith("DEFAULT_TIME_GRANULARITY=")
                ),
                None,
            )
            template = copy.deepcopy(request.asl_template)
            dimensions = template.get("dimensions")
            temporal = next(
                (
                    item for item in dimensions
                    if isinstance(item, dict)
                    and HttpDataRetrievalAdapter._is_temporal_asl_item(item)
                ),
                None,
            ) if isinstance(dimensions, list) else None
            if grain is None or temporal is None:
                return None
            temporal["granularity"] = grain
            time_context = template.get("time_context")
            if request.time_range is not None and isinstance(time_context, dict):
                template["time_context"] = {
                    **time_context,
                    "type": "range",
                    "start": request.time_range.start.isoformat(),
                    "end": (
                        request.time_range.end_exclusive - timedelta(days=1)
                    ).isoformat(),
                }
            template["ambiguity"] = []
            return template
        if (
            "DETERMINISTIC_TIME_FAST_PATH" not in request.assumptions
            or request.asl_template is None
            or request.time_range is None
            or semantic_model_id is None
        ):
            return None
        template = copy.deepcopy(request.asl_template)
        metrics = template.get("metrics")
        time_context = template.get("time_context")
        if not isinstance(metrics, list) or not isinstance(time_context, dict):
            return None
        expected = {
            metric.metric_id.split(":", 1)[1]
            for metric in request.metrics
            if metric.metric_id and metric.metric_id.startswith(f"{semantic_model_id}:")
        }
        selected = {
            str(item.get("name"))
            for item in metrics
            if isinstance(item, dict) and item.get("name")
        }
        # Detail queries legitimately have no metrics.  They still need the
        # deterministic time fast path: regenerating ASL for a time-only
        # follow-up can reinterpret a previously grounded brand filter as a
        # product-name LIKE filter.  Reuse is safe when the verified template
        # is a row projection with at least one dimension and both metric sets
        # are empty.
        detail_projection = (
            not expected
            and not selected
            and str(template.get("projection_mode") or "ROWS").upper() == "ROWS"
            and isinstance(template.get("dimensions"), list)
            and bool(template.get("dimensions"))
        )
        if (
            (not expected and not detail_projection)
            or selected != expected
            or not time_context.get("anchor")
        ):
            return None
        template["time_context"] = {
            **time_context,
            "type": "custom",
            "start": request.time_range.start.isoformat(),
            "end": (request.time_range.end_exclusive - timedelta(days=1)).isoformat(),
        }
        template["ambiguity"] = []
        return template

    @staticmethod
    def _repair_model81_trend_time(
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
        semantic_model_id: int | None,
    ) -> None:
        """Bind model 81 trends to its authoritative transaction-date dimension."""
        if (
            semantic_model_id != 81
            or request.primary_intent != PrimaryIntent.TREND_ANALYSIS
            or request.time_range is None
        ):
            return
        # Contextual grain replacements are rendered into the canonical
        # executable question while ``original_question`` intentionally keeps
        # the audit history. Prefer executable text so “改成按季度” cannot be
        # repaired back to the previous monthly dimension.
        text = request.rewritten_question or request.original_question
        granularity = next(
            (
                value
                for pattern, value in (
                    (r"按(?:日|天)", "day"),
                    (r"按周", "week"),
                    (r"按月", "month"),
                    (r"按季度", "quarter"),
                    (r"按年", "year"),
                )
                if re.search(pattern, text)
            ),
            "month",
        )
        dimensions = asl.get("dimensions")
        if not isinstance(dimensions, list):
            dimensions = []
        temporal = next(
            (
                item for item in dimensions
                if HttpDataRetrievalAdapter._is_temporal_asl_item(item)
            ),
            None,
        )
        if temporal is None:
            temporal = {
                "name": "transaction_date",
                "attr": None,
                "level": None,
                "granularity": granularity,
            }
            # A time repair must not discard requested business dimensions
            # such as product, dealer, or hospital.
            asl["dimensions"] = [*dimensions, temporal]
        else:
            temporal["granularity"] = granularity
        asl["time_context"] = {
            "type": "range",
            "anchor": "sales_order.created_date",
            "start": request.time_range.start.isoformat(),
            "end": (request.time_range.end_exclusive - timedelta(days=1)).isoformat(),
        }
        asl["sort"] = {
            # Keep ORDER BY aligned with the actual selected time dimension.
            # Oagnet may return the physical anchor rather than the semantic
            # dimension code; mixing the two produces an unknown SQL column.
            "field": str(temporal.get("name") or "transaction_date"),
            "direction": "ASC",
            "field_type": "dimension",
        }

    @staticmethod
    def _repair_required_model81_dimensions(
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
        semantic_model_id: int | None,
    ) -> None:
        """Restore stable model-81 dimensions explicitly requested by users.

        Oagnet occasionally keeps the hospital name grouping but drops the
        sibling hospital-level field. Model 81 has a registered, unambiguous
        hospital.hospital_level attribute, so this repair is safer than either
        returning an incomplete table or asking the user for information that
        was already present in the question.
        """
        if semantic_model_id != 81 or "医院等级" not in request.dimensions:
            return
        dimensions = asl.get("dimensions")
        if not isinstance(dimensions, list):
            dimensions = []
        projected = {
            re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(item.get("name") or "").lower())
            for item in dimensions
            if isinstance(item, dict)
        }
        if not any(
            token in name
            for name in projected
            for token in ("医院等级", "医院级别", "hospitallevel", "hospitalgrade")
        ):
            dimensions.append({
                "name": "hospital.hospital_level",
                "attr": None,
                "level": None,
                "granularity": None,
            })
            asl["dimensions"] = dimensions

    @staticmethod
    def _deduplicate_relationship_identity_dimensions(asl: dict[str, Any]) -> None:
        """Remove a relationship's duplicate identity-name projection.

        The ASL generator can emit both ``dealer`` and
        ``dealer.dealer_name`` for a request grouped by dealer.  The SQL
        translator already expands the relationship dimension to its business
        display name, so keeping the physical name attribute produces two
        identical SELECT/GROUP BY columns.  Prefer the relationship dimension
        because it preserves the model's business alias; unrelated attributes
        such as ``dealer.province`` remain untouched.
        """
        dimensions = asl.get("dimensions")
        if not isinstance(dimensions, list):
            return

        relationship_names = {
            str(item.get("name") or "").strip().lower()
            for item in dimensions
            if isinstance(item, dict)
            and "." not in str(item.get("name") or "").strip()
            and str(item.get("name") or "").strip()
        }
        seen: set[str] = set()
        deduplicated: list[Any] = []
        removed_to_relationship: dict[str, str] = {}
        for item in dimensions:
            if not isinstance(item, dict):
                deduplicated.append(item)
                continue
            name = str(item.get("name") or "").strip()
            normalized = name.lower()
            if normalized in seen:
                continue
            seen.add(normalized)

            parts = normalized.split(".")
            if len(parts) == 2:
                relationship, attribute = parts
                if (
                    relationship in relationship_names
                    and attribute in {"name", f"{relationship}_name"}
                ):
                    removed_to_relationship[normalized] = relationship
                    continue
            deduplicated.append(item)

        if len(deduplicated) == len(dimensions):
            return
        asl["dimensions"] = deduplicated

        sort = asl.get("sort")
        if isinstance(sort, dict):
            sort_field = str(sort.get("field") or "").strip().lower()
            if sort_field in removed_to_relationship:
                sort["field"] = removed_to_relationship[sort_field]

    @staticmethod
    def _bind_canonical_time_range(
        asl: dict[str, Any],
        request: CanonicalAnalysisRequest,
        *,
        time_policy: str = "OPTIONAL",
    ) -> None:
        """Keep generated ASL on the canonical half-open request interval.

        The SQL translator's custom/range contract treats ``end`` as inclusive,
        while the analysis request stores ``end_exclusive``.  Rebinding both
        dates here prevents a multi-turn month range from accidentally including
        the first day of the following month.
        """
        if request.time_range is None:
            return
        time_context = asl.get("time_context")
        if not isinstance(time_context, dict) or not time_context.get("anchor"):
            if time_policy.upper() == "REQUIRED":
                raise AdapterError(
                    "ASL_TIME_ANCHOR_MISSING",
                    "time-bound analysis omitted a registered semantic time anchor",
                )
            return
        asl["time_context"] = {
            **time_context,
            "type": (
                time_context.get("type")
                if time_context.get("type") in {"custom", "range"}
                else "range"
            ),
            "start": request.time_range.start.isoformat(),
            "end": (
                request.time_range.end_exclusive - timedelta(days=1)
            ).isoformat(),
        }

    @staticmethod
    def _is_temporal_asl_item(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        granularity = str(item.get("granularity") or "").strip().lower()
        reference = ".".join(
            str(item.get(key) or "")
            for key in ("name", "field", "attr", "anchor")
        ).lower()
        is_time_reference = bool(re.search(
            r"(?:^|[._])(?:date|time|datetime|createdat|created_at|updatedat|"
            r"updated_at|paytime|pay_time|transactiondate|transaction_date|"
            r"day|week|month|quarter|year)(?:$|[._])|日期|时间|年月|月份",
            reference,
        ))
        if not is_time_reference:
            return False
        return granularity in {
            "", "hour", "day", "week", "month", "quarter", "year",
            "小时", "日", "天", "周", "月", "季度", "年",
        }

    @classmethod
    def _remove_unrequested_metric_window_time(
        cls, asl: dict[str, Any], request: CanonicalAnalysisRequest
    ) -> None:
        """Remove calendar scope invented from a precomputed metric's name."""
        if request.time_range is not None or not any(
            value.startswith("METRIC_WINDOW_IS_DEFINITION=")
            for value in request.assumptions
        ):
            return
        asl["time_context"] = None
        for key in ("dimensions", "filters"):
            values = asl.get(key)
            if isinstance(values, list):
                asl[key] = [
                    item for item in values
                    if not cls._is_temporal_asl_item(item)
                ]

    @classmethod
    def _unwrap_sql_response(cls, value: Any) -> dict[str, Any]:
        """Accept the new flat contract and the legacy nested result wrapper."""
        value = cls._json_object(value, "SQL_RESPONSE_INVALID")
        if "result" not in value:
            return value
        nested = cls._json_object(value.get("result"), "SQL_RESPONSE_INVALID")
        # Outer transport status wins only when it explicitly reports failure;
        # all business fields come from the inner response.
        if value.get("success") is False:
            nested["success"] = False
        for key in ("error", "code", "error_code", "retryable", "ambiguity"):
            if key not in nested and key in value:
                nested[key] = value[key]
        return nested

    _DETAIL_FIELD_TOKENS = {
        # Model 81 registers the business order identifier as order_key.
        # It is the same user-facing concept as 订单号; rejecting it caused
        # valid sales-detail ASL to fail the projection completeness gate.
        "订单号": ("订单号", "orderid", "orderno", "ordercode", "orderkey"),
        "金额": ("金额", "amount", "payamount", "paymentamount"),
        "状态": ("状态", "status", "state"),
        "门店": ("门店", "店铺", "shop", "store"),
        "商品名称": ("商品名称", "商品名", "商品", "product", "goodsname", "productname", "itemname"),
        "商品分类": ("商品分类", "品类", "类目", "goodscategory", "productcategory", "categoryname"),
        "客户名称": ("客户名称", "客户名", "customername", "clientname"),
        "供应商名称": ("供应商名称", "供应商", "suppliername", "vendorname"),
        "经销商名称": ("经销商名称", "经销商", "dealer", "dealername", "distributorname"),
        "医院名称": ("医院名称", "医院", "hospital", "hospitalname", "medicalinstitutionname"),
        "厂家名称": ("厂家名称", "厂家", "厂商", "manufacturer", "manufacturername", "makername"),
        "制造商名称": ("制造商名称", "制造商", "manufacturer", "manufacturername", "makername"),
        "品牌名称": ("品牌名称", "品牌", "brandname"),
        "母品牌": ("母品牌", "母品牌名称", "parentbrand", "parentbrandname"),
        "医院等级": ("医院等级", "医院级别", "hospitallevel", "hospitalgrade"),
        "适用科室": ("适用科室", "科室名称", "deptname", "departmentname"),
        "科室名称": ("科室名称", "适用科室", "deptname", "departmentname"),
        "联系人姓名": ("联系人姓名", "联系人", "contactname", "linkman"),
        "联系人手机号": ("联系人手机号", "联系电话", "联系方式", "contactphone", "mobile", "phone"),
        "邮箱": ("邮箱", "电子邮箱", "email", "mail"),
        "地址": ("地址", "联系地址", "注册地址", "address", "location"),
        "所在省份": ("所在省份", "省份", "province"),
        "所在城市": ("所在城市", "城市", "city"),
        "合作状态": ("合作状态", "状态", "cooperationstatus", "status"),
    }

    _DETAIL_RETRIEVAL_CONCEPT_GROUPS = (
        ("产品", "商品", "货品", "物料"),
        ("商品分类", "产品分类", "品类", "类目", "类别", "耗材"),
        ("经销商", "分销商", "渠道商"),
        ("供应商", "供货商", "厂商"),
        ("科室", "部门", "组织"),
        ("医院", "医疗机构", "院区"),
        ("客户", "会员", "用户"),
        ("门店", "店铺", "网点"),
        ("订单", "交易", "销售"),
        ("品牌", "厂牌", "制造商", "生产厂家"),
        ("联系方式", "联系人", "电话", "手机", "邮箱", "地址"),
        ("地区", "区域", "省份", "城市"),
    )

    @staticmethod
    def _detail_semantic_query(
        request: CanonicalAnalysisRequest, semantic_query: str
    ) -> str:
        """Render overloaded coverage wording as an unambiguous row request.

        Semantic models commonly register ``医院覆盖`` both as a count metric
        synonym and as a natural way to request the covered-hospital list.  Once
        intent normalization has explicitly selected ``DETAIL_QUERY`` and bound
        a concrete result entity/field, retaining the metric-shaped phrase makes
        Oagnet's exact-metric guard require a count that the row query must not
        execute.  This rendering changes only the execution wording; the
        canonical request remains the auditable source of intent and fields.
        """
        entity = str(request.entity or "").strip()
        rendered = semantic_query
        if entity and f"{entity}覆盖" in rendered:
            replacement = f"覆盖到的{entity}"
            if f"{entity}名称" in request.fields:
                replacement += "名称"
            rendered = re.sub(
                rf"{re.escape(entity)}覆盖(?:情况|数据)?(?:明细)?",
                replacement + "明细",
                rendered,
            )
        if request.fields:
            rendered += "；逐行明细必须返回字段：" + "、".join(request.fields)
        return rendered

    @classmethod
    def _detail_time_uses_sales_records(
        cls, request: CanonicalAnalysisRequest, semantic_query: str
    ) -> bool:
        """Whether the requested range explicitly qualifies sales facts.

        Generic occurrences such as ``销售人员`` are subject labels, not sales
        events.  Only a trusted structured assumption or an explicit fact/event
        phrase can select the transaction-time semantic scope.
        """
        if any(
            assumption in request.assumptions
            for assumption in (
                "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE",
                "TRANSACTION_TIME_SCOPE=SALES_RECORD",
            )
        ):
            return True
        if request.time_range is None:
            return False
        text = " ".join([
            semantic_query,
            request.original_question,
            str(request.entity or ""),
            *request.fields,
        ])
        return cls._has_explicit_sales_fact_event(text)

    @staticmethod
    def _has_explicit_sales_fact_event(text: str) -> bool:
        return bool(re.search(
            r"销售(?:记录|订单|明细|数据|额|金额|量|趋势|走势|事实)|"
            r"(?:交易|订单|成交|购买)(?:记录|明细|数据|事实)|"
            r"(?:有|存在|发生|产生|正在|曾经|目前).{0,8}"
            r"销售(?!人员|员工|团队|岗位|部门|代表|经理)",
            text,
        ))

    @classmethod
    def _detail_retrieval_query(
        cls, request: CanonicalAnalysisRequest, semantic_query: str
    ) -> str:
        """Build compact relationship-aware recall text for detail projections.

        The execution question retains all user literals and filters. Recall has
        a different job: it must bring back the result entity, requested
        attributes and connector entities. Reusing a long business sentence can
        crowd a related profile/master-data entity out of a small vector top-k,
        while using only a display phrase such as ``适用科室`` can select a
        denormalized summary column on the subject. This compact text expands
        only generic business concepts found in the request and never invents a
        physical table or field name.
        """
        values: list[str] = []

        def add(value: str | None) -> None:
            normalized = re.sub(r"\s+", "", value or "").strip()
            if normalized and normalized not in values:
                values.append(normalized)

        add(request.entity)
        if request.entity:
            add(f"{request.entity}档案")
            add(f"{request.entity}画像")
        for field in request.fields:
            add(field)
        # Retrieval must see the caller's semantic roles as well as the raw
        # literals.  A value such as ``上海市`` may rank a province record above
        # the current ``城市`` dimension, while ASL generation still correctly
        # chooses a city field.  Supplying the role labels lets Oagnet recall
        # the latest published dimension/entity mapping instead of treating
        # that valid field as an unregistered invention.  Only semantic labels
        # are added here; physical fields continue to come exclusively from
        # the current semantic model.
        for dimension in request.dimensions:
            add(str(dimension))
        for item in request.filters:
            if isinstance(item, dict):
                add(str(item.get("field") or ""))
        for token in re.findall(r"(?<![0-9A-Za-z])[0-9A-Za-z][0-9A-Za-z_.-]{1,63}", semantic_query):
            add(token)

        concept_text = " ".join(
            [semantic_query, request.entity or "", *request.fields]
        ).casefold()
        for group in cls._DETAIL_RETRIEVAL_CONCEPT_GROUPS:
            if (
                "销售" in group
                and re.search(r"销售(?:人员|员工|团队|岗位|部门|代表|经理)", concept_text)
                and not cls._has_explicit_sales_fact_event(concept_text)
            ):
                continue
            if any(term.casefold() in concept_text for term in group):
                for term in group:
                    add(term)
        partner_query = bool(
            request.entity in {"经销商", "供应商"}
            or any(term in concept_text for term in ("经销商", "供应商", "分销商", "渠道商"))
        )
        product_sales_query = (
            any(term in concept_text for term in ("产品", "商品", "货品", "物料"))
            and any(term in concept_text for term in ("销售", "订单", "交易", "成交"))
        )
        if partner_query and product_sales_query:
            # A natural phrase such as ``<name>产品`` may denote either a
            # manufacturer or a brand. Recall the registered catalog endpoint
            # and let source-backed value resolution choose the unique field.
            for term in ("品牌", "厂牌", "制造商", "生产厂家"):
                add(term)
        if cls._detail_time_uses_sales_records(request, semantic_query):
            # These are semantic labels, not physical names. They make the
            # registered transaction-date dimension available to Oagnet after a
            # clarification renderer has intentionally removed the old wording.
            # Explicit date ranges need the same anchor recall for relationship
            # detail queries such as hospital coverage and cooperating partners;
            # otherwise an unrelated master-data date can win vector recall and
            # force a second, spurious time-field clarification.
            for term in ("销售记录", "销售订单", "交易日期", "销售日期", "订单日期"):
                add(term)
            # A machine-readable semantic marker survives question rewriting
            # and tells Oagnet that the explicit date range qualifies joined
            # sales facts. It selects only from registered recalled time
            # bindings; it never exposes or prescribes a physical field.
            add("TRANSACTION_TIME_SCOPE=SALES_RECORD")
        # Keep the original wording last so domain-specific names and values
        # remain available to semantic recall after the structural anchors have
        # established which relationship endpoints and attributes are needed.
        add(semantic_query)
        return " ".join(values)

    @classmethod
    def _missing_detail_fields(
        cls, requested_fields: list[str], dimensions: list[Any]
    ) -> list[str]:
        """Verify that every explicitly requested row-level field is projected.

        ASL dimensions may use a Chinese alias or a registered physical name,
        so matching is conservative over a small auditable synonym table.
        Unknown field names still require an exact normalized projection.
        """
        projected_parts: list[str] = []
        for dimension in dimensions:
            values = dimension.values() if isinstance(dimension, dict) else [dimension]
            projected_parts.extend(str(value) for value in values if value is not None)
        projected = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", " ".join(projected_parts).lower())

        missing: list[str] = []
        for field in requested_fields:
            normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", field.lower())
            tokens = cls._DETAIL_FIELD_TOKENS.get(field, (normalized,))
            if not any(token and token.lower() in projected for token in tokens):
                missing.append(field)
        return missing

    @staticmethod
    def _validate_read_only_sql(sql: str) -> None:
        """Fail closed before calling the independently exposed SQL executor.

        The SQL comes from a trusted translator, but the execute endpoint accepts
        raw SQL. This guard prevents a translator defect or prompt injection from
        turning the data agent into a write-capable database client.
        """
        if len(sql) > 200_000:
            raise AdapterError("SQL_SAFETY_REJECTED", "generated SQL is too large")
        # Remove literals and comments before token inspection so keywords inside
        # a label do not trigger false positives while hidden comment payloads do.
        normalized = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
        normalized = re.sub(r"--[^\r\n]*", " ", normalized)
        normalized = re.sub(r"'(?:''|\\.|[^'])*'", "''", normalized)
        normalized = re.sub(r'"(?:""|\\.|[^"])*"', '""', normalized)
        normalized = normalized.strip()
        statements = [part.strip() for part in normalized.split(";") if part.strip()]
        if len(statements) != 1 or not re.match(r"^(select|with)\b", statements[0], re.I):
            raise AdapterError(
                "SQL_SAFETY_REJECTED", "only one read-only SELECT statement is allowed"
            )
        forbidden = re.compile(
            r"\b(insert|update|delete|replace|merge|create|alter|drop|truncate|rename|"
            r"grant|revoke|call|execute|prepare|handler|load|outfile|dumpfile|"
            r"for\s+update|lock\s+in\s+share\s+mode|get_lock|release_lock|sleep|benchmark)\b",
            re.I,
        )
        if forbidden.search(statements[0]):
            raise AdapterError(
                "SQL_SAFETY_REJECTED", "generated SQL contains a prohibited operation"
            )

    @staticmethod
    def _validate_sql_relationship_graph(sql: str) -> None:
        """Reject SQL that references a relation table it never joined.

        A stale/incomplete semantic relation can otherwise produce syntactically
        plausible SQL whose ON/WHERE clauses mention a missing bridge table.
        Detecting that defect before execution yields a stable retryable semantic
        error instead of leaking a database-specific "unknown column" failure.
        """
        normalized = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
        normalized = re.sub(r"--[^\r\n]*", " ", normalized)
        normalized = re.sub(r"'(?:''|\\.|[^'])*'", "''", normalized)
        table_pattern = re.compile(
            r"\b(?:FROM|JOIN)\s+`?([A-Za-z_]\w*)`?"
            r"(?:\s+(?:AS\s+)?`?([A-Za-z_]\w*)`?)?",
            re.I,
        )
        reserved = {
            "on", "where", "left", "right", "inner", "outer", "join",
            "group", "order", "having", "limit", "union", "cross",
        }
        declared: set[str] = set()
        for match in table_pattern.finditer(normalized):
            declared.add(match.group(1).lower())
            alias = (match.group(2) or "").lower()
            if alias and alias not in reserved:
                declared.add(alias)
        referenced = {
            match.group(1).lower()
            for match in re.finditer(
                r"(?<![A-Za-z0-9_])`?([A-Za-z_]\w*)`?\s*\.\s*`?[A-Za-z_]\w*`?",
                normalized,
            )
        }
        missing = sorted(referenced - declared)
        if missing:
            raise AdapterError(
                "SQL_RELATIONSHIP_GRAPH_INCOMPLETE",
                "generated SQL references tables that are absent from FROM/JOIN",
                details={"missing_tables": missing},
            )

    @staticmethod
    def _result_file_url(data: dict[str, Any]) -> str | None:
        value = next(
            (
                data.get(key)
                for key in (
                    "result_file_url", "download_url", "minio_url", "data_url", "file_url"
                )
                if data.get(key) is not None
            ),
            None,
        )
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip().lower().startswith(("http://", "https://")):
            raise AdapterError(
                "SQL_RESPONSE_INVALID", "large-result MinIO URL must be an absolute HTTP(S) URL"
            )
        value = value.strip()
        if len(value) > 4096:
            raise AdapterError("SQL_RESPONSE_INVALID", "large-result MinIO URL is too long")
        return value

    @staticmethod
    def _json_object(value: Any, code: str) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise AdapterError(code, "response contains invalid JSON") from exc
        if not isinstance(value, dict):
            raise AdapterError(code, "response JSON object missing")
        return value

    @staticmethod
    def _dataset(
        data: dict[str, Any], *, request_id: str, has_result_file: bool = False,
        distinct_projection: bool = False,
    ) -> Dataset:
        rows_from_data = data.get("data")
        uses_preview = (
            ("data" not in data and "preview_data" in data)
            or (
                has_result_file
                and isinstance(rows_from_data, list)
                and isinstance(data.get("row_count"), int)
                and data["row_count"] > len(rows_from_data)
            )
            or "preview_count" in data
            or "preview_truncated" in data
        )
        rows = data.get("data", data.get("preview_data"))
        columns = data.get("columns")
        download_only = rows is None and has_result_file
        if download_only:
            rows = []
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise AdapterError("SQL_RESPONSE_INVALID", "query data must be a list of objects")
        if not isinstance(columns, list) or not all(isinstance(col, str) for col in columns):
            raise AdapterError("SQL_RESPONSE_INVALID", "query columns must be a list of strings")
        declared_count = (
            data.get("preview_count", data.get("preview_row_count", len(rows)))
            if uses_preview
            else len(rows) if download_only else data.get("row_count", len(rows))
        )
        if not isinstance(declared_count, int) or isinstance(declared_count, bool) or declared_count != len(rows):
            raise AdapterError("SQL_RESPONSE_INVALID", "row_count does not match returned preview rows")
        total_count = data.get("total_count")
        if uses_preview and total_count is None:
            total_count = data.get("row_count")
        if download_only and total_count is None:
            total_count = data.get("row_count")
        if total_count is not None and (
            not isinstance(total_count, int)
            or isinstance(total_count, bool)
            or total_count < len(rows)
        ):
            raise AdapterError(
                "SQL_RESPONSE_INVALID",
                "total_count must be an integer no smaller than returned rows",
            )
        truncated_raw = data.get("truncated", data.get("preview_truncated"))
        if truncated_raw is not None and not isinstance(truncated_raw, bool):
            raise AdapterError("SQL_RESPONSE_INVALID", "truncated must be a boolean")
        truncated = (
            truncated_raw
            if isinstance(truncated_raw, bool)
            else download_only or total_count is not None and total_count > len(rows)
        )
        if total_count is not None and total_count > len(rows) and not truncated:
            raise AdapterError(
                "SQL_RESPONSE_INVALID",
                "truncated=false conflicts with total_count greater than returned rows",
            )
        if distinct_projection and rows:
            # Relationship queries are sets.  Keep a final defensive boundary
            # here because legacy translator versions and physical join paths
            # can still return duplicate visible rows even when the ASL asks
            # for DISTINCT.  Normalize only surrounding whitespace and dedupe
            # the complete projected row; ordinary transaction detail never
            # enters this branch.
            normalized_rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in rows:
                normalized = {
                    key: value.strip() if isinstance(value, str) else value
                    for key, value in row.items()
                }
                marker = json.dumps(
                    normalized,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                if marker in seen:
                    continue
                seen.add(marker)
                normalized_rows.append(normalized)
            rows = normalized_rows
            if not truncated:
                total_count = len(rows)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"columns": columns, "rows": rows},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()[:20]
        snapshot_id = data.get("snapshot_id")
        data_as_of_raw = data.get("data_as_of")
        quality_status = data.get("quality_status")
        source_snapshot_complete = all(
            (
                isinstance(snapshot_id, str) and bool(snapshot_id.strip()),
                isinstance(data_as_of_raw, str) and bool(data_as_of_raw.strip()),
                isinstance(quality_status, str) and bool(quality_status.strip()),
            )
        )
        if source_snapshot_complete:
            try:
                data_as_of = datetime.fromisoformat(
                    data_as_of_raw.strip().replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise AdapterError(
                    "SQL_RESPONSE_INVALID", "data_as_of must be an ISO-8601 timestamp"
                ) from exc
            if data_as_of.tzinfo is None or data_as_of.utcoffset() is None:
                raise AdapterError(
                    "SQL_RESPONSE_INVALID", "data_as_of must include a timezone"
                )
        else:
            snapshot_id = f"response-{fingerprint}"
            data_as_of = datetime.now(timezone.utc)
            quality_status = "UNVERIFIED_SOURCE_SNAPSHOT"
        source_data_as_of_raw = data.get("source_data_as_of")
        source_watermark_field_raw = data.get("source_watermark_field")
        source_watermark_present = (
            source_data_as_of_raw is not None or source_watermark_field_raw is not None
        )
        source_data_as_of: datetime | date | None = None
        source_watermark_field: str | None = None
        if source_watermark_present:
            if not source_snapshot_complete or str(quality_status).strip().upper() != "PASS":
                raise AdapterError(
                    "SQL_RESPONSE_INVALID",
                    "source watermark requires a complete PASS snapshot contract",
                )
            if not isinstance(source_data_as_of_raw, str) or not source_data_as_of_raw.strip():
                raise AdapterError(
                    "SQL_RESPONSE_INVALID",
                    "source_data_as_of must be a non-empty ISO-8601 date or timestamp",
                )
            if not isinstance(source_watermark_field_raw, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*",
                source_watermark_field_raw.strip(),
            ):
                raise AdapterError(
                    "SQL_RESPONSE_INVALID",
                    "source_watermark_field must be a metadata field in table.column form",
                )
            source_text = source_data_as_of_raw.strip()
            try:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_text):
                    source_data_as_of = date.fromisoformat(source_text)
                else:
                    source_data_as_of = datetime.fromisoformat(
                        source_text.replace("Z", "+00:00")
                    )
            except ValueError as exc:
                raise AdapterError(
                    "SQL_RESPONSE_INVALID",
                    "source_data_as_of must be an ISO-8601 date or timestamp",
                ) from exc
            source_watermark_field = source_watermark_field_raw.strip()
        return Dataset(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            snapshot_id=snapshot_id.strip(),
            data_as_of=data_as_of,
            source_data_as_of=source_data_as_of,
            source_watermark_field=source_watermark_field,
            quality_status=quality_status.strip(),
            total_row_count=total_count if total_count is not None else len(rows),
            truncated=truncated,
        )


class HttpPolicyAdapter:
    def __init__(self, settings: Settings, client: PlatformHttpClient) -> None:
        self.settings = settings
        self.client = client

    async def health(self) -> bool:
        return await self.client.health(self.settings.policy_base_url)

    async def authorize(
        self, request: CanonicalAnalysisRequest, identity: TrustedIdentity
    ) -> tuple[bool, str]:
        data = await self.client.post(
            self.settings.policy_base_url,
            self.settings.policy_authorize_path,
            {
                "intent": request.primary_intent.value,
                "tenant_id": identity.tenant_id,
                "user_id": identity.user_id,
                "roles": identity.roles,
                "metrics": [
                    {"metric_id": m.metric_id or "", "version": m.version or ""}
                    for m in request.metrics
                ],
                "entity": request.entity,
                "fields": request.fields,
                "risk_level": request.risk_level,
            },
            identity=identity,
            application_id=request.application_id,
            retryable=False,
        )
        if not isinstance(data, dict) or not isinstance(data.get("allowed"), bool):
            raise AdapterError("POLICY_RESPONSE_INVALID", "invalid policy response")
        policy_ref = data.get("policy_ref")
        if not isinstance(policy_ref, str) or not policy_ref:
            raise AdapterError("POLICY_RESPONSE_INVALID", "policy_ref missing")
        return data["allowed"], policy_ref


class HttpAnalysisAdapter:
    def __init__(self, settings: Settings, client: PlatformHttpClient) -> None:
        self.settings = settings
        self.client = client

    async def health(self) -> bool:
        return await self.client.health(self.settings.analysis_base_url)

    async def run(
        self,
        request: CanonicalAnalysisRequest,
        dataset: Dataset,
        identity: TrustedIdentity,
    ) -> dict[str, Any]:
        # Rows are intentionally not sent. The external algorithm service must
        # consume a controlled snapshot/dataset reference in production.
        return await self.client.post(
            self.settings.analysis_base_url,
            self.settings.analysis_run_path,
            {
                "analysis_type": request.primary_intent.value,
                "tenant_id": identity.tenant_id,
                "user_id": identity.user_id,
                "metric_ids": [m.metric_id for m in request.metrics if m.metric_id],
                "entity": request.entity,
                "dimensions": request.dimensions,
                "filters": request.filters,
                "time_range": request.time_range.model_dump(mode="json") if request.time_range else None,
                "comparison_type": request.comparison_type,
                "forecast_horizon": str(request.forecast_horizon_periods) if request.forecast_horizon_periods else None,
                "report_template_id": request.report_template_id,
                "operators": [str(op) for op in request.operators],
            },
            identity=identity,
            application_id=request.application_id,
        )


# Contract-facing name; the runtime keeps HttpDataRetrievalAdapter for backward
# compatibility with the existing ASL -> SQL integration.
HttpQueryAdapter = HttpDataRetrievalAdapter


def build_http_adapters(
    settings: Settings,
    knowledge_cache: KnowledgeSearchCache | None = None,
) -> AdapterBundle:
    client = PlatformHttpClient(settings)
    semantic = HttpSemanticAdapter(settings, client)
    retrieval = HttpDataRetrievalAdapter(settings, client)
    return AdapterBundle(
        semantic=semantic,
        retrieval=retrieval,
        knowledge=HttpKnowledgeAdapter(settings, client, knowledge_cache),
        policy=HttpPolicyAdapter(settings, client),
        analysis=HttpAnalysisAdapter(settings, client),
        semantic_query=CompositeSemanticQueryTool(semantic, retrieval),
    )
