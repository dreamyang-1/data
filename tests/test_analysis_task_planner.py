"""分析任务规划器与展示映射的专项测试。

覆盖：分析意图识别后的任务展开（取数+分析独立节点）、两期对比的期间
拆分、归因的维度取数展开、复用型单任务不扩展，以及 ANALYSIS_TOOL 事件
在调度执行节点的映射（六节点顺序冻结回归）。
"""
from __future__ import annotations

import pytest

from app.domain.models import (
    CanonicalAnalysisRequest,
    MetricRef,
    PlannerExtraction,
    PrimaryIntent,
)
from app.planning.analysis_task_planner import (
    _comparison_periods,
    build_analysis_task_plan,
)
from app.api import _new_agent_think_step, _thinking_section


def _structured(intent: str, **overrides):
    base = {
        "意图": intent,
        "业务域": ["医药销售域"],
        "实体": ["销售订单"],
        "指标": [{"name": "含税销售总额"}],
        "维度": [],
        "展示字段": [],
        "过滤条件": [],
        "时间粒度": {"unit": "月", "time_range": "2025年10月至2025年11月", "anchor": None},
        "排序": [],
        "限制": None,
        "输出要求": "默认输出表格",
        "是否去重": None,
        "是否查询数据库": "是",
    }
    base.update(overrides)
    return base


def _extraction(intent: PrimaryIntent, structured: dict) -> PlannerExtraction:
    return PlannerExtraction(intent=intent, parameters=[], structured=structured)


class TestComparisonPeriodSplit:
    def test_two_months_split_into_retrieval_plus_analysis(self):
        question = "2025年11月和10月的含税销售总额对比"
        plan = build_analysis_task_plan(
            question,
            _extraction(PrimaryIntent.COMPARISON_ANALYSIS, _structured("对比分析")),
        )
        assert plan is not None
        assert len(plan.tasks) == 3
        retrieval, retrieval2, analysis = plan.tasks
        # 基期在前、当前期在后，两个取数任务都是独立查询节点
        assert retrieval.question == "查询2025年10月的含税销售总额"
        assert retrieval2.question == "查询2025年11月的含税销售总额"
        assert retrieval.primary_intent is PrimaryIntent.METRIC_QUERY
        assert retrieval2.primary_intent is PrimaryIntent.METRIC_QUERY
        assert retrieval.extraction["是否查询数据库"] == "是"
        assert retrieval.extraction["时间粒度"]["time_range"] == "2025年10月"
        assert retrieval2.extraction["时间粒度"]["time_range"] == "2025年11月"
        # 分析任务依赖全部取数任务，带分析绑定，不再查库
        assert analysis.primary_intent is PrimaryIntent.COMPARISON_ANALYSIS
        assert analysis.depends_on == ["task-1", "task-2"]
        assert "不再查询数据库" in analysis.question
        binding = analysis.extraction["分析绑定"]
        assert binding["analysis_type"] == "comparison"
        assert binding["tool"] == "对比分析工具"
        assert binding["merge"] == "period_roles"
        assert binding["periods"] == [
            {"task": "task-1", "label": "2025年10月", "role": "基期"},
            {"task": "task-2", "label": "2025年11月", "role": "当前期"},
        ]

    def test_year_comparison_split(self):
        question = "2024年和2025年的含税销售总额对比"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.COMPARISON_ANALYSIS,
                _structured("对比分析", 时间粒度={"unit": "年", "time_range": "2024年至2025年", "anchor": None}),
            ),
        )
        assert plan is not None and len(plan.tasks) == 3
        assert plan.tasks[0].question == "查询2024年的含税销售总额"
        assert plan.tasks[1].question == "查询2025年的含税销售总额"

    def test_relative_periods_split(self):
        question = "本月与上月含税销售总额对比"
        plan = build_analysis_task_plan(
            question,
            _extraction(PrimaryIntent.COMPARISON_ANALYSIS, _structured("对比分析")),
        )
        assert plan is not None and len(plan.tasks) == 3
        assert plan.tasks[0].question == "查询上月的含税销售总额"
        assert plan.tasks[1].question == "查询本月的含税销售总额"

    def test_unsplittable_comparison_falls_back_to_single_retrieval(self):
        question = "近12个月各月含税销售总额对比"
        plan = build_analysis_task_plan(
            question,
            _extraction(PrimaryIntent.COMPARISON_ANALYSIS, _structured("对比分析")),
            task_question="查询近12个月各月的含税销售总额",
            task_name="查询近12个月各月的含税销售总额",
        )
        assert plan is not None and len(plan.tasks) == 2
        assert plan.tasks[0].question == "查询近12个月各月的含税销售总额"
        assert plan.tasks[1].depends_on == ["task-1"]
        assert plan.tasks[1].extraction["分析绑定"]["merge"] == "concat"

    def test_mixed_absolute_and_relative_not_split(self):
        assert _comparison_periods("2025年11月和上月的销量对比") is None

    def test_same_period_not_split(self):
        assert _comparison_periods("2025年11月和2025年11月对比") is None

    def test_three_periods_not_split(self):
        assert _comparison_periods("2025年10月、11月和12月对比") is None


