import asyncio
import json
import re
import time
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient as RawTestClient

from app.config import Settings
from app.domain.models import (
    AgentResponse,
    CanonicalAnalysisRequest,
    ChatRequest,
    ContextMode,
    ExtensionExecution,
    PrimaryIntent,
    TimeRange,
    TurnRelation,
)
from app.main import create_app
from app.services.orchestrator import (
    DataAnalysisOrchestrator,
    QUERY_EXECUTION_CHAIN,
    _business_datetime_text,
    _quality_status_text,
)
from app.presentation import (
    SEMANTIC_QUERY_TOOL_NAME,
    SQL_EXECUTION_TOOL_NAME,
    SQL_TRANSLATION_TOOL_NAME,
)
from app.services.progress import emit_progress
from app.api import (
    _answer_chunk_delay,
    _answer_chunks,
    _forward_traced_progress,
    _markdown_hard_line_breaks,
    _prepare_regeneration,
    _progress_heartbeat_events,
    _thinking_chunk_delay,
    _thinking_chunks,
    _thinking_events,
    _thinking_section,
    _thinking_title,
)


def test_user_visible_dataset_summary_uses_chinese_status_and_beijing_time():
    assert _quality_status_text("PASS") == "通过"
    assert _quality_status_text("FAIL") == "不通过"
    assert _business_datetime_text(
        datetime(2026, 9, 7, 5, 11, 7, tzinfo=timezone.utc)
    ) == "2026-09-07 13:11:07（北京时间）"


def TestClient(app, **kwargs):
    """Business fixtures now call as a trusted backend with explicit identity."""
    headers = {'Authorization': 'Bearer phase0c-fixture-token', 'X-Tenant-Id': 't1', 'X-User-Id': 'u1'}
    headers.update(kwargs.pop('headers', {}))
    return RawTestClient(app, headers=headers, **kwargs)


def build_test_app(**overrides):
    defaults = {
        "env": "test",
        "adapter_mode": "mock",
        "intent_model_enabled": False,
        "allow_missing_trusted_identity_headers": False,
        "business_question_collection_enabled": False,
        "trusted_backend_token": "phase0c-fixture-token",
    }
    defaults.update(overrides)
    return create_app(Settings(**defaults))


def thinking_content(
    events: list[dict], stage: str, *, status: str | None = None
) -> str:
    return "".join(
        event["content"]
        for event in events
        if event.get("type") == "message_chunk"
        and event.get("step") != "output"
        and event.get("meta", {}).get("stage") == stage
        and (status is None or event.get("meta", {}).get("status") == status)
    )


def test_markdown_hard_line_breaks_preserve_document_logical_lines():
    assert _markdown_hard_line_breaks(
        "用户原始问题：查询销售额\n补全后的问题：查询销售额\n\n结构化提取：\n指标：销售额"
    ) == (
        "用户原始问题：查询销售额  \n补全后的问题：查询销售额\n\n"
        "结构化提取：  \n指标：销售额"
    )


def test_chat_endpoint():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={"semantic_model_id": 81,
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


def test_stream_replaces_local_structure_with_exact_asl_json():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat/stream",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={
                "semantic_model_id": 81,
                "conversation_id": "asl-display-stream",
                "application_id": "app1",
                "message_id": "m1",
                "question": "查询本月销售额",
            },
        )

    events = [
        json.loads(block.removeprefix("data: "))
        for block in response.text.strip().split("\n\n")
    ]
    intent = next(
        event for event in events
        if event.get("type") == "message_chunk"
        and event.get("meta", {}).get("stage") == "INTENT_RECOGNITION"
        and event.get("meta", {}).get("status") == "COMPLETED"
    )
    intent_message = intent["meta"]["message"]
    assert "结构化提取" not in intent_message
    assert "轮次关系：" not in intent_message
    assert "上下文补全：" not in intent_message
    assert "是否需要追问：" not in intent_message
    assert "不追问理由：" not in intent_message

    asl_event = next(
        event for event in events
        if event.get("type") == "message_chunk"
        and event.get("meta", {}).get("stage") == "ASL_GENERATION"
    )
    asl_message = asl_event["meta"]["message"]
    assert f"工具：{SEMANTIC_QUERY_TOOL_NAME}。" in asl_message
    asl_json = asl_message.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    assert json.loads(asl_json) == {
        "version": "2.0",
        "intent": "query",
        "metrics": [{
            "name": "metric.sales_amount",
            "alias": "销售额",
        }],
        "ambiguity": [],
    }
    assert asl_event["meta"]["display_model"] == "OagentASL"
    planning_content = thinking_content(
        events, "TASK_PLANNING", status="COMPLETED"
    )
    execution_running_content = thinking_content(
        events, "DATA_RETRIEVAL", status="RUNNING"
    )
    assert f"规划调用：{QUERY_EXECUTION_CHAIN}" in planning_content
    assert f"执行链路：{QUERY_EXECUTION_CHAIN}" in execution_running_content
    assert "语义解析" not in QUERY_EXECUTION_CHAIN
    assert QUERY_EXECUTION_CHAIN == (
        f"{SEMANTIC_QUERY_TOOL_NAME} → {SQL_TRANSLATION_TOOL_NAME} → "
        f"{SQL_EXECUTION_TOOL_NAME} → 数据集输出 → 结果校验 → 洞察分析"
    )
    asl_index = events.index(asl_event)
    retrieval_completed_index = next(
        index for index, event in enumerate(events)
        if event.get("type") == "message_chunk"
        and event.get("meta", {}).get("stage") == "DATA_RETRIEVAL"
        and event.get("meta", {}).get("status") == "COMPLETED"
    )
    assert asl_index < retrieval_completed_index
    insight_content = thinking_content(
        events, "INSIGHT_ANALYSIS", status="COMPLETED"
    )
    assert "#### ◉ 数据洞察分析" in insight_content
    assert "本次查询共命中" in insight_content
    assert "这次结果的核心值" in insight_content
    assert "只基于本次查询结果和已验证证据" in insight_content


