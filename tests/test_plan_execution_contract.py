from uuid import uuid4

import pytest

from app.domain.models import (
    AgentResponse,
    AtomicTask,
    EvidenceItem,
    PrimaryIntent,
    ReliabilityReport,
    TaskPlan,
)
from app.planning.analysis_task_planner import (
    normalize_analysis_task_plan,
    validate_task_plan_execution_contract,
)
from app.planning.task_dag import TaskPlanningError
from app.services.orchestrator import DataAnalysisOrchestrator


QUESTION = "按季度统计2025年销售总金额并进行分析"


def extraction():
    return {
        "意图": "趋势分析",
        "实体": ["销售订单"],
        "指标": [{"name": "含税销售总额"}],
        "维度": ["业务日期"],
        "展示字段": [],
        "过滤条件": [],
        "排序": [{"field": "业务日期", "order": "asc"}],
        "时间粒度": {"unit": "季度", "time_range": "2025年", "anchor": None},
        "限制": None,
        "是否查询数据库": "是",
    }


def query_task(task_id="task-1", question=QUESTION):
    return AtomicTask(
        task_id=task_id,
        question=question,
        extraction=extraction(),
        primary_intent=PrimaryIntent.METRIC_QUERY,
    )


def local_task(operation="__missing__", task_id="task-2"):
    value = extraction()
    value["是否查询数据库"] = "否"
    if operation != "__missing__":
        value["结果操作"] = {"source_task_id": "task-1", "operation": operation}
    return AtomicTask(
        task_id=task_id,
        question="分析2025年各季度含税销售总额趋势，不再查询数据库",
        depends_on=["task-1"],
        extraction=value,
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
    )


@pytest.mark.parametrize("operation", [
    "__missing__",
    {"type": "sort", "field": "业务日期", "direction": "asc"},
    {"type": "derive", "operator": "subtract", "left_field": "含税销售总额",
     "right_field": "含税销售总额", "result_field": "错误差值"},
])
def test_preplanned_trend_shapes_use_the_same_analysis_playbook(operation):
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), local_task(operation)])
    normalized = normalize_analysis_task_plan(QUESTION, plan, PrimaryIntent.TREND_ANALYSIS)
    assert normalized.planner == "DETERMINISTIC_RULE"
    assert len(normalized.tasks) >= 2
    analysis = normalized.tasks[-1]
    assert analysis.extraction["分析绑定"]["analysis_type"] == "trend"
    assert analysis.extraction["分析绑定"]["tool"] == "趋势分析工具"
    assert "task-1" in analysis.depends_on
    validate_task_plan_execution_contract(normalized, PrimaryIntent.TREND_ANALYSIS)


@pytest.mark.parametrize(("intent", "question", "analysis_type"), [
    (PrimaryIntent.TREND_ANALYSIS, QUESTION, "trend"),
    (PrimaryIntent.COMPARISON_ANALYSIS, "对比2025年第三季度和第四季度的含税销售总额", "comparison"),
    (PrimaryIntent.COMPOSITION_ANALYSIS, "分析2025年各季度含税销售总额占比", "composition"),
    (PrimaryIntent.ANOMALY_ANALYSIS, "检测2025年各季度含税销售总额异常", "anomaly"),
    (PrimaryIntent.ROOT_CAUSE_ANALYSIS, "2025年第四季度含税销售总额较第三季度下降，是什么原因", "root_cause"),
    (PrimaryIntent.FORECAST_ANALYSIS, "基于2025年各季度含税销售总额预测未来1个季度", "forecast"),
])
def test_all_analysis_playbooks_cover_preplanned_multiple_tasks(intent, question, analysis_type):
    plan = TaskPlan(
        planner="STRUCTURED_MODEL",
        tasks=[query_task(), local_task({"type": "sort", "field": "业务日期"})],
    )
    normalized = normalize_analysis_task_plan(question, plan, intent)
    assert normalized.tasks[-1].extraction["分析绑定"]["analysis_type"] == analysis_type
    validate_task_plan_execution_contract(normalized, intent)


def test_plain_sort_is_not_upgraded_to_an_analysis():
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), local_task({"type": "sort"})])
    assert normalize_analysis_task_plan(QUESTION, plan, PrimaryIntent.METRIC_QUERY) is plan
    validate_task_plan_execution_contract(plan, PrimaryIntent.METRIC_QUERY)


