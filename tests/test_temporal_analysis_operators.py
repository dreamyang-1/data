from __future__ import annotations

from datetime import date

import pytest

from app.analysis import AnalysisEngine
from app.analysis.interpretation import InsightInterpretationLayer
from app.analysis.operators import (
    AnalysisOperatorRegistry,
    OperatorInputError,
    PeriodOverPeriodOperator,
    YearOverYearOperator,
)
from app.analysis.chain_tools.temporal_window import (
    align_structured_time_window,
    detect_edge_bucket_alignment,
)
from app.analysis.intents import normalize_locked_root_intent
from app.planning.task_dag import (
    _normalize_trend_time_order,
    _period_change_series_question,
)
from app.domain.models import (
    CanonicalAnalysisRequest,
    KnowledgeContext,
    MetricRef,
    PrimaryIntent,
    TimeRange,
)

# drug_sales 库实测月度值：外周插管中心静脉导管含税销售总额。
CATHETER_MONTHLY = [
    {"业务日期": "2025-10", "含税销售总额": 3377369.32},
    {"业务日期": "2025-11", "含税销售总额": 510715.15},
    {"业务日期": "2025-12", "含税销售总额": 1042521.39},
]

# 2026-10-19 14:24 事故的原始数据形态：全库月度含税销售总额，7-9月为稀疏月。
FULL_MONTHLY_6_POINTS = [
    {"业务日期": "2025-07", "含税销售总额": 3456.00},
    {"业务日期": "2025-08", "含税销售总额": 6912.01},
    {"业务日期": "2025-09", "含税销售总额": 5184.00},
    {"业务日期": "2025-10", "含税销售总额": 542836197.90},
    {"业务日期": "2025-11", "含税销售总额": 143433629.90},
    {"业务日期": "2025-12", "含税销售总额": 206539057.70},
]

TODAY = date(2026, 10, 19)


def trend_request(question: str, time_range: TimeRange | None = None) -> CanonicalAnalysisRequest:
    request = CanonicalAnalysisRequest(
        conversation_id="temporal-test",
        tenant_id="tenant",
        user_id="user",
        original_question=question,
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        metrics=[MetricRef(input="含税销售总额", canonical_name="含税销售总额")],
    )
    request.time_range = time_range
    return request


class TestTrendTimeOrderNormalization:
    """2026-10-19 16:14 实测：环比极值类问题排序被规划器写成指标降序，
    SQL 按指标返回后时间乱序，趋势引擎拒算。趋势意图下排序统一为
    时间维度升序。"""

    def test_metric_desc_sort_replaced_by_temporal_asc(self) -> None:
        structured = {
            "维度": ["业务日期"],
            "排序": [{"field": "含税销售总额", "order": "desc"}],
            "排序类型": "降序",
        }
        _normalize_trend_time_order(structured)
        assert structured["排序"] == [{"field": "业务日期", "order": "asc"}]
        assert structured["排序类型"] == "升序"

    def test_empty_sort_gets_temporal_asc(self) -> None:
        structured = {"维度": ["业务日期"], "排序": [], "排序类型": None}
        _normalize_trend_time_order(structured)
        assert structured["排序"] == [{"field": "业务日期", "order": "asc"}]

    def test_already_temporal_asc_unchanged(self) -> None:
        structured = {
            "维度": ["业务日期"],
            "排序": [{"field": "业务日期", "order": "asc"}],
            "排序类型": "升序",
        }
        _normalize_trend_time_order(structured)
        assert structured["排序"] == [{"field": "业务日期", "order": "asc"}]

    def test_no_temporal_dimension_unchanged(self) -> None:
        structured = {
            "维度": ["省份"],
            "排序": [{"field": "含税销售总额", "order": "desc"}],
        }
        _normalize_trend_time_order(structured)
        assert structured["排序"] == [{"field": "含税销售总额", "order": "desc"}]

    def test_multi_dimension_sorts_by_declared_temporal_dim(self) -> None:
        structured = {
            "维度": ["渠道业态", "业务日期"],
            "排序": [{"field": "渠道业态", "order": "asc"}],
        }
        _normalize_trend_time_order(structured)
        assert structured["排序"] == [{"field": "业务日期", "order": "asc"}]


