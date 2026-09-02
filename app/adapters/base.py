from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.models import (
    CanonicalAnalysisRequest,
    DataQueryResult,
    Dataset,
    EvidenceItem,
    KnowledgeContext,
    MetricRef,
    TrustedIdentity,
)


class SemanticAdapter(Protocol):
    async def health(self) -> bool: ...

    async def resolve_metrics(
        self, request: CanonicalAnalysisRequest, semantic_model_id: int | None
    ) -> list[MetricRef]: ...

    async def definition(self, metric: MetricRef) -> EvidenceItem: ...

    async def lineage(self, metric: MetricRef, identity: TrustedIdentity) -> EvidenceItem: ...


@dataclass(frozen=True)
class MetricDiscovery:
    """Current semantic-snapshot evidence used before completeness gating.

    This is deliberately smaller than an executable ASL plan. It only lets
    orchestration bind a metric that the current SQL semantic layer verified;
    normal query planning still regenerates and validates the final ASL.
    """

    metrics: list[MetricRef]
    time_independent_snapshot: bool = False
    evidence_fingerprint: str | None = None
    subject: str | None = None
    filters: tuple[dict[str, Any], ...] = ()
    dimensions: tuple[str, ...] = ()


class DataRetrievalAdapter(Protocol):
    async def health(self) -> bool: ...

    async def rewrite_health(self) -> bool: ...

    async def discover_metrics(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> MetricDiscovery: ...

    async def discover_attribute_details(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> MetricDiscovery: ...

    async def query(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> DataQueryResult: ...


class SemanticQueryTool(Protocol):
    """One agent-facing tool for metric binding, ASL planning and SQL translation."""

    async def query(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> DataQueryResult: ...

    async def discover_metrics(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> MetricDiscovery: ...

    async def discover_attribute_details(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> MetricDiscovery: ...


class KnowledgeAdapter(Protocol):
    async def health(self) -> bool: ...

    async def retrieve_analysis_context(
        self,
        request: CanonicalAnalysisRequest,
        dataset: Dataset,
        identity: TrustedIdentity,
    ) -> KnowledgeContext: ...


class PolicyAdapter(Protocol):
    async def health(self) -> bool: ...

    async def authorize(
        self, request: CanonicalAnalysisRequest, identity: TrustedIdentity
    ) -> tuple[bool, str]: ...


class AnalysisAdapter(Protocol):
    async def health(self) -> bool: ...

    async def run(
        self,
        request: CanonicalAnalysisRequest,
        dataset: Dataset,
        identity: TrustedIdentity,
    ) -> dict[str, Any]: ...


class AdapterError(RuntimeError):
    """A sanitized platform dependency failure safe to expose to orchestration."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        upstream_code: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.upstream_code = upstream_code
        self.details = details


@dataclass(frozen=True)
class AdapterBundle:
    semantic: SemanticAdapter
    retrieval: DataRetrievalAdapter
    knowledge: KnowledgeAdapter
    policy: PolicyAdapter | None = None
    analysis: AnalysisAdapter | None = None
    semantic_query: SemanticQueryTool | None = None

    @property
    def query(self) -> SemanticQueryTool | DataRetrievalAdapter:
        """Return the combined agent-facing query tool when configured."""
        return self.semantic_query or self.retrieval