def test_missing_core_contract_is_rejected_before_execution():
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), local_task()])
    with pytest.raises(TaskPlanningError, match="缺少可执行"):
        validate_task_plan_execution_contract(plan, PrimaryIntent.METRIC_QUERY)
    sort_only = TaskPlan(
        planner="STRUCTURED_MODEL",
        tasks=[query_task(), local_task({"type": "sort"})],
    )
    with pytest.raises(TaskPlanningError, match="未绑定对应分析链路"):
        validate_task_plan_execution_contract(sort_only, PrimaryIntent.TREND_ANALYSIS)


def test_mixed_plan_preserves_real_calculation_and_unrelated_branch():
    unrelated = query_task("task-x", "查询2025年订单笔数")
    terminal = local_task(task_id="task-3")
    terminal.extraction["计算绑定"] = {
        "operation": "divide",
        "left": {"source_task_id": "task-1", "column": "含税销售总额"},
        "right": {"source_task_id": "task-1", "column": "订单笔数"},
        "result_field": "平均每单销售金额",
        "format": "number",
    }
    terminal.extraction["计算绑定来源"] = "REGISTERED_COMPOUND"
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), unrelated, terminal])
    normalized = normalize_analysis_task_plan(
        "分析2025年各地区销售总金额占比", plan, PrimaryIntent.COMPOSITION_ANALYSIS,
    )
    assert [task.task_id for task in normalized.tasks] == ["task-1", "task-x", "task-3", "task-4"]
    assert normalized.tasks[-1].depends_on == ["task-3"]
    assert normalized.tasks[-1].extraction["分析绑定"]["analysis_type"] == "composition"
    assert normalized.tasks[2].extraction["计算绑定"]["result_field"] == "平均每单销售金额"


def test_compound_calculation_is_preserved_before_its_analysis():
    terminal = local_task()
    terminal.extraction["计算绑定"] = {
        "operation": "divide",
        "left": {"source_task_id": "task-1", "column": "含税销售总额"},
        "right": {"source_task_id": "task-1", "column": "订单笔数"},
        "result_field": "平均每单销售金额",
        "format": "number",
    }
    terminal.extraction["计算绑定来源"] = "DECLARED_CALCULATION"
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), terminal])
    normalized = normalize_analysis_task_plan(
        "分析2025年各地区平均每单销售金额占比", plan, PrimaryIntent.COMPOSITION_ANALYSIS,
    )
    assert len(normalized.tasks) == 3
    assert normalized.tasks[1].extraction["计算绑定"]["result_field"] == "平均每单销售金额"
    assert normalized.tasks[2].depends_on == ["task-2"]
    assert normalized.tasks[2].extraction["分析绑定"]["analysis_type"] == "composition"


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "2025年上海市，平均每单销售金额是多少？",
    "2025年上海市平均每单销售金额是多少？",
])
async def test_registered_compound_text_without_metric_array_still_binds_formula(monkeypatch, question):
    from unittest.mock import AsyncMock
    from app.domain import semantic_description
    from app.planning.compound_question import CompoundJudgment, CompoundQuestionRouter
    from test_task_planning_stages import setup_planner

    fixed = {"tasks": [
        {"question": "统计2025年上海市含税销售总额和订单笔数，用于计算平均每单销售金额",
         "metric_targets": ["含税销售总额", "订单笔数"]},
        {"question": "根据前序结果计算平均每单销售金额，不再查询数据库",
         "depends_on": [0], "metric_targets": [], "expected_output": "平均每单销售金额"},
    ]}

    def extract(boundaries):
        items = []
        for task in boundaries["tasks"]:
            local = "不再查询数据库" in task["question"]
            items.append({"task_index": task["task_index"], "extraction": {
                "意图": "指标计算" if local else "指标查询",
                "指标": [] if local else [{"name": name} for name in task.get("metric_targets") or []],
                "维度": [], "展示字段": [], "过滤条件": [],
                "是否查询数据库": "否" if local else "是",
                "输出要求": "计算平均每单销售金额" if local else None,
            }})
        return {"tasks": items}

    planner, _ = setup_planner(monkeypatch, fixed, extract=extract)
    spec = ("| 指标 | 同义词 | 业务口径 | 单位 | 依赖指标 |\n"
            "| 平均每单销售金额 | 订单均价 | 含税销售总额÷订单笔数 | 元/笔 | 含税销售总额,订单笔数 |\n"
            "| 含税销售总额 | 无 | 含税金额合计 | 元 | 无 |\n"
            "| 订单笔数 | 订单数 | 订单号去重数 | 笔 | 无 |")
    monkeypatch.setattr(semantic_description, "load_semantic_description", AsyncMock(return_value={
        "available": True, "source": "semantic_model_130.md", "content": spec}))
    router = CompoundQuestionRouter(planner.settings)
    router._single = planner
    outcome = await router.plan_with_judgment(
        CompoundJudgment(is_compound=False), question, semantic_model_id=130,
    )
    assert outcome.plan is not None and len(outcome.plan.tasks) >= 2
    calculation = outcome.plan.tasks[-1]
    binding = calculation.extraction["计算绑定"]
    assert binding["operation"] == "divide"
    assert binding["result_field"] == "平均每单销售金额"
    assert calculation.extraction["指标"] == [{"name": "平均每单销售金额"}]
    assert calculation.extraction["计算绑定来源"] == "REGISTERED_COMPOUND"
    assert calculation.primary_intent is not PrimaryIntent.COMPARISON_ANALYSIS
    assert all(task.primary_intent is not PrimaryIntent.COMPARISON_ANALYSIS
               for task in outcome.plan.tasks)
    assert all("计算绑定" not in (task.extraction or {}) for task in outcome.plan.tasks[:-1])


