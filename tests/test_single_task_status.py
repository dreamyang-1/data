"""Single-task progress and persisted state follow the actual execution outcome."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import (
    AgentResponse, ChatRequest, ClarificationDecisionTrace, PlannerExtraction,
    PrimaryIntent, ReliabilityReport, TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.planning.compat import PlanningResult
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import emit_progress, progress_scope
from app.stores import InMemorySessionStore


IDENTITY = TrustedIdentity(tenant_id="t1", user_id="u1")


def setup_request(monkeypatch):
    service = DataAnalysisOrchestrator(
        settings=Settings(_env_file=None, env="test", adapter_mode="mock",
                          intent_model_enabled=False, multi_question_enabled=True,
                          analysis_synthesis_enabled=False),
        classifier=RuleBasedIntentClassifier(), adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(), task_planner=SimpleNamespace(),
    )
    chat = ChatRequest(application_id="app", conversation_id="single-status",
                       message_id="m1", question="查询本月销售额",
                       semantic_model_id=81, business_domain_ids=[205])
    outcome = PlanningResult(plan=None, single_extraction=PlannerExtraction(
        intent=PrimaryIntent.METRIC_QUERY,
        structured={"实体": ["销售订单"], "指标": [{"name": "销售额"}], "维度": []},
    ), single_task_question=chat.question, single_task_name=chat.question)
    monkeypatch.setattr("app.services.orchestrator.plan_question", AsyncMock(return_value=outcome))
    return service, chat


def response_for(chat, status):
    response = AgentResponse(request_id=uuid4(), conversation_id=chat.conversation_id,
                             status=status, intent=PrimaryIntent.METRIC_QUERY, answer="测试结果")
    if status == "NEEDS_CLARIFICATION":
        response.clarification_decision_traces = [ClarificationDecisionTrace(
            conversation_id=chat.conversation_id, source_stage="STRUCTURED_ASL_BINDING",
            reason_type="USER_SEMANTIC_AMBIGUITY", blocking_slot="hospital",
            expected_answer_type="choice", candidate_ids=["hospital-a", "hospital-b"],
            is_user_ambiguity=True, system_repair_possible=False, decision="ASK",
        )]
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "label"), [
    ("COMPLETED", "已完成"), ("PARTIAL_SUCCESS", "部分完成"),
    ("NEEDS_CLARIFICATION", "待补充条件"), ("SAFE_FALLBACK", "执行失败"),
    ("FAILED", "执行失败"), ("CANCELLED", "已取消"),
])
async def test_single_task_outcome_updates_progress_and_followup_memory(monkeypatch, status, label):
    service, chat = setup_request(monkeypatch)
    original_chat = chat.model_copy(deep=True)
    events = []

    async def execute(current, identity):
        assert current._task_table[0]["执行状态"] == "执行中"
        current._last_result_summary = {"columns": ["销售额"], "row_count": 1}
        return response_for(current, status)

    monkeypatch.setattr(service, "_handle", execute)
    with progress_scope(lambda event: events.append(copy.deepcopy(event))):
        response = await service._handle_request(chat, IDENTITY)
    assert response.status == status
    tables = [e["task_table"] for e in events if e.get("task_table")]
    assert [t[0]["执行状态"] for t in tables] == ["待执行", "执行中", label]
    saved = await service.sessions.get_task_table("t1", "u1", "app", chat.conversation_id)
    assert saved["tasks"][0]["执行状态"] == label
    assert saved["tasks"][0]["结果概要"]["row_count"] == 1
    # An idempotent replay must not restart or reset the completed task.
    with progress_scope(lambda event: events.append(copy.deepcopy(event))):
        replay = await service._handle_request(original_chat, IDENTITY)
    assert replay.status == status
    assert len([e for e in events if e.get("task_table")]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["analysis_incomplete", "suppressed_clarification"])
async def test_single_status_uses_final_response_after_clarification_policy(monkeypatch, scenario):
    service, chat = setup_request(monkeypatch)
    response = response_for(chat, "PARTIAL_SUCCESS" if scenario == "analysis_incomplete" else "NEEDS_CLARIFICATION")
    response.clarification_decision_traces = []
    if scenario == "analysis_incomplete":
        response.reliability = ReliabilityReport(level="LIMITED", score=0.5, gates={"analysis_completed": False})
    monkeypatch.setattr(service, "_handle", AsyncMock(return_value=response))
    result = await service._handle_request(chat, IDENTITY)
    assert result.status == ("PARTIAL_SUCCESS" if scenario == "analysis_incomplete" else "SAFE_FALLBACK")
    assert chat._task_table[0]["执行状态"] == ("分析未完成" if scenario == "analysis_incomplete" else "执行失败")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_single_task_exception_and_cancellation_are_persisted(monkeypatch, cancel):
    service, chat = setup_request(monkeypatch)
    entered = asyncio.Event()

    async def execute(current, identity):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("execution failed")

    monkeypatch.setattr(service, "_handle", execute)
    events = []
    with progress_scope(lambda event: events.append(copy.deepcopy(event))):
        task = asyncio.create_task(service._handle_request(chat, IDENTITY))
        await asyncio.wait_for(entered.wait(), timeout=5)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
    expected = "已取消" if cancel else "执行失败"
    assert [e["task_table"][0]["执行状态"] for e in events if e.get("task_table")] == [
        "待执行", "执行中", expected,
    ]
    saved = await service.sessions.get_task_table("t1", "u1", "app", chat.conversation_id)
    assert saved["tasks"][0]["执行状态"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["absent", "previous_message", "dag", "multiple"])
async def test_single_status_does_not_modify_another_tasks_table(monkeypatch, kind):
    service, chat = setup_request(monkeypatch)
    chat._task_table = None if kind == "absent" else [{"执行状态": "待执行"}]
    chat._task_table_message_id = "previous" if kind == "previous_message" else chat.message_id
    if kind == "multiple":
        chat._task_table.append({"执行状态": "待执行"})
    if kind == "dag":
        chat._dag_task_card = True
    before = copy.deepcopy(chat._task_table)
    await service._set_single_task_status(chat, "已完成")
    assert chat._task_table == before


def test_stream_releases_planning_before_single_task_status():
    from test_api import TestClient, build_test_app

    class Workflow:
        async def ainvoke(self, state):
            table = [{"任务": "task-1", "名称": "查询销售额", "执行状态": "待执行"}]
            await emit_progress("INTENT_RECOGNITION", "COMPLETED", "意图：指标查询。")
            await emit_progress("TASK_PLANNING", "COMPLETED", "任务1：查询销售额。",
                                task_count=1, task_table=copy.deepcopy(table))
            table[0]["执行状态"] = "执行中"
            await emit_progress("TASK_STATUS", "RUNNING", "", heading_only=True,
                                task_table=copy.deepcopy(table))
            await emit_progress("ASL_VALIDATION", "COMPLETED", "解析完成。")
            table[0]["执行状态"] = "已完成"
            await emit_progress("TASK_STATUS", "RUNNING", "", heading_only=True,
                                task_table=copy.deepcopy(table))
            return {"response": response_for(state["chat"], "COMPLETED")}

    app = build_test_app(runtime_mode="V1")
    with TestClient(app) as client:
        object.__setattr__(app.state.container, "workflow", Workflow())
        result = client.post("/agent_chat/stream", json={
            "semantic_model_id": 81, "business_domain_ids": [205], "application_id": "app",
            "conversation_id": "stream-single-status", "message_id": "m1", "question": "查询销售额",
        })
    assert result.status_code == 200
    events = [json.loads(block.removeprefix("data: ")) for block in result.text.strip().split("\n\n")]
    tables = [e["task_table"] for e in events if e.get("task_table")]
    assert [t[0]["执行状态"] for t in tables] == ["待执行", "执行中", "已完成"]
    assert not any(e.get("type") == "message_chunk" and e.get("meta", {}).get("stage") == "TASK_STATUS"
                   for e in events)
    chunks = [e.get("meta", {}).get("stage") for e in events if e.get("type") == "message_chunk"]
    assert chunks.index("INTENT_RECOGNITION") < chunks.index("TASK_PLANNING") < chunks.index("ASL_VALIDATION")
    assert events[-1]["type"] == "complete"