class TestAnalysisTypeExpansion:
    def test_trend_expands_with_sub_tools(self):
        # STALE_TEST: 2026-10-22 用户要求默认执行全部趋势能力。
        question = "近12个月含税销售总额趋势如何"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 时间粒度={"unit": "月", "time_range": "近12个月", "anchor": None}),
            ),
            task_question="查询近12个月各月的含税销售总额",
        )
        assert plan is not None and len(plan.tasks) == 3
        analysis = plan.tasks[-1]
        assert analysis.primary_intent is PrimaryIntent.TREND_ANALYSIS
        binding = analysis.extraction["分析绑定"]
        assert binding["tool"] == "趋势分析工具"
        assert binding["sub_tools"] == {
            "period_over_period": "环比计算工具",
            "year_over_year": "同比计算工具",
        }

    def test_root_cause_expands_total_and_dimension_retrievals(self):
        question = "本月总销量下滑，是什么原因导致的"
        structured = _structured(
            "归因分析",
            指标=[{"name": "销售量"}],
            维度=["经销商", "省份", "产品"],
            时间粒度={"unit": "月", "time_range": "本月与上月", "anchor": None},
        )
        plan = build_analysis_task_plan(
            question, _extraction(PrimaryIntent.ROOT_CAUSE_ANALYSIS, structured),
        )
        assert plan is not None and len(plan.tasks) == 5
        total = plan.tasks[0]
        # STALE_TEST: Test4 proves combined-period totals cannot support two-period attribution.
        # Preserve a date bucket in totals and every marginal dimension query.
        assert total.extraction["维度"] == ["业务日期"]
        assert total.question == "查询本月与上月分月各业务日期的销售量"
        dim_tasks = plan.tasks[1:4]
        assert [task.extraction["维度"] for task in dim_tasks] == [
            ["业务日期", "经销商"], ["业务日期", "省份"], ["业务日期", "产品"]]
        assert all('分月' in task.question for task in dim_tasks)
        analysis = plan.tasks[-1]
        assert analysis.depends_on == ["task-1", "task-2", "task-3", "task-4"]
        assert analysis.extraction["分析绑定"]["analysis_type"] == "root_cause"

    def test_root_cause_without_dimensions_falls_back(self):
        question = "本月总销量下滑，是什么原因导致的"
        structured = _structured("归因分析", 指标=[{"name": "销售量"}], 维度=[])
        plan = build_analysis_task_plan(
            question, _extraction(PrimaryIntent.ROOT_CAUSE_ANALYSIS, structured),
            task_question="查询本月与上月的销售量",
        )
        assert plan is not None and len(plan.tasks) == 2

    def test_forecast_expands(self):
        question = "基于近12个月销量预测未来3个月销售量"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.FORECAST_ANALYSIS,
                _structured("预测分析", 指标=[{"name": "销售量"}],
                            时间粒度={"unit": "月", "time_range": "近12个月", "anchor": None}),
            ),
            task_question="查询近12个月各月的销售量",
        )
        assert plan is not None and len(plan.tasks) == 2
        assert plan.tasks[1].primary_intent is PrimaryIntent.FORECAST_ANALYSIS
        assert plan.tasks[1].extraction["分析绑定"]["tool"] == "预测分析工具"

    def test_anomaly_and_composition_expand(self):
        for intent, label in (
            (PrimaryIntent.ANOMALY_ANALYSIS, "异常分析"),
            (PrimaryIntent.COMPOSITION_ANALYSIS, "占比分析"),
        ):
            plan = build_analysis_task_plan(
                "分析测试问题",
                _extraction(intent, _structured(label)),
                task_question="查询相关数据",
            )
            assert plan is not None and len(plan.tasks) == 2, label
            assert plan.tasks[1].primary_intent is intent


class TestExpansionGuards:
    def test_non_analysis_intent_not_expanded(self):
        assert build_analysis_task_plan(
            "查询2025年10月含税销售总额",
            _extraction(PrimaryIntent.METRIC_QUERY, _structured("指标查询")),
        ) is None

    def test_snapshot_reuse_not_expanded(self):
        structured = _structured("对比分析", 是否查询数据库="否")
        assert build_analysis_task_plan(
            "对比上一轮结果",
            _extraction(PrimaryIntent.COMPARISON_ANALYSIS, structured),
        ) is None

    def test_param_reuse_prefix_not_expanded(self):
        assert build_analysis_task_plan(
            "复用上一轮查询结果：对比A与B的销量",
            _extraction(PrimaryIntent.COMPARISON_ANALYSIS, _structured("对比分析")),
            task_question="复用上一轮查询结果：对比A与B的销量",
        ) is None

    def test_missing_metric_not_expanded(self):
        structured = _structured("趋势分析", 指标=[])
        assert build_analysis_task_plan(
            "看看趋势",
            _extraction(PrimaryIntent.TREND_ANALYSIS, structured),
        ) is None

    def test_task_texts_stay_within_source_vocabulary(self):
        question = "2025年11月和10月的含税销售总额对比"
        plan = build_analysis_task_plan(
            question,
            _extraction(PrimaryIntent.COMPARISON_ANALYSIS, _structured("对比分析")),
        )
        source_numbers = {"2025", "11", "10"}
        import re
        for task in plan.tasks:
            numbers = set(re.findall(r"\d+", task.question))
            assert numbers <= source_numbers, (task.question, numbers)

    def test_analysis_task_always_scheduled_after_retrievals(self):
        # 顺序回归：分析任务所在执行层必须晚于全部取数任务，
        # 调度执行节点内先取数、后分析工具调用
        from app.planning.compound_question import execution_layers, validate_plan
        question = "2025年11月和10月的含税销售总额对比"
        plan = build_analysis_task_plan(
            question,
            _extraction(PrimaryIntent.COMPARISON_ANALYSIS, _structured("对比分析")),
        )
        validate_plan(plan)
        layers = execution_layers(plan)
        layer_of = {
            task.task_id: index
            for index, layer in enumerate(layers)
            for task in layer
        }
        analysis = plan.tasks[-1]
        for retrieval in plan.tasks[:-1]:
            assert layer_of[retrieval.task_id] < layer_of[analysis.task_id]


class TestAnalysisToolDisplayMapping:
    def test_analysis_tool_maps_to_execution_section(self):
        # 六节点顺序冻结：分析工具归入既有调度执行节点，不新增顶层节点
        assert _thinking_section("ANALYSIS_TOOL") == "execution"

    def test_analysis_tool_maps_to_execute_exe_step(self):
        assert _new_agent_think_step("ANALYSIS_TOOL") == "execute_exe"

    def test_frozen_top_level_sections_unchanged(self):
        assert _thinking_section("INTENT_RECOGNITION") == "intent"
        assert _thinking_section("TASK_PLANNING") == "planning"
        assert _thinking_section("DATA_RETRIEVAL") == "execution"
        assert _thinking_section("RELIABILITY_CHECK") == "validation"
        assert _thinking_section("INSIGHT_ANALYSIS") == "insight"
        assert _thinking_section("FINAL_OUTPUT") == "final_output"


