"""Required predecessor outputs are checked before parsing/cached child reuse."""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.adapters import build_mock_adapters
from app.analysis.synthesis import SynthesisClaim, SynthesisOutput
from app.config import Settings
from app.domain.models import AgentResponse, AtomicTask, ChatRequest, EvidenceItem, PrimaryIntent, TaskPlan, TrustedIdentity
from app.planning import MultiQuestionPlanner
from app.planning.task_dependencies import TaskDependencySkipped, confirmed_empty_result, dependency_skip
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import progress_scope
from app.stores import InMemorySessionStore


def result(payload=None, *, status="COMPLETED", kind="QUERY_RESULT", conversation="child"):
    return AgentResponse(request_id=uuid4(), conversation_id=conversation, status=status,
        intent=PrimaryIntent.METRIC_QUERY, answer="本次查询执行成功，返回0条结果。" if payload and payload.get("row_count") == 0 else "查询已完成。",
        clarification_questions=["请补充前置任务的时间范围。"] if status == "NEEDS_CLARIFICATION" else [],
        evidence=[] if payload is None else [EvidenceItem(evidence_id="query", kind=kind, source_ref="test", payload=payload)])


@pytest.mark.parametrize("payload,empty", [
    ({"row_count": 0}, True),
    ({"row_count": 0, "total_row_count": 0, "returned_row_count": 0, "total_row_count_confirmed": True, "truncated": False}, True),
    ({"row_count": 1, "value": 0}, False),
    ({"row_count": 0, "total_row_count": 20}, False),
    ({"row_count": 20, "returned_row_count": 0}, False),
    ({"row_count": 0, "truncated": True}, False),
    ({"row_count": 0, "total_row_count_confirmed": False}, False),
    ({"returned_row_count": 0}, False),
    ({"row_count": False}, False),
    (None, False),
])
def test_confirmed_empty_not_preview_or_zero_metric(payload, empty):
    assert confirmed_empty_result(result(payload)) is empty


def test_derived_empty_and_mixed_query_proofs():
    assert confirmed_empty_result(result({"row_count": 0}, kind="ANALYSIS_RESULT"))
    value = result({"row_count": 0})
    value.evidence.append(EvidenceItem(evidence_id="positive", kind="QUERY_RESULT", source_ref="test", payload={"row_count": 1}))
    assert not confirmed_empty_result(value)
    value = result({"row_count": 1})
    value.evidence.append(EvidenceItem(evidence_id="derived", kind="ANALYSIS_RESULT", source_ref="test", payload={"row_count": 0}))
    assert not confirmed_empty_result(value)


@pytest.mark.parametrize("value", [None, RuntimeError("failed"), TaskDependencySkipped("skipped"),
    result(status="NEEDS_CLARIFICATION"), result(status="SAFE_FALLBACK"), result(status="FAILED"), result({"row_count": 0})])
def test_required_dependency_unavailable(value):
    skipped = dependency_skip(["source"], {"source": value}, {"source": "任务1"})
    assert isinstance(skipped, TaskDependencySkipped)
    assert "任务1" in str(skipped) and "本任务未执行" in str(skipped)
    assert "无需为本任务单独补充条件" in str(skipped)
    assert dependency_skip([], {"source": value}, {}) is None


class Classifier:
    def classify(self, *args, **kwargs):
        raise AssertionError("the stub overrides child handling")


