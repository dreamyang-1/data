"""Completed composite queries retain their ASL boundary and child status."""
import copy
import json
from datetime import datetime, timezone

import pytest

from app.domain.models import (
    AgentPromptConfig, AtomicTask, ChatRequest, DataQueryResult, Dataset,
    PrimaryIntent, TaskPlan, TrustedIdentity,
)
from app.planning import MultiQuestionPlanner
from app.services.progress import progress_scope
from test_orchestrator import service


def hospital_plan():
    tasks = []
    for index, (question, metric, dimensions) in enumerate([
        ("查询上海地区各经销商的已合作医院数", "已合作医院数", ["经销商"]),
        ("查询上海地区的区域全部医院总数", "区域全部医院总数", []),
    ], 1):
        tasks.append(AtomicTask(
            task_id=f"task-{index}", question=question,
            primary_intent=PrimaryIntent.METRIC_QUERY,
            extraction={"意图":"统计查询", "实体":["医院"],
                        "指标":[{"name":metric}], "维度":dimensions,
                        "展示字段":[], "过滤条件":[{"field":"省份", "op":"=", "value":["上海"]}]},
        ))
    return TaskPlan(planner="DETERMINISTIC_RULE", tasks=tasks)


def parent_chat():
    chat = ChatRequest(
        semantic_model_id=81, business_domain_ids=[205],
        conversation_id="composite-surface", application_id="test", message_id="m1",
        question="查询各经销商在上海地区的已合作医院数，并查询上海地区的区域全部医院总数",
        prompt=AgentPromptConfig(user="已发布业务规则，不得跨业务域"),
    )
    chat._completed_question_execution = True
    return chat


@pytest.mark.asyncio
async def test_completed_children_reach_asl_with_their_own_extraction():
    agent = service()
    agent.settings.surface_asl_execution_enabled = True
    agent.task_planner = MultiQuestionPlanner(agent.settings)
    plan = hospital_plan()
    before = copy.deepcopy(plan)
    calls = []

    async def query_surface(request, identity, *, mentions, structured_extraction=None):
        calls.append((request.rewritten_question, structured_extraction,
                      request.semantic_model_id, list(request.business_domain_ids)))
        return DataQueryResult(
            asl={"version":"2.0", "metrics":[{"name":"fixture_count"}],
                 "dimensions":[], "filters":[], "ambiguity":[]},
            sql="SELECT 1",
            dataset=Dataset(columns=["数量"], rows=[{"数量":1}], row_count=1,
                            data_as_of=datetime.now(timezone.utc), quality_status="PASS",
                            snapshot_id=request.conversation_id),
        )

    agent.adapters.query.retrieval.query_surface = query_surface
    events = []
    with progress_scope(events.append):
        response = await agent._handle_task_plan(parent_chat(), TrustedIdentity(tenant_id="t", user_id="u"), plan)
    assert response.status == "COMPLETED", [(r.question, r.status) for r in response.task_results]
    assert len(calls) == 2
    by_question = {item[0]: item for item in calls}
    for task in plan.tasks:
        assert by_question[task.question][1:] == (task.extraction, 81, [205])
    assert plan == before
    assert not any(event["stage"] == "CLARIFICATION_EXECUTION" for event in events)
    terminals = [e for e in events if e.get("task_terminal")]
    assert {e["task_id"] for e in terminals} == {"task-1", "task-2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("completed,answered", [(True, False), (False, False), (True, True)])
async def test_child_context_preserves_prompt_without_bypassing_pending_answers(completed, answered):
    from app.domain.models import AgentResponse
    from uuid import uuid4
    agent = service()
    agent.task_planner = MultiQuestionPlanner(agent.settings)
    chat = parent_chat()
    chat._completed_question_execution = completed
    seen = []

    async def handle_child(child, identity):
        seen.append(child)
        return AgentResponse(request_id=uuid4(), conversation_id=child.conversation_id,
                             status="COMPLETED", intent=PrimaryIntent.METRIC_QUERY, answer="完成")

    agent._handle = handle_child
    await agent._handle_task_plan(chat, TrustedIdentity(tenant_id="t", user_id="u"), hospital_plan(),
                                 task_answers={"task-2":"1"} if answered else None)
    assert all(child.prompt == chat.prompt and child.prompt is not chat.prompt for child in seen)
    assert seen[0]._completed_question_execution is completed
    assert seen[1]._completed_question_execution is (completed and not answered)
    if answered:
        assert seen[1].question == "1"


@pytest.mark.asyncio
async def test_dependent_child_still_uses_existing_context_path():
    from app.domain.models import AgentResponse
    from uuid import uuid4
    agent = service()
    agent.task_planner = MultiQuestionPlanner(agent.settings)
    plan = hospital_plan()
    plan.tasks[1].depends_on = ["task-1"]
    flags = []

    async def handle_child(child, identity):
        flags.append(child._completed_question_execution)
        return AgentResponse(request_id=uuid4(), conversation_id=child.conversation_id,
                             status="COMPLETED", intent=PrimaryIntent.METRIC_QUERY, answer="完成")

    agent._handle = handle_child
    await agent._handle_task_plan(parent_chat(), TrustedIdentity(tenant_id="t", user_id="u"), plan)
    assert flags == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["native", "contextual"])
