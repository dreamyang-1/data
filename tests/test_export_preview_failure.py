import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from app.domain.models import ChatRequest, DataQueryResult, Dataset, TrustedIdentity
from app.services.progress import progress_scope
from test_analysis_orchestration import service
from test_http_adapters import StubClient, request, IDENTITY
from test_api import TestClient, build_test_app


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("cause,expected", [
    ("AccessDenied private-secret", "访问被拒绝"),
    ("missing openpyxl private-secret", "导出组件不可用"),
    ("unknown private-secret", "生成或上传失败"),
])
async def test_adapter_preserves_total_preview_and_sanitizes_export_error(legacy, cause, expected):
    rows = [{"订单号": f"O-{n}"} for n in range(1523 if legacy else 20)]
    raw = {"success": True, "data": rows, "columns": ["订单号"],
           "row_count": 1523, "download_url": None, "export_error": cause}
    if not legacy:
        raw.update(preview_count=20, preview_truncated=True)
    client = StubClient([
        {"success": True, "result": json.dumps({"metrics": [], "ambiguity": []})},
        {"success": True, "sql": "SELECT ..."}, raw,
    ])
    result = await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        request(), IDENTITY, semantic_model_id=8, business_domain_id=None,
    )
    assert result.dataset.row_count == 20
    assert result.dataset.total_row_count == 1523
    assert result.dataset.truncated
    assert expected in result.result_export_error
    assert "private-secret" not in result.result_export_error
    assert result.result_file_url is None
    assert "result_export_error" not in result.model_dump()


@pytest.mark.asyncio
async def test_unexplained_oversized_payload_still_keeps_existing_guard():
    response = await service(row_count=1523).handle(ChatRequest(
        application_id="app", conversation_id="no-export-contract", message_id="m1",
        question="查询最近一年销售过费森尤斯产品的经销商名单",
        semantic_model_id=1, business_domain_id=1,
    ), TrustedIdentity(tenant_id="tenant", user_id="user"))
    assert response.status == "SAFE_FALLBACK"
    assert "最大行数" in response.answer


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "查询最近一年销售过费森尤斯产品的经销商名单", "分析2026年1月到2月销售额趋势", "查询2026年7月销售额并导出Excel",
])
async def test_export_failure_returns_preview_without_whole_dataset_analysis(question):
    agent = service()
    rows = [{"订单号": f"ORDER-{n:04d}", "金额": n + 1001} for n in range(20)]
    result = DataQueryResult(
        asl={"metrics": [], "dimensions": [{"name": "orders.id"}]},
        sql="SELECT id, amount FROM orders", result_export_error="完整附件导出失败：文件存储访问被拒绝。",
        dataset=Dataset(columns=["订单号", "金额"], rows=rows, row_count=20,
                        total_row_count=1523, truncated=True, snapshot_id="test-preview",
                        data_as_of=datetime.now(timezone.utc)),
    )
    agent.adapters.retrieval.query = AsyncMock(return_value=result)
    agent._persist_query_dataset = AsyncMock(side_effect=AssertionError("preview must not be persisted as complete"))
    agent._import_query_result_file = AsyncMock(side_effect=AssertionError("no file exists"))
    events = []
    with progress_scope(events.append):
        response = await agent.handle(ChatRequest(
            application_id="app", conversation_id="export-failed", message_id="m1",
            question=question, semantic_model_id=1, business_domain_id=1,
        ), TrustedIdentity(tenant_id="tenant", user_id="user"))
    assert response.status == "PARTIAL_SUCCESS", response.answer
    assert "1523 条" in response.answer
    assert "20 条预览" in response.answer
    assert "ORDER-0000" in response.answer and "ORDER-0019" in response.answer
    assert "访问被拒绝" in response.answer
    assert "最大行数" not in response.answer
    assert not response.files and response.result_file_url is None
    assert response.dataset_id is None and not response.chart_specs
    assert not any(e.kind == "ANALYSIS_RESULT" for e in response.evidence)
    assert response.reliability.level == "LIMITED"
    stages = [e["stage"] for e in events]
    assert stages.index("DATA_RETRIEVAL") < stages.index("RELIABILITY_CHECK") < stages.index("INSIGHT_ANALYSIS")
    assert "DETERMINISTIC_ANALYSIS" not in stages
    previous = await agent.sessions.get_last_request("tenant", "user", "app", "export-failed")
    assert "LATEST_RESULT_DATASET_NOT_REUSABLE" in previous.assumptions


@pytest.mark.parametrize("export_failed", [True, False])
def test_stream_large_result_preserves_six_node_order_and_preview(monkeypatch, export_failed):
    app = build_test_app()
    rows = [{"订单号": f"ORDER-{n:04d}", "金额": n + 1001} for n in range(20)]
    result = DataQueryResult(
        asl={"metrics": [], "dimensions": [{"name": "orders.id"}]},
        sql="SELECT id, amount FROM orders",
        result_export_error="完整附件导出失败：文件存储访问被拒绝。" if export_failed else None,
        result_file_url=None if export_failed else "https://files.example/result.xlsx",
        dataset=Dataset(columns=["订单号", "金额"], rows=rows, row_count=20,
                        total_row_count=1523, truncated=True, snapshot_id="test-stream-preview",
                        data_as_of=datetime.now(timezone.utc)),
    )
    with TestClient(app) as client:
        agent = app.state.container.orchestrator
        monkeypatch.setattr(agent.adapters.retrieval, "query", AsyncMock(return_value=result))
        monkeypatch.setattr(agent, "_import_query_result_file", AsyncMock(return_value=None))
        response = client.post("/agent_chat/stream", json={
            "application_id": "app", "conversation_id": "preview-stream",
            "message_id": "m1", "semantic_model_id": 1, "business_domain_id": 1,
            "question": "查询最近一年销售过费森尤斯产品的经销商名单",
        })
    assert response.status_code == 200
    events = [json.loads(block.removeprefix("data: "))
              for block in response.text.strip().split("\n\n")]
    stages = ["INTENT_RECOGNITION", "TASK_PLANNING", "DATA_RETRIEVAL",
              "RELIABILITY_CHECK", "INSIGHT_ANALYSIS"]
    positions = [next(i for i, e in enumerate(events)
                      if e.get("meta", {}).get("stage") == stage) for stage in stages]
    output_index = next(i for i, e in enumerate(events)
                        if e.get("step") == "output" and e.get("content"))
    assert positions + [output_index] == sorted(positions + [output_index])
    answer = "".join(e.get("content", "") for e in events
                     if e.get("step") == "output" and e.get("type") == "message_chunk")
    assert "1523" in answer and "20 条预览" in answer
    assert answer.count("ORDER-") == 20
    if export_failed:
        assert "附件导出失败" in answer
        assert "https://files.example" not in answer
    else:
        assert "https://files.example/result.xlsx" in answer
        assert "附件导出失败" not in answer
