"""User-requested report wording changes, without hiding genuine failures."""
import pytest

from app.analysis.interpretation import AnswerPlan
from app.domain.models import AtomicTask, ChatRequest, TaskExecutionResult, TaskPlan
from app.presentation.root_report import collect_root_materials, render_root_report
from app.services.orchestrator import DataAnalysisOrchestrator


QUESTION = "2025年上海市，平均每单销售金额是多少？"
CALCULATION = "根据上述任务的查询结果计算平均每单销售金额（含税销售总额除以订单笔数），不再查询数据库。"
TABLE = "| 含税销售总额 | 订单笔数 |\n| --- | --- |\n| 120 | 3 |"


@pytest.mark.parametrize("question", [QUESTION, "分析销售趋势", "医院名单", ""])
def test_single_report_does_not_repeat_question_but_keeps_summary_and_results(question):
    report = AnswerPlan(headline="实际结果为40元。", key_facts=["有3笔订单。"],
                        limitations=["时间范围不完整。"]).render_report(
        question=question, table=TABLE, chart="<svg>verified</svg>")
    assert "本次分析：" not in report
    assert "实际结果为40元。" in report and "有3笔订单。" in report
    assert TABLE in report and "<svg>verified</svg>" in report
    assert "时间范围不完整。" in report


@pytest.mark.parametrize("legacy", [False, True])
def test_failed_calculation_keeps_one_short_status_not_repeated_task_instruction(legacy):
    chat = ChatRequest(semantic_model_id=81, application_id="app", conversation_id="test", message_id="m", question=QUESTION)
    failed = DataAnalysisOrchestrator._pure_computation_unavailable(chat)
    if legacy:
        failed = failed.model_copy(update={"answer":
            "本次尚未生成可复核的计算结果，计算、排序或筛选未完成。请核对前序数据是否完整、"
            "计算要求是否明确；本次不以模型文字替代计算结果。已完成的查询结果仍会保留。"})
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="source", question="统计金额和订单笔数"),
        AtomicTask(task_id="calc", question=CALCULATION, depends_on=["source"])])
    results = [TaskExecutionResult(task_id="source", question=plan.tasks[0].question,
                                   status="COMPLETED", answer=TABLE),
               TaskExecutionResult(task_id="calc", question=CALCULATION,
                                   status=failed.status, answer=failed.answer, reliability=failed.reliability)]
    materials = collect_root_materials(plan, results, {"calc": failed}, {})
    report, selected = render_root_report(QUESTION, materials, None)
    assert selected == ["source"] and TABLE in report
    assert report.count("计算未完成，暂未获得可用的计算结果。") == 1
    assert CALCULATION not in report and "本次不以模型文字替代计算结果" not in report
    assert "本次尚未生成可复核" not in report and "尚未完成的内容" not in report
    assert results[1].status == "FAILED" and failed.reliability.level == "FAIL"
    answer, selected = render_root_report(QUESTION, materials[1:], None)
    assert answer == "计算未完成，暂未获得可用的计算结果。" and not selected
    assert "概况总结" not in answer


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIAL_SUCCESS"])
def test_successful_calculation_has_no_unfinished_notice(status):
    item = {"task_id": "calc", "status": status, "question": CALCULATION,
            "summary": "平均金额为40元。", "facts": {"computation": {"row_count": 1}},
            "presentation": {"table": "| 平均金额 |\n| --- |\n| 40 |"}}
    report, selected = render_root_report(QUESTION, [item], None)
    assert selected == ["calc"] and "平均金额为40元。" in report
    assert "未完成" not in report and "本次分析：" not in report


def test_specific_failure_reasons_and_independent_tasks_are_not_hidden():
    items = [{"task_id": "source", "status": "COMPLETED", "summary": TABLE},
             {"task_id": "calc", "status": "FAILED", "question": CALCULATION,
              "summary": "分母为零，无法计算平均金额。", "calculation_incomplete": True},
             {"task_id": "other", "status": "NEEDS_CLARIFICATION", "question": "查另一指标",
              "missing_information": ["请补充另一指标的口径。"]}]
    report, selected = render_root_report(QUESTION, items, None)
    assert TABLE in report and selected == ["source"]
    assert "分母为零，无法计算平均金额。" in report
    assert "请补充另一指标的口径。" in report
    assert CALCULATION not in report
