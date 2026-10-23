from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.domain.models import (
    AtomicTask, ChatRequest, ConversationControl, PlannerExtraction, PrimaryIntent, TaskPlan,
)
from app.planning.task_dag import SingleQuestionPlanner, database_query_choice
from app.services.orchestrator import DataAnalysisOrchestrator, ExplicitDatasetUnavailableError
from app.services.progress import progress_scope
from test_conversation_result_followup import _agent, _first_trend, IDENTITY
from test_dataset_lineage_contract import setup, save, remember, request, ROWS


@pytest.mark.parametrize("value,expected", [
    (False, False), (True, True), ("否", False), ("是", True),
    (" no ", False), ("true", True), ("否（基于前序查询结果）", False),
    ("是(需要新数据)", True), (None, None), ("不确定", None), (0, None), ({}, None),
])
def test_route_flag_normalization_and_sanitization(value, expected):
    raw = {"是否查询数据库": value}
    assert database_query_choice(raw) is expected
    cleaned = SingleQuestionPlanner._sanitize_extraction(raw, intent=PrimaryIntent.METRIC_QUERY)
    assert database_query_choice(cleaned) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [False, True])
async def test_explicit_no_overrides_model_new_query_without_sql(surface, monkeypatch):
    agent, retrieval, _, _ = _agent()
    conversation = f"structured-no-{surface}"
    first = await _first_trend(agent, conversation)
    assert first.dataset_id
    chat = ChatRequest(application_id="app", conversation_id=conversation,
        message_id="m2", question="根据刚才结果做个简要总结", semantic_model_id=81)
    chat._planner_extraction = PlannerExtraction(
        intent=PrimaryIntent.TREND_ANALYSIS, structured={"是否查询数据库": "否"},
    )
    chat._model_data_source = "NEW_QUERY"
    events = []
    if surface:
        canonical = agent.classifier.classify(chat.question, IDENTITY, conversation)
        canonical.primary_intent = PrimaryIntent.TREND_ANALYSIS
        canonical.query_resolution_type = "DIRECT_DATA_QUERY"
        complete = AsyncMock(return_value="snapshot-reused")
        monkeypatch.setattr(agent, "_complete_query_result", complete)
        with progress_scope(events.append):
            result = await agent._handle_surface_query(chat, IDENTITY, request=canonical)
        assert result == "snapshot-reused"
        reused = complete.call_args.args[3]
        assert reused.sql == "UPLOADED_DATASET_NO_SQL"
        assert len(reused.dataset.rows) == 3
    else:
        with progress_scope(events.append):
            result = await agent.handle(chat, IDENTITY)
        assert result.status == "COMPLETED"
    assert len(retrieval.requests) == 1
    stages = [event["stage"] for event in events]
    assert "ASL_GENERATION" in stages
    if "DATA_RETRIEVAL" in stages:
        assert stages.index("ASL_GENERATION") < stages.index("DATA_RETRIEVAL")


@pytest.mark.asyncio
async def test_explicit_yes_overrides_model_snapshot_and_queries_again():
    agent, retrieval, _, _ = _agent()
    await _first_trend(agent, "structured-yes")
    chat = ChatRequest(application_id="app", conversation_id="structured-yes",
        message_id="m2", question="查询2025年11月销售额", semantic_model_id=81)
    chat._planner_extraction = PlannerExtraction(structured={"是否查询数据库": "是"})
    chat._model_data_source = "SNAPSHOT"
    await agent.handle(chat, IDENTITY)
    assert len(retrieval.requests) == 2
    assert retrieval.requests[-1].execution_mode == "QUERY_DATABASE"


@pytest.mark.asyncio
async def test_no_prior_result_never_falls_back_to_sql():
    agent, retrieval, _, _ = _agent()
    chat = ChatRequest(application_id="app", conversation_id="no-source",
        message_id="m1", question="根据上一轮查询结果做个简要总结", semantic_model_id=81)
    chat._planner_extraction = PlannerExtraction(structured={"是否查询数据库": "否"})
    chat._model_data_source = "NEW_QUERY"
    result = await agent.handle(chat, IDENTITY)
    assert result.status != "COMPLETED"
    assert retrieval.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing_store", "missing_reference", "partial", "expired", "corrupt", "scope"])