def response(evidence):
    return AgentResponse(
        request_id=uuid4(),
        conversation_id="test",
        status="COMPLETED",
        intent=PrimaryIntent.TREND_ANALYSIS,
        answer="完成",
        reliability=ReliabilityReport(level="HIGH", score=1, gates={}),
        evidence=evidence,
    )


def analysis_evidence(facts=None, analysis_type="trend"):
    return EvidenceItem(
        evidence_id="analysis",
        kind="ANALYSIS_RESULT",
        source_ref="deterministic:test",
        payload={"tool": "趋势分析工具", "analysis_type": analysis_type, "facts": facts or {}},
    )


def test_analysis_output_requires_matching_tool_type_and_facts():
    task = local_task()
    task.extraction["分析绑定"] = {"analysis_type": "trend", "tool": "趋势分析工具"}
    valid = response([analysis_evidence({"direction": "up"})])
    assert DataAnalysisOrchestrator._task_missing_output(task, valid) == []
    assert DataAnalysisOrchestrator._validate_task_output(task, valid).status == "COMPLETED"
    for invalid in (
        response([analysis_evidence()]),
        response([analysis_evidence({"direction": "up"}, analysis_type="comparison")]),
        response([]),
    ):
        result = DataAnalysisOrchestrator._validate_task_output(task, invalid)
        assert result.status == "SAFE_FALLBACK"
        assert result.reliability.level == "FAIL"


def test_compound_output_requires_actual_result_not_select_passthrough():
    task = local_task()
    task.extraction["计算绑定"] = {
        "operation": "divide", "result_field": "平均每单销售金额",
    }

    def calculation_evidence(operation, row):
        return EvidenceItem(
            evidence_id="calculation",
            kind="ANALYSIS_RESULT",
            source_ref="dataset",
            payload={"operation": operation, "preview": [row]},
        )

    valid = response([calculation_evidence(
        {"type": "derive", "operator": "divide", "result_field": "平均每单销售金额"},
        {"平均每单销售金额": 10766.6365},
    )])
    assert DataAnalysisOrchestrator._validate_task_output(task, valid).status == "COMPLETED"
    invalid = response([calculation_evidence(
        {"type": "select", "columns": ["含税销售总额", "订单笔数"]},
        {"含税销售总额": 100, "订单笔数": 2},
    )])
    assert DataAnalysisOrchestrator._validate_task_output(task, invalid).status == "SAFE_FALLBACK"
    wrong_operation = response([calculation_evidence(
        {"type": "derive", "operator": "subtract", "result_field": "平均每单销售金额"},
        {"平均每单销售金额": 50},
    )])
    assert DataAnalysisOrchestrator._validate_task_output(task, wrong_operation).status == "SAFE_FALLBACK"


def test_collapsed_query_only_plan_is_recompiled_from_root_goal():
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        query_task(), query_task("task-2", "统计2024年各季度含税销售总额"),
    ])
    normalized = normalize_analysis_task_plan(QUESTION, plan, PrimaryIntent.TREND_ANALYSIS)
    assert normalized.tasks[-1].extraction["分析绑定"]["analysis_type"] == "trend"
    validate_task_plan_execution_contract(normalized, PrimaryIntent.TREND_ANALYSIS)