def test_chat_collects_sync_and_stream_questions_but_not_refresh(tmp_path):
    document_path = tmp_path / "实际业务问题.md"
    app = build_test_app(
        business_question_collection_enabled=True,
        business_question_document_path=document_path,
    )
    base_payload = {
        "semantic_model_id": 81,
        "conversation_id": "business-conversation",
        "application_id": "business-app",
    }
    with TestClient(app) as client:
        sync_response = client.post(
            "/agent_chat",
            json={
                **base_payload,
                "message_id": "business-sync-1",
                "question": "统计上海地区本月销售额",
            },
        )
        stream_response = client.post(
            "/agent_chat/stream",
            json={
                **base_payload,
                "message_id": "business-stream-1",
                "question": "按经销商展示销售额",
            },
        )
        refresh_response = client.post(
            "/agent_chat/refresh",
            json={
                **base_payload,
                "message_id": "business-refresh-1",
                "question": "这条刷新问题不应重复收集",
            },
        )
        revise_response = client.post(
            "/agent_chat/refresh",
            json={
                **base_payload,
                "message_id": "business-revise-1",
                "refresh_request_id": "business-revise-attempt-1",
                "original_question": "查询旧产品销售额",
                "question": "查询新产品销售额",
            },
        )

    assert sync_response.status_code == 200
    assert stream_response.status_code == 200
    assert refresh_response.status_code == 200
    assert revise_response.status_code == 200
    content = document_path.read_text(encoding="utf-8")
    assert content.count("统计上海地区本月销售额") == 1
    assert content.count("按经销商展示销售额") == 1
    assert "这条刷新问题不应重复收集" not in content
    assert content.count("查询新产品销售额") == 1


