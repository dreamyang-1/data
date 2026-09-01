import json
import re
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.models import (
    AgentResponse,
    CanonicalAnalysisRequest,
    ChatRequest,
    ExtensionExecution,
    PrimaryIntent,
)
from app.main import create_app
from app.api import (
    _answer_chunk_delay,
    _answer_chunks,
    _prepare_regeneration,
    _thinking_section,
    _thinking_title,
)
from app.services.orchestrator import DataAnalysisOrchestrator


def build_test_app(**overrides):
    defaults = {
        "env": "test",
        "adapter_mode": "mock",
        "intent_model_enabled": False,
        "allow_missing_trusted_identity_headers": False,
    }
    defaults.update(overrides)
    return create_app(Settings(**defaults))


def test_chat_endpoint():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={
                "conversation_id": "c1",
                "application_id": "app1",
                "message_id": "m1",
                "question": "查询本月销售额",
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["intent_source"] == "DETERMINISTIC_RULE_FAST_PATH"
    assert response.json()["intent_confidence"] == 0.95


def test_answer_transport_chunks_preserve_full_answer_and_bound_event_count():
    answer = "上海地区振德医疗品牌医用外科口罩销售分析。" * 200
    chunks = _answer_chunks(answer)
    assert "".join(chunks) == answer
    assert len(chunks) <= 300
    assert _answer_chunk_delay(len(chunks)) * len(chunks) <= 0.901


def test_normal_answer_uses_small_streaming_chunks():
    answer = "查询完成，上海地区销售额为一百万元，较上月增长百分之十。"
    chunks = _answer_chunks(answer)
    assert "".join(chunks) == answer
    assert len(chunks) > 1
    assert max(map(len, chunks)) <= 12


def test_file_inspection_summary_reports_successful_parse_without_internal_path():
    summary = DataAnalysisOrchestrator._file_inspection_think_summary({
        "status": "READ_SUCCESS",
        "file_name": "sales.xlsx",
        "format": "XLSX",
        "sheet_count": 2,
        "sheets": ["订单", "商品"],
        "row_count": 120,
        "column_count": 8,
        "columns": ["订单号", "商品名称", "销售额"],
        "columns_truncated": True,
        "dataset_id": "private-dataset-id",
    })
    assert "### ◉ 问题补全与意图识别" in summary
    assert "sales.xlsx" in summary
    assert "120 行数据" in summary
    assert "8 个字段" in summary
    assert "private-dataset-id" not in summary


def test_intent_summary_marks_file_based_analysis_only_when_selected():
    request = CanonicalAnalysisRequest(
        conversation_id="c-file",
        application_id="app-file",
        tenant_id="t1",
        user_id="u1",
        original_question="分析上传文件中的销售趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
    )
    file_summary = DataAnalysisOrchestrator._intent_think_summary(
        request,
        file_status="READ_SUCCESS",
        file_based=True,
    )
    normal_summary = DataAnalysisOrchestrator._intent_think_summary(
        request,
        file_status="READ_SUCCESS",
        file_based=False,
    )
    assert "### ◉ 问题补全与意图识别" in file_summary
    assert "任务意图：基于用户文件进行趋势分析" in file_summary
    assert "文件判断：检测到用户上传文件" in file_summary
    assert "任务意图：基于用户文件进行" not in normal_summary
    assert "文件判断：" not in normal_summary


def test_readiness_checks_memory_dependencies():
    with TestClient(build_test_app()) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {
        "redis_short_memory": True,
        "mysql_long_memory": True,
    }
    assert response.json()["readiness_profiles"] == {
        "core": True,
        "query_pipeline": True,
        "full_feature": True,
    }
    assert response.json()["degraded_capabilities"] == []


def test_identity_headers_are_not_required():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat",
            json={"application_id": "app1", "conversation_id": "c1", "message_id": "m1", "question": "你好"},
        )
    assert response.status_code == 200