async def test_unusable_result_fails_closed_for_explicit_no(kind):
    agent = setup()
    base = save(agent, truncated=kind == "partial")
    if kind == "expired":
        base = replace(base, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    elif kind == "scope":
        base = replace(base, semantic_model_id=82)
    if kind != "missing_reference":
        await remember(agent, base)
    if kind == "missing_store":
        agent.dataset_store = None
    elif kind == "corrupt":
        agent.dataset_store.client.objects.pop((base.bucket, base.object_name))
    current = request("根据已有结果做简要总结")
    current.query_resolution_type = "DIRECT_DATA_QUERY"
    current.assumptions.append("STRUCTURED_NO_DATABASE_QUERY")
    with pytest.raises(ExplicitDatasetUnavailableError, match="未重新查询数据库"):
        await agent._try_dataset_followup(current)
    assert current.execution_mode != "QUERY_DATABASE"


@pytest.mark.asyncio
async def test_reuse_returns_complete_rows_not_preview():
    agent = setup()
    base = save(agent, rows=ROWS * 10)
    await remember(agent, base)
    current = request("根据已有结果做简要总结")
    current.conversation_control = ConversationControl.NEW_REQUEST
    current.assumptions.append("STRUCTURED_NO_DATABASE_QUERY")
    result, dataset_id = await agent._try_dataset_followup(current)
    assert dataset_id == base.dataset_id
    assert result.dataset.rows == ROWS * 10
    assert not result.dataset.truncated


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["否", "是"])
async def test_compound_model_does_not_override_task_route(flag, monkeypatch):
    agent, _, _, _ = _agent()
    chat = ChatRequest(application_id="app", conversation_id="compound-route",
        message_id="m2", question="汇总并筛选已有结果", semantic_model_id=81)
    chat._model_data_source = "SNAPSHOT"
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="汇总已有结果", extraction={"是否查询数据库": flag}),
        AtomicTask(task_id="task-2", question="筛选已有结果", depends_on=["task-1"],
            extraction={"是否查询数据库": "否"}),
    ])
    execution = AsyncMock(return_value="per-task-routes")
    snapshot = AsyncMock(side_effect=AssertionError("compound route must not replace task plan"))
    monkeypatch.setattr(agent, "_handle_task_plan", execution)
    monkeypatch.setattr(agent, "_try_dataset_followup", snapshot)
    result = await agent._route_compound_followup(chat, IDENTITY, plan, chat.question)
    assert result == "per-task-routes"
    execution.assert_awaited_once()
    snapshot.assert_not_awaited()


def test_explicit_routes_are_not_repeat_result_cache_candidates():
    for flag in ("否", "是"):
        chat = ChatRequest(application_id="app", conversation_id="cache-choice",
            message_id="m", question="分析2025年销售额", semantic_model_id=81)
        chat._planner_extraction = PlannerExtraction(structured={"是否查询数据库": flag})
        assert not DataAnalysisOrchestrator._is_repeat_cache_candidate(chat)


def test_dependent_reuse_flag_is_not_inferred_from_dependency_alone():
    task = AtomicTask(task_id="task-2", question="按前序结果查询产品", depends_on=["task-1"],
        extraction={"是否查询数据库": "是"})
    assert not DataAnalysisOrchestrator._task_reuses_dependency_result(task)
    task.extraction["是否查询数据库"] = "否"
    assert DataAnalysisOrchestrator._task_reuses_dependency_result(task)


def test_same_question_with_distinct_route_flags_is_not_deduplicated():
    from app.planning.compound_question import deduplicate_plan
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="first", question="查询销售额", extraction={"是否查询数据库": "是"}),
        AtomicTask(task_id="second", question="查询销售额", extraction={"是否查询数据库": "否"}),
    ])
    result, aliases = deduplicate_plan(plan)
    assert len(result.tasks) == 2
    assert aliases == {}


def test_message_fingerprint_distinguishes_explicit_query_route():
    chat = ChatRequest(application_id="app", conversation_id="route-idempotency",
        message_id="same-message", question="查询销售额", semantic_model_id=81)
    chat._planner_extraction = PlannerExtraction(structured={"是否查询数据库": "是"})
    query_hash = DataAnalysisOrchestrator._request_fingerprint(chat, IDENTITY)
    chat._planner_extraction.structured["是否查询数据库"] = "否"
    assert query_hash != DataAnalysisOrchestrator._request_fingerprint(chat, IDENTITY)


@pytest.mark.asyncio
async def test_root_and_dependent_reuse_tasks_without_magic_text_do_not_query():
    from app.planning import CompoundQuestionRouter
    agent, retrieval, _, _ = _agent()
    agent.task_planner = CompoundQuestionRouter(agent.settings)
    conversation = "structured-root-and-dependent-reuse"
    await _first_trend(agent, conversation)
    chat = ChatRequest(application_id="app", conversation_id=conversation, message_id="m2",
        question="根据已有查询结果，先做简要分析，再展示前1条", semantic_model_id=81)
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="analysis", question="根据已有查询结果做简要分析",
            primary_intent=PrimaryIntent.TREND_ANALYSIS, extraction={"是否查询数据库": "否"}),
        AtomicTask(task_id="preview", question="根据前序查询结果，只看前1条", depends_on=["analysis"],
            primary_intent=PrimaryIntent.DETAIL_QUERY, extraction={"是否查询数据库": "否"}),
    ])
    response = await agent._handle_task_plan(chat, IDENTITY, plan)
    assert response.status == "COMPLETED"
    assert len(retrieval.requests) == 1


@pytest.mark.parametrize("prefix", ["根据前序查询结果，", "基于上一轮结果：", "根据上述任务的查询结果", "根据已有结果，"])
def test_result_reference_prefix_keeps_display_limit_distinct_from_ranking(prefix):
    from app.services.dataset_followup import plan_dataset_followup
    assert plan_dataset_followup(prefix + "只看前1条", ["金额"], [{"金额": 20}, {"金额": 10}]) == {
        "type": "limit", "count": 1,
    }