def test_question_collection_failure_does_not_break_chat(tmp_path):
    app = build_test_app(
        business_question_collection_enabled=True,
        # Opening an existing directory for append fails inside the collector.
        business_question_document_path=tmp_path,
    )
    with TestClient(app) as client:
        response = client.post(
            "/agent_chat",
            json={"semantic_model_id": 81,
                "conversation_id": "collector-failure-conversation",
                "application_id": "collector-failure-app",
                "message_id": "collector-failure-message",
                "question": "查询本月销售额",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"


def test_answer_transport_chunks_use_new_agent_six_character_rule():
    answer = "上海地区振德医疗品牌医用外科口罩销售分析。" * 200
    chunks = _answer_chunks(answer)
    assert "".join(chunks) == answer
    assert all(len(chunk) == 6 for chunk in chunks[:-1])
    assert 1 <= len(chunks[-1]) <= 6
    assert _answer_chunk_delay(len(chunks)) * len(chunks) <= 0.901


def test_normal_answer_uses_small_streaming_chunks():
    answer = "查询完成，上海地区销售额为一百万元，较上月增长百分之十。"
    chunks = _answer_chunks(answer)
    assert "".join(chunks) == answer
    assert len(chunks) > 1
    assert all(len(chunk) == 6 for chunk in chunks[:-1])
    assert 1 <= len(chunks[-1]) <= 6


def test_answer_transport_chunk_size_must_be_positive():
    with pytest.raises(ValueError, match="chunk_size must be greater than zero"):
        _answer_chunks("答案", chunk_size=0)


def test_thinking_transport_streams_unicode_and_bounds_large_nodes():
    short = "结构化提取：上海市销售趋势"
    short_chunks = _thinking_chunks(short, chunk_size=4, max_chunks=120)
    assert "".join(short_chunks) == short
    assert len(short_chunks) > 1
    assert all(len(chunk) == 4 for chunk in short_chunks[:-1])

    large = "语义字段" * 2000
    large_chunks = _thinking_chunks(large, chunk_size=4, max_chunks=120)
    assert "".join(large_chunks) == large
    assert len(large_chunks) <= 120
    assert _thinking_chunk_delay(len(large_chunks)) == 0.03


def test_thinking_transport_reconstructs_exact_asl_markdown():
    asl = {"version": "2.0", "filters": [{"field": "城市", "value": "上海市"}]}
    message = "结构化提取（ASL）：\n```json\n" + json.dumps(
        asl, ensure_ascii=False, indent=2
    ) + "\n```"
    serialized = _thinking_events(
        {
            "stage": "ASL_GENERATION",
            "status": "COMPLETED",
            "message": message,
            "display_model": "OagentASL",
        },
        heading="#### ◉ 调度执行",
        chunk_size=3,
    )
    events = [
        json.loads(item.removeprefix("data: ").strip())
        for item in serialized
    ]
    chunks = [item for item in events if item["type"] == "message_chunk"]
    reconstructed = "".join(item["content"] for item in chunks)
    assert len(chunks) > 1
    assert reconstructed.startswith("\n\n#### ◉ 调度执行")
    assert reconstructed.endswith("\n\n")
    normalized = reconstructed.replace("  \n", "\n")
    extracted = normalized.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    assert json.loads(extracted) == asl
    assert chunks[0]["meta"]["display_model"] == "OagentASL"
    assert "message" not in chunks[1]["meta"]
    assert chunks[-1]["is_last"] is True


def test_thinking_transport_preserves_inline_svg_chart_markup():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 10">'
        '<title>销售额趋势</title><path d="M0 9 L20 1"/></svg>'
    )
    serialized = _thinking_events(
        {
            "stage": "INSIGHT_ANALYSIS",
            "status": "COMPLETED",
            "message": f"数据洞察分析\n\n#### 图表\n\n{svg}",
        },
        heading="#### ◉ 数据洞察分析",
        chunk_size=3,
    )
    events = [
        json.loads(item.removeprefix("data: ").strip())
        for item in serialized
    ]
    reconstructed = "".join(
        item["content"] for item in events if item["type"] == "message_chunk"
    )

    assert svg in reconstructed
    assert reconstructed.count("<svg") == 1
    assert reconstructed.count("</svg>") == 1


def test_progress_heartbeat_uses_visible_existing_stage_contract():
    serialized = _progress_heartbeat_events(
        "INTENT_RECOGNITION",
        elapsed_seconds=6.04,
    )
    events = [
        json.loads(item.removeprefix("data: ").strip())
        for item in serialized
    ]

    assert len(events) == 1
    visible = events[0]
    assert visible["type"] == "message_chunk"
    assert visible["step"] == "step1"
    assert visible["index"] == 6040
    assert visible["is_last"] is False
    assert visible["content"] == (
        "<stage>⏳ 正在识别问题中的指标、维度与筛选条件"
        "（已用时 6 秒）...</stage>"
    )
    assert visible["meta"] == {
        "stage": "INTENT_RECOGNITION",
        "status": "RUNNING",
        "elapsed_seconds": 6.0,
    }


def test_progress_delivery_is_not_blocked_by_slow_telemetry():
    delivered = asyncio.Event()

    class SlowTracker:
        def handle(self, _event):
            time.sleep(0.15)
            raise AssertionError("streaming progress must not wait for telemetry")

    async def downstream(_event):
        delivered.set()

    async def exercise():
        task = asyncio.create_task(
            _forward_traced_progress(
                SlowTracker(),
                downstream,
                {"stage": "INTENT_RECOGNITION", "status": "RUNNING"},
            )
        )
        await asyncio.wait_for(delivered.wait(), timeout=0.03)
        await asyncio.wait_for(task, timeout=0.03)

    asyncio.run(exercise())


def test_slow_stream_emits_visible_progress_while_waiting():
    app = build_test_app(
        runtime_mode="V1",
        thinking_stream_heartbeat_seconds=0.05,
    )

    class SlowWorkflow:
        async def ainvoke(self, state):
            # Hidden internal events must not postpone visible progress. They
            # used to restart the wait timeout even though the page could not
            # render them, recreating a several-second frozen interval.
            for _ in range(6):
                await emit_progress(
                    "QUESTION_REWRITE",
                    "RUNNING",
                    "internal context normalization",
                )
                await asyncio.sleep(0.03)
            chat = state["chat"]
            return {"response": AgentResponse(
                request_id=uuid4(),
                conversation_id=chat.conversation_id,
                status="COMPLETED",
                intent=PrimaryIntent.CHAT,
                answer="处理完成。",
            )}

    with TestClient(app) as client:
        object.__setattr__(app.state.container, "workflow", SlowWorkflow())
        response = client.post(
            "/agent_chat/stream",
            json={
                "semantic_model_id": 81,
                "application_id": "app1",
                "conversation_id": "visible-waiting-progress",
                "message_id": "m1",
                "question": "请处理这个问题",
            },
        )

    events = [
        json.loads(block.removeprefix("data: "))
        for block in response.text.strip().split("\n\n")
    ]
    heartbeats = [
        event for event in events
        if event.get("type") == "message_chunk"
        and "已用时" in event.get("content", "")
        and event.get("meta", {}).get("status") == "RUNNING"
    ]

    assert response.status_code == 200
    assert len(heartbeats) >= 2
    assert all(event["step"] == "step1" for event in heartbeats)
    assert all(event["is_last"] is False for event in heartbeats)
    assert len({event["index"] for event in heartbeats}) == len(heartbeats)
    assert all("<stage>⏳ 正在识别问题" in event["content"] for event in heartbeats)
    assert all("已用时" in event["content"] for event in heartbeats)
    assert next(
        event for event in events if event["type"] == "complete"
    )["status"] == "COMPLETED"


def test_default_visible_progress_heartbeat_is_one_second():
    assert (
        Settings.model_fields["thinking_stream_heartbeat_seconds"].default
        == 1.0
    )


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
    assert "### ◉ 意图识别" in file_summary
    assert "任务意图：基于用户文件进行趋势分析" in file_summary
    assert "文件判断：检测到用户上传文件，按文件数据链路处理" in file_summary
    assert "任务意图：基于用户文件进行" not in normal_summary
    assert "文件判断：检测到用户上传文件，但当前问题不使用该文件" in normal_summary


def test_intent_summary_hides_internal_turn_and_clarification_diagnostics():
    standalone = CanonicalAnalysisRequest(
        conversation_id="turn-relation-display",
        application_id="app",
        tenant_id="t1",
        user_id="u1",
        original_question="按月分析B产品销售趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        turn_relation=TurnRelation.STANDALONE_NEW_TOPIC,
        context_mode=ContextMode.NONE,
    )
    standalone_summary = DataAnalysisOrchestrator._intent_think_summary(
        standalone
    )
    assert "轮次关系：" not in standalone_summary
    assert "上下文补全：" not in standalone_summary
    assert "是否需要追问：" not in standalone_summary
    assert "不追问理由：" not in standalone_summary
    assert "参数规范化：已完成" in standalone_summary

    followup = standalone.model_copy(
        deep=True,
        update={
            "original_question": "11月较10月下降多少",
            "turn_relation": TurnRelation.CURRENT_TOPIC_FOLLOWUP,
            "context_mode": ContextMode.CURRENT_THREAD,
        },
    )
    followup_summary = DataAnalysisOrchestrator._intent_think_summary(followup)
    assert "轮次关系：" not in followup_summary
    assert "上下文补全：" not in followup_summary

    model_enriched = standalone.model_copy(
        deep=True,
        update={
            "intent_source": "STRUCTURED_MODEL",
            "assumptions": [
                "MODEL_QUESTION_COMPLETION_APPLIED",
                "MODEL_ENTITY_EXTRACTION_APPLIED",
            ],
        },
    )
    model_summary = DataAnalysisOrchestrator._intent_think_summary(
        model_enriched
    )
    assert "任务意图：趋势分析（置信度" in model_summary
    assert "意图判定依据：" in model_summary


def test_intent_summary_does_not_render_a_pre_asl_structure():
    request = CanonicalAnalysisRequest(
        conversation_id="vector-display-only",
        application_id="app",
        tenant_id="t1",
        user_id="u1",
        original_question="查询最近一年销售过费森尤斯产品的经销商名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        dimensions=["经销商", "产品"],
        fields=["经销商名称", "未提取"],
        filters=[{"field": "商品名称", "operator": "EQ", "value": "费森尤斯"}],
        semantic_entity_mentions=["费森尤斯", "模型猜测值"],
        semantic_display_slots={
            "entity": "经销商",
            "fields": ["经销商名称"],
            "entity_values": ["费森尤斯医疗用品股份有限公司"],
            "filters": [{
                "field": "商品品牌",
                "operator": "EQ",
                "value": "费森尤斯医疗用品股份有限公司",
            }],
        },
    )

    summary = DataAnalysisOrchestrator._intent_think_summary(request)

    assert "结构化提取" not in summary
    assert "查询字段：" not in summary
    assert "实体：费森尤斯医疗用品股份有限公司" not in summary
    assert "商品品牌 EQ 费森尤斯医疗用品股份有限公司" not in summary
    assert "维度：" not in summary
    assert "商品名称 EQ 费森尤斯" not in summary
    assert "模型猜测值" not in summary
    assert "未提取" not in summary


def test_intent_summary_omits_all_local_structure_when_nothing_is_grounded():
    request = CanonicalAnalysisRequest(
        conversation_id="no-vector-display",
        application_id="app",
        tenant_id="t1",
        user_id="u1",
        original_question="查一下这个",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="模型猜测对象",
        fields=["未提取"],
    )

    summary = DataAnalysisOrchestrator._intent_think_summary(request)

    assert "模型猜测对象" not in summary
    assert "未提取" not in summary
    assert "结构化提取" not in summary
    assert "指标：" not in summary
    assert "排序数量：" not in summary


def test_intent_display_v2_is_multiline_and_does_not_mutate_execution_request():
    request = CanonicalAnalysisRequest(
        conversation_id="intent-display-v2",
        application_id="app",
        tenant_id="t1",
        user_id="u1",
        original_question="查询费森尤斯产品的经销商名单",
        rewritten_question="查询费森尤斯品牌产品的经销商名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        filters=[{"field": "商品品牌", "operator": "EQ", "value": "费森尤斯"}],
        semantic_display_slots={
            "entity": "经销商",
            "fields": ["经销商名称"],
            "filters": [{
                "field": "商品品牌",
                "operator": "EQ",
                "value": "费森尤斯",
            }],
        },
    )
    before = request.model_copy(deep=True)

    summary = DataAnalysisOrchestrator._intent_think_summary(
        request,
        business_domain_labels=("医药销售域",),
    )

    assert request == before
    assert summary.startswith("### ◉ 意图识别\n\n")
    assert "\n用户原始问题：" in summary
    assert "\n补全后的问题：" in summary
    assert "\n业务域：医药销售域" in summary
    assert "文件判断：" not in summary
    assert "\n结构化提取" not in summary
    assert "商品品牌 EQ 费森尤斯" not in summary
    assert "\n是否需要追问：" not in summary


def test_default_trend_time_is_not_reconstructed_before_asl_generation():
    request = CanonicalAnalysisRequest(
        conversation_id="intent-display-watermark-time",
        application_id="app",
        tenant_id="t1",
        user_id="u1",
        original_question="按月分析费森尤斯产品的销售趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        time_range=TimeRange(
            start=date(2025, 9, 4),
            end_exclusive=date(2026, 9, 5),
        ),
        assumptions=["DEFAULT_TIME_RANGE=LATEST_ONE_YEAR"],
    )

    summary = DataAnalysisOrchestrator._intent_think_summary(request)

    assert "时间区间：" not in summary
    assert "2025-09-04 至 2026-09-05" not in summary


