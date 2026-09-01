from __future__ import annotations

from datetime import datetime, timezone

from app.adapters.base import AdapterBundle
from app.adapters.semantic_query import CompositeSemanticQueryTool
from app.domain.models import CanonicalAnalysisRequest, DataQueryResult, Dataset, EvidenceItem, KnowledgeContext, KnowledgeDocument, MetricRef, TrustedIdentity

METRICS = {
    "销售额": ("metric.sales_amount", "v1", "元"), "订单量": ("metric.order_count", "v1", "单"),
    "销售量": ("metric.sales_quantity", "v1", "件"), "客户数": ("metric.customer_count", "v1", "人"),
    "客单价": ("metric.avg_order_value", "v1", "元/单"), "退款额": ("metric.refund_amount", "v1", "元"),
    "退款率": ("metric.refund_rate", "v1", "%"),
}

class MockSemanticAdapter:
    async def health(self) -> bool: return True
    async def resolve_metrics(self, request: CanonicalAnalysisRequest, semantic_model_id: int | None) -> list[MetricRef]:
        resolved = []
        for metric in request.metrics:
            if spec := METRICS.get(metric.input):
                resolved.append(metric.model_copy(update={"metric_id": spec[0], "version": spec[1], "canonical_name": metric.input, "unit": spec[2]}))
        return resolved

    async def definition(self, metric: MetricRef) -> EvidenceItem:
        return EvidenceItem(evidence_id=f"definition:{metric.metric_id}:{metric.version}", kind="METRIC_DEFINITION", source_ref="mock-semantic-model", payload={"definition": f"{metric.canonical_name}的已发布模拟口径"})

    async def lineage(self, metric: MetricRef, identity: TrustedIdentity) -> EvidenceItem:
        return EvidenceItem(evidence_id=f"lineage:{metric.metric_id}:{metric.version}", kind="DATA_LINEAGE", source_ref="mock-lineage-service", payload={"business_lineage": ["销售交易域", "指标层"]})

class MockDataRetrievalAdapter:
    async def health(self) -> bool: return True
    async def rewrite_health(self) -> bool: return True
    async def query(self, request: CanonicalAnalysisRequest, identity: TrustedIdentity, *, semantic_model_id: int | None, business_domain_id: int | None) -> DataQueryResult:
        if request.entity:
            columns = request.fields or ["订单号"]
            rows = [{field: f"脱敏示例-{i + 1}" for i, field in enumerate(columns)}]
        else:
            metric = request.metrics[0] if request.metrics else None
            metric_id = (
                metric.metric_id
                if metric and metric.metric_id
                else METRICS.get(metric.input, (metric.input, "unverified", None))[0]
                if metric
                else "metric.unknown"
            )
            columns, rows = ["metric_id", "value"], [{"metric_id": metric_id, "value": "128000.00"}]
        dataset = Dataset(columns=columns, rows=rows, row_count=len(rows), snapshot_id="mock-snapshot-v1", data_as_of=datetime.now(timezone.utc))
        asl_metrics = [
            {
                "name": m.metric_id or METRICS.get(m.input, (m.input, "unverified", None))[0],
                "alias": m.canonical_name or m.input,
            }
            for m in request.metrics
        ]
        return DataQueryResult(asl={"version": "2.0", "intent": "query", "metrics": asl_metrics, "ambiguity": []}, sql="SELECT mock_read_only_result", dataset=dataset, data_source_id="mock")

class MockKnowledgeAdapter:
    async def health(self) -> bool: return True

    async def retrieve_analysis_context(self, request: CanonicalAnalysisRequest, dataset: Dataset, identity: TrustedIdentity) -> KnowledgeContext:
        return KnowledgeContext(query=request.original_question, documents=[KnowledgeDocument(content="模拟业务知识：分析结论必须同时由数据证据验证。", source="mock-analysis.md", block_id="1", kb_name="mock")])

class MockPolicyAdapter:
    async def health(self) -> bool: return True

    async def authorize(self, request, identity) -> tuple[bool, str]:
        # 当前平台尚未启用权限中心。该适配器只提供稳定扩展点，不自行扩大权限。
        return True, "policy-disabled-by-platform"

class MockAnalysisAdapter:
    async def health(self) -> bool: return True

    async def run(self, request, dataset, identity) -> dict:
        return {"analysis_type": request.primary_intent.value, "dataset": dataset}

def build_mock_adapters() -> AdapterBundle:
    semantic = MockSemanticAdapter()
    retrieval = MockDataRetrievalAdapter()
    return AdapterBundle(
        semantic=semantic,
        retrieval=retrieval,
        knowledge=MockKnowledgeAdapter(),
        policy=MockPolicyAdapter(),
        analysis=MockAnalysisAdapter(),
        semantic_query=CompositeSemanticQueryTool(semantic, retrieval),
    )
