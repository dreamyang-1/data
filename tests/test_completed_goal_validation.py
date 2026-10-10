"""Completed root goal is authoritative; task checks are supporting evidence."""
import copy
import hashlib
import json
from uuid import uuid4
from types import SimpleNamespace

import pytest

from app.adapters import build_mock_adapters
from app.analysis.synthesis import QwenAnalysisSynthesizer, SynthesisOutput
from app.config import Settings
from app.domain.models import (
    AgentResponse, AtomicTask, ChatRequest, EvidenceItem, ReliabilityReport,
    TaskPlan, TrustedIdentity,
)
from app.presentation.reliability import root_quality_status, render_reliability_validation
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import progress_scope
from app.stores import InMemorySessionStore
from test_analysis_synthesis import settings, transport_for


@pytest.mark.parametrize("context", [None, {
    "original_question": "换成去年呢", "completed_question": "旧的根问题",
    "planning": {"tasks": [{"question": "只统计中间销售额"}]},
}])
@pytest.mark.asyncio
async def test_synthesis_completed_goal_overrides_stale_planning_without_mutation(context):
    before = copy.deepcopy(context)
    calls = []
    question = "2025年华山医院销售额最多的销售员是谁，销售了哪些产品？"
    await QwenAnalysisSynthesizer(settings(), transport_for({"claims": ["已有数据支持的分析。"]}, calls)).synthesize_combined(
        question, [{"task_id": "a", "question": "查询中间销售额", "status": "COMPLETED"}],
        planning_context=context,
    )
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert payload["completed_question"] == payload["planning_context"]["completed_question"] == question
    assert payload["tasks"][0]["question"] != question
    assert context == before
    if context:
        assert payload["untrusted_user_question"] == "换成去年呢"
        assert payload["planning_context"]["planning"] == context["planning"]


@pytest.mark.parametrize("statuses,expected", [
    ([], ""), (["PASS"], "PASS"), (["PASS", ""], ""),
    (["PASS", "UNKNOWN"], ""), (["PASS", "WARNING"], "WARNING"),
    (["PASS", "FAILED"], "FAIL"), (["WARN", "ERROR"], "FAIL"),
])
def test_root_quality_uses_data_evidence_not_task_status(statuses, expected):
    evidence = [EvidenceItem(evidence_id=str(i), kind="QUERY_RESULT", source_ref="mock",
                            payload={"quality_status": status}) for i, status in enumerate(statuses)]
    before = copy.deepcopy(evidence)
    assert root_quality_status(evidence) == expected
    assert evidence == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["complete", "partial", "empty", "failure", "clarification", "checkpoint", "resume"])