def test_intent_display_v2_does_not_render_local_filter_or_entity_projection():
    request = CanonicalAnalysisRequest(
        conversation_id="intent-display-enum",
        application_id="app",
        tenant_id="t1",
        user_id="u1",
        original_question="查询TDC-3产品的主要适用科室",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        filters=[{"field": "适用科室类型", "operator": "EQ", "value": 1}],
        semantic_display_slots={
            "filters": [{
                "field": "适用科室类型", "operator": "EQ", "value": "1",
            }],
            "entity_values": ["TDC-3"],
        },
    )

    summary = DataAnalysisOrchestrator._intent_think_summary(request)

    assert "适用科室类型 EQ 主要适用" not in summary
    assert "实体：TDC-3" not in summary
    assert "实体：1" not in summary


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
            json={"semantic_model_id": 81, "application_id": "app1", "conversation_id": "c1", "message_id": "m1", "question": "你好"},
        )
    assert response.status_code == 200


def test_regeneration_keeps_conversation_and_removes_replaced_turn_from_history():
    payload = ChatRequest(semantic_model_id=81,
        application_id="app-refresh",
        conversation_id="conversation-original",
        message_id="message-original",
        question="查询销售额",
        regenerate=True,
        replaces_message_id="replaced-user-turn",
        history=[
            {"role": "user", "content": "查询销售量"},
            {"role": "assistant", "content": "旧销售量答案"},
            {
                "role": "user",
                "message_id": "replaced-user-turn",
                "content": "第2轮问题：查询销售额",
            },
            {"role": "assistant", "content": "应被替换的旧销售额答案"},
        ],
    )

    execution, conversation_id, message_id = _prepare_regeneration(payload)

    assert conversation_id == "conversation-original"
    assert message_id == "message-original"
    assert execution.conversation_id == "conversation-original"
    assert execution.message_id.startswith("refresh-")
    assert execution.regenerate is False
    assert execution._is_regeneration_execution is True
    assert execution._bypass_repeat_query_cache is True
    assert execution._regeneration_mode == "REFRESH"
    assert [item.content for item in execution.history] == [
        "查询销售量",
        "旧销售量答案",
    ]


