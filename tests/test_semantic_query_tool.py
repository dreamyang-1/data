from datetime import datetime, timezone

import pytest

from app.adapters.base import AdapterError
from app.adapters.semantic_query import CompositeSemanticQueryTool
from app.domain.models import (
    CanonicalAnalysisRequest,
    DataQueryResult,
    Dataset,
    MetricRef,
    PrimaryIntent,
    TrustedIdentity,
)


class SemanticStub:
    def __init__(self, resolved: list[MetricRef]) -> None:
        self.resolved = resolved
        self.calls = 0

    async def resolve_metrics(self, request, semantic_model_id):
        self.calls += 1
        return self.resolved


class RetrievalStub:
    def __init__(self) -> None:
        self.request = None

    async def health(self):
        return True

    async def rewrite_health(self):
        return False

    async def query(
        self, request, identity, *, semantic_model_id, business_domain_id
    ):
        self.request = request.model_copy(deep=True)
        return DataQueryResult(
            asl={"version": "2.0", "metrics": []},
            sql="SELECT 1",
            dataset=Dataset(
                columns=["value"],
                rows=[{"value": 1}],
                row_count=1,
                snapshot_id="snapshot-1",
                data_as_of=datetime.now(timezone.utc),
            ),
        )


def request_with_metrics(*names: str) -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="c1",
        application_id="app1",
        tenant_id="t1",
        user_id="u1",
        original_question="查询销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input=name) for name in names],
    )


@pytest.mark.asyncio
async def test_combined_tool_binds_metrics_before_query_planning():
    semantic = SemanticStub([
        MetricRef(
            input="销售额",
            canonical_name="含税销售总额",
            metric_id="81:tax_inclusive_sales_amount",
        )
    ])
    retrieval = RetrievalStub()
    tool = CompositeSemanticQueryTool(semantic, retrieval)

    result = await tool.query(
        request_with_metrics("销售额"),
        TrustedIdentity(tenant_id="t1", user_id="u1"),
        semantic_model_id=81,
        business_domain_id=None,
    )

    assert result.sql == "SELECT 1"
    assert semantic.calls == 1
    assert retrieval.request.metrics[0].metric_id == "81:tax_inclusive_sales_amount"


@pytest.mark.asyncio
async def test_combined_tool_exposes_underlying_readiness_checks():
    tool = CompositeSemanticQueryTool(SemanticStub([]), RetrievalStub())

    assert await tool.health() is True
    assert await tool.rewrite_health() is False


@pytest.mark.asyncio
async def test_combined_tool_rejects_partial_metric_binding_before_retrieval():
    semantic = SemanticStub([
        MetricRef(input="销售额", metric_id="81:sales_amount")
    ])
    retrieval = RetrievalStub()
    tool = CompositeSemanticQueryTool(semantic, retrieval)

    with pytest.raises(AdapterError) as exc_info:
        await tool.query(
            request_with_metrics("销售额", "订单数"),
            TrustedIdentity(tenant_id="t1", user_id="u1"),
            semantic_model_id=81,
            business_domain_id=None,
        )

    assert exc_info.value.code == "SEMANTIC_METRIC_BINDING_INCOMPLETE"
    assert retrieval.request is None