async def test_native_plan_and_contextual_child_do_not_take_surface_shortcut(boundary):
    from types import SimpleNamespace
    from uuid import uuid4
    from app.domain.models import AgentResponse
    agent = service()
    agent.task_planner = MultiQuestionPlanner(agent.settings)
    chat, plan = parent_chat(), hospital_plan()
    if boundary == "native":
        chat._semantic_decision = SimpleNamespace(source="V2_AUTHORIZED_PLAN")
    else:
        plan.tasks[1].question = "再查询上海地区的区域全部医院总数"
    flags = []

    async def handle_child(child, identity):
        flags.append(child._completed_question_execution)
        return AgentResponse(request_id=uuid4(), conversation_id=child.conversation_id,
                             status="COMPLETED", intent=PrimaryIntent.METRIC_QUERY, answer="完成")

    agent._handle = handle_child
    await agent._handle_task_plan(chat, TrustedIdentity(tenant_id="t", user_id="u"), plan)
    assert flags == ([False, False] if boundary == "native" else [True, False])


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["clarify", "failure", "dependency", "checkpoint"])
async def test_every_child_has_terminal_progress_even_without_query(outcome):
    from app.domain.models import AgentResponse
    from uuid import uuid4
    agent = service()
    agent.task_planner = MultiQuestionPlanner(agent.settings)
    plan = hospital_plan()
    if outcome == "dependency":
        plan.tasks[1].depends_on = ["task-1"]
    calls = []

    async def handle_child(child, identity):
        calls.append(child.question)
        if outcome in {"failure", "dependency"}:
            raise RuntimeError("fixture failure")
        return AgentResponse(
            request_id=uuid4(), conversation_id=child.conversation_id,
            status="NEEDS_CLARIFICATION" if outcome == "clarify" else "COMPLETED",
            intent=PrimaryIntent.METRIC_QUERY, answer="请选择指标口径" if outcome == "clarify" else "完成",
            clarification_questions=["请选择指标口径"] if outcome == "clarify" else [],
        )

    agent._handle = handle_child
    chat = parent_chat()
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    if outcome == "checkpoint":
        import hashlib
        saved = {
            task.task_id: AgentResponse(
                request_id=uuid4(), conversation_id="cached-" + task.task_id,
                status="COMPLETED", intent=PrimaryIntent.METRIC_QUERY, answer="完成",
            ).model_dump(mode="json") for task in plan.tasks
        }
        await agent.sessions.put_dag_checkpoint("t", "u", chat.application_id, chat.conversation_id, chat.message_id, {
            "plan_fingerprint": hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
            "authorized_scope": chat.authorized_semantic_scope.fingerprint(),
            "completed": saved,
            "conversations": {task.task_id:"cached-" + task.task_id for task in plan.tasks},
        })
    events = []
    with progress_scope(events.append):
        await agent._handle_task_plan(chat, identity, plan)
    terminals = [e for e in events if e.get("task_terminal")]
    assert {e["task_id"] for e in terminals} == {"task-1", "task-2"}
    if outcome == "dependency":
        assert terminals[1]["task_result_status"] == "SKIPPED"
    if outcome == "checkpoint":
        assert calls == []