def test_regeneration_message_id_is_idempotent_per_refresh_attempt():
    common = {"semantic_model_id": 81,
        "application_id": "app-refresh",
        "conversation_id": "conversation-original",
        "message_id": "message-original",
        "question": "查询 TDC-3 产品的适用科室",
        "regenerate": True,
    }
    first, _, _ = _prepare_regeneration(
        ChatRequest(**common, refresh_request_id="attempt-1")
    )
    retry, _, _ = _prepare_regeneration(
        ChatRequest(**common, refresh_request_id="attempt-1")
    )
    next_click, _, _ = _prepare_regeneration(
        ChatRequest(**common, refresh_request_id="attempt-2")
    )

    assert first.message_id == retry.message_id
    assert first.message_id != next_click.message_id


def test_legacy_modified_resubmit_is_distinct_from_pure_refresh():
    common = {
        "semantic_model_id": 81,
        "application_id": "app-refresh",
        "conversation_id": "conversation-original",
        "message_id": "original-message-id",
        "regenerate": True,
    }
    refresh, _, _ = _prepare_regeneration(ChatRequest(
        **common,
        question="查询 TDC-3 产品的主要适用科室",
    ))
    revise, _, _ = _prepare_regeneration(ChatRequest(
        **common,
        original_question="查询 TDC-3 产品的主要适用科室",
        question="查询 TDC-3 产品的所有适用科室",
    ))

    assert refresh._regeneration_mode == "REFRESH"
    assert revise._regeneration_mode == "REVISE"
    assert refresh.message_id != revise.message_id


def test_regeneration_accepts_explicit_id_only_for_last_user_turn():
    payload = ChatRequest(semantic_model_id=81,
        application_id="app-refresh",
        conversation_id="conversation-original",
        message_id="refresh-attempt",
        refresh_request_id="attempt-1",
        replaces_message_id="user-turn-2",
        original_question="查询 TDC-3 产品的主要适用科室",
        question="查询 TDC-3 产品的所有适用科室",
        history=[
            {"role": "user", "message_id": "user-turn-1", "content": "查询销售额"},
            {"role": "assistant", "content": "旧销售额答案"},
            {
                "role": "user",
                "message_id": "user-turn-2",
                "content": "查询 TDC‑3 产品的主要适用科室",
            },
            {"role": "assistant", "content": "应移除的旧科室答案"},
        ],
    )

    execution, _, _ = _prepare_regeneration(payload)

    assert [item.message_id for item in execution.history] == ["user-turn-1", None]


def test_regeneration_rejects_replacing_an_earlier_user_turn():
    payload = ChatRequest(semantic_model_id=81,
        application_id="app-refresh",
        conversation_id="conversation-original",
        message_id="refresh-attempt",
        replaces_message_id="user-turn-1",
        original_question="查询 TDC-3 产品的主要适用科室",
        question="查询 TDC-3 产品的所有适用科室",
        history=[
            {"role": "user", "message_id": "user-turn-1", "content": "查询销售额"},
            {"role": "assistant", "content": "旧销售额答案"},
            {
                "role": "user",
                "message_id": "user-turn-2",
                "content": "查询 TDC-3 产品的主要适用科室",
            },
            {"role": "assistant", "content": "最后一问的旧答案"},
        ],
    )

    with pytest.raises(Exception) as exc_info:
        _prepare_regeneration(payload)

    assert getattr(exc_info.value, "status_code", None) == 400
    assert "最后一条用户消息" in str(getattr(exc_info.value, "detail", ""))


def test_revise_last_question_does_not_require_history_or_replacement_id():
    payload = ChatRequest(semantic_model_id=81,
        application_id="app-refresh",
        conversation_id="conversation-original",
        message_id="refresh-attempt",
        refresh_request_id="attempt-revise-1",
        original_question="按月查询销售额",
        question="按季度查询销售额",
        regenerate=True,
    )

    execution, conversation_id, message_id = _prepare_regeneration(payload)

    assert execution.history == []
    assert execution._regeneration_mode == "REVISE"
    assert conversation_id == "conversation-original"
    assert message_id == "refresh-attempt"


def test_legacy_replacement_id_without_history_is_accepted_for_last_turn_only():
    payload = ChatRequest(semantic_model_id=81,
        application_id="app-refresh",
        conversation_id="conversation-original",
        message_id="refresh-attempt",
        question="查询销售额",
        replaces_message_id="legacy-last-user-message",
        regenerate=True,
    )

    execution, _, _ = _prepare_regeneration(payload)

    assert execution.history == []
    assert execution._regeneration_mode == "REFRESH"


def test_regeneration_text_fallback_normalizes_unicode_dash():
    payload = ChatRequest(semantic_model_id=81,
        application_id="app-refresh",
        conversation_id="conversation-original",
        message_id="refresh-attempt",
        original_question="查询 TDC-3 产品的主要适用科室",
        question="查询 TDC-3 产品的所有适用科室",
        history=[
            {"role": "user", "content": "查询销售额"},
            {"role": "assistant", "content": "旧销售额答案"},
            {"role": "user", "content": "查询 TDC‑3 产品的主要适用科室"},
            {"role": "assistant", "content": "应移除的旧科室答案"},
        ],
    )

    execution, _, _ = _prepare_regeneration(payload)

    assert [item.content for item in execution.history] == ["查询销售额", "旧销售额答案"]