class TestQuarterLabelParsing:
    """2026-10-19 16:36 实测：SQL 翻译层季度分组生成“2025-Q3”标签，
    引擎 _time_key 原模式不接受连字符，趋势计算被拒。"""

    def test_engine_accepts_hyphenated_quarter_labels(self) -> None:
        request = trend_request("按季度分析2025年含税销售总额趋势")
        output = AnalysisEngine().analyze(
            request,
            ["业务日期", "含税销售总额"],
            [
                {"业务日期": "2025-Q3", "含税销售总额": 15552.01},
                {"业务日期": "2025-Q4", "含税销售总额": 892808885.50},
            ],
            KnowledgeContext(query=""),
        )
        assert output.facts["actual_granularity"] == "quarter"
        assert output.facts["start"] == pytest.approx(15552.01)
        assert output.facts["end"] == pytest.approx(892808885.50)

    def test_continuity_detects_quarter_grain_for_hyphenated_labels(self) -> None:
        from app.analysis.engine import AnalysisEngine

        continuity = AnalysisEngine._temporal_continuity(["2025-Q1", "2025-Q4"])
        assert continuity["grain"] == "quarter"
        assert continuity["is_complete"] is False


class TestPeriodChangeSeriesRerouting:
    """2026-10-19 前端实测：同比/环比问题被模型判为对比分析后走两期引擎
    拒算（T3 无提取停摆、T7 模型自算环比）。改判规则的正反例。"""

    def test_yoy_with_range_is_series(self) -> None:
        # T3 原题
        assert _period_change_series_question(
            "分析2025年10月到2025年12月含税销售总额的同比增长情况"
        )

    def test_pop_with_which_month_is_series(self) -> None:
        # T7 原题（补全前后都命中）
        assert _period_change_series_question("哪个月环比降幅最大")
        assert _period_change_series_question(
            "分析2025年10月到2025年12月的含税销售总额趋势中，哪个月环比降幅最大"
        )

    def test_pop_with_monthly_wording_is_series(self) -> None:
        assert _period_change_series_question("统计2025年10月到12月各月的销售额环比")

    def test_two_explicit_periods_stays_comparison(self) -> None:
        assert not _period_change_series_question("11月环比10月变化多少")
        assert not _period_change_series_question("12月较11月环比如何")

    def test_object_comparison_stays_comparison(self) -> None:
        assert not _period_change_series_question("华东和华南销售额的同比对比")
        assert not _period_change_series_question("对比各区域销售额同比增长差异")

    def test_plain_trend_without_pop_yoy_not_matched(self) -> None:
        assert not _period_change_series_question("分析2025年10月到12月的销售趋势")


