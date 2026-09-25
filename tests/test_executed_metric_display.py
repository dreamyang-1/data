"""Progress and metric evidence describe the ASL actually sent to SQL."""
import copy
import json
from datetime import datetime, timezone

import pytest

from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, DataQueryResult, Dataset, MetricRef, PrimaryIntent, TrustedIdentity
from app.domain.semantic_scope import AuthorizedSemanticScope
from app.presentation.execution_trace import executed_asl_metrics
from app.presentation.reliability import _evidence_line
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import progress_scope
from test_http_adapters import StubClient


def request(stale=False):
    return CanonicalAnalysisRequest(
        conversation_id="metric-display", tenant_id="t", user_id="u",
        original_question="查询上海地区的区域医院覆盖率", primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_model_id=81,
        authorized_semantic_scope=AuthorizedSemanticScope(semantic_model_id=81, scope_mode="MODEL_WIDE"),
        metrics=[MetricRef(input="旧指标", metric_id="81:stale_metric")] if stale else [],
    )


@pytest.mark.parametrize("metrics", [
    [{"name": "screening_area_hospital_coverage", "alias": "区域医院覆盖率"}],
    [{"name": "count_a", "alias": "数量A"}, {"name": "count_b"}], [],
])
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.asyncio
async def test_surface_progress_uses_final_metrics_without_mutating_execution(monkeypatch, metrics, stale):
    asl = {"version": "2.0", "subject": {"entity": "hospital"},
           "metrics": metrics, "dimensions": [], "filters": [], "ambiguity": []}
    async def generate(*args, **kwargs):
        return {"asl": copy.deepcopy(asl)}
    monkeypatch.setattr("app.adapters.surface_asl.generate_surface_asl", generate)
    client = StubClient([
        {"success": True, "sql": "SELECT 1 AS result"},
        {"success": True, "columns": ["result"], "data": [{"result": 1}], "row_count": 1},
    ])
    original = request(stale)
    before = original.model_dump()
    events = []
    with progress_scope(events.append):
        result = await HttpDataRetrievalAdapter(Settings(), client).query_surface(
            original, TrustedIdentity(tenant_id="t", user_id="u"), mentions=[],
        )
    assert original.model_dump() == before
    assert result.asl == asl == json.loads(client.calls[0][2]["asl"])
    assert client.calls[1][2]["sql"] == "SELECT 1 AS result"
    message = next(e["message"] for e in events if e["stage"] == "SEMANTIC_QUERY_PLANNING")
    assert "指标绑定=" not in message
    assert "旧指标" not in message and "stale_metric" not in message
    assert "指标ID" not in message
    assert "已选指标（来自最终ASL）=" in message
    for metric in metrics:
        assert metric["name"] in message
        if metric.get("alias"):
            assert metric["alias"] in message
    if not metrics:
        assert "无（本次 ASL 未选择指标）" in message
    stages = [e["stage"] for e in events]
    assert stages.index("ASL_GENERATION") < stages.index("SEMANTIC_QUERY_PLANNING") < stages.index("SQL_EXECUTION")


@pytest.mark.parametrize("stale", [False, True])
def test_evidence_uses_executed_metrics_even_when_request_empty_or_stale(stale):
    original = request(stale)
    result = DataQueryResult(
        asl={"metrics": [{"name": "screening_area_hospital_coverage", "alias": "区域医院覆盖率"}]},
        sql="SELECT 1",
        dataset=Dataset(columns=["区域医院覆盖率"], rows=[{"区域医院覆盖率": 1}],
                        row_count=1, data_as_of=datetime.now(timezone.utc), snapshot_id="metric-test"),
    )
    evidence = DataAnalysisOrchestrator._semantic_metric_evidence(
        original, result, semantic_model_id=81, business_domain_id=None,
    )
    assert len(evidence) == 1
    assert evidence[0].payload["metric_id"] == "screening_area_hospital_coverage"
    text = _evidence_line(evidence[0])
    assert "区域医院覆盖率" in text and "screening_area_hospital_coverage" in text
    assert "不代表已独立审计全部业务口径" in text
    assert "旧指标" not in text and "已完成口径绑定" not in text
    result.asl["metrics"] = []
    assert not DataAnalysisOrchestrator._semantic_metric_evidence(
        original, result, semantic_model_id=81, business_domain_id=None,
    )


def test_read_metrics_is_pure_and_preserves_codes_without_fabricated_ids():
    asl = {"metrics": [{"name": "81:count", "alias": "医院数"},
                       {"name": "81:count", "alias": "医院数"},
                       {"name": "81:count", "alias": "另一个展示列"}, None, {"alias": "无编码"}]}
    before = copy.deepcopy(asl)
    assert executed_asl_metrics(asl) == [
        {"name": "81:count", "alias": "医院数"}, {"name": "81:count", "alias": "另一个展示列"}]
    assert asl == before
    assert executed_asl_metrics({"metrics": None}) == []
    assert executed_asl_metrics({}) == []