def test_regeneration_text_fallback_accepts_known_ui_question_prefix_only():
    payload = ChatRequest(semantic_model_id=81,
        application_id="app-refresh",
        conversation_id="conversation-original",
        message_id="refresh-attempt",
        question="查询销售额",
        history=[
            {"role": "user", "content": "查询销售量"},
            {"role": "assistant", "content": "旧销售量答案"},
            {"role": "user", "content": "第2轮问题：查询销售额"},
            {"role": "assistant", "content": "应移除的旧销售额答案"},
        ],
    )

    execution, _, _ = _prepare_regeneration(payload)

    assert [item.content for item in execution.history] == ["查询销售量", "旧销售量答案"]


def test_refresh_endpoint_is_idempotent_and_keeps_original_session_scope():
    app = build_test_app()
    payload = {"semantic_model_id": 81,
        "application_id": "app-refresh",
        "conversation_id": "conversation-original",
        "message_id": "external-message",
        "refresh_request_id": "attempt-1",
        "question": "查询本月销售额",
    }

    with TestClient(app) as client:
        first = client.post("/agent_chat/refresh", json=payload)
        retry = client.post("/agent_chat/refresh", json=payload)
        next_click = client.post(
            "/agent_chat/refresh",
            json={**payload, "refresh_request_id": "attempt-2"},
        )
        response_keys = list(app.state.container.sessions._responses)

    assert first.status_code == retry.status_code == next_click.status_code == 200
    assert first.json()["conversation_id"] == "conversation-original"
    assert retry.json()["request_id"] == first.json()["request_id"]
    assert next_click.json()["request_id"] != first.json()["request_id"]
    assert response_keys
    assert all(key[3] == "conversation-original" for key in response_keys)


def test_revise_last_question_endpoint_does_not_require_history():
    app = build_test_app()
    payload = {"semantic_model_id": 81,
        "application_id": "app-refresh",
        "conversation_id": "conversation-original",
        "message_id": "external-message",
        "refresh_request_id": "revise-attempt-1",
        "original_question": "按月查询销售额",
        "question": "按季度查询销售额",
    }

    with TestClient(app) as client:
        response = client.post("/agent_chat/refresh", json=payload)

    assert response.status_code == 200
    assert response.json()["conversation_id"] == "conversation-original"


def test_regeneration_rejects_unknown_explicit_replacement_message_id():
    payload = ChatRequest(semantic_model_id=81,
        application_id="app-refresh",
        conversation_id="conversation-original",
        message_id="refresh-attempt",
        question="查询销售额",
        replaces_message_id="missing-user-turn",
        history=[{"role": "user", "message_id": "other", "content": "查询销售额"}],
    )

    with pytest.raises(Exception) as exc_info:
        _prepare_regeneration(payload)

    assert getattr(exc_info.value, "status_code", None) == 400


def test_development_can_temporarily_use_fallback_identity():
    with RawTestClient(
        build_test_app(allow_missing_trusted_identity_headers=True),
        headers={'Authorization': 'Bearer phase0c-fixture-token'},
    ) as client:
        response = client.post(
            "/agent_chat",
            json={"semantic_model_id": 81,
                "application_id": "app1",
                "conversation_id": "anonymous-development",
                "message_id": "m1",
                "question": "你好",
            },
        )

    # Current backend contract guarantees a globally unique conversation ID.
    assert response.status_code == 200
    assert response.json()['status'] == 'COMPLETED'


def test_application_header_is_ignored_and_body_scope_is_used():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat",
            headers={
                "X-Tenant-Id": "t1",
                "X-User-Id": "u1",
                "X-Application-Id": "app-from-gateway",
            },
            json={"semantic_model_id": 81,
                "application_id": "different-app",
                "conversation_id": "c1",
                "message_id": "m1",
                "question": "你好",
            },
        )
    assert response.status_code == 403
    assert response.json()['detail']['code'] == 'STATE_NAMESPACE_MISMATCH'


