from __future__ import annotations

import inspect

from app.adapters.base import (
    AdapterError,
    DataRetrievalAdapter,
    MetricDiscovery,
    SemanticAdapter,
)
from app.domain.models import (
    CanonicalAnalysisRequest,
    DataQueryResult,
    TrustedIdentity,
)
from app.services.semantic_context import build_semantic_context_snapshot


def _accepts_kwarg(func, name: str) -> bool:
    """判断下游实现是否声明了该关键字参数，避免破坏旧签名的调用方。"""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


class CompositeSemanticQueryTool:
    """Agent-facing facade over metric binding, ASL generation and SQL planning.

    The underlying services remain independently deployable. SQL execution is
    still performed by the retrieval adapter's dedicated execution dependency.
    """

    def __init__(
        self,
        semantic: SemanticAdapter,
        retrieval: DataRetrievalAdapter,
    ) -> None:
        self.semantic = semantic
        self.retrieval = retrieval

    async def health(self) -> bool:
        """Expose the retrieval/query-contract health through the facade."""
        return bool(await self.retrieval.health())

    async def rewrite_health(self) -> bool:
        """Expose entity-rewrite readiness expected by the application probe."""
        return bool(await self.retrieval.rewrite_health())

    async def discover_metrics(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> MetricDiscovery:
        """Resolve newly published metrics from the live semantic snapshot."""
        discover = getattr(self.retrieval, "discover_metrics", None)
        if not callable(discover):
            return MetricDiscovery(metrics=[])
        return await discover(
            request,
            identity,
            semantic_model_id=semantic_model_id,
            business_domain_id=business_domain_id,
        )

    async def discover_attribute_details(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> MetricDiscovery:
        """Resolve a metricless entity-attribute query from the live snapshot."""
        discover = getattr(self.retrieval, "discover_attribute_details", None)
        if not callable(discover):
            return MetricDiscovery(metrics=[])
        return await discover(
            request,
            identity,
            semantic_model_id=semantic_model_id,
            business_domain_id=business_domain_id,
        )

    async def query_surface(self, request, identity, *, mentions, structured_extraction=None):
        """Leave metric/dimension binding to the scoped ASL planner."""
        generate = getattr(self.retrieval, "query_surface", None)
        if not callable(generate):
            raise AdapterError("SURFACE_ASL_UNAVAILABLE", "retrieval does not support surface planning")
        kwargs = {"mentions": mentions}
        if structured_extraction is not None and _accepts_kwarg(generate, "structured_extraction"):
            # 只有支持结构化提取入参的实现才透传，mock/旧实现保持原签名可用。
            kwargs["structured_extraction"] = structured_extraction
        return await generate(request, identity, **kwargs)

    async def query(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> DataQueryResult:
        # Explicit scopes bind through Oagnet's strict domain-set query. The
        # legacy sql-translator name resolver is model-wide and cannot be used
        # as a preflight for these requests.
        scoped_binding_in_query = bool(request.authorized_semantic_scope and request.business_domain_ids
                                       and getattr(self.semantic, 'model_only_metric_resolution', False))
        if request.metrics and not all(metric.metric_id for metric in request.metrics) and not scoped_binding_in_query:
            resolved = await self.semantic.resolve_metrics(request, semantic_model_id)
            if resolved:
                requested_names = {metric.input for metric in request.metrics}
                resolved_names = {metric.input for metric in resolved}
                if len(resolved) != len(request.metrics) or resolved_names != requested_names:
                    raise AdapterError(
                        "SEMANTIC_METRIC_BINDING_INCOMPLETE",
                        "semantic service returned only part of the requested metrics",
                    )
                request.metrics = resolved
        # Preserve the selected canonical assets as internal-only evidence.
        # The field is excluded from serialization, so this does not change the
        # ASL/SQL request contract or expose the snapshot downstream.
        request.semantic_context_snapshot = build_semantic_context_snapshot(
            request, semantic_model_id
        )
        return await self.retrieval.query(
            request,
            identity,
            semantic_model_id=semantic_model_id,
            business_domain_id=business_domain_id,
        )