def test_composite_stream_keeps_child_clarification_inside_its_execution_block():
    from uuid import uuid4
    from app.domain.models import AgentResponse
    from app.services.progress import emit_progress, task_progress_scope
    from test_api import TestClient, build_test_app
    app = build_test_app()

    class MixedWorkflow:
        async def ainvoke(self, state):
            await emit_progress("INTENT_RECOGNITION", "COMPLETED", "独立新问题", is_composite=True, task_count=2)
            await emit_progress("TASK_PLANNING", "COMPLETED", "拆分判断完成。已拆分为以下任务：\n任务1：指标一\n任务2：指标二")
            with task_progress_scope(is_child_task=True, task_id="task-2", task_index=1, task_count=2, task_question="指标二"):
                await emit_progress("COMPLETENESS_CHECK", "NEEDS_INPUT", "缺少条件")
                await emit_progress("CLARIFICATION_EXECUTION", "COMPLETED", "跳过所有工具调用，无工具发起请求")
                await emit_progress("CLARIFICATION_RESULT", "COMPLETED", "本轮查询任务暂不执行")
                await emit_progress("DATA_RETRIEVAL", "SKIPPED", "本任务需要补充指标口径", task_terminal=True, task_result_status="NEEDS_CLARIFICATION")
            with task_progress_scope(is_child_task=True, task_id="task-1", task_index=0, task_count=2, task_question="指标一"):
                await emit_progress("DATA_RETRIEVAL", "RUNNING", "任务一取数")
                await emit_progress("SQL_EXECUTION", "COMPLETED", "任务一SQL成功")
                await emit_progress("DATA_RETRIEVAL", "COMPLETED", "任务一取数完成")
                await emit_progress("RELIABILITY_CHECK", "COMPLETED", "任务一数据校验")
                await emit_progress("INSIGHT_ANALYSIS", "COMPLETED", "任务一分析")
                await emit_progress("DATA_RETRIEVAL", "COMPLETED", "本任务处理完成", task_terminal=True, task_result_status="COMPLETED")
            return {"response":AgentResponse(request_id=uuid4(), conversation_id=state["chat"].conversation_id,
                     status="NEEDS_CLARIFICATION", intent=PrimaryIntent.METRIC_QUERY,
                     execution_shape="COMPOSITE", answer="任务一成功；任务二需要补充指标口径。")}

    with TestClient(app) as client:
        object.__setattr__(app.state.container, "workflow", MixedWorkflow())
        response = client.post("/agent_chat/stream", json={"semantic_model_id":81, "application_id":"app",
                               "conversation_id":"mixed-child-display", "message_id":"m1", "question":"查询两个指标"})
    assert response.status_code == 200
    events = [json.loads(b.removeprefix("data: ")) for b in response.text.strip().split("\n\n")]
    text = "".join(e.get("content", "") for e in events if e.get("type") == "message_chunk")
    for forbidden in ["跳过所有工具", "本轮查询任务暂不执行", "调研执行", "结果生成"]:
        assert forbidden not in text
    assert text.index("任务一取数完成") < text.index("本任务需要补充指标口径") < text.index("任务一数据校验")
    assert "任务2：指标二" in text
    headings = ["意图识别", "任务拆分与规划", "调度执行", "结果校验", "数据洞察分析", "最终输出"]
    offsets = [text.index("#### ◉ " + heading) for heading in headings]
    assert offsets == sorted(offsets)
    assert all(text.count("#### ◉ " + heading) == 1 for heading in headings)
    assert next(e for e in events if e.get("type") == "complete")["status"] == "NEEDS_CLARIFICATION"