class TestAnalysisInputMerge:
    @staticmethod
    def _orchestrator():
        from app.services.orchestrator import DataAnalysisOrchestrator
        return object.__new__(DataAnalysisOrchestrator)

    @staticmethod
    def _reference(columns):
        class _Ref:
            def __init__(self, cols):
                self.columns = cols
        return _Ref(columns)

    def _task(self, depends_on):
        from app.domain.models import AtomicTask
        return AtomicTask(
            task_id="task-9", question="分析，不再查询数据库",
            depends_on=depends_on, extraction={"指标": [{"name": "含税销售总额"}]},
            primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
        )

    def _request(self, intent=PrimaryIntent.COMPARISON_ANALYSIS):
        return CanonicalAnalysisRequest(
            conversation_id="c1", tenant_id="t", user_id="u",
            original_question="2025年11月和10月的含税销售总额对比",
            primary_intent=intent,
            metrics=[MetricRef(input="含税销售总额", canonical_name="含税销售总额")],
        )

    def test_period_roles_merge_shape(self):
        orchestrator = self._orchestrator()
        task = self._task(["task-1", "task-2"])
        references = {
            "task-1": (self._reference(["含税销售总额"]), [{"含税销售总额": 100.0}]),
            "task-2": (self._reference(["含税销售总额"]), [{"含税销售总额": 80.0}]),
        }
        binding = {
            "analysis_type": "comparison", "merge": "period_roles",
            "periods": [
                {"task": "task-1", "label": "2025年10月", "role": "基期"},
                {"task": "task-2", "label": "2025年11月", "role": "当前期"},
            ],
        }
        columns, rows = orchestrator._merge_analysis_inputs(
            task, references, binding, self._request()
        )
        assert columns == ["期间", "含税销售总额", "period_role"]
        assert rows == [
            {"期间": "2025年10月", "含税销售总额": 100.0, "period_role": "基期"},
            {"期间": "2025年11月", "含税销售总额": 80.0, "period_role": "当前期"},
        ]

    def test_root_cause_contribution_shape(self):
        orchestrator = self._orchestrator()
        task = self._task(["task-1", "task-2"])
        references = {
            "task-1": (
                self._reference(["业务日期", "销售量"]),
                [{"业务日期": "2025-09", "销售量": 1000.0},
                 {"业务日期": "2025-10", "销售量": 800.0}],
            ),
            "task-2": (
                self._reference(["经销商", "业务日期", "销售量"]),
                [{"经销商": "A", "业务日期": "2025-09", "销售量": 600.0},
                 {"经销商": "A", "业务日期": "2025-10", "销售量": 300.0},
                 {"经销商": "B", "业务日期": "2025-09", "销售量": 400.0},
                 {"经销商": "B", "业务日期": "2025-10", "销售量": 500.0}],
            ),
        }
        request = CanonicalAnalysisRequest(
            conversation_id="c1", tenant_id="t", user_id="u",
            original_question="本月总销量下滑，是什么原因导致的",
            primary_intent=PrimaryIntent.ROOT_CAUSE_ANALYSIS,
            metrics=[MetricRef(input="销售量", canonical_name="销售量")],
        )
        columns, rows = orchestrator._root_cause_contribution_shape(
            task, references, request
        )
        # STALE_TEST: 完整归因明细新增两期值，便于复核每项增减。
        assert columns == ["经销商", "销售量变化", "基期值", "本期值", "整体变化"]
        by_member = {row["经销商"]: row for row in rows}
        assert by_member["A"]["销售量变化"] == pytest.approx(-300.0)
        assert by_member["B"]["销售量变化"] == pytest.approx(100.0)
        assert by_member["A"]["整体变化"] == pytest.approx(-200.0)

    def test_root_cause_shape_rejects_single_period(self):
        from app.analysis.engine import AnalysisError
        orchestrator = self._orchestrator()
        task = self._task(["task-1"])
        references = {
            "task-1": (
                self._reference(["业务日期", "销售量"]),
                [{"业务日期": "2025-10", "销售量": 800.0}],
            ),
        }
        with pytest.raises(AnalysisError):
            orchestrator._root_cause_contribution_shape(
                task, references,
                CanonicalAnalysisRequest(
                    conversation_id="c1", tenant_id="t", user_id="u",
                    original_question="归因", primary_intent=PrimaryIntent.ROOT_CAUSE_ANALYSIS,
                    metrics=[MetricRef(input="销售量", canonical_name="销售量")],
                ),
            )


class TestTrendRetrievalWindow:
    def test_listed_months_extended_one_bucket_for_pop(self):
        # STALE_TEST: 2026-10-22 用户要求默认执行全部趋势能力。
        question = "分析2025年10月、11月、12月的含税销售总额趋势"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 时间粒度={
                    "unit": "月", "time_range": "2025年10月、11月、12月", "anchor": None,
                }),
            ),
            task_question="查询2025年第四季度分月含税销售总额",
        )
        assert plan is not None and len(plan.tasks) == 3
        grain = plan.tasks[0].extraction["时间粒度"]
        assert grain["time_range"] == "2025-09-01至2025-12-31"
        reasons = grain["window_alignment"]["reasons"]
        assert any("环比" in reason and "基期" in reason for reason in reasons)

    def test_range_text_extended_one_bucket(self):
        question = "分析2025年10月至12月的含税销售总额趋势"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 时间粒度={
                    "unit": "月", "time_range": "2025年10月至2025年12月", "anchor": None,
                }),
            ),
        )
        grain = plan.tasks[0].extraction["时间粒度"]
        assert grain["time_range"] == "2025-09-01至2025-12-31"

    def test_yoy_signal_extends_twelve_months(self):
        # STALE_TEST: 2026-10-22 用户要求默认执行全部趋势能力。
        question = "分析2025年10月至12月含税销售总额同比趋势"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 时间粒度={
                    "unit": "月", "time_range": "2025年10月至2025年12月", "anchor": None,
                }),
            ),
        )
        grain = plan.tasks[0].extraction["时间粒度"]
        # STALE_TEST: 同比基期独立查询，默认同时补充环比基期。
        assert grain["time_range"] == "2025-09-01至2025-12-31"
        assert plan.tasks[1].extraction["时间粒度"]["time_range"] == "2024-10-01至2024-12-31"

    def test_unparseable_window_not_extended(self):
        question = "分析最近的含税销售总额趋势"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 时间粒度={
                    "unit": "月", "time_range": "最近", "anchor": None,
                }),
            ),
        )
        grain = plan.tasks[0].extraction["时间粒度"]
        assert grain["time_range"] == "最近"

    def test_analysis_question_yoy_wording_follows_source(self):
        with_yoy = build_analysis_task_plan(
            "分析2025年10月至12月含税销售总额同比趋势",
            _extraction(PrimaryIntent.TREND_ANALYSIS, _structured("趋势分析")),
        )
        assert "同比" in with_yoy.tasks[-1].question
        without_yoy = build_analysis_task_plan(
            "分析2025年10月、11月、12月的含税销售总额趋势",
            _extraction(PrimaryIntent.TREND_ANALYSIS, _structured("趋势分析")),
        )
        assert "同比" in without_yoy.tasks[-1].question
        assert "环比" in without_yoy.tasks[-1].question