async def test_root_validation_and_insight_share_completed_goal_for_all_result_states(case):
    events, insights, calls = [], [], []
    config = Settings(_env_file=None, env="test", adapter_mode="mock", multi_question_model_enabled=False)
    sessions = InMemorySessionStore()
    goal = "2025年上海市各经销商的已合作医院数及区域医院覆盖率"
    tasks = [AtomicTask(task_id="a", question="统计各经销商已合作医院数"),
             AtomicTask(task_id="b", question="计算各经销商区域医院覆盖率")]
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=tasks)

    def result(chat, status="COMPLETED"):
        return AgentResponse(
            request_id=uuid4(), conversation_id=chat.conversation_id, status=status,
            intent="METRIC_QUERY", answer="查询返回0行" if case == "empty" else "实际结果",
            evidence=[EvidenceItem(evidence_id="query", kind="QUERY_RESULT", source_ref="mock",
                payload={"columns": ["医院数"], "row_count": 0 if case == "empty" else 1,
                         "quality_status": "PASS"})] if status != "NEEDS_CLARIFICATION" else [],
            reliability=ReliabilityReport(level="HIGH" if status == "COMPLETED" else "LIMITED",
                score=1 if status == "COMPLETED" else .5, gates={"query_succeeded": status == "COMPLETED"},
                warnings=["仅提供部分结果，不能作为全量统计。"] if status == "PARTIAL_SUCCESS" else []),
            clarification_questions=["请补充指标口径"] if status == "NEEDS_CLARIFICATION" else [],
        )

    class Service(DataAnalysisOrchestrator):
        async def _handle(self, chat, identity):
            calls.append(chat.question)
            if case == "failure":
                raise RuntimeError("mock query unavailable")
            status = ("NEEDS_CLARIFICATION" if case == "clarification" else
                      "PARTIAL_SUCCESS" if case == "partial" and "覆盖率" in chat.question else "COMPLETED")
            return result(chat, status)

    class Synthesizer:
        async def synthesize_combined(self, question, materials, *, planning_context, **kwargs):
            insights.append((question, copy.deepcopy(planning_context), copy.deepcopy(materials)))
            return "围绕完整问题解释已有数据。", SynthesisOutput(claims=[])

    # Root result aggregation consumes an already validated plan; planner
    # implementation/version is deliberately not part of this unit contract.
    planner = SimpleNamespace(deduplicate=lambda value: (value, {}),
                              execution_layers=lambda value: [value.tasks])
    service = Service(settings=config, classifier=SimpleNamespace(), adapters=build_mock_adapters(),
        sessions=sessions, task_planner=planner, analysis_synthesizer=Synthesizer())
    chat = ChatRequest(semantic_model_id=81, application_id="app", conversation_id="root-"+case,
                       message_id="m1", question="换2025年的" if case == "resume" else goal)
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    chat._analysis_root_context = {"original_question": chat.question, "completed_question": goal}
    pending = None
    if case == "resume":
        pending = {"analysis_root_context": {"original_question": "上海市各经销商覆盖情况",
                   "completed_question": "上海市各经销商的已合作医院数及区域医院覆盖率",
                   "resolved_task_questions": {"a": "2025年上海市各经销商的已合作医院数"}}, "state_version": 0}
        goal = pending["analysis_root_context"]["completed_question"] + "\n补充确认后的任务范围：" + pending["analysis_root_context"]["resolved_task_questions"]["a"]
    if case == "checkpoint":
        await sessions.put_dag_checkpoint("t", "u", "app", chat.conversation_id, "m1", {
            "authorized_scope": chat.authorized_semantic_scope.fingerprint(),
            "plan_fingerprint": hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
            "completed": {"a": result(chat).model_dump(mode="json")},
            "conversations": {"a": "cached-child"},
            "analysis_root_context": copy.deepcopy(chat._analysis_root_context),
        })
    with progress_scope(events.append):
        response = await service._handle_task_plan(chat, identity, plan, dag_pending=pending)
    checks = [event for event in events if event["stage"] == "RELIABILITY_CHECK"]
    assert len(checks) == 1 and not checks[0].get("is_child_task")
    assert f"补全后的问题“{goal}”" in checks[0]["message"]
    assert "补全后的问题“统计各经销商已合作医院数”" not in checks[0]["message"]
    if case in {"failure", "clarification"}:
        assert checks[0]["status"] == "FAILED"
        assert not insights and response.reliability.level == "FAIL"
    else:
        assert len(insights) == 1
        question, context, materials = insights[0]
        assert question == context["completed_question"] == goal
        assert context["result_validation"] == response.reliability.model_dump(mode="json")
        assert [item["question"] for item in materials] == [task.question for task in tasks]
        insight_start = next(event for event in events if event["stage"] == "INSIGHT_ANALYSIS")
        assert events.index(checks[0]) < events.index(insight_start)
    if case == "partial":
        assert response.status == "PARTIAL_SUCCESS" and response.reliability.level == "LIMITED"
        assert "仅提供部分结果" in checks[0]["message"]
    if case == "checkpoint":
        assert calls == [tasks[1].question]
    if case == "empty":
        assert all(item["query_results"][0]["row_count"] == 0 for item in insights[0][2])


def test_single_goal_validation_preserves_failures_and_data_limits():
    report = ReliabilityReport(level="LIMITED", score=.5, gates={"complete": False},
                               warnings=["当前数据不完整，不能推断全年。"])
    text = render_reliability_validation(report, [], "WARNING", completed_question="2025年全年销量")
    assert "2025年全年销量" in text and "有限可信" in text and "不能推断全年" in text
    assert report.gates == {"complete": False}
