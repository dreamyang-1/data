from __future__ import annotations

from app.adapters.base import AdapterError, DataRetrievalAdapter, SemanticAdapter
from app.domain.models import (
    CanonicalAnalysisRequest,
    DataQueryResult,
    TrustedIdentity,
)
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

    async def query(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> DataQueryResult:
        if request.metrics and not all(metric.metric_id for metric in request.metrics):
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
        return await self.retrieval.query(
            request,
            identity,
            semantic_model_id=semantic_model_id,
            business_domain_id=business_domain_id,
        )