class TestExtendWindowForPopBaseline:
    def test_absolute_range(self):
        from app.analysis.chain_tools.temporal_window import extend_window_for_pop_baseline
        rewritten = extend_window_for_pop_baseline(
            {"unit": "月", "time_range": "2025年10月至2025年12月", "anchor": None}
        )
        assert rewritten["time_range"] == "2025-09-01至2025-12-31"
        assert rewritten["window_alignment"]["derived_from"] == "2025年10月至2025年12月"

    def test_disjunctive_months(self):
        from app.analysis.chain_tools.temporal_window import extend_window_for_pop_baseline
        rewritten = extend_window_for_pop_baseline(
            {"unit": "月", "time_range": "2025年10月、11月、12月", "anchor": None}
        )
        assert rewritten["time_range"] == "2025-09-01至2025-12-31"

    def test_unparseable_returns_none(self):
        from app.analysis.chain_tools.temporal_window import extend_window_for_pop_baseline
        assert extend_window_for_pop_baseline(
            {"unit": "月", "time_range": "前段时间", "anchor": None}
        ) is None


class TestAnalysisToolSummaries:
    @staticmethod
    def _orchestrator():
        from app.services.orchestrator import DataAnalysisOrchestrator
        return object.__new__(DataAnalysisOrchestrator)

    def test_pop_summary_shows_period_rates(self):
        orchestrator = self._orchestrator()
        summary = orchestrator._analysis_sub_tool_summary("period_over_period", {
            "metric_column": "含税销售总额", "period_count": 2,
            "changes": [
                {"from": "2025-10", "to": "2025-11", "base": 100.0, "current": 26.42,
                 "absolute_change": -73.58, "change_rate": -0.7358},
                {"from": "2025-11", "to": "2025-12", "base": 26.42, "current": 38.04,
                 "absolute_change": 11.62, "change_rate": 0.44},
            ],
        })
        assert "2025-11较2025-10 -73.58%" in summary
        assert "2025-12较2025-11 +44.00%" in summary

    def test_yoy_summary_not_computable_shows_reason(self):
        orchestrator = self._orchestrator()
        summary = orchestrator._analysis_sub_tool_summary("year_over_year", {
            "status": "NOT_COMPUTABLE",
            "reason": "查询结果中没有上年同期数据，同比无法计算",
        })
        assert summary.startswith("不可计算：")
        assert "上年同期" in summary

    def test_yoy_summary_computable(self):
        orchestrator = self._orchestrator()
        summary = orchestrator._analysis_sub_tool_summary("year_over_year", {
            "month_count": 2,
            "changes": [
                {"label": "2025-10", "status": "COMPUTABLE", "change_rate": 0.12},
                {"label": "2025-11", "status": "NO_BASELINE"},
            ],
        })
        assert "2025-10同比 +12.00%" in summary
        assert "缺上年基期" in summary

    def test_main_tool_summary_trend(self):
        orchestrator = self._orchestrator()
        summary = orchestrator._analysis_main_tool_summary(
            "trend", "robust_trend_diagnostics",
            {"start": 100.0, "end": 38.0, "absolute_change": -62.0,
             "change_rate": -0.62,
             "change_points": [{"label": "2025-11", "type": "TROUGH"}]},
        )
        assert "robust_trend_diagnostics" in summary
        assert "整体变化" in summary
        assert "2025-11（谷值）" in summary

    def test_main_tool_summary_comparison(self):
        orchestrator = self._orchestrator()
        summary = orchestrator._analysis_main_tool_summary(
            "comparison", "two_group_comparison",
            {"base_label": "2025年10月", "current_label": "2025年11月",
             "base": 100.0, "current": 26.42,
             "absolute_change": -73.58, "change_rate": -0.7358},
        )
        assert "2025年11月较2025年10月" in summary
        assert "-73.58%" in summary

    def test_main_tool_summary_fallback(self):
        orchestrator = self._orchestrator()
        summary = orchestrator._analysis_main_tool_summary("anomaly", "mad_outlier", {"a": 1})
        assert "mad_outlier" in summary


class TestAnalysisRequestSignals:
    @staticmethod
    def _orchestrator():
        from app.services.orchestrator import DataAnalysisOrchestrator
        return object.__new__(DataAnalysisOrchestrator)

    def _request(self, binding, question="分析2025年10月、11月、12月的含税销售总额趋势"):
        from app.domain.models import AtomicTask
        from types import SimpleNamespace
        orchestrator = self._orchestrator()
        task = AtomicTask(
            task_id="task-2", question="根据上述任务的查询结果，趋势分析，不再查询数据库",
            depends_on=["task-1"], extraction={"指标": [{"name": "含税销售总额"}]},
            primary_intent=PrimaryIntent.TREND_ANALYSIS,
        )
        chat = SimpleNamespace(
            conversation_id="c1", application_id="app", question=question,
            semantic_model_id=120, knowledge_base_names=[],
        )
        identity = SimpleNamespace(tenant_id="t", user_id="u")
        return orchestrator._analysis_task_request(chat, identity, task, binding)

    def test_pop_signal_forced_for_trend(self):
        binding = {"analysis_type": "trend", "intent": "TREND_ANALYSIS",
                   "source_question": "分析2025年10月、11月、12月的含税销售总额趋势"}
        request = self._request(binding)
        assert "环比" in request.original_question
        assert "同比" not in request.original_question

    def test_yoy_signal_comes_from_source_question_only(self):
        binding = {"analysis_type": "trend", "intent": "TREND_ANALYSIS",
                   "source_question": "分析2025年10月至12月含税销售总额同比趋势"}
        request = self._request(binding)
        assert "同比" in request.original_question
        # 任务文本里不再查询数据库等措辞不影响信号来源
        assert "根据上述任务" not in request.original_question

    def test_source_question_recorded_in_assumptions(self):
        binding = {"analysis_type": "trend", "intent": "TREND_ANALYSIS",
                   "source_question": "分析2025年10月、11月、12月的含税销售总额趋势"}
        request = self._request(binding)
        assert any(
            item.startswith("ANALYSIS_SOURCE_QUESTION=")
            for item in request.assumptions
        )


