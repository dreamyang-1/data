from __future__ import annotations

import pytest

from app.analysis import AnalysisEngine, AnalysisError
from app.analysis.chain_tools.composition import cumulative_contribution_threshold
from app.analysis.chain_tools.temporal_window import disjunctive_periods_to_range
from app.analysis.operators import (
    AnalysisOperatorRegistry,
    CumulativeContributionOperator,
    OperatorInputError,
)
from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    KnowledgeContext,
    MetricRef,
    PrimaryIntent,
)


def composition_request(question: str, operators=None) -> CanonicalAnalysisRequest:
    request = CanonicalAnalysisRequest(
        conversation_id="composition-test",
        tenant_id="tenant",
        user_id="user",
        original_question=question,
        primary_intent=PrimaryIntent.COMPOSITION_ANALYSIS,
        metrics=[MetricRef(input="含税销售总额", canonical_name="含税销售总额")],
    )
    if operators is not None:
        request.operators = operators
    return request


# 头部分布形态取自 drug_sales 实测（2154家经销商、80%需319家、
# 存在1家负净额），按比例缩小的等价形状。
PARETO_ROWS = [
    {"经销商名称": f"经销商{index:03d}", "含税销售总额": 1000 - index * 8}
    for index in range(1, 121)
] + [
    {"经销商名称": "净退货商家", "含税销售总额": -500.0},
]


class TestCumulativeContributionOperator:
    def test_boundary_entity_included_in_threshold_set(self) -> None:
        result = CumulativeContributionOperator().run(
            ["经销商名称", "含税销售总额"], PARETO_ROWS, threshold=0.8
        )
        facts = result.facts
        threshold_set = facts["threshold_set"]
        assert facts["threshold"] == 0.8
        # 边界实体必须计入：最后一项跨过阈值且在集合内
        assert facts["boundary_label"] == threshold_set[-1]["label"]
        assert threshold_set[-1]["cumulative_share"] >= 0.8
        # 前一项尚未跨过阈值
        assert threshold_set[-2]["cumulative_share"] < 0.8
        assert facts["threshold_set_coverage"] == pytest.approx(
            threshold_set[-1]["cumulative_share"]
        )
        # 份额按降序排列
        values = [item["value"] for item in threshold_set]
        assert values == sorted(values, reverse=True)

    def test_negative_group_disclosed_not_in_prefix(self) -> None:
        result = CumulativeContributionOperator().run(
            ["经销商名称", "含税销售总额"], PARETO_ROWS, threshold=0.8
        )
        facts = result.facts
        assert facts["negative_group_count"] == 1
        assert "净退货商家" not in {
            item["label"] for item in facts["threshold_set"]
        }
        # 负值组计入总量抵减（分母语义），披露进 warnings
        assert any("负值组" in warning for warning in result.warnings)

    def test_total_not_positive_rejected(self) -> None:
        rows = [
            {"经销商名称": "甲", "含税销售总额": -100.0},
            {"经销商名称": "乙", "含税销售总额": 50.0},
        ]
        with pytest.raises(OperatorInputError, match="不大于0"):
            CumulativeContributionOperator().run(
                ["经销商名称", "含税销售总额"], rows, threshold=0.8
            )

    def test_multiple_numeric_columns_rejected(self) -> None:
        rows = [
            {"经销商名称": "甲", "含税销售总额": 100, "销售总数量": 5},
            {"经销商名称": "乙", "含税销售总额": 200, "销售总数量": 8},
        ]
        with pytest.raises(OperatorInputError, match="一个数值指标列"):
            CumulativeContributionOperator().run(
                ["经销商名称", "含税销售总额", "销售总数量"], rows, threshold=0.8
            )

    def test_invalid_threshold_rejected(self) -> None:
        with pytest.raises(OperatorInputError, match="阈值"):
            CumulativeContributionOperator().run(
                ["经销商名称", "含税销售总额"], PARETO_ROWS, threshold=1.5
            )

    def test_registered_in_registry(self) -> None:
        result = AnalysisOperatorRegistry().execute(
            "cumulative_contribution",
            ["经销商名称", "含税销售总额"],
            PARETO_ROWS,
            threshold=0.8,
        )
        assert result.facts["threshold_set_size"] >= 1


class TestCumulativeThresholdParsing:
    def test_contribution_percentage(self) -> None:
        assert cumulative_contribution_threshold(
            "哪些经销商贡献了80%的含税销售总额"
        ) == pytest.approx(0.8)

    def test_cumulative_share_wording(self) -> None:
        assert cumulative_contribution_threshold(
            "各经销商累计占比达到60%的有哪些"
        ) == pytest.approx(0.6)

    def test_plain_share_not_matched(self) -> None:
        assert cumulative_contribution_threshold("各业态含税销售总额占比") is None

    def test_no_default_threshold(self) -> None:
        # “主要贡献者”无显式阈值，不能擅自按80%口径计算
        assert cumulative_contribution_threshold("销售额主要贡献经销商") is None

    def test_invalid_percentage_rejected(self) -> None:
        assert cumulative_contribution_threshold("贡献了120%的销售额") is None