def service_fixture(source_result, plan, *, synthesize=False):
    settings = Settings(_env_file=None, env="test", multi_question_model_enabled=False)
    synthesis = SimpleNamespace(synthesize_combined=AsyncMock(return_value=("整体分析", SynthesisOutput(claims=[SynthesisClaim(statement="前置查询未返回匹配数据。")], final_answer={"overview": "前置查询未返回匹配数据。"}))))
    service = DataAnalysisOrchestrator(settings=settings, classifier=Classifier(),
        adapters=build_mock_adapters(), sessions=InMemorySessionStore(7200, 7200),
        task_planner=MultiQuestionPlanner(settings), analysis_synthesizer=synthesis if synthesize else None)
    calls = []

    async def handle_child(chat, identity):
        calls.append(chat.question)
        if chat.question == plan.tasks[0].question:
            if isinstance(source_result, Exception):
                raise source_result
            return source_result.model_copy(update={"conversation_id": chat.conversation_id})
        return result({"row_count": 1}, conversation=chat.conversation_id)

    service._handle = handle_child
    chat = ChatRequest(semantic_model_id=81, application_id="app", conversation_id="dependency-empty", message_id="m1",
        question="2025年9月针对上海交通大学医学院附属新华医院销售订单金额最多的销售员是谁？销售了哪些产品？")
    return service, calls, chat, TrustedIdentity(tenant_id="t", user_id="u"), synthesis


@pytest.mark.asyncio
@pytest.mark.parametrize("source_question,child_question", [
    ("2025年9月上海交通大学医学院附属新华医院各业务员的含税销售总额是多少？", "2025年9月由上述任务返回的销售员负责销售的产品明细有哪些？"),
    ("查询销售额最高的经销商", "查询上述经销商的合作医院名单"),
    ("查询销量最高的商品", "查询上述商品的适用科室"),
    ("查询合作时长大于3个月的经销商", "根据结果筛选名单，不再查询数据库"),
])
async def test_original_and_three_similar_empty_predecessor_cases(source_question, child_question):
    plan = TaskPlan(planner="DETERMINISTIC_RULE", final_deliverable="COMBINED_REPORT", tasks=[
        AtomicTask(task_id="source", question=source_question),
        AtomicTask(task_id="child", question=child_question, depends_on=["source"]),
    ])
    service, calls, chat, identity, synthesis = service_fixture(result({"row_count": 0, "total_row_count_confirmed": True}), plan, synthesize=True)
    events = []
    with progress_scope(events.append):
        response = await service._handle_task_plan(chat, identity, plan)
    assert calls == [source_question]
    assert [task.status for task in response.task_results] == ["COMPLETED", "SKIPPED"]
    assert response.status == "PARTIAL_SUCCESS"
    assert response.clarification_questions == [] and response.awaiting_task_ids == [] and response.dag_resume_token is None
    assert "返回0条结果" in response.task_results[1].answer
    assert "无需为本任务单独补充条件" in response.answer
    assert "缺少要返回的业务对象" not in response.answer
    assert "未执行的后续任务" in response.answer
    child_events = [event for event in events if event.get("task_id") == "child"]
    assert len(child_events) == 1 and child_events[0]["stage"] == "DATA_RETRIEVAL" and child_events[0]["status"] == "SKIPPED"
    material = synthesis.synthesize_combined.call_args.args[1][1]
    assert material["status"] == "SKIPPED" and material["summary"] == response.task_results[1].answer
    manifest = next(item.payload for item in response.evidence if item.kind == "REPORT_DATASET_MANIFEST")
    assert manifest["sections"][1]["status"] == "SKIPPED"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [result({"row_count": 0}), result({"row_count": 0}, kind="ANALYSIS_RESULT"),
    result(status="SAFE_FALLBACK"), result(status="NEEDS_CLARIFICATION"), RuntimeError("database unavailable")])
async def test_multilevel_multi_input_skip_preserves_independent_sibling(source):
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="source", question="查询前置数据"),
        AtomicTask(task_id="independent", question="查询独立数据"),
        AtomicTask(task_id="child", question="查询下游数据", depends_on=["source", "independent"]),
        AtomicTask(task_id="grandchild", question="查询再下游数据", depends_on=["child"]),
    ])
    service, calls, chat, identity, _ = service_fixture(source, plan)
    response = await service._handle_task_plan(chat, identity, plan)
    assert set(calls) == {"查询前置数据", "查询独立数据"}
    assert [task.status for task in response.task_results][1:] == ["COMPLETED", "SKIPPED", "SKIPPED"]
    assert "因前置结果不可用已跳过" in response.task_results[3].answer
    assert response.awaiting_task_ids == (["source"] if isinstance(source, AgentResponse) and source.status == "NEEDS_CLARIFICATION" else [])