class TestLockedRootIntentNormalization:
    """根意图锁定（2026-10-23 起规划只认意图识别节点的根意图）后，
    多期序列环比/同比被判成对比分析会在规划合同校验直接拒算
    （2026-10-24 实测 SAFE_FALLBACK：意图是对比、绑定按趋势链路展开）。
    归一化的正反例与展开校验闭环。"""

    def test_multi_period_pop_normalized_to_trend(self) -> None:
        assert normalize_locked_root_intent(
            PrimaryIntent.COMPARISON_ANALYSIS,
            "统计2025年10月到12月各月的销售额环比变化情况",
        ) is PrimaryIntent.TREND_ANALYSIS

    def test_two_period_pop_stays_comparison(self) -> None:
        assert normalize_locked_root_intent(
            PrimaryIntent.COMPARISON_ANALYSIS, "11月环比10月变化多少"
        ) is PrimaryIntent.COMPARISON_ANALYSIS

    def test_object_comparison_stays_comparison(self) -> None:
        assert normalize_locked_root_intent(
            PrimaryIntent.COMPARISON_ANALYSIS, "华东和华南销售额的同比对比"
        ) is PrimaryIntent.COMPARISON_ANALYSIS

    def test_other_intents_untouched(self) -> None:
        assert normalize_locked_root_intent(
            PrimaryIntent.TREND_ANALYSIS,
            "统计2025年10月到12月各月的销售额环比变化情况",
        ) is PrimaryIntent.TREND_ANALYSIS
        assert normalize_locked_root_intent(
            PrimaryIntent.METRIC_QUERY, "2025年10月销售额"
        ) is PrimaryIntent.METRIC_QUERY
        assert normalize_locked_root_intent(None, "环比") is None

    def test_normalized_intent_passes_execution_contract(self) -> None:
        # 归一化后的意图走趋势链路展开，合同校验不再拒算
        from app.planning.analysis_task_planner import (
            PlannerExtraction,
            build_analysis_task_plan,
            validate_task_plan_execution_contract,
        )
        question = "统计2025年10月到12月各月的销售额环比变化情况"
        intent = normalize_locked_root_intent(PrimaryIntent.COMPARISON_ANALYSIS, question)
        structured = {
            "业务域": [], "实体": ["销售订单"],
            "指标": [{"name": "含税销售总额"}],
            "维度": [{"name": "业务日期"}],
            "过滤条件": [], "展示字段": [], "排序": [], "限制": None,
            "时间粒度": {"unit": "月", "time_range": "2025年10月至2025年12月",
                         "anchor": "业务日期"},
            "意图": "趋势分析", "是否查询数据库": "是",
        }
        plan = build_analysis_task_plan(
            question,
            PlannerExtraction(intent=intent, parameters=[], structured=structured),
            task_question="统计2025年10月到12月各月的含税销售总额",
        )
        assert plan is not None
        validate_task_plan_execution_contract(plan, intent)



class TestPeriodOverPeriodOperator:
    def test_two_changes_with_real_october_december_values(self) -> None:
        result = PeriodOverPeriodOperator().run(
            ["业务日期", "含税销售总额"], CATHETER_MONTHLY
        )
        changes = result.facts["changes"]
        assert len(changes) == 2
        assert changes[0]["absolute_change"] == pytest.approx(510715.15 - 3377369.32)
        assert changes[0]["change_rate"] == pytest.approx(-0.8488, abs=1e-4)
        assert changes[1]["absolute_change"] == pytest.approx(1042521.39 - 510715.15)
        assert changes[1]["change_rate"] == pytest.approx(1.0413, abs=1e-4)
        assert result.facts["period_count"] == 2

    def test_zero_base_period_reports_absolute_change_only(self) -> None:
        rows = [
            {"业务日期": "2025-10", "含税销售总额": 0},
            {"业务日期": "2025-11", "含税销售总额": 100},
        ]
        result = PeriodOverPeriodOperator().run(["业务日期", "含税销售总额"], rows)
        assert result.facts["changes"][0]["change_rate"] is None
        assert result.facts["changes"][0]["absolute_change"] == 100
        assert any("基期为0" in warning for warning in result.warnings)

    def test_single_row_rejected(self) -> None:
        with pytest.raises(OperatorInputError):
            PeriodOverPeriodOperator().run(
                ["业务日期", "含税销售总额"], CATHETER_MONTHLY[:1]
            )

    def test_multiple_metric_columns_rejected(self) -> None:
        rows = [
            {"业务日期": "2025-10", "含税销售总额": 1, "销售总数量": 2},
        ]
        with pytest.raises(OperatorInputError):
            PeriodOverPeriodOperator().run(
                ["业务日期", "含税销售总额", "销售总数量"], rows * 2
            )