class TestEngineCompositionParetoBranch:
    def test_pareto_question_runs_operator(self) -> None:
        request = composition_request("哪些经销商贡献了80%的含税销售总额")
        output = AnalysisEngine().analyze(
            request, ["经销商名称", "含税销售总额"], PARETO_ROWS,
            KnowledgeContext(query=""),
        )
        assert output.method == "cumulative_contribution"
        assert output.facts["boundary_label"] == output.facts["threshold_set"][-1]["label"]
        assert output.facts["group_count"] == len(PARETO_ROWS)
        assert output.facts["threshold"] == pytest.approx(0.8)

    def test_numeric_identity_code_is_not_mistaken_for_metric(self) -> None:
        request = composition_request("哪些经销商贡献了80%的含税销售总额")
        rows = [
            dict(row, **{"经销商（编码）": str(index)})
            for index, row in enumerate(PARETO_ROWS, start=1)
        ]
        output = AnalysisEngine().analyze(
            request, ["经销商名称", "经销商（编码）", "含税销售总额"], rows,
            KnowledgeContext(query=""),
        )
        assert output.facts["metric_column"] == "含税销售总额"
        assert output.facts["label_column"] == "经销商名称"
        assert output.facts["group_count"] == len(PARETO_ROWS)

    def test_plain_share_keeps_existing_path(self) -> None:
        request = composition_request("2025年12月各业态含税销售总额占比")
        rows = [
            {"渠道业态": "物流平台", "含税销售总额": 29539.0},
            {"渠道业态": "集中配送", "含税销售总额": 16820.0},
        ]
        output = AnalysisEngine().analyze(
            request, ["渠道业态", "含税销售总额"], rows, KnowledgeContext(query="")
        )
        assert output.method == "share_of_total"

    def test_topn_preview_rejected_for_pareto(self) -> None:
        request = composition_request(
            "哪些经销商贡献了80%的含税销售总额",
            operators=[AnalysisOperator.TOP_N, AnalysisOperator.SORT],
        )
        with pytest.raises(AnalysisError, match="TopN预览"):
            AnalysisEngine().analyze(
                request, ["经销商名称", "含税销售总额"], PARETO_ROWS[:10],
                KnowledgeContext(query=""),
            )


class TestTwoPeriodComparison:
    """两期对比：覆盖区间改写后按月分组返回两行，走既有 two_group_comparison。
    数值为 2025-10/11 全库实测含税销售总额。"""

    def test_adjacent_month_buckets_compare(self) -> None:
        request = CanonicalAnalysisRequest(
            conversation_id="comparison-test",
            tenant_id="tenant",
            user_id="user",
            original_question="2025年11月和10月的含税销售总额对比",
            primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
            comparison_type="期间对比",
            metrics=[MetricRef(input="含税销售总额", canonical_name="含税销售总额")],
        )
        rows = [
            {"业务日期": "2025-10", "含税销售总额": 542836197.90},
            {"业务日期": "2025-11", "含税销售总额": 143433629.90},
        ]
        output = AnalysisEngine().analyze(
            request, ["业务日期", "含税销售总额"], rows, KnowledgeContext(query="")
        )
        facts = output.facts
        assert output.method == "two_group_comparison"
        assert facts["base_label"] == "2025-10"
        assert facts["current_label"] == "2025-11"
        assert facts["absolute_change"] == pytest.approx(143433629.90 - 542836197.90)
        assert facts["change_rate"] == pytest.approx(-0.7358, abs=1e-4)


class TestDisjunctivePeriodsToRange:
    def test_two_periods_with_year_on_first(self) -> None:
        assert disjunctive_periods_to_range("2025年11月和10月") == "2025-10-01至2025-11-30"

    def test_enumerated_months(self) -> None:
        # 2026-10-19 13:57 前端事故原题的时间形态
        assert disjunctive_periods_to_range("2025年10月、11月、12月") == "2025-10-01至2025-12-31"

    def test_both_periods_with_year(self) -> None:
        assert disjunctive_periods_to_range("2025年11月和2025年10月") == "2025-10-01至2025-11-30"

    def test_date_level_periods(self) -> None:
        assert disjunctive_periods_to_range("2025年10月20日和12月15日") == "2025-10-20至2025-12-15"

    def test_no_year_refuses_to_guess(self) -> None:
        assert disjunctive_periods_to_range("11月和12月") is None

    def test_continuous_range_untouched(self) -> None:
        # “到”区间不是离散列举，交给区间解析
        assert disjunctive_periods_to_range("2025年10月到12月") is None

    def test_unparseable_segment_untouched(self) -> None:
        assert disjunctive_periods_to_range("最近3个月和10月") is None