def test_legacy_application_header_setting_no_longer_requires_header():
    with TestClient(build_test_app(require_trusted_application_header=True)) as client:
        response = client.post(
            "/agent_chat",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={"semantic_model_id": 81,
                "application_id": "app1",
                "conversation_id": "c1",
                "message_id": "m1",
                "question": "你好",
            },
        )
    assert response.status_code == 401
    assert response.json()['detail']['code'] == 'STATE_APPLICATION_REQUIRED'


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
            json={"semantic_model_id": 81,
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
            json={"semantic_model_id": 81,
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
    assert all(len(chunk) == 6 for chunk in chunks[:-1])
    assert 1 <= len(chunks[-1]) <= 6
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
    milestone_chunks: list[list[dict]] = []
    for data in think_chunks:
        if data.get("index") == 0:
            milestone_chunks.append([])
        assert milestone_chunks
        milestone_chunks[-1].append(data)
    assert all(
        "".join(chunk["content"] for chunk in chunks).startswith("\n\n")
        for chunks in milestone_chunks
    )
    assert all(
        "".join(chunk["content"] for chunk in chunks).endswith("\n\n")
        for chunks in milestone_chunks
    )
    assert all(chunks[-1]["is_last"] is True for chunks in milestone_chunks)
    assert any(len(chunks) > 1 for chunks in milestone_chunks)
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
        "#### ◉ 意图识别",
        "#### ◉ 任务拆分与规划",
    ]
    assert all(all_thinking_content.count(heading) == 1 for heading in expected_headings)
    assert len(re.findall(r"(?m)^\s*#{1,6}\s+", all_thinking_content)) == 2
    intent_chunk = next(
        data for data in think_chunks
        if data.get("meta", {}).get("stage") == "INTENT_RECOGNITION"
    )
    assert intent_chunk["step"] == "step1"
    assert "#### ◉ 意图识别" in thinking_content(
        events, "INTENT_RECOGNITION", status="RUNNING"
    )
    completed_intent_chunk = next(
        data for data in think_chunks
        if data.get("meta", {}).get("stage") == "INTENT_RECOGNITION"
        and data.get("meta", {}).get("status") == "COMPLETED"
        and data.get("index") == 0
    )
    completed_intent_content = thinking_content(
        events, "INTENT_RECOGNITION", status="COMPLETED"
    )
    assert "#### ◉ 意图识别" not in completed_intent_content
    assert completed_intent_chunk["meta"]["display_model"] == "IntentRecognitionDisplayV2"
    assert completed_intent_chunk["meta"]["display_version"] == "V2"
    assert "用户原始问题：" in completed_intent_content
    assert re.search(
        r"用户原始问题：[^\n]+  \n补全后的问题：",
        completed_intent_content,
    )
    planning_completed_chunk = next(
        data for data in think_chunks
        if data.get("meta", {}).get("stage") == "TASK_PLANNING"
        and data.get("meta", {}).get("status") == "COMPLETED"
    )
    planning_completed_content = thinking_content(
        events, "TASK_PLANNING", status="COMPLETED"
    )
    assert "#### ◉ 任务拆分与规划" in planning_completed_content
    assert "拆分判断完成" in planning_completed_content
    assert "当前问题无需拆分，按单任务执行。  \n任务1：" in planning_completed_content
    assert "规划调用：" in planning_completed_content
    assert f"规划调用：{QUERY_EXECUTION_CHAIN}" in planning_completed_content
    completed_think_stages = [
        data.get("meta", {}).get("stage")
        for data in events
        if data["type"] == "message_chunk"
        and data.get("meta", {}).get("status") == "COMPLETED"
    ]
    assert "OUTPUT_SUMMARY" not in completed_think_stages
    if "TASK_PLANNING" in completed_think_stages:
        assert completed_think_stages.index("INTENT_RECOGNITION") < completed_think_stages.index("TASK_PLANNING")


def test_chat_stream_uses_document_chat_section_format():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat/stream",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={"semantic_model_id": 81,
                "application_id": "app1",
                "conversation_id": "document-chat-format",
                "message_id": "m1",
                "question": "今天工作辛苦了。",
            },
        )

    events = [
        json.loads(block.removeprefix("data: "))
        for block in response.text.strip().split("\n\n")
    ]
    thinking = "".join(
        event["content"] for event in events
        if event["type"] == "message_chunk" and event.get("step") != "output"
    )

    assert "#### ◉ 意图识别" in thinking
    assert "用户原始问句：今天工作辛苦了。" in thinking
    assert "输出总结" not in thinking
    assert "#### 2、最终输出" in thinking
    assert "任务拆分与规划" not in thinking
    assert "调度执行" not in thinking


def test_missing_parameter_stream_uses_document_clarification_section_format():
    with TestClient(build_test_app()) as client:
        response = client.post(
            "/agent_chat/stream",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={"semantic_model_id": 81,
                "application_id": "app1",
                "conversation_id": "document-clarification-format",
                "message_id": "m1",
                "question": "查询经销商销售数据",
            },
        )

    events = [
        json.loads(block.removeprefix("data: "))
        for block in response.text.strip().split("\n\n")
    ]
    thinking = "".join(
        event["content"] for event in events
        if event["type"] == "message_chunk" and event.get("step") != "output"
    )
    completed = next(event for event in events if event["type"] == "complete")

    assert completed["status"] == "NEEDS_CLARIFICATION"
    assert "#### ◉ 意图识别" in thinking
    assert "用户原始问句：查询经销商销售数据" in thinking
    assert "#### 2、任务拆分与规划" in thinking
    assert "当前任务参数不完整，暂停子任务拆分" in thinking
    assert "#### 3、调研执行" in thinking
    assert "跳过所有工具调用，无工具发起请求" in thinking
    assert "#### 4、结果生成" in thinking
    assert "等待用户补充参数后再继续处理" in thinking
    assert "#### 5、最终输出" in thinking
    assert "#### ◉ 输出总结" not in thinking
    assert not any(
        event.get("meta", {}).get("stage") == "DATA_RETRIEVAL"
        for event in events if event["type"] == "message_chunk"
    )


