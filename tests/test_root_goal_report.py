import json
from types import SimpleNamespace

import pytest

from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.domain.models import AtomicTask, TaskPlan, TaskExecutionResult
from app.presentation.root_report import collect_root_materials, render_root_report
from test_analysis_synthesis import settings, transport_for


def materials():
    return [
        {"task_id": "numerator", "question": "已合作医院数", "status": "COMPLETED",
         "presentation": {"table": "| 已合作医院数 |\n| --- |\n| 5 |"}},
        {"task_id": "denominator", "question": "区域医院总数", "status": "COMPLETED",
         "presentation": {"table": "| 区域医院总数 |\n| --- |\n| 10 |"}},
        {"task_id": "coverage", "question": "覆盖率", "status": "COMPLETED",
         "presentation": {"table": "| 覆盖率 |\n| --- |\n| 50% |", "chart": "<svg>existing-chart</svg>"}},
    ]


@pytest.mark.parametrize("ids", [["coverage"], ["numerator", "denominator"]])
def test_final_selects_user_deliverables_not_all_tasks_or_only_last_task(ids):
    answer, selected = render_root_report("用户明确需要的结果", materials(),
        {"overview": "整体简短回答", "result_task_ids": ids})
    assert selected == ids
    for item in materials():
        assert (item["presentation"]["table"] in answer) == (item["task_id"] in ids)
    assert answer.count("### 1、概况总结") == 1
    assert "任务1" not in answer


@pytest.mark.parametrize("final", [None, {}, {"result_task_ids": ["invented"]}, {"result_task_ids": ["coverage", "invented"]}])
def test_bad_or_missing_model_selection_preserves_results_without_gate(final):
    answer, selected = render_root_report("整体问题", materials(), final)
    assert selected == ["numerator", "denominator", "coverage"]
    assert "50%" in answer


def test_partial_results_and_preview_limits_survive_selection():
    items = materials()[:1] + [{"task_id": "failed", "question": "区域医院总数",
        "status": "FAILED", "summary": "服务暂不可用"}]
    items[0]["query_results"] = [{"truncated": True, "returned_row_count": 20, "row_count": 700}]
    answer, selected = render_root_report("覆盖率", items, {"result_task_ids": ["numerator"]})
    assert "服务暂不可用" in answer and "20条预览" in answer
    assert "不能作为全量统计" in answer and selected == ["numerator"]
    assert "0%" not in answer


def test_cached_and_empty_branches_are_not_omitted_from_model_material():
    plan = TaskPlan(planner="STRUCTURED_MODEL", analyze_summary="先查两项指标。", tasks=[
        AtomicTask(task_id="a", question="查询甲项", extraction={"指标": [{"name": "甲项"}]}),
        AtomicTask(task_id="b", question="查询乙项", depends_on=["a"]),
    ])
    results = [TaskExecutionResult(task_id="a", question="查询甲项", status="COMPLETED", answer="没有记录"),
               TaskExecutionResult(task_id="b", question="查询乙项", status="NEEDS_CLARIFICATION", answer="无法判断时间")]
    items = collect_root_materials(plan, results, {}, {})
    assert [item["status"] for item in items] == ["COMPLETED", "NEEDS_CLARIFICATION"]
    assert items[0]["structured_parameters"] == plan.tasks[0].extraction
    assert items[1]["depends_on"] == ["a"]
    answer, _ = render_root_report("整体问题", items, None)
    assert "没有记录" in answer and "无法判断时间" in answer


@pytest.mark.asyncio
async def test_one_call_returns_root_insight_and_separate_final_plan_with_full_context():
    calls = []
    context = {"original_question": "它们呢", "completed_question": "查询上海各经销商覆盖率",
               "planning": {"analyze_summary": "查询分子分母后计算覆盖率。", "tasks": [
                   {"task_id": "a", "depends_on": [], "extraction": {"指标": ["医院数"]}},
                   {"task_id": "b", "depends_on": ["a"], "expected_output": "覆盖率"}]}}
    output = {"claims": [{"statement": "围绕整体目标的分析依据。"}],
              "final_answer": {"overview": "简短回答", "findings": ["关键发现"],
                               "tips": ["预览限制"], "result_task_ids": ["coverage"]}}
    text, parsed = await QwenAnalysisSynthesizer(settings(), transport_for(output, calls)).synthesize_combined(
        context["completed_question"], materials(), planning_context=context)
    assert len(calls) == 1 and text == "围绕整体目标的分析依据。"
    assert parsed.final_answer == output["final_answer"]
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert payload["completed_question"] == context["completed_question"]
    assert payload["planning_context"] == context
    assert payload["untrusted_user_question"] == "它们呢"
    assert "先逐项交代" not in calls[0]["messages"][0]["content"]


def test_malformed_optional_final_plan_does_not_discard_insight():
    text = json.dumps({"claims": ["可用分析"], "final_answer": {"overview": 123, "tips": "not-list", "result_task_ids": [1, "a"]}})
    result = QwenAnalysisSynthesizer._parse_report(text, set())
    assert result.claims[0].statement == "可用分析"
    assert result.final_answer == {"result_task_ids": ["a"]}