class TestYearOverYearOperator:
    def test_all_months_without_last_year_baseline_rejects(self) -> None:
        result = YearOverYearOperator().run(
            ["业务日期", "含税销售总额"], CATHETER_MONTHLY
        )
        assert result.facts["status"] == "NOT_COMPUTABLE"
        assert "上年同期" in result.facts["reason"]
        assert result.facts["computable_count"] == 0
        assert any("同比不可计算" in warning for warning in result.warnings)

    def test_computes_when_baseline_year_present(self) -> None:
        rows = [
            {"业务日期": "2024-10", "含税销售总额": 1000000.0},
            {"业务日期": "2024-11", "含税销售总额": 2000000.0},
            {"业务日期": "2025-10", "含税销售总额": 1500000.0},
            {"业务日期": "2025-11", "含税销售总额": 1200000.0},
        ]
        result = YearOverYearOperator().run(["业务日期", "含税销售总额"], rows)
        assert result.facts["computable_count"] == 2
        by_label = {item["label"]: item for item in result.facts["changes"]}
        assert by_label["2025-10"]["absolute_change"] == pytest.approx(500000.0)
        assert by_label["2025-10"]["change_rate"] == pytest.approx(0.5)
        assert by_label["2025-11"]["change_rate"] == pytest.approx(-0.4)

    def test_partial_baseline_marks_missing_months(self) -> None:
        rows = [
            {"业务日期": "2024-11", "含税销售总额": 2000000.0},
            {"业务日期": "2025-10", "含税销售总额": 1500000.0},
            {"业务日期": "2025-11", "含税销售总额": 1200000.0},
        ]
        result = YearOverYearOperator().run(["业务日期", "含税销售总额"], rows)
        by_label = {item["label"]: item for item in result.facts["changes"]}
        assert by_label["2025-10"]["status"] == "NO_BASELINE"
        assert by_label["2025-11"]["status"] == "COMPUTABLE"
        assert any("缺少上年同期基期" in warning for warning in result.warnings)

    def test_label_without_year_rejected(self) -> None:
        rows = [
            {"业务日期": "10月", "含税销售总额": 1},
            {"业务日期": "11月", "含税销售总额": 2},
        ]
        with pytest.raises(OperatorInputError):
            YearOverYearOperator().run(["业务日期", "含税销售总额"], rows)


class TestRegistryRegistration:
    def test_new_operators_registered(self) -> None:
        registry = AnalysisOperatorRegistry()
        assert registry.execute(
            "period_over_period_change", ["业务日期", "含税销售总额"], CATHETER_MONTHLY
        ).facts["period_count"] == 2
        yoy = registry.execute(
            "year_over_year_change", ["业务日期", "含税销售总额"], CATHETER_MONTHLY
        )
        assert yoy.facts["status"] == "NOT_COMPUTABLE"