class TestTrendExecutionWindow:
    def test_retrieval_question_carries_extended_window(self):
        question = "分析2025年10月、11月、12月的含税销售总额趋势"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 时间粒度={
                    "unit": "月", "time_range": "2025年10月、11月、12月", "anchor": None,
                }),
            ),
            task_question="查询2025年第四季度分月含税销售总额",
            task_name="查询2025年第四季度分月含税销售总额",
        )
        retrieval = plan.tasks[0]
        # Oagnet 主路径按问题文本解析 time_context：执行文本带扩展窗口
        assert retrieval.question == "查询2025-09-01至2025-12-31分月的含税销售总额"
        # 任务表展示名保持用户声明的窗口
        assert retrieval.name == "查询2025年第四季度分月含税销售总额"
        binding = plan.tasks[-1].extraction["分析绑定"]
        assert binding["display_window_start"] == "2025-10"

    def test_display_window_start_absent_without_parseable_window(self):
        question = "分析最近的含税销售总额趋势"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 时间粒度={
                    "unit": "月", "time_range": "最近", "anchor": None,
                }),
            ),
        )
        assert plan.tasks[-1].extraction["分析绑定"]["display_window_start"] is None


class TestEngineDisplayWindow:
    @staticmethod
    def _trend_facts():
        from app.analysis import AnalysisEngine
        from app.domain.models import KnowledgeContext
        request = CanonicalAnalysisRequest(
            conversation_id="c1", tenant_id="t", user_id="u",
            original_question="分析2025年10月、11月、12月的含税销售总额趋势（需计算环比）",
            primary_intent=PrimaryIntent.TREND_ANALYSIS,
            metrics=[MetricRef(input="含税销售总额", canonical_name="含税销售总额")],
        )
        request.assumptions.append("DISPLAY_WINDOW_START=2025-10")
        output = AnalysisEngine().analyze(
            request,
            ["业务日期", "含税销售总额"],
            [
                {"业务日期": "2025-09", "含税销售总额": 500000000.0},
                {"业务日期": "2025-10", "含税销售总额": 542836197.9},
                {"业务日期": "2025-11", "含税销售总额": 143433629.9},
                {"业务日期": "2025-12", "含税销售总额": 206539057.7},
            ],
            KnowledgeContext(query=""),
        )
        return output.facts

    def test_display_window_uses_user_scope(self):
        facts = self._trend_facts()
        window = facts["display_window"]
        assert window["start_label"] == "2025-10"
        assert window["end_label"] == "2025-12"
        assert window["absolute_change"] == pytest.approx(206539057.7 - 542836197.9)
        assert window["total"] == pytest.approx(542836197.9 + 143433629.9 + 206539057.7)
        assert window["mean"] == pytest.approx(window["total"] / 3)
        assert window["baseline_labels"] == ["2025-09"]
        # 环比仍用含基期的完整序列：9→10月环比必须存在
        pop = facts["period_over_period"]["changes"]
        assert pop[0]["from"] == "2025-09" and pop[0]["to"] == "2025-10"

    def test_recovery_share_present(self):
        facts = self._trend_facts()
        assert facts["later_recovery"] > 0
        assert facts["recovery_share_of_largest_drop"] == pytest.approx(
            63105427.8 / 399402568.0, rel=1e-3
        )

    def test_no_assumption_no_display_window(self):
        from app.analysis import AnalysisEngine
        from app.domain.models import KnowledgeContext
        request = CanonicalAnalysisRequest(
            conversation_id="c1", tenant_id="t", user_id="u",
            original_question="趋势（需计算环比）",
            primary_intent=PrimaryIntent.TREND_ANALYSIS,
            metrics=[MetricRef(input="含税销售总额", canonical_name="含税销售总额")],
        )
        output = AnalysisEngine().analyze(
            request,
            ["业务日期", "含税销售总额"],
            [{"业务日期": "2025-10", "含税销售总额": 100.0},
             {"业务日期": "2025-11", "含税销售总额": 120.0}],
            KnowledgeContext(query=""),
        )
        assert "display_window" not in output.facts


class TestAnalysisToolFormatting:
    @staticmethod
    def _orchestrator():
        from app.services.orchestrator import DataAnalysisOrchestrator
        return object.__new__(DataAnalysisOrchestrator)

    def test_large_number_no_scientific_notation(self):
        orchestrator = self._orchestrator()
        assert orchestrator._analysis_fmt(-336297140.2, signed=True) == "-336,297,140.2"
        assert orchestrator._analysis_fmt(542836197.9041) == "542,836,197.9041"
        assert orchestrator._analysis_fmt(893000000.0) == "893,000,000"
        assert "e+" not in orchestrator._analysis_fmt(1.6e8)

    def test_main_summary_shows_display_window_and_diagnostics(self):
        orchestrator = self._orchestrator()
        summary = orchestrator._analysis_main_tool_summary("trend", "robust_trend_diagnostics", {
            "display_window": {
                "start_label": "2025-10", "end_label": "2025-12",
                "absolute_change": -336297140.2, "change_rate": -0.6195,
                "total": 892808885.5, "mean": 297602961.83,
                "baseline_labels": ["2025-09"],
            },
            "trend_diagnostics": {
                "decision_method": "theil_sen_pairwise_slope", "direction": "DOWN",
                "robust_slope_per_period": -168148570.1, "normalized_slope": -0.81,
                "direction_consistency": 0.5, "coefficient_of_variation": 0.59,
            },
            "change_points": [{"label": "2025-11", "type": "TROUGH"}],
            "recovery_share_of_largest_drop": 0.158,
        })
        assert "展示窗口2025-10至2025-12" in summary
        assert "-336,297,140.2" in summary and "e+" not in summary
        assert "窗口合计 892,808,885.5" in summary
        assert "月均 297,602,961.83" in summary
        assert "theil_sen_pairwise_slope" in summary
        assert "标准化斜率 -0.81" in summary
        assert "方向一致性 50.00%" in summary
        assert "变异系数 0.59" in summary
        assert "2025-11（谷值）" in summary
        assert "回升占跌幅 15.80%" in summary

    def test_pop_summary_includes_absolute_change(self):
        orchestrator = self._orchestrator()
        summary = orchestrator._analysis_sub_tool_summary("period_over_period", {
            "changes": [
                {"from": "2025-11", "to": "2025-12", "absolute_change": 63105427.8,
                 "change_rate": 0.44},
            ],
        })
        assert "+44.00%" in summary
        assert "+63,105,427.8" in summary