def test_regeneration_uses_isolated_ids_and_removes_replaced_turn_from_history():
    payload = ChatRequest(
        application_id="app-refresh",
        conversation_id="conversation-original",
        message_id="message-original",
        question="查询销售额",
        regenerate=True,
        history=[
            {"role": "user", "content": "查询销售量"},
            {"role": "assistant", "content": "旧销售量答案"},
            {"role": "user", "content": "第2轮问题：查询销售额"},
            {"role": "assistant", "content": "应被替换的旧销售额答案"},
        ],
    )

    execution, conversation_id, message_id = _prepare_regeneration(payload)

    assert conversation_id == "conversation-original"
    assert message_id == "message-original"
    assert execution.conversation_id.startswith("refresh-")
    assert execution.message_id == execution.conversation_id
    assert execution.regenerate is False
    assert [item.content for item in execution.history] == [
        "查询销售量",
        "旧销售量答案",
    ]


def test_development_can_temporarily_use_fallback_identity():
    with TestClient(
        build_test_app(allow_missing_trusted_identity_headers=True)
    ) as client:
        response = client.post(
            "/agent_chat",
            json={
                "application_id": "app1",
                "conversation_id": "anonymous-development",
                "message_id": "m1",
                "question": "你好",
            },
        )

    assert response.status_code == 200


def test_application_header_is_ignored_and_body_scope_is_used():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat",
            headers={
                "X-Tenant-Id": "t1",
                "X-User-Id": "u1",
                "X-Application-Id": "app-from-gateway",
            },
            json={
                "application_id": "different-app",
                "conversation_id": "c1",
                "message_id": "m1",
                "question": "你好",
            },
        )
    assert response.status_code == 200
    assert response.json()["conversation_id"] == "c1"


def test_legacy_application_header_setting_no_longer_requires_header():
    with TestClient(build_test_app(require_trusted_application_header=True)) as client:
        response = client.post(
            "/agent_chat",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={
                "application_id": "app1",
                "conversation_id": "c1",
                "message_id": "m1",
                "question": "你好",
            },
        )
    assert response.status_code == 200


def test_stream_returns_sanitized_error_event_after_accepting_unexpected_failure():
    app = build_test_app()

    class BrokenWorkflow:
        async def ainvoke(self, _):
            raise RuntimeError("secret internal exception")

    with TestClient(app, raise_server_exceptions=False) as client:
        object.__setattr__(app.state.container, "workflow", BrokenWorkflow())
        response = client.post(
            "/agent_chat/stream",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={
                "application_id": "app1",
                "conversation_id": "stream-error",
                "message_id": "m1",
                "question": "你好",
            },
        )

    assert response.status_code == 200
    assert '"type": "updata_state"' in response.text
    assert '"type": "error"' in response.text
    assert "INTERNAL_ERROR" in response.text
    assert "secret internal exception" not in response.text