def test_structural_share_shift_uses_two_period_datasets_and_one_analysis():
    question = "以2025年11月为基期、12月为本期，仅分析各渠道业态含税销售总额的份额变化"
    base = query_task(question="统计2025年11月各渠道业态的含税销售总额")
    base.extraction["维度"] = ["渠道业态"]
    current = query_task("task-2", "统计2025年12月各渠道业态的含税销售总额")
    current.extraction["维度"] = ["渠道业态"]
    terminal = local_task(task_id="task-3", operation={
        "type": "derive", "operator": "subtract", "left_field": "12月占比",
        "right_field": "11月占比", "result_field": "占比",
    })
    terminal.depends_on = ["task-1", "task-2"]
    normalized = normalize_analysis_task_plan(
        question, TaskPlan(planner="STRUCTURED_MODEL", tasks=[base, current, terminal]),
        PrimaryIntent.COMPOSITION_ANALYSIS,
    )
    assert len(normalized.tasks) == 3
    analysis = normalized.tasks[-1]
    assert analysis.depends_on == ["task-1", "task-2"]
    assert analysis.extraction["分析绑定"]["merge"] == "period_roles"
    assert [item["role"] for item in analysis.extraction["分析绑定"]["periods"]] == ["基期", "当前期"]


def test_model_invented_statistics_do_not_replace_anomaly_analysis():
    question = "检测2025年10月每日含税销售总额异常波动"
    terminal = local_task(operation={
        "type": "aggregate", "aggregations": [
            {"field": "含税销售总额", "function": "stddev", "alias": "样本标准差"},
        ],
    })
    normalized = normalize_analysis_task_plan(
        question, TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), terminal]),
        PrimaryIntent.ANOMALY_ANALYSIS,
    )
    assert normalized.tasks[-1].extraction["分析绑定"]["analysis_type"] == "anomaly"


def test_explicit_independent_aggregate_is_preserved_before_analysis():
    question = "计算2025年各季度含税销售总额的平均值并分析趋势"
    terminal = local_task(operation={
        "type": "aggregate", "aggregations": [
            {"field": "含税销售总额", "function": "avg", "alias": "平均值"},
        ],
    })
    normalized = normalize_analysis_task_plan(
        question, TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), terminal]),
        PrimaryIntent.TREND_ANALYSIS,
    )
    assert len(normalized.tasks) == 3
    assert normalized.tasks[-1].depends_on == ["task-2"]
    assert normalized.tasks[-1].extraction["分析绑定"]["analysis_type"] == "trend"
    assert normalize_analysis_task_plan(
        question, normalized, PrimaryIntent.TREND_ANALYSIS,
    ) is normalized


def test_duplicate_analysis_subgraph_is_recompiled_against_raw_dataset():
    question = "仅使用MAD方法检测2025年每日含税销售总额的异常波动"
    existing = local_task()
    existing.extraction["分析绑定"] = {
        "analysis_type": "anomaly", "intent": PrimaryIntent.ANOMALY_ANALYSIS.value,
        "tool": "异常检测工具", "merge": "concat", "periods": [],
    }
    duplicate = local_task({"type": "sort", "field": "业务日期"}, task_id="task-3")
    duplicate.depends_on = ["task-2"]
    normalized = normalize_analysis_task_plan(
        question,
        TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), existing, duplicate]),
        PrimaryIntent.ANOMALY_ANALYSIS,
    )
    assert len(normalized.tasks) == 2
    analysis = normalized.tasks[-1]
    assert analysis.depends_on == ["task-1"]
    assert analysis.extraction["分析绑定"]["requirements"]["tool_selection"] == {
        "mode": "SELECTED", "capabilities": ["MAD"],
    }
    validate_task_plan_execution_contract(normalized, PrimaryIntent.ANOMALY_ANALYSIS)


def test_dependent_retrieval_is_not_mistaken_for_raw_analysis_input():
    question = "对2025年11月与12月各渠道业态的含税销售总额进行对比分析，按增减额降序展示"
    base = query_task(question="查询2025年11月各渠道业态含税销售总额")
    current = query_task("task-2", "查询2025年12月各渠道业态含税销售总额")
    intermediate = query_task("task-3", "根据前序结果计算各渠道增减额")
    for task in (base, current, intermediate):
        task.extraction["维度"] = ["渠道业态"]
    intermediate.extraction["计算绑定"] = {
        "operation": "subtract", "result_field": "增减额",
    }
    intermediate.extraction["计算绑定来源"] = "DECLARED_CALCULATION"
    intermediate.depends_on = ["task-1", "task-2"]
    terminal = local_task(task_id="task-4")
    terminal.depends_on = ["task-3"]
    terminal.extraction["分析绑定"] = {
        "analysis_type": "comparison", "intent": PrimaryIntent.COMPARISON_ANALYSIS.value,
        "tool": "对比分析工具", "merge": "period_roles", "periods": [],
    }
    normalized = normalize_analysis_task_plan(
        question,
        TaskPlan(planner="STRUCTURED_MODEL", tasks=[base, current, intermediate, terminal]),
        PrimaryIntent.COMPARISON_ANALYSIS,
    )
    assert len(normalized.tasks) == 3
    analysis = normalized.tasks[-1]
    assert analysis.depends_on == ["task-1", "task-2"]
    assert analysis.extraction["分析绑定"]["requirements"][
        "comparison_dimensions"
    ] == ["渠道业态"]
    assert all("计算绑定" not in (task.extraction or {}) for task in normalized.tasks)