class TestAlignStructuredTimeWindow:
    def test_rolling_twelve_months_aligns_to_full_months(self) -> None:
        grain = {"unit": "月", "time_range": "最近12个月", "anchor": None}
        rewritten = align_structured_time_window(
            "按月分析外周插管中心静脉导管的销售趋势", grain,
            intent="TREND_ANALYSIS", today=TODAY,
        )
        assert rewritten is not None
        # STALE_TEST: 滚动范围保留日界；完整自然周期需明确指定。
        assert rewritten["time_range"] == "2025-10-19至2026-10-19"
        assert rewritten["window_alignment"]["derived_from"] == "最近12个月"

    def test_rolling_range_with_yoy_extends_baseline_window(self) -> None:
        grain = {"unit": "月", "time_range": "最近12个月", "anchor": None}
        rewritten = align_structured_time_window(
            "分析最近12个月含税销售总额的同比增长情况", grain,
            intent="TREND_ANALYSIS", today=TODAY,
        )
        assert rewritten is not None
        assert rewritten["time_range"] == "2025-10-19至2026-10-19"
        assert not any("扩展12个月" in note for note in rewritten["window_alignment"]["reasons"])

    def test_absolute_range_with_yoy_extends_start_by_twelve_months(self) -> None:
        grain = {"unit": "月", "time_range": "2025年10月到2025年12月", "anchor": None}
        rewritten = align_structured_time_window(
            "分析2025年10月到2025年12月含税销售总额的同比情况", grain,
            intent="TREND_ANALYSIS", today=TODAY,
        )
        # STALE_TEST: 绝对目标期不在此函数扩展同比。
        assert rewritten is None

    def test_absolute_range_without_yoy_not_rewritten(self) -> None:
        grain = {"unit": "月", "time_range": "2025年10月到2025年12月", "anchor": None}
        assert align_structured_time_window(
            "分析2025年10月到2025年12月的含税销售总额趋势", grain,
            intent="TREND_ANALYSIS", today=TODAY,
        ) is None

    def test_non_trend_intent_not_rewritten(self) -> None:
        grain = {"unit": "月", "time_range": "最近12个月", "anchor": None}
        assert align_structured_time_window(
            "最近12个月各省份销售额对比", grain,
            intent="COMPARISON_ANALYSIS", today=TODAY,
        ) is None

    def test_quarter_grain_aligns_to_quarter_boundary(self) -> None:
        grain = {"unit": "季度", "time_range": "最近4个季度", "anchor": None}
        rewritten = align_structured_time_window(
            "按季度分析含税销售总额趋势", grain,
            intent="TREND_ANALYSIS", today=TODAY,
        )
        assert rewritten is not None
        # STALE_TEST: 滚动范围保留日界；完整自然周期需明确指定。
        assert rewritten["time_range"] == "2025-10-19至2026-10-19"

    def test_week_grain_aligns_to_iso_week(self) -> None:
        grain = {"unit": "周", "time_range": "最近4周", "anchor": None}
        rewritten = align_structured_time_window(
            "按周分析含税销售总额趋势", grain,
            intent="TREND_ANALYSIS", today=TODAY,
        )
        assert rewritten is not None
        # STALE_TEST: 最近4周保留当天，不隐式切换为4个完整自然周。
        assert rewritten["time_range"] == "2026-09-21至2026-10-19"


class TestDetectEdgeBucketAlignment:
    def test_mid_month_window_start_marks_first_bucket_partial(self) -> None:
        result = detect_edge_bucket_alignment(
            ["2025-10", "2025-11", "2025-12"],
            date(2025, 10, 19), date(2027, 1, 1), "month",
        )
        assert result["first_bucket_partial"] is True
        assert result["first_partial_label"] == "2025-10"
        assert result["last_bucket_partial"] is False

    def test_month_aligned_window_has_no_partial_bucket(self) -> None:
        result = detect_edge_bucket_alignment(
            ["2025-10", "2025-11", "2025-12"],
            date(2025, 10, 1), date(2026, 1, 1), "month",
        )
        assert result["first_bucket_partial"] is False
        assert result["last_bucket_partial"] is False

    def test_window_end_inside_last_bucket_marks_partial(self) -> None:
        result = detect_edge_bucket_alignment(
            ["2025-11", "2025-12"],
            date(2025, 11, 1), date(2025, 12, 15), "month",
        )
        assert result["last_bucket_partial"] is True
        assert result["last_partial_label"] == "2025-12"

    def test_none_window_returns_all_false(self) -> None:
        result = detect_edge_bucket_alignment(["2025-10"], None, None, "month")
        assert result["first_bucket_partial"] is False