@pytest.mark.asyncio
async def test_single_row_zero_metric_does_not_skip_child():
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="source", question="查询数值"),
        AtomicTask(task_id="child", question="说明数值结果", depends_on=["source"]),
    ])
    service, calls, chat, identity, _ = service_fixture(result({"row_count": 1, "value": 0}), plan)
    response = await service._handle_task_plan(chat, identity, plan)
    assert calls == ["查询数值", "说明数值结果"]
    assert all(task.status == "COMPLETED" for task in response.task_results)


@pytest.mark.asyncio
async def test_revalidate_parent_before_restoring_cached_child_and_retry_after_recovery():
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="source", question="查询前置数据"),
        AtomicTask(task_id="child", question="说明数值结果", depends_on=["source"]),
    ])
    service, calls, chat, identity, _ = service_fixture(result({"row_count": 1}), plan)
    checkpoints = []
    original_put = service.sessions.put_dag_checkpoint

    async def capture_checkpoint(*args, **kwargs):
        checkpoints.append(args[-1])
        return await original_put(*args, **kwargs)

    service.sessions.put_dag_checkpoint = capture_checkpoint
    await service.sessions.put_dag_checkpoint("t", "u", "app", chat.conversation_id, chat.message_id, {
        "schema_version": "1.0", "plan_fingerprint": hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
        "authorized_scope": chat.authorized_semantic_scope.fingerprint(),
        "completed": {"source": result({"row_count": 0}).model_dump(mode="json"), "child": result({"row_count": 1}).model_dump(mode="json")},
        "conversations": {"source": "old-source", "child": "old-child"},
    })
    response = await service._handle_task_plan(chat, identity, plan)
    assert calls == [] and response.task_results[1].status == "SKIPPED"
    # Terminal roots normally clear their checkpoint; inspect the saved state
    # before that cleanup, as would be restored after interruption/restart.
    saved = checkpoints[-1]
    assert "child" not in saved["completed"]
    saved["completed"]["source"] = result({"row_count": 1}).model_dump(mode="json")
    await service.sessions.put_dag_checkpoint("t", "u", "app", chat.conversation_id, chat.message_id, saved)
    recovered = await service._handle_task_plan(chat, identity, plan)
    assert calls == ["说明数值结果"] and recovered.task_results[1].status == "COMPLETED"


def test_empty_dependency_terminal_keeps_public_stage_order_without_fake_asl_sql():
    from app.api import _CompositeChildProgressOrderer, _thinking_section

    orderer = _CompositeChildProgressOrderer()
    output = []
    for stage, index, terminal in [
        ("ASL_GENERATION", 0, False), ("SQL_EXECUTION", 0, False),
        ("DATA_RETRIEVAL", 0, True), ("RELIABILITY_CHECK", 0, False),
        ("DATA_RETRIEVAL", 1, True), ("INSIGHT_ANALYSIS", None, False),
    ]:
        event = dict(stage=stage, status="SKIPPED" if index == 1 else "COMPLETED", message=stage)
        if index is not None:
            event.update(task_id=f"task-{index}", task_index=index, task_count=2,
                         is_child_task=True, task_terminal=terminal)
        output += orderer.push(event)
    output += orderer.flush()
    sections = [_thinking_section(event["stage"]) for event in output]
    assert sections == sorted(sections, key={"parsing": 0, "execution": 1, "validation": 2, "insight": 3}.get)
    assert not any(event.get("task_index") == 1 and event["stage"] in {"ASL_GENERATION", "SQL_EXECUTION"} for event in output)