def test_stream_emits_new_agent_compatible_data_only_envelopes():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat/stream",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={
                "application_id": "app1",
                "conversation_id": "real-stream",
                "message_id": "m1",
                "question": "你能做什么",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    text = response.text
    assert "event:" not in text

    events: list[dict] = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        assert len(lines) == 1 and lines[0].startswith("data: ")
        events.append(json.loads(lines[0].removeprefix("data: ")))
    types = [data["type"] for data in events]
    assert types.index("updata_state") < types.index("message_chunk")
    assert types.index("message_chunk") < types.index("answer")
    assert types.index("answer") < types.index("complete")
    chunks = [data["content"] for data in events if data["type"] == "message_chunk" and data.get("step") == "output"]
    answer = next(data for data in events if data["type"] == "answer")
    completed = next(data for data in events if data["type"] == "complete")
    assert "".join(chunks) == answer["content"] == completed["answer"]
    assert completed["content"] == ""
    assert any(
        data["type"] == "message_chunk" and data.get("meta", {}).get("stage") == "INTENT_RECOGNITION"
        for data in events
    )
    assert not any(
        data["type"] == "message_chunk"
        and data.get("meta", {}).get("stage") == "FILE_INSPECTION"
        for data in events
    )
    think_chunks = [
        data for data in events
        if data["type"] == "message_chunk" and data.get("step") != "output"
    ]
    assert all(
        data["content"].startswith("\n\n")
        and data["content"].endswith("\n\n")
        for data in think_chunks
    )
    assert {data["step"] for data in think_chunks} <= {
        "step1", "execute_plan", "execute_exe", "response_result"
    }
    assert all(
        data.get("data") in {
            "accepted", "heartbeat", "step1", "execute_plan",
            "execute_exe", "response_result",
        }
        for data in events if data["type"] == "updata_state"
    )
    all_thinking_content = "".join(data["content"] for data in think_chunks)
    expected_headings = [
        "### ◉ 意图识别",
        "### ◉ 任务拆分与规划",
        "### ◉ 输出总结",
    ]
    assert all(all_thinking_content.count(heading) == 1 for heading in expected_headings)
    assert len(re.findall(r"(?m)^\s*#{1,6}\s+", all_thinking_content)) == 3
    intent_chunk = next(
        data for data in think_chunks
        if data.get("meta", {}).get("stage") == "INTENT_RECOGNITION"
    )
    assert intent_chunk["step"] == "step1"
    assert "### ◉ 意图识别" not in intent_chunk["content"]
    completed_intent_chunk = next(
        data for data in think_chunks
        if data.get("meta", {}).get("stage") == "INTENT_RECOGNITION"
        and data.get("meta", {}).get("status") == "COMPLETED"
    )
    assert "### ◉ 意图识别" in completed_intent_chunk["content"]
    planning_running_chunk = next(
        data for data in think_chunks
        if data.get("meta", {}).get("stage") == "TASK_PLANNING"
        and data.get("meta", {}).get("status") == "RUNNING"
    )
    assert "### ◉ 任务拆分与规划" in planning_running_chunk["content"]
    assert "正在判断是否需要拆分" in planning_running_chunk["content"]
    summary_chunk = next(
        data for data in think_chunks
        if data.get("meta", {}).get("stage") == "OUTPUT_SUMMARY"
    )
    assert summary_chunk["step"] == "response_result"
    completed_think_stages = [
        data.get("meta", {}).get("stage")
        for data in events
        if data["type"] == "message_chunk"
        and data.get("meta", {}).get("status") == "COMPLETED"
    ]
    assert "OUTPUT_SUMMARY" in completed_think_stages
    if "TASK_PLANNING" in completed_think_stages:
        assert completed_think_stages.index("INTENT_RECOGNITION") < completed_think_stages.index("TASK_PLANNING")


def test_all_seven_thinking_stages_have_normalized_unnumbered_headings():
    expected = {
        "INTENT_RECOGNITION": "### ◉ 意图识别",
        "FILE_INSPECTION": "### ◉ 文件感知与解析",
        "TASK_PLANNING": "### ◉ 任务拆分与规划",
        "DATA_RETRIEVAL": "### ◉ 调度执行",
        "RELIABILITY_CHECK": "### ◉ 结果校验",
        "INSIGHT_ANALYSIS": "### ◉ 数据洞察分析",
        "OUTPUT_SUMMARY": "### ◉ 输出总结",
    }

    assert {
        stage: _thinking_title(_thinking_section(stage))
        for stage in expected
    } == expected


