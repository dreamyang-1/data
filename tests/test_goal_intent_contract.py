from types import SimpleNamespace

import pytest

from app.domain.models import AtomicTask, PrimaryIntent, TaskPlan
from app.planning import analysis_task_planner
from app.planning.compound_question import (
    CompoundJudgment,
    CompoundJudgmentEntry,
    CompoundTaskPlanner,
)
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, TaskPlanningError
from app.services.semantic_decision import completed_turn_planning_intent


def chat_with(judgment=None, decision=None):
    return SimpleNamespace(
        _compound_judgment=judgment,
        _semantic_decision=decision,
        _completed_question_execution=True,
    )


def test_single_goal_intent_comes_from_question_completion():
    chat = chat_with(
        judgment=CompoundJudgment(is_compound=False, intent="趋势分析"),
        decision=SimpleNamespace(tasks=[SimpleNamespace(intent=PrimaryIntent.METRIC_QUERY)]),
    )
    assert completed_turn_planning_intent(chat) is PrimaryIntent.TREND_ANALYSIS


def test_compound_goal_has_no_artificial_single_root_intent():
    judgment = CompoundJudgment(
        is_compound=True,
        entries=(
            CompoundJudgmentEntry(question="分析销售趋势", intent="趋势分析"),
            CompoundJudgmentEntry(question="检测订单异常", intent="异常分析"),
        ),
    )
    assert completed_turn_planning_intent(chat_with(judgment=judgment)) is None


def test_completed_turn_without_intent_contract_is_rejected():
    with pytest.raises(TaskPlanningError, match="用户意图合同"):
        completed_turn_planning_intent(chat_with(), required=True)


@pytest.mark.asyncio
async def test_compound_planner_does_not_overwrite_branch_intents():
    class SinglePlanner:
        def __init__(self):
            self.received = []

        async def plan_question(self, question, **kwargs):
            self.received.append((question, kwargs["root_intent"]))
            return QuestionDecomposition(
                intent=kwargs["root_intent"],
                tasks=(DecomposedTask(question=question, intent=kwargs["root_intent"]),),
            )

    single = SinglePlanner()
    planner = CompoundTaskPlanner(SimpleNamespace(), single)
    judgment = CompoundJudgment(
        is_compound=True,
        entries=(
            CompoundJudgmentEntry(question="统计销售额", intent="指标查询"),
            CompoundJudgmentEntry(question="查询订单明细", intent="明细查询"),
        ),
    )
    plan = await planner.plan(
        judgment, source_question="统计销售额并查询订单明细",
        root_intent=PrimaryIntent.ANOMALY_ANALYSIS,
    )
    assert single.received == [
        ("统计销售额", PrimaryIntent.METRIC_QUERY),
        ("查询订单明细", PrimaryIntent.DETAIL_QUERY),
    ]
    assert [task.primary_intent for task in plan.tasks] == [
        PrimaryIntent.METRIC_QUERY, PrimaryIntent.DETAIL_QUERY,
    ]


@pytest.mark.asyncio
async def test_compound_analysis_branch_is_normalized_independently():
    trend_question = "分析2025年各季度含税销售总额趋势"
    structured = {
        "意图": "趋势分析", "指标": [{"name": "含税销售总额"}],
        "维度": ["业务日期"], "展示字段": [], "过滤条件": [],
        "排序": [{"field": "业务日期", "order": "asc"}],
        "时间粒度": {"unit": "季度", "time_range": "2025年", "anchor": None},
        "限制": None, "是否查询数据库": "是",
    }

    class SinglePlanner:
        async def plan_question(self, question, **kwargs):
            intent = kwargs["root_intent"]
            if question != trend_question:
                return QuestionDecomposition(
                    intent=intent,
                    tasks=(DecomposedTask(question=question, intent=intent),),
                )
            local = dict(structured, 是否查询数据库="否", 结果操作={
                "source_task_index": 0,
                "operation": {"type": "sort", "field": "业务日期"},
            })
            return QuestionDecomposition(
                intent=intent,
                tasks=(
                    DecomposedTask(question="统计2025年各季度含税销售总额", structured=structured),
                    DecomposedTask(
                        question="分析趋势，不再查询数据库", depends_on=(0,),
                        intent=intent, structured=local,
                    ),
                ),
            )

    planner = CompoundTaskPlanner(SimpleNamespace(), SinglePlanner())
    judgment = CompoundJudgment(
        is_compound=True,
        entries=(
            CompoundJudgmentEntry(question=trend_question, intent="趋势分析"),
            CompoundJudgmentEntry(question="统计2025年订单笔数", intent="指标查询"),
        ),
    )
    plan = await planner.plan(
        judgment, source_question="分析含税销售总额趋势并统计订单笔数",
    )
    assert len(plan.tasks) >= 3
    analysis = next(task for task in plan.tasks if (task.extraction or {}).get("分析绑定"))
    assert analysis.primary_intent is PrimaryIntent.TREND_ANALYSIS
    assert analysis.extraction["分析绑定"]["analysis_type"] == "trend"
    assert plan.tasks[-1].primary_intent is PrimaryIntent.METRIC_QUERY


@pytest.mark.asyncio
async def test_standardized_compound_branch_still_uses_canonical_normalization(monkeypatch):
    question = "对2025年11月与12月各渠道业态的含税销售总额进行对比分析"
    extraction = {
        "指标": [{"name": "含税销售总额"}], "维度": ["渠道业态"],
        "时间粒度": {"unit": "月", "time_range": "2025年11月", "anchor": None},
        "过滤条件": [], "排序": [], "是否查询数据库": "是",
    }
    query = AtomicTask(task_id="task-1", question="查询11月各渠道业态含税销售总额", extraction=extraction)
    intermediate = AtomicTask(
        task_id="task-2", question="根据前序结果计算增减额", depends_on=["task-1"],
        extraction={**extraction, "计算绑定": {"operation": "subtract", "result_field": "增减额"}},
    )
    terminal = AtomicTask(
        task_id="task-3", question="执行对比分析，不再查询数据库", depends_on=["task-2"],
        extraction={**extraction, "是否查询数据库": "否", "分析绑定": {
            "analysis_type": "comparison", "intent": "COMPARISON_ANALYSIS",
        }},
    )
    standardized = TaskPlan(
        planner="STRUCTURED_MODEL", tasks=[query, intermediate, terminal],
    )

    class SinglePlanner:
        async def plan_question(self, text, **kwargs):
            return QuestionDecomposition(
                intent=kwargs["root_intent"],
                tasks=(DecomposedTask(question=text, intent=kwargs["root_intent"]),),
            )

    monkeypatch.setattr(
        analysis_task_planner, "standardize_period_analysis_plan",
        lambda text, decomposition: standardized if text == question else None,
    )
    planner = CompoundTaskPlanner(SimpleNamespace(), SinglePlanner())
    judgment = CompoundJudgment(is_compound=True, entries=(
        CompoundJudgmentEntry(question=question, intent="对比分析"),
        CompoundJudgmentEntry(question="统计2025年订单笔数", intent="指标查询"),
    ))
    plan = await planner.plan(judgment, source_question=question + "并统计订单笔数")
    analysis = next(task for task in plan.tasks if (task.extraction or {}).get("分析绑定"))
    assert analysis.depends_on == ["task-1", "task-2"]
    assert analysis.extraction["分析绑定"]["requirements"][
        "comparison_dimensions"
    ] == ["渠道业态"]
    assert len(plan.tasks) == 4