class TestCollapseAnalysisComputationTasks:
    @staticmethod
    def _decomposition(intent, tasks):
        from app.planning.task_dag import QuestionDecomposition
        return QuestionDecomposition(intent=intent, tasks=tuple(tasks))

    def test_yoy_computation_task_collapsed_to_single_retrieval(self):
        from app.planning.task_dag import (
            DecomposedTask, _collapse_analysis_computation_tasks,
        )
        retrieval = DecomposedTask(
            question="查询2025年下半年及2024年同期各月的订单笔数",
            intent=PrimaryIntent.METRIC_QUERY,
            structured=_structured("趋势分析", 指标=[{"name": "订单笔数"}],
                时间粒度={"unit": "月", "time_range": "2025年下半年", "anchor": None}),
        )
        invented = DecomposedTask(
            question="基于已获取的数据，不再查询数据库，计算2025年下半年各月订单笔数的同比增长率",
            depends_on=(0,),
            intent=PrimaryIntent.TREND_ANALYSIS,
            structured=_structured("趋势分析", 是否查询数据库="否"),
        )
        collapsed = _collapse_analysis_computation_tasks(
            self._decomposition(PrimaryIntent.TREND_ANALYSIS, [retrieval, invented])
        )
        assert len(collapsed.tasks) == 1
        assert collapsed.tasks[0].question == retrieval.question
        assert collapsed.tasks[0].depends_on == ()
        assert collapsed.intent is PrimaryIntent.TREND_ANALYSIS

    def test_metric_query_computation_tasks_preserved(self):
        from app.planning.task_dag import (
            DecomposedTask, _collapse_analysis_computation_tasks,
        )
        retrieval = DecomposedTask(question="查询销售额", intent=PrimaryIntent.METRIC_QUERY)
        calc = DecomposedTask(
            question="根据上述任务的查询结果计算订单平均金额，不再查询数据库",
            depends_on=(0,), intent=PrimaryIntent.METRIC_QUERY,
        )
        decomposition = self._decomposition(PrimaryIntent.METRIC_QUERY, [retrieval, calc])
        assert _collapse_analysis_computation_tasks(decomposition) is decomposition

    def test_collapsed_single_task_feeds_analysis_expander(self):
        # 规整后的单任务必须能被分析任务规划器接管成规范DAG
        # STALE_TEST: 2026-10-22 用户要求默认执行全部趋势能力。
        question = "2025年下半年各月订单笔数同比趋势"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 指标=[{"name": "订单笔数"}],
                    时间粒度={"unit": "月", "time_range": "2025年下半年", "anchor": None}),
            ),
            task_question="查询2025年下半年各月的订单笔数",
        )
        assert plan is not None and len(plan.tasks) == 3
        grain = plan.tasks[0].extraction["时间粒度"]
        # STALE_TEST: 目标期和同比基期分别查询。
        # STALE_TEST: explicit YoY selects no MoM baseline.
        assert grain["time_range"] == "2025-07-01至2025-12-31"
        assert plan.tasks[1].extraction["时间粒度"]["time_range"] == "2024-07-01至2024-12-31"
        assert plan.tasks[-1].extraction["分析绑定"]["display_window_start"] == "2025-07"
        assert "同比" in plan.tasks[-1].question


class TestHalfYearWindow:
    def test_half_year_parsed(self):
        from app.analysis.chain_tools.temporal_window import parse_absolute_range
        assert parse_absolute_range("2025年下半年") == (
            __import__("datetime").date(2025, 7, 1),
            __import__("datetime").date(2025, 12, 31),
        )

    def test_half_year_pop_extension(self):
        from app.analysis.chain_tools.temporal_window import extend_window_for_pop_baseline
        rewritten = extend_window_for_pop_baseline(
            {"unit": "月", "time_range": "2025年下半年", "anchor": None}
        )
        assert rewritten["time_range"] == "2025-06-01至2025-12-31"


class TestGroupingExcludesTimeDimensions:
    def test_comparison_retrieval_text_has_no_time_dimension(self):
        question = "2025年11月和10月的含税销售总额对比"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.COMPARISON_ANALYSIS,
                _structured("对比分析", 维度=["业务日期", "月份"]),
            ),
        )
        assert plan is not None
        assert plan.tasks[0].question == "查询2025年10月的含税销售总额"
        assert plan.tasks[1].question == "查询2025年11月的含税销售总额"
        assert "各业务日期" not in plan.tasks[0].question

    def test_business_grouping_kept(self):
        question = "2025年11月和10月各省份的含税销售总额对比"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.COMPARISON_ANALYSIS,
                _structured("对比分析", 维度=["省份", "业务日期"]),
            ),
        )
        assert "各省份" in plan.tasks[0].question
        assert "业务日期" not in plan.tasks[0].question