def test_stream_tool_result_preserves_new_agent_application_error_status():
    app = build_test_app()

    class FailedWebToolWorkflow:
        async def ainvoke(self, state):
            chat = state["chat"]
            return {"response": AgentResponse(
                request_id=uuid4(),
                conversation_id=chat.conversation_id,
                status="FAILED",
                intent=PrimaryIntent.DETAIL_QUERY,
                answer="联网搜索工具未能返回可用于回答的信息。",
                extension_executions=[ExtensionExecution(
                    name="web_search_data",
                    kind="HTTP_TOOL",
                    status="FAILED",
                    output={
                        "code": 503,
                        "error_type": "network_service_error",
                        "msg": "联网搜索失败：请求超时",
                    },
                    error="联网搜索失败：请求超时",
                    status_code=503,
                    error_type="network_service_error",
                )],
            )}

    with TestClient(app) as client:
        object.__setattr__(app.state.container, "workflow", FailedWebToolWorkflow())
        response = client.post(
            "/agent_chat/stream",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={
                "application_id": "app1",
                "conversation_id": "web-tool-error-stream",
                "message_id": "m1",
                "question": "瑞金医院地址在哪里？",
            },
        )

    events = [
        json.loads(block.removeprefix("data: "))
        for block in response.text.strip().split("\n\n")
    ]
    tool_result = next(item for item in events if item["type"] == "tool_result")
    assert tool_result["status"] == "error"
    assert tool_result["status_code"] == 503
    assert tool_result["error_type"] == "network_service_error"


def test_stream_uses_complete_label_with_cancelled_business_status():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat/stream",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={
                "application_id": "app1",
                "conversation_id": "stream-cancel",
                "message_id": "m1",
                "question": "取消",
            },
        )

    assert response.status_code == 200
    assert '"type": "updata_state"' in response.text
    assert '"type": "complete"' in response.text
    assert '"status": "CANCELLED"' in response.text


def test_stream_message_id_conflict_is_http_409_before_accepted_event():
    app = build_test_app()
    headers = {"X-Tenant-Id": "t1", "X-User-Id": "u1"}
    original = {
        "application_id": "app1",
        "conversation_id": "stream-conflict",
        "message_id": "same-message",
        "question": "你好",
    }
    changed = {**original, "question": "帮我删除销售额数据"}
    with TestClient(app) as client:
        assert client.post(
            "/agent_chat", headers=headers, json=original
        ).status_code == 200
        response = client.post(
            "/agent_chat/stream", headers=headers, json=changed
        )
    assert response.status_code == 409
    assert "data:" not in response.text
    assert response.json()["detail"]["code"] == "MESSAGE_ID_REUSE_CONFLICT"


def test_removed_management_routes_are_not_exposed():
    with TestClient(build_test_app()) as client:
        assert client.get("/v1/data-analysis/capabilities/skills").status_code == 404
        assert client.get("/v1/data-analysis/memory").status_code == 404
        assert client.post("/v1/data-analysis/memory/candidates").status_code == 404
        assert client.post("/v1/data-analysis/datasets/x/export-report").status_code == 404


def test_chat_accepts_platform_skill_tool_and_mcp_contract():
    payload = {
        "application_id": "app1",
        "conversation_id": "extension-contract",
        "message_id": "m1",
        "question": "你好",
        "use_longterm_memory": False,
        "tools": [{
            "name": "inventory_lookup", "url": "http://192.168.1.20/tool",
            "http_method": "post", "inputSchema": {"type": "object"},
        }],
        "skills": [{"code": "analysis", "slug": "metric_query"}],
        "mcp": [{
            "mcp_server_url": "http://192.168.1.21/mcp",
            "connect_type": "streamable_http",
        }],
        "temp_file_paths": [],
    }
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json=payload,
        )
    assert response.status_code == 200


def test_chat_rejects_ambiguous_multiple_spreadsheets():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={
                "application_id": "app1", "conversation_id": "files",
                "message_id": "m1", "question": "分析这些文件",
                "temp_file_paths": ["uploads/a.xlsx", "uploads/b.xlsx"],
            },
        )
    assert response.status_code == 422
    assert "一个CSV/XLSX" in response.json()["detail"]