def test_composite_stream_keeps_root_question_and_suppresses_child_intents():
    with TestClient(build_test_app(multi_question_model_enabled=False)) as client:
        response = client.post(
            "/agent_chat/stream",
            headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"},
            json={
                "application_id": "app1",
                "conversation_id": "composite-intent-stream",
                "message_id": "m1",
                "question": "查询 TDC-3 产品的主要适用科室、次要适用科室",
                "semantic_model_id": 81,
            },
        )

    assert response.status_code == 200
    events = [
        json.loads(block.removeprefix("data: "))
        for block in response.text.strip().split("\n\n")
    ]
    intent_chunks = [
        event for event in events
        if event["type"] == "message_chunk"
        and event.get("meta", {}).get("stage") == "INTENT_RECOGNITION"
    ]
    assert len(intent_chunks) > 1
    intent = intent_chunks[0]
    assert intent["meta"]["display_model"] == "CompositeIntentRecognitionDisplayV2"
    assert intent["meta"]["is_composite"] is True
    intent_content = "".join(event["content"] for event in intent_chunks)
    assert "查询 TDC-3 产品的主要适用科室、次要适用科室" in intent_content
    assert "任务意图：明细查询" in intent_content
    assert "复合查询" not in intent_content
    assert "共享业务标识" not in intent_content
    assert "结构化拆分" not in intent_content
    assert "参数规范化：已识别" not in intent_content
    assert "1. 查询 TDC-3 产品的主要适用科室" in intent_content
    assert "2. 查询 TDC-3 产品的次要适用科室" in intent_content
    assert not intent["meta"].get("is_child_task")
    planning_content = "".join(
        event["content"] for event in events
        if event["type"] == "message_chunk"
        and event.get("meta", {}).get("stage") == "TASK_PLANNING"
    )
    assert "任务1：查询 TDC-3 产品的主要适用科室" in planning_content
    assert "任务2：查询 TDC-3 产品的次要适用科室" in planning_content
    assert planning_content.count("规划调用：") == 2
    completed = next(event for event in events if event["type"] == "complete")
    assert completed["execution_shape"] == "COMPOSITE"
    assert len(completed["task_results"]) == 2
    assert "| 查询目标 | 结果内容 |" not in completed["answer"]
    assert "\\|" not in completed["answer"]
    assert "<br>" not in completed["answer"]
    child_execution = [
        event for event in events
        if event["type"] == "message_chunk"
        and event.get("meta", {}).get("is_child_task")
        and event.get("meta", {}).get("stage") == "DATA_RETRIEVAL"
    ]
    assert child_execution
    assert {event["meta"]["task_id"] for event in child_execution} == {
        "task-1", "task-2",
    }
    child_execution_text = "".join(event["content"] for event in child_execution)
    assert "任务1执行链路：" in child_execution_text
    assert "任务2执行链路：" in child_execution_text
    child_public_progress = [
        event for event in events
        if event["type"] == "message_chunk"
        and event.get("meta", {}).get("is_child_task")
        and event.get("meta", {}).get("stage") in {
            "DATA_RETRIEVAL", "RELIABILITY_CHECK", "INSIGHT_ANALYSIS",
        }
    ]
    stage_rank = {
        "DATA_RETRIEVAL": 0,
        "RELIABILITY_CHECK": 1,
        "INSIGHT_ANALYSIS": 2,
    }
    ranks = [stage_rank[event["meta"]["stage"]] for event in child_public_progress]
    assert ranks == sorted(ranks)
    # Both child SQL/tool traces must finish inside 调度执行 before either
    # result validation or insight analysis is rendered.
    assert {
        event["meta"]["task_id"] for event in child_public_progress
        if event["meta"]["stage"] == "DATA_RETRIEVAL"
    } == {"task-1", "task-2"}
    completed_retrieval_text = "".join(
        event["content"] for event in child_public_progress
        if event["meta"]["stage"] == "DATA_RETRIEVAL"
        and event["meta"]["status"] == "COMPLETED"
    )
    assert "查询字段：" in completed_retrieval_text
    assert "返回行数：" in completed_retrieval_text
    assert "结果总行数：" in completed_retrieval_text
    assert "数据质量：" not in completed_retrieval_text
    assert re.search(
        r"查询快照时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}（北京时间）",
        completed_retrieval_text,
    )
    assert "数据预览：" in completed_retrieval_text
    for internal_label in (
        "columns=", "row_count=", "total_row_count=",
        "quality_status=", "data_as_of=", "rows_preview=",
    ):
        assert internal_label not in completed_retrieval_text
    completed_validation_text = "".join(
        event["content"] for event in child_public_progress
        if event["meta"]["stage"] == "RELIABILITY_CHECK"
        and event["meta"]["status"] == "COMPLETED"
    )
    assert "校验结论：高可信" in completed_validation_text
    assert "数据质量：通过" in completed_validation_text
    assert "证据（" in completed_validation_text
    assert "查询结果：" in completed_validation_text
    assert "（来源：本轮只读数据库查询返回的数据集" in completed_validation_text
    assert "告警：无。" in completed_validation_text
    assert "HIGH" not in completed_validation_text
    assert "PASS" not in completed_validation_text


def test_all_six_analytic_thinking_stages_have_normalized_headings():
    expected = {
        "INTENT_RECOGNITION": "#### ◉ 意图识别",
        "FILE_INSPECTION": "#### ◉ 文件感知与解析",
        "TASK_PLANNING": "#### ◉ 任务拆分与规划",
        "DATA_RETRIEVAL": "#### ◉ 调度执行",
        "RELIABILITY_CHECK": "#### ◉ 结果校验",
        "INSIGHT_ANALYSIS": "#### ◉ 数据洞察分析",
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
            json={"semantic_model_id": 81,
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
            json={"semantic_model_id": 81,
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
    original = {"semantic_model_id": 81,
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
    payload = {"semantic_model_id": 81,
        "application_id": "app1",
        "conversation_id": "extension-contract",
        "message_id": "m1",
        "question": "你好",
        "use_longterm_memory": False,
        "tools": [{
            "name": "inventory_lookup", "url": "http://tool.example.invalid/tool",
            "http_method": "post", "inputSchema": {"type": "object"},
        }],
        "skills": [{"code": "analysis", "slug": "metric_query"}],
        "mcp": [{
            "mcp_server_url": "http://mcp.example.invalid/mcp",
            "connect_type": "streamable_http",
            "slug": "",
            "display_name": "Excel数据分析",
            "time_out": 120,
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
            json={"semantic_model_id": 81,
                "application_id": "app1", "conversation_id": "files",
                "message_id": "m1", "question": "分析这些文件",
                "temp_file_paths": ["uploads/a.xlsx", "uploads/b.xlsx"],
            },
        )
    assert response.status_code == 422
    assert "一个CSV/XLSX" in response.json()["detail"]