class TestGroundingSeasonWhitelist:
    def test_half_year_month_expansion_not_rejected(self):
        from app.domain.models import AtomicTask, TaskPlan
        from app.planning.compound_question import validate_plan
        plan = TaskPlan(
            planner="STRUCTURED_MODEL",
            tasks=[
                AtomicTask(task_id="task-1", question="查询2025年7月至12月各月的订单笔数"),
                AtomicTask(task_id="task-2",
                           question="根据上述任务的查询结果做趋势分析，不再查询数据库",
                           depends_on=["task-1"]),
            ],
        )
        validate_plan(plan, source_question="2025年下半年各月订单笔数同比趋势")

    def test_quarter_month_expansion_not_rejected(self):
        from app.domain.models import AtomicTask, TaskPlan
        from app.planning.compound_question import validate_plan
        plan = TaskPlan(
            planner="STRUCTURED_MODEL",
            tasks=[
                AtomicTask(task_id="task-1", question="查询10月至12月各医院等级的销售总数量"),
                AtomicTask(task_id="task-2",
                           question="根据上述任务的查询结果计算占比，不再查询数据库",
                           depends_on=["task-1"]),
            ],
        )
        validate_plan(plan, source_question="第四季度各医院等级的销售总数量构成")

    def test_unrelated_number_still_rejected(self):
        from app.domain.models import AtomicTask, TaskPlan
        from app.planning.compound_question import validate_plan, TaskPlanningError
        plan = TaskPlan(
            planner="STRUCTURED_MODEL",
            tasks=[
                AtomicTask(task_id="task-1", question="查询2025年3月的订单笔数"),
                AtomicTask(task_id="task-2",
                           question="根据上述任务的查询结果做趋势分析，不再查询数据库",
                           depends_on=["task-1"]),
            ],
        )
        with pytest.raises(TaskPlanningError):
            validate_plan(plan, source_question="2025年下半年各月订单笔数同比趋势")


class TestCollapseTimeWindowIsomorphic:
    def test_yoy_two_retrievals_collapsed_to_latest(self):
        from app.planning.task_dag import (
            DecomposedTask, QuestionDecomposition, _collapse_analysis_computation_tasks,
        )
        structured_2025 = _structured("趋势分析", 指标=[{"name": "订单笔数"}],
            时间粒度={"unit": "月", "time_range": "2025年7月至12月", "anchor": None})
        structured_2024 = _structured("趋势分析", 指标=[{"name": "订单笔数"}],
            时间粒度={"unit": "月", "time_range": "2024年7月至12月", "anchor": None})
        tasks = [
            DecomposedTask(question="查询2025年7月至12月各月的订单笔数",
                           intent=PrimaryIntent.TREND_ANALYSIS, structured=structured_2025),
            DecomposedTask(question="查询2024年7月至12月各月的订单笔数",
                           intent=PrimaryIntent.TREND_ANALYSIS, structured=structured_2024),
            DecomposedTask(question="基于上述数据计算同比趋势，不再查询数据库",
                           depends_on=(0, 1), intent=PrimaryIntent.TREND_ANALYSIS),
        ]
        collapsed = _collapse_analysis_computation_tasks(
            QuestionDecomposition(intent=PrimaryIntent.TREND_ANALYSIS, tasks=tuple(tasks))
        )
        assert len(collapsed.tasks) == 1
        # 保留用户窗口（2025段）所在任务，窗口扩展由规划器完成
        assert "2025年7月至12月" in collapsed.tasks[0].question

    def test_non_isomorphic_retrievals_not_collapsed(self):
        from app.planning.task_dag import (
            DecomposedTask, QuestionDecomposition, _collapse_analysis_computation_tasks,
        )
        tasks = [
            DecomposedTask(question="查询2025年各月的订单笔数",
                           intent=PrimaryIntent.TREND_ANALYSIS,
                           structured=_structured("趋势分析", 指标=[{"name": "订单笔数"}])),
            DecomposedTask(question="查询2025年各月的含税销售总额",
                           intent=PrimaryIntent.TREND_ANALYSIS,
                           structured=_structured("趋势分析", 指标=[{"name": "含税销售总额"}])),
            DecomposedTask(question="基于上述数据分析，不再查询数据库",
                           depends_on=(0, 1), intent=PrimaryIntent.TREND_ANALYSIS),
        ]
        decomposition = QuestionDecomposition(
            intent=PrimaryIntent.TREND_ANALYSIS, tasks=tuple(tasks))
        assert _collapse_analysis_computation_tasks(decomposition) is decomposition


class TestTrendRetrievalSort:
    def test_sort_added_when_missing(self):
        question = "分析近1年的销售总数量的月度变化趋势"
        plan = build_analysis_task_plan(
            question,
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 指标=[{"name": "销售总数量"}],
                            维度=["业务日期"], 排序=[],
                            时间粒度={"unit": "月", "time_range": "近1年", "anchor": None}),
            ),
            task_question="查询近1年各月的销售总数量",
        )
        assert plan.tasks[0].extraction["排序"] == [{"field": "业务日期", "order": "asc"}]

    def test_existing_sort_preserved(self):
        plan = build_analysis_task_plan(
            "分析2025年10月、11月、12月的含税销售总额趋势",
            _extraction(
                PrimaryIntent.TREND_ANALYSIS,
                _structured("趋势分析", 排序=[{"field": "业务日期", "order": "asc"}]),
            ),
            task_question="查询2025年第四季度分月含税销售总额",
        )
        assert plan.tasks[0].extraction["排序"] == [{"field": "业务日期", "order": "asc"}]


class TestChartDisplayWindowClip:
    @staticmethod
    def _orchestrator():
        from app.services.orchestrator import DataAnalysisOrchestrator
        return object.__new__(DataAnalysisOrchestrator)

    def test_chart_clipped_to_display_window(self):
        from app.domain.models import ChartSpec
        orchestrator = self._orchestrator()
        spec = ChartSpec(
            chart_type="LINE", title="趋势", x_field="业务日期",
            y_fields=["含税销售总额"],
            data=[
                {"业务日期": "2025-09", "含税销售总额": 5.0},
                {"业务日期": "2025-10", "含税销售总额": 5.4},
                {"业务日期": "2025-11", "含税销售总额": 1.4},
                {"业务日期": "2025-12", "含税销售总额": 2.1},
            ],
            point_count=4,
        )
        clipped = orchestrator._clip_chart_to_display_window(spec, "2025-10")
        assert [p["业务日期"] for p in clipped.data] == ["2025-10", "2025-11", "2025-12"]
        assert clipped.point_count == 3

    def test_chart_untouched_without_parseable_start(self):
        from app.domain.models import ChartSpec
        orchestrator = self._orchestrator()
        spec = ChartSpec(
            chart_type="LINE", title="趋势", x_field="业务日期",
            y_fields=["v"], data=[{"业务日期": "2025-09", "v": 1}], point_count=1,
        )
        assert orchestrator._clip_chart_to_display_window(spec, "最近") is spec