class TestEngineTrendIntegration:
    def test_partial_first_bucket_flagged_and_trimmed_comparison_provided(self) -> None:
        request = trend_request(
            "分析2025年10月到2025年12月的外周插管中心静脉导管的销售趋势",
            TimeRange(start=date(2025, 10, 19), end_exclusive=date(2026, 10, 20)),
        )
        output = AnalysisEngine().analyze(
            request, ["业务日期", "含税销售总额"], CATHETER_MONTHLY, KnowledgeContext(query="")
        )
        integrity = output.facts["edge_bucket_integrity"]
        assert integrity["first_bucket_partial"] is True
        assert integrity["first_partial_label"] == "2025-10"
        assert output.facts["period_changes"][0]["left_bucket_partial"] is True
        trimmed = output.facts["overall_change_excluding_partial"]
        assert trimmed["excluded_label"] == "2025-10"
        assert trimmed["start"] == pytest.approx(510715.15)
        assert trimmed["end"] == pytest.approx(1042521.39)
        assert trimmed["change_rate"] == pytest.approx(1.0413, abs=1e-4)
        assert any("不完整期间" in warning for warning in output.warnings)
        assert "不完整期间" in output.answer

    def test_pop_question_attaches_period_over_period_facts(self) -> None:
        request = trend_request(
            "分析2025年10月到2025年12月外周插管中心静脉导管的销售趋势及每月环比"
        )
        output = AnalysisEngine().analyze(
            request, ["业务日期", "含税销售总额"], CATHETER_MONTHLY, KnowledgeContext(query="")
        )
        pop = output.facts["period_over_period"]
        assert pop["period_count"] == 2
        assert pop["changes"][0]["change_rate"] == pytest.approx(-0.8488, abs=1e-4)

    def test_pop_with_partial_first_bucket_excludes_first_change(self) -> None:
        request = trend_request(
            "按月分析外周插管中心静脉导管的销售趋势和环比",
            TimeRange(start=date(2025, 10, 19), end_exclusive=date(2026, 1, 1)),
        )
        output = AnalysisEngine().analyze(
            request, ["业务日期", "含税销售总额"], CATHETER_MONTHLY, KnowledgeContext(query="")
        )
        pop = output.facts["period_over_period"]
        assert pop["excluded_partial_first"] == "2025-10"
        assert pop["period_count"] == 1
        assert pop["changes"][0]["from"] == "2025-11"
        assert any("环比已剔除" in warning for warning in output.warnings)

    def test_yoy_question_without_baseline_reports_not_computable(self) -> None:
        request = trend_request(
            "分析2025年10月到2025年12月含税销售总额的同比增长情况"
        )
        output = AnalysisEngine().analyze(
            request, ["业务日期", "含税销售总额"], CATHETER_MONTHLY, KnowledgeContext(query="")
        )
        yoy = output.facts["year_over_year"]
        assert yoy["status"] == "NOT_COMPUTABLE"
        assert "上年同期" in yoy["reason"]
        assert "同比分析" in output.answer

    def test_no_pop_facts_without_pop_wording(self) -> None:
        request = trend_request("分析2025年10月到2025年12月的含税销售总额趋势")
        output = AnalysisEngine().analyze(
            request, ["业务日期", "含税销售总额"], CATHETER_MONTHLY, KnowledgeContext(query="")
        )
        assert "period_over_period" not in output.facts
        assert "year_over_year" not in output.facts


class TestInterpretationRegression:
    def test_six_point_series_with_abnormal_changes_no_longer_crashes(self) -> None:
        """复现 2026-10-19 14:24 事故：6点序列触发异常波动分支时崩溃。"""
        request = trend_request("分析2025年7月到2025年12月的含税销售总额趋势")
        analysis = AnalysisEngine().analyze(
            request, ["业务日期", "含税销售总额"], FULL_MONTHLY_6_POINTS,
            KnowledgeContext(query=""),
        )
        structured = InsightInterpretationLayer().interpret(request, analysis)
        assert structured.abnormal_changes
        # 9→10月灌数断层被检出为需核查的异常变动。
        statements = [insight.statement for insight in structured.insights]
        assert any("需重点核查" in statement for statement in statements)

    def test_interpretation_discloses_partial_bucket_and_yoy_rejection(self) -> None:
        request = trend_request(
            "分析2025年10月到2025年12月含税销售总额的同比和环比",
            TimeRange(start=date(2025, 10, 19), end_exclusive=date(2026, 1, 1)),
        )
        analysis = AnalysisEngine().analyze(
            request, ["业务日期", "含税销售总额"], CATHETER_MONTHLY, KnowledgeContext(query="")
        )
        structured = InsightInterpretationLayer().interpret(request, analysis)
        statements = [insight.statement for insight in structured.insights]
        assert any("不完整期间" in statement for statement in statements)
        assert any("同比不可计算" in statement for statement in statements)
        assert any("环比变动最大" in statement for statement in statements)