def test_selected_capability_recompiles_an_unscoped_existing_analysis():
    question = "仅使用MAD方法检测2025年每日含税销售总额的异常波动"
    existing = local_task()
    existing.extraction["分析绑定"] = {
        "analysis_type": "anomaly", "intent": PrimaryIntent.ANOMALY_ANALYSIS.value,
        "tool": "异常检测工具", "merge": "concat", "periods": [],
    }
    normalized = normalize_analysis_task_plan(
        question, TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), existing]),
        PrimaryIntent.ANOMALY_ANALYSIS,
    )
    assert normalized.tasks[-1].depends_on == ["task-1"]
    assert normalized.tasks[-1].extraction["分析绑定"]["requirements"][
        "tool_selection"
    ]["capabilities"] == ["MAD"]


def test_analysis_terminal_cannot_consume_another_analysis_result():
    first = local_task()
    first.extraction["分析绑定"] = {"analysis_type": "anomaly"}
    second = local_task(task_id="task-3")
    second.depends_on = ["task-2"]
    second.extraction["分析绑定"] = {"analysis_type": "anomaly"}
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[query_task(), first, second])
    with pytest.raises(TaskPlanningError, match="不得消费另一个分析任务"):
        validate_task_plan_execution_contract(plan, PrimaryIntent.ANOMALY_ANALYSIS)


def test_selected_capability_requires_matching_completed_facts():
    from app.analysis.tool_selection import selected_capability_errors
    selection = {"mode": "SELECTED", "capabilities": ["MAD"]}
    valid = {"capability_results": [
        {"method": "MEDIAN_RELATIVE_DEVIATION", "status": "COMPLETED"},
    ]}
    assert selected_capability_errors(PrimaryIntent.ANOMALY_ANALYSIS, selection, valid) == []
    invalid = {"capability_results": [{"method": "IQR", "status": "COMPLETED"}]}
    assert selected_capability_errors(PrimaryIntent.ANOMALY_ANALYSIS, selection, invalid)


def test_share_shift_requires_cross_period_facts_not_independent_shares():
    from app.analysis.tool_selection import selected_capability_errors
    selection = {"mode": "SELECTED", "capabilities": ["share_shift"]}
    valid = {"capability_results": [
        {"method": "structural_share_shift", "status": "COMPLETED"},
    ]}
    assert selected_capability_errors(PrimaryIntent.COMPOSITION_ANALYSIS, selection, valid) == []
    invalid = {"capability_results": [
        {"method": "part_divided_by_total", "status": "COMPLETED"},
    ]}
    assert selected_capability_errors(PrimaryIntent.COMPOSITION_ANALYSIS, selection, invalid)


def test_backtest_only_contract_rejects_future_forecast():
    from app.analysis.tool_selection import selected_capability_errors
    selection = {"mode": "SELECTED", "capabilities": ["forecast_backtest"]}
    facts = {"capability_results": [
        {"method": "expanding_window_one_step_backtest", "status": "COMPLETED"},
    ], "forecast": None}
    assert selected_capability_errors(PrimaryIntent.FORECAST_ANALYSIS, selection, facts) == []
    facts["forecast"] = 123
    assert selected_capability_errors(PrimaryIntent.FORECAST_ANALYSIS, selection, facts)


def test_selected_forecast_model_must_match_actual_model():
    from app.analysis.tool_selection import selected_capability_errors
    selection = {"mode": "SELECTED", "capabilities": ["model:recent_mean_3"]}
    facts = {"selected_model": "recent_mean_3", "capability_results": [
        {"method": "selected_model_refit", "status": "COMPLETED"},
    ]}
    assert selected_capability_errors(PrimaryIntent.FORECAST_ANALYSIS, selection, facts) == []
    facts["selected_model"] = "naive_last"
    assert selected_capability_errors(PrimaryIntent.FORECAST_ANALYSIS, selection, facts)