class TestYearPrefixedMentionGuard:
    def test_year_prefixed_grouping_is_structural(self):
        from app.intent.classifier import RuleBasedIntentClassifier
        guard = RuleBasedIntentClassifier._is_structural_entity_mention
        assert guard("2025年各省份") is True
        assert guard("2025年10月各业务日期") is True
        assert guard("2026年按月份") is True

    def test_real_business_name_not_blocked(self):
        from app.intent.classifier import RuleBasedIntentClassifier
        guard = RuleBasedIntentClassifier._is_structural_entity_mention
        assert guard("费森尤斯") is False
        # 年份+非分组词不命中新守卫，行为与改动前一致
        assert guard("2025年销售额") is False


class TestYearInheritanceRange:
    def test_trailing_segment_inherits_year(self):
        from datetime import date
        from app.analysis.chain_tools.temporal_window import parse_absolute_range
        assert parse_absolute_range("2025年7月至12月") == (date(2025, 7, 1), date(2025, 12, 31))
        assert parse_absolute_range("2024年10月至2025年12月") == (date(2024, 10, 1), date(2025, 12, 31))

    def test_yoy_collapse_to_extended_plan(self):
        from app.planning.task_dag import (
            DecomposedTask, QuestionDecomposition, _collapse_analysis_computation_tasks,
        )
        def structured(time_range):
            return _structured("趋势分析", 指标=[{"name": "订单笔数"}], 维度=["业务日期"],
                时间粒度={"unit": "月", "time_range": time_range, "anchor": None})
        tasks = [
            DecomposedTask(question="查询2025年7月至12月各月的订单笔数",
                           intent=PrimaryIntent.TREND_ANALYSIS, structured=structured("2025年7月至12月")),
            DecomposedTask(question="查询2024年7月至12月各月的订单笔数",
                           intent=PrimaryIntent.TREND_ANALYSIS, structured=structured("2024年7月至12月")),
            DecomposedTask(question="基于上述数据计算同比趋势，不再查询数据库",
                           depends_on=(0, 1), intent=PrimaryIntent.TREND_ANALYSIS),
        ]
        collapsed = _collapse_analysis_computation_tasks(
            QuestionDecomposition(intent=PrimaryIntent.TREND_ANALYSIS, tasks=tuple(tasks)))
        first = collapsed.tasks[0]
        from app.domain.models import PlannerExtraction
        plan = build_analysis_task_plan(
            "2025年下半年各月订单笔数同比趋势",
            PlannerExtraction(intent=PrimaryIntent.TREND_ANALYSIS,
                              parameters=[], structured=first.structured),
            task_question=first.question, task_name="获取2025年下半年月度订单笔数",
        )
        retrieval = plan.tasks[0]
        # STALE_TEST: 用户目标时间不被同比基期覆盖。
        # STALE_TEST: explicit YoY selects no MoM baseline.
        assert retrieval.extraction["时间粒度"]["time_range"] == "2025-07-01至2025-12-31"
        assert retrieval.question == "查询2025-07-01至2025-12-31分月的订单笔数"
        assert plan.tasks[1].extraction["时间粒度"]["time_range"] == "2024-07-01至2024-12-31"
        assert retrieval.name == "获取2025年下半年月度订单笔数"
        assert retrieval.extraction["排序"] == [{"field": "业务日期", "order": "asc"}]
        assert plan.tasks[-1].extraction["分析绑定"]["display_window_start"] == "2025-07"


class TestAnalysisToolGapOperations:
    def test_gap_operations_detected(self):
        from app.planning.task_dag import _uses_analysis_tool_gap_operations
        assert _uses_analysis_tool_gap_operations(
            {"结果操作": {"operation": {"type": "monthly_growth"}}}) is True
        assert _uses_analysis_tool_gap_operations(
            {"结果操作": {"operation": {"type": "pipeline",
             "operations": [{"type": "derive"}, {"type": "extrema_difference"}]}}}) is True

    def test_analysis_shaped_operations_not_gap(self):
        from app.planning.task_dag import _uses_analysis_tool_gap_operations
        assert _uses_analysis_tool_gap_operations(
            {"结果操作": {"operation": {"type": "pipeline",
             "operations": [{"type": "derive", "operator": "divide"}]}}}) is False
        assert _uses_analysis_tool_gap_operations(
            {"计算绑定": {"operation": "subtract"}}) is False
        assert _uses_analysis_tool_gap_operations({}) is False

    def test_legal_subtract_binding_still_collapsed(self):
        # 09:15 实证：模型的 subtract 计算绑定+derive divide 结果操作虽合法，
        # 仍属分析型计算，必须规整给分析工具
        from app.planning.task_dag import (
            DecomposedTask, QuestionDecomposition, _collapse_analysis_computation_tasks,
        )
        def structured(time_range):
            return _structured("趋势分析", 指标=[{"name": "订单笔数"}], 维度=["业务日期"],
                时间粒度={"unit": "月", "time_range": time_range, "anchor": None})
        calc_structured = structured("2025年下半年及2024年下半年")
        calc_structured["是否查询数据库"] = "否"
        calc_structured["计算绑定"] = {
            "operation": "subtract",
            "left": {"source_task_index": 0, "column": "订单笔数"},
            "right": {"source_task_index": 1, "column": "订单笔数"},
            "result_field": "订单笔数同比增量", "format": "number"}
        calc_structured["结果操作"] = {"source_task_index": 0, "operation": {
            "type": "pipeline", "operations": [
                {"type": "derive", "operator": "divide",
                 "left_field": "订单笔数同比增量", "right_field": "去年同期订单笔数",
                 "result_field": "订单笔数同比增长率"}]}}
        tasks = [
            DecomposedTask(question="查询2025年7月至12月各月的订单笔数",
                           intent=PrimaryIntent.TREND_ANALYSIS, structured=structured("2025年7月至12月")),
            DecomposedTask(question="查询2024年7月至12月各月的订单笔数",
                           intent=PrimaryIntent.TREND_ANALYSIS, structured=structured("2024年7月至12月")),
            DecomposedTask(question="基于上述数据计算同比趋势，不再查询数据库",
                           depends_on=(0, 1), intent=PrimaryIntent.TREND_ANALYSIS,
                           structured=calc_structured),
        ]
        collapsed = _collapse_analysis_computation_tasks(
            QuestionDecomposition(intent=PrimaryIntent.TREND_ANALYSIS, tasks=tuple(tasks)))
        assert len(collapsed.tasks) == 1
