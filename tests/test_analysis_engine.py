from __future__ import annotations

from decimal import Decimal

import pytest

from app.analysis import AnalysisEngine, AnalysisError
from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    KnowledgeContext,
    KnowledgeDocument,
    MetricRef,
    PrimaryIntent,
)


def request(intent: PrimaryIntent, metric: str = "销售额") -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="analysis-test",
        tenant_id="tenant",
        user_id="user",
        original_question="test",
        primary_intent=intent,
        metrics=[MetricRef(input=metric, canonical_name=metric)],
    )


def test_trend_computes_change_instead_of_echoing_rows() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.TREND_ANALYSIS),
        ["月份", "销售额"],
        [{"月份": "1月", "销售额": 100}, {"月份": "2月", "销售额": 125}],
        KnowledgeContext(query=""),
    )
    assert output.method == "robust_trend_diagnostics"
    assert output.facts["change_rate"] == pytest.approx(0.25)
    assert "25.00%" in output.answer
    assert output.facts["trend_diagnostics"]["direction"] == "UP"
    assert output.facts["decision_source"] == "DETERMINISTIC_ALGORITHM"
    assert output.facts["llm_role"] == "PRESENTATION_ONLY"
    assert "时间序列能够验证的变化解释" in output.answer


def test_multidimensional_trend_aggregates_duplicate_months_for_overall_series() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.TREND_ANALYSIS, metric="销售量"),
        ["月份", "省份", "销售量"],
        [
            {"月份": "2026-01", "省份": "上海市", "销售量": 10},
            {"月份": "2026-01", "省份": "江苏省", "销售量": 20},
            {"月份": "2026-02", "省份": "上海市", "销售量": 15},
            {"月份": "2026-02", "省份": "江苏省", "销售量": 25},
        ],
        KnowledgeContext(query=""),
    )

    assert output.facts["start"] == 30
    assert output.facts["end"] == 40
    assert output.facts["multidimensional_time_aggregation"] is True
    assert output.answer.startswith("按时间汇总各业务维度后")


def test_decline_ranking_compares_each_product_first_and_last_period():
    analysis_request = request(PrimaryIntent.COMPARISON_ANALYSIS)
    analysis_request.original_question = "找出销售额下降最大的两个产品"
    analysis_request.ranking_limit = 2
    analysis_request.dimensions = ["产品"]
    output = AnalysisEngine().analyze(
        analysis_request,
        ["月份", "产品", "销售额"],
        [
            {"月份": "2026-01", "产品": "A", "销售额": 100},
            {"月份": "2026-02", "产品": "A", "销售额": 40},
            {"月份": "2026-01", "产品": "B", "销售额": 80},
            {"月份": "2026-02", "产品": "B", "销售额": 60},
            {"月份": "2026-01", "产品": "C", "销售额": 20},
            {"月份": "2026-02", "产品": "C", "销售额": 30},
        ],
        KnowledgeContext(query=""),
    )

    assert output.method == "entity_period_decline_ranking"
    assert output.facts["selected_labels"] == ["A", "B"]
    assert output.facts["rankings"][0]["change"] == -60
    assert "| 排名 | 产品 | 开始周期 | 结束周期 | 期初值 | 期末值 | 下降金额 | 变化率 |" in output.answer
    assert "| 1 | A | 2026-01 | 2026-02 | 100 | 40 | 60 | -60.00% |" in output.answer


def test_hospital_ranking_uses_hospital_name_not_level_as_object_label():
    analysis_request = request(PrimaryIntent.METRIC_QUERY, metric="含税销售总额")
    analysis_request.original_question = "查询含税销售总额排名前10的医院"
    analysis_request.dimensions = ["医院", "医院等级"]
    analysis_request.ranking_limit = 10
    analysis_request.operators = [AnalysisOperator.TOP_N, AnalysisOperator.SORT]
    output = AnalysisEngine().analyze_ranking(
        analysis_request,
        ["hospital.hospital_name", "hospital.hospital_level", "含税销售总额"],
        [
            {"hospital.hospital_name": "甲医院", "hospital.hospital_level": "三级", "含税销售总额": 100},
            {"hospital.hospital_name": "乙医院", "hospital.hospital_level": "二级", "含税销售总额": 80},
        ],
        KnowledgeContext(query=""),
    )
    assert output.facts["label_column"] == "hospital.hospital_name"
    assert output.facts["profile_columns"] == ["医院等级"]
    assert output.facts["rankings"][0]["profile"] == {"医院等级": "三级"}
    assert "| 排名 | 医院名称 | 含税销售总额 | 医院等级 |" in output.answer
    assert "| 1 | 甲医院 | 100 | 三级 |" in output.answer


def test_large_ranking_values_never_use_scientific_notation():
    analysis_request = request(PrimaryIntent.METRIC_QUERY, metric="含税销售总额")
    analysis_request.original_question = "查询累计销售额最高的经销商"
    analysis_request.dimensions = ["经销商"]
    analysis_request.ranking_limit = 1
    analysis_request.operators = [AnalysisOperator.TOP_N, AnalysisOperator.SORT]

    output = AnalysisEngine().analyze_ranking(
        analysis_request,
        ["经销商名称", "含税销售总额"],
        [{"经销商名称": "甲公司", "含税销售总额": 34071300.25}],
        KnowledgeContext(query=""),
    )

    assert "34,071,300.25" in output.answer
    assert "e+" not in output.answer.lower()


def test_trend_rejects_result_grain_that_conflicts_with_explicit_monthly_request() -> None:
    analysis_request = request(PrimaryIntent.TREND_ANALYSIS)
    analysis_request.original_question = "查询最近一段时间的销售额，按月趋势"

    with pytest.raises(AnalysisError, match="要求 month，实际 day"):
        AnalysisEngine().analyze(
            analysis_request,
            ["日期", "销售额"],
            [
                {"日期": "2025-10-17", "销售额": 100},
                {"日期": "2025-10-18", "销售额": 125},
            ],
            KnowledgeContext(query=""),
        )


def test_trend_records_verified_monthly_grain() -> None:
    analysis_request = request(PrimaryIntent.TREND_ANALYSIS)
    analysis_request.original_question = "查询销售额逐月趋势"

    output = AnalysisEngine().analyze(
        analysis_request,
        ["月份", "销售额"],
        [
            {"月份": "2025-10", "销售额": 100},
            {"月份": "2025-11", "销售额": 125},
        ],
        KnowledgeContext(query=""),
    )

    assert output.facts["requested_granularity"] == "month"
    assert output.facts["actual_granularity"] == "month"


def test_comparison_handles_zero_base_without_fake_percentage() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.COMPARISON_ANALYSIS),
        ["期间", "销售额"],
        [{"期间": "上期", "销售额": 0}, {"期间": "本期", "销售额": 10}],
        KnowledgeContext(query=""),
    )
    assert output.facts["change_rate"] is None
    assert "无法计算变化率" in output.answer


def test_comparison_calculates_multiple_metrics_and_largest_change() -> None:
    analysis_request = request(PrimaryIntent.COMPARISON_ANALYSIS)
    analysis_request.original_question = "比较销售额、订单量、客单价，并说明哪个指标变化最大"
    analysis_request.metrics = [
        MetricRef(input="销售额"),
        MetricRef(input="订单量"),
        MetricRef(input="客单价"),
    ]
    output = AnalysisEngine().analyze(
        analysis_request,
        ["月份", "销售额", "订单量", "客单价"],
        [
            {"月份": "2026-06", "销售额": 100, "订单量": 10, "客单价": 10},
            {"月份": "2026-07", "销售额": 120, "订单量": 15, "客单价": 8},
        ],
        KnowledgeContext(query=""),
    )

    assert output.method == "two_group_multi_metric_comparison"
    assert output.facts["largest_change_metric"] == "订单量"
    assert len(output.facts["comparisons"]) == 3
    assert "订单量变化最大" in output.answer


def explicit_object_request() -> CanonicalAnalysisRequest:
    analysis_request = request(PrimaryIntent.COMPARISON_ANALYSIS)
    analysis_request.original_question = "对比甲、乙、丙三家合作方的增长率与合作时长"
    analysis_request.comparison_type = "对象间比较"
    analysis_request.dimensions = ["合作方"]
    analysis_request.metrics = [
        MetricRef(input="增长率"),
        MetricRef(input="合作时长"),
    ]
    analysis_request.filters = [{
        "field": "合作方名称",
        "operator": "IN",
        "value": ["甲", "乙", "丙"],
    }]
    return analysis_request


def test_explicit_three_object_multi_metric_comparison_is_descriptive() -> None:
    output = AnalysisEngine().analyze(
        explicit_object_request(),
        ["合作方名称", "增长率", "合作时长"],
        [
            {"合作方名称": "丙", "增长率": 0.1, "合作时长": 8},
            {"合作方名称": "甲", "增长率": 0.2, "合作时长": 5},
            {"合作方名称": "乙", "增长率": -0.1, "合作时长": 12},
        ],
        KnowledgeContext(query=""),
    )

    assert output.method == "explicit_object_descriptive_comparison"
    assert output.facts["comparison_mode"] == "DESCRIPTIVE_NO_BASELINE"
    assert output.facts["requested_object_count"] == 3
    assert output.facts["returned_object_count"] == 3
    assert output.facts["missing_objects"] == []
    assert [item["label"] for item in output.facts["objects"]] == ["甲", "乙", "丙"]
    assert output.facts["metric_summaries"][0]["highest_labels"] == ["甲"]
    assert output.warnings == []


def test_explicit_object_comparison_discloses_missing_object_without_treating_as_zero() -> None:
    output = AnalysisEngine().analyze(
        explicit_object_request(),
        ["合作方名称", "增长率", "合作时长"],
        [
            {"合作方名称": "甲", "增长率": 0.2, "合作时长": 5},
            {"合作方名称": "丙", "增长率": 0.1, "合作时长": 8},
        ],
        KnowledgeContext(query=""),
    )

    assert output.facts["returned_object_count"] == 2
    assert output.facts["missing_objects"] == ["乙"]
    assert "未返回对象：乙" in output.warnings[0]
    assert "未返回不等同于指标为0" in output.warnings[0]
    assert "未返回对象" not in output.answer


def test_explicit_object_comparison_rejects_only_one_returned_object() -> None:
    with pytest.raises(AnalysisError, match="至少需要 2 个唯一对象"):
        AnalysisEngine().analyze(
            explicit_object_request(),
            ["合作方名称", "增长率", "合作时长"],
            [{"合作方名称": "甲", "增长率": 0.2, "合作时长": 5}],
            KnowledgeContext(query=""),
        )


def test_explicit_object_comparison_rejects_unrequested_rows() -> None:
    with pytest.raises(AnalysisError, match="未被用户指定的对象：丁"):
        AnalysisEngine().analyze(
            explicit_object_request(),
            ["合作方名称", "增长率", "合作时长"],
            [
                {"合作方名称": "甲", "增长率": 0.2, "合作时长": 5},
                {"合作方名称": "丁", "增长率": 0.3, "合作时长": 6},
            ],
            KnowledgeContext(query=""),
        )


def test_explicit_two_object_single_metric_does_not_invent_period_roles() -> None:
    analysis_request = explicit_object_request()
    analysis_request.metrics = [MetricRef(input="增长率")]
    analysis_request.filters[0]["value"] = ["甲", "乙"]

    output = AnalysisEngine().analyze(
        analysis_request,
        ["合作方名称", "增长率"],
        [
            {"合作方名称": "甲", "增长率": 0.2},
            {"合作方名称": "乙", "增长率": 0.1},
        ],
        KnowledgeContext(query=""),
    )

    assert output.method == "explicit_object_descriptive_comparison"
    assert "base_label" not in output.facts


def test_comparison_orders_reversed_iso_periods_before_calculation() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.COMPARISON_ANALYSIS),
        ["期间", "销售额"],
        [{"期间": "2026-02", "销售额": 120}, {"期间": "2026-01", "销售额": 100}],
        KnowledgeContext(query=""),
    )
    assert output.facts["base_label"] == "2026-01"
    assert output.facts["current_label"] == "2026-02"
    assert output.facts["change_rate"] == pytest.approx(0.2)
    assert output.facts["period_order_source"] == "time_order"


def test_comparison_rejects_two_objects_without_explicit_roles() -> None:
    with pytest.raises(AnalysisError, match="period_role"):
        AnalysisEngine().analyze(
            request(PrimaryIntent.COMPARISON_ANALYSIS),
            ["对象", "销售额"],
            [{"对象": "A", "销售额": 100}, {"对象": "B", "销售额": 120}],
            KnowledgeContext(query=""),
        )


def test_comparison_uses_explicit_period_role_even_when_rows_are_reversed() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.COMPARISON_ANALYSIS),
        ["期间", "period_role", "销售额"],
        [
            {"期间": "B", "period_role": "CURRENT", "销售额": 120},
            {"期间": "A", "period_role": "BASE", "销售额": 100},
        ],
        KnowledgeContext(query=""),
    )
    assert output.facts["base"] == 100
    assert output.facts["current"] == 120
    assert output.facts["period_order_source"] == "period_role"


def test_composition_rejects_negative_values() -> None:
    with pytest.raises(AnalysisError, match="负值"):
        AnalysisEngine().analyze(
            request(PrimaryIntent.COMPOSITION_ANALYSIS),
            ["渠道", "销售额"],
            [{"渠道": "A", "销售额": 10}, {"渠道": "B", "销售额": -2}],
            KnowledgeContext(query=""),
        )


def test_composition_calculates_share_of_total() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.COMPOSITION_ANALYSIS),
        ["渠道", "销售额"],
        [{"渠道": "A", "销售额": 30}, {"渠道": "B", "销售额": 70}],
        KnowledgeContext(query=""),
    )
    assert output.facts["total"] == 100
    assert output.facts["shares"][0]["share"] == pytest.approx(0.7)


def test_anomaly_uses_robust_candidates() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.ANOMALY_ANALYSIS),
        ["日期", "销售额"],
        [
            {"日期": "d1", "销售额": 10}, {"日期": "d2", "销售额": 10},
            {"日期": "d3", "销售额": 10}, {"日期": "d4", "销售额": 10},
            {"日期": "d5", "销售额": 100},
        ],
        KnowledgeContext(query=""),
    )
    assert output.facts["candidate_count"] == 1
    assert "统计偏离" in output.answer


def test_forecast_requires_six_points() -> None:
    with pytest.raises(AnalysisError, match="6个"):
        AnalysisEngine().analyze(
            request(PrimaryIntent.FORECAST_ANALYSIS),
            ["月份", "销售额"],
            [{"月份": str(i), "销售额": i} for i in range(5)],
            KnowledgeContext(query=""),
        )


def test_forecast_returns_baseline_and_interval() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.FORECAST_ANALYSIS),
        ["月份", "销售额"],
        [{"月份": f"2026-{i + 1:02d}", "销售额": 10 + i * 2} for i in range(6)],
        KnowledgeContext(query=""),
    )
    assert output.method == "deterministic_backtest_model_selection"
    assert output.facts["selected_model"] == "linear_drift"
    assert output.facts["forecast"] == pytest.approx(22)
    assert len(output.facts["residual_reference_interval"]) == 2
    assert output.facts["validation_mae"] == pytest.approx(0)
    assert set(output.facts["candidate_scores"]) == {
        "naive_last", "recent_mean_3", "linear_drift"
    }
    assert "正式预测应由算法模型复核" in output.answer


def test_trend_rejects_out_of_order_time_rows() -> None:
    with pytest.raises(AnalysisError, match="升序"):
        AnalysisEngine().analyze(
            request(PrimaryIntent.TREND_ANALYSIS),
            ["月份", "销售额"],
            [{"月份": "2026-02", "销售额": 20}, {"月份": "2026-01", "销售额": 10}],
            KnowledgeContext(query=""),
        )


def test_multiple_numeric_measure_columns_require_explicit_binding() -> None:
    ambiguous = request(PrimaryIntent.TREND_ANALYSIS, metric="未绑定指标")
    with pytest.raises(AnalysisError, match="多个数值指标列"):
        AnalysisEngine().analyze(
            ambiguous,
            ["月份", "销售额", "订单量"],
            [
                {"月份": "2026-01", "销售额": 10, "订单量": 2},
                {"月份": "2026-02", "销售额": 20, "订单量": 3},
            ],
            KnowledgeContext(query=""),
        )


def test_anomaly_zero_mad_does_not_flag_tiny_noise() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.ANOMALY_ANALYSIS),
        ["日期", "销售额"],
        [
            {"日期": "d1", "销售额": 100}, {"日期": "d2", "销售额": 100},
            {"日期": "d3", "销售额": 100}, {"日期": "d4", "销售额": 100},
            {"日期": "d5", "销售额": 101},
        ],
        KnowledgeContext(query=""),
    )
    assert output.facts["candidate_count"] == 0


def test_root_cause_rejects_level_values_without_contribution_column() -> None:
    with pytest.raises(AnalysisError, match="贡献值或变化量"):
        AnalysisEngine().analyze(
            request(PrimaryIntent.ROOT_CAUSE_ANALYSIS),
            ["渠道", "销售额"],
            [{"渠道": "A", "销售额": 100}, {"渠道": "B", "销售额": 50}],
            KnowledgeContext(query=""),
        )


def test_root_cause_rejects_ambiguous_contribution_columns() -> None:
    with pytest.raises(AnalysisError, match="多个贡献指标"):
        AnalysisEngine().analyze(
            request(PrimaryIntent.ROOT_CAUSE_ANALYSIS, metric="经营结果"),
            ["区域", "销售额变化", "订单量变化"],
            [
                {"区域": "华东", "销售额变化": -10, "订单量变化": -2},
                {"区域": "华南", "销售额变化": 3, "订单量变化": 1},
            ],
            KnowledgeContext(query=""),
        )


def test_comparison_rejects_more_than_two_unordered_groups() -> None:
    with pytest.raises(AnalysisError, match="仅返回"):
        AnalysisEngine().analyze(
            request(PrimaryIntent.COMPARISON_ANALYSIS),
            ["期间", "销售额"],
            [
                {"期间": "A", "销售额": 1}, {"期间": "B", "销售额": 2},
                {"期间": "C", "销售额": 3},
            ],
            KnowledgeContext(query=""),
        )


def test_unbounded_entity_metric_sort_is_validated_as_ranking_not_period_comparison() -> None:
    analysis_request = request(PrimaryIntent.COMPARISON_ANALYSIS, "整体业务规模")
    analysis_request.original_question = "列出经销商并按整体业务规模排序"
    analysis_request.comparison_type = "对象间比较"
    analysis_request.dimensions = ["经销商"]
    analysis_request.operators = [
        AnalysisOperator.COMPARE,
        AnalysisOperator.GROUP_BY,
        AnalysisOperator.SORT,
    ]
    rows = [
        {"经销商名称": f"经销商{index}", "整体业务规模": 900 - index * 100}
        for index in range(8)
    ]

    output = AnalysisEngine().analyze(
        analysis_request,
        ["经销商名称", "整体业务规模"],
        rows,
        KnowledgeContext(query=""),
    )

    assert output.method == "validated_ordered_metric_ranking"
    assert output.facts["ranking_mode"] == "FULL_ORDERED_SET"
    assert output.facts["ranking_limit"] is None
    assert output.facts["returned_object_count"] == 8
    assert [item["label"] for item in output.facts["rankings"]] == [
        f"经销商{index}" for index in range(8)
    ]
    assert "base_label" not in output.facts


def test_dealer_recommendation_ranks_by_primary_metric_with_profile_columns() -> None:
    analysis_request = request(
        PrimaryIntent.COMPARISON_ANALYSIS, "经销商近一年销售额"
    )
    analysis_request.original_question = "按适用科室推荐前3家经销商"
    analysis_request.comparison_type = "对象间比较"
    analysis_request.dimensions = ["经销商"]
    analysis_request.metrics = [
        MetricRef(input="经销商近一年销售额"),
        MetricRef(input="近三月业绩增长率"),
        MetricRef(input="合作时长"),
        MetricRef(input="合作次数"),
    ]
    analysis_request.operators = [
        AnalysisOperator.FILTER,
        AnalysisOperator.GROUP_BY,
        AnalysisOperator.COMPARE,
        AnalysisOperator.SORT,
        AnalysisOperator.TOP_N,
    ]
    analysis_request.ranking_limit = 3
    analysis_request.assumptions = [
        "DEALER_RECOMMENDATION_DEFAULT_RANKING=经销商近一年销售额"
    ]
    rows = [
        {
            "经销商名称": "甲", "经销商近一年销售额": 300,
            "近三月业绩增长率": 0.2, "合作时长": 24, "合作次数": 8,
        },
        {
            "经销商名称": "乙", "经销商近一年销售额": 200,
            "近三月业绩增长率": 0.3, "合作时长": 18, "合作次数": 6,
        },
        {
            "经销商名称": "丙", "经销商近一年销售额": 100,
            "近三月业绩增长率": -0.1, "合作时长": 12, "合作次数": 4,
        },
    ]

    output = AnalysisEngine().analyze_ranking(
        analysis_request,
        list(rows[0]),
        rows,
        KnowledgeContext(query=""),
    )

    assert output.method == "validated_top_n_ranking"
    assert output.facts["metric_column"] == "经销商近一年销售额"
    assert output.facts["returned_object_count"] == 3
    assert [item["label"] for item in output.facts["rankings"]] == ["甲", "乙", "丙"]
    assert output.facts["profile_columns"] == [
        "近三月业绩增长率", "合作时长", "合作次数",
    ]
    assert output.facts["rankings"][0]["profile"] == {
        "近三月业绩增长率": 0.2,
        "合作时长": 24,
        "合作次数": 8,
    }
    assert "| 排名 | 经销商名称 | 经销商近一年销售额 | 近三月业绩增长率 | 合作时长 | 合作次数 |" in output.answer
    assert "| 1 | 甲 | 300 | 0.2 | 24 | 8 |" in output.answer


def test_bounded_ranking_discloses_when_fewer_objects_qualify() -> None:
    analysis_request = request(
        PrimaryIntent.COMPARISON_ANALYSIS, "经销商近一年销售额"
    )
    analysis_request.dimensions = ["经销商"]
    analysis_request.metrics = [MetricRef(input="经销商近一年销售额")]
    analysis_request.operators = [
        AnalysisOperator.GROUP_BY,
        AnalysisOperator.SORT,
        AnalysisOperator.TOP_N,
    ]
    analysis_request.ranking_limit = 3
    rows = [{"经销商名称": "唯一符合条件公司", "经销商近一年销售额": 300}]

    output = AnalysisEngine().analyze_ranking(
        analysis_request,
        list(rows[0]),
        rows,
        KnowledgeContext(query=""),
    )

    assert output.facts["requested_object_count"] == 3
    assert output.facts["ranking_shortfall"] == 2
    assert "仅有 1 个对象" in output.answer
    assert "| 1 | 唯一符合条件公司 | 300 |" in output.answer
    assert "不足请求的前 3 名" in output.answer


def test_dealer_ranking_prefers_name_over_prefixed_location_attributes() -> None:
    analysis_request = request(
        PrimaryIntent.COMPARISON_ANALYSIS, "经销商近一年销售额"
    )
    analysis_request.original_question = "限定上海和心血管内科，筛选经销商并展示TOP3画像"
    analysis_request.comparison_type = "对象间比较"
    analysis_request.dimensions = ["经销商"]
    analysis_request.metrics = [
        MetricRef(input="经销商近一年销售额"),
        MetricRef(input="近三月业绩增长率"),
        MetricRef(input="合作时长"),
        MetricRef(input="合作次数"),
    ]
    analysis_request.operators = [
        AnalysisOperator.FILTER,
        AnalysisOperator.GROUP_BY,
        AnalysisOperator.COMPARE,
        AnalysisOperator.SORT,
        AnalysisOperator.TOP_N,
    ]
    analysis_request.ranking_limit = 3
    rows = [
        {
            "dealer.dealer_name": "甲",
            "dealer.province": "上海市",
            "dealer.city": "上海市",
            "经销商近一年销售额": 300,
            "近三月业绩增长率": 0.2,
            "合作时长": 24,
            "合作次数": 8,
        },
        {
            "dealer.dealer_name": "乙",
            "dealer.province": "上海市",
            "dealer.city": "上海市",
            "经销商近一年销售额": 200,
            "近三月业绩增长率": 0.1,
            "合作时长": 18,
            "合作次数": 6,
        },
        {
            "dealer.dealer_name": "丙",
            "dealer.province": "上海市",
            "dealer.city": "上海市",
            "经销商近一年销售额": 100,
            "近三月业绩增长率": -0.1,
            "合作时长": 12,
            "合作次数": 4,
        },
    ]

    output = AnalysisEngine().analyze_ranking(
        analysis_request,
        list(rows[0]),
        rows,
        KnowledgeContext(query=""),
    )

    assert output.facts["label_column"] == "dealer.dealer_name"
    assert [item["label"] for item in output.facts["rankings"]] == ["甲", "乙", "丙"]


def test_unbounded_entity_metric_sort_rejects_unsorted_rows() -> None:
    analysis_request = request(PrimaryIntent.COMPARISON_ANALYSIS, "整体业务规模")
    analysis_request.original_question = "按整体业务规模排序经销商"
    analysis_request.comparison_type = "对象间比较"
    analysis_request.dimensions = ["经销商"]
    analysis_request.operators = [
        AnalysisOperator.COMPARE,
        AnalysisOperator.GROUP_BY,
        AnalysisOperator.SORT,
    ]

    with pytest.raises(AnalysisError, match="结果顺序不正确"):
        AnalysisEngine().analyze(
            analysis_request,
            ["经销商名称", "整体业务规模"],
            [
                {"经销商名称": "甲", "整体业务规模": 10},
                {"经销商名称": "乙", "整体业务规模": 20},
                {"经销商名称": "丙", "整体业务规模": 5},
            ],
            KnowledgeContext(query=""),
        )


def test_sort_operator_does_not_bypass_period_comparison_two_row_gate() -> None:
    analysis_request = request(PrimaryIntent.COMPARISON_ANALYSIS)
    analysis_request.comparison_type = "指定时段对比"
    analysis_request.dimensions = ["月份"]
    analysis_request.operators = [
        AnalysisOperator.COMPARE,
        AnalysisOperator.GROUP_BY,
        AnalysisOperator.SORT,
    ]

    with pytest.raises(AnalysisError, match="仅返回基期与对比期两行"):
        AnalysisEngine().analyze(
            analysis_request,
            ["月份", "销售额"],
            [
                {"月份": "2026-01", "销售额": 10},
                {"月份": "2026-02", "销售额": 20},
                {"月份": "2026-03", "销售额": 30},
            ],
            KnowledgeContext(query=""),
        )


def test_root_cause_never_claims_causality_from_knowledge_text() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.ROOT_CAUSE_ANALYSIS),
        ["渠道", "销售额变化"],
        [{"渠道": "A", "销售额变化": -20}, {"渠道": "B", "销售额变化": -5}],
        KnowledgeContext(
            query="",
            documents=[KnowledgeDocument(content="促销结束可能影响销量", source="手册")],
        ),
    )
    assert output.facts["causality_established"] is False
    assert "不足以证明因果" in output.answer
    assert "尚未被当前数据验证" in output.answer


def test_non_numeric_metric_is_rejected_not_silently_dropped() -> None:
    with pytest.raises(AnalysisError, match="数值"):
        AnalysisEngine().analyze(
            request(PrimaryIntent.TREND_ANALYSIS),
            ["月份", "销售额"],
            [{"月份": "1月", "销售额": "未知"}, {"月份": "2月", "销售额": "10"}],
            KnowledgeContext(query=""),
        )


def test_data_quality_reports_nulls_and_duplicates() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.DATA_QUALITY),
        ["订单号", "金额"],
        [
            {"订单号": "1", "金额": None},
            {"订单号": "1", "金额": None},
            {"订单号": "2", "金额": 10},
        ],
        KnowledgeContext(query=""),
    )
    assert output.facts["duplicate_row_count"] == 1
    assert output.facts["null_counts"]["金额"] == 2


def test_empty_data_quality_scope_is_not_reported_as_failure_or_pass() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.DATA_QUALITY),
        ["订单号", "金额"],
        [],
        KnowledgeContext(query=""),
    )

    assert output.method == "empty_dataset_quality_assessment"
    assert output.facts["quality_conclusion"] == "NOT_ASSESSED"
    assert output.facts["empty_scope"] is True
    assert "不代表数据质量通过" in output.answer


def test_trend_reports_pattern_and_largest_period_change() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.TREND_ANALYSIS),
        ["月份", "销售额"],
        [
            {"月份": "2026-01", "销售额": 100},
            {"月份": "2026-02", "销售额": 80},
            {"月份": "2026-03", "销售额": 90},
        ],
        KnowledgeContext(query=""),
    )
    assert output.facts["pattern"] == "存在波动"
    assert output.facts["largest_drop"]["absolute_change"] == -20
    assert output.facts["largest_rise"]["absolute_change"] == 10
    assert output.facts["change_points"] == [
        {"label": "2026-02", "before_change": -20.0, "after_change": 10.0, "type": "TROUGH"}
    ]
    assert output.facts["temporal_continuity"]["is_complete"] is True


def test_trend_discloses_missing_time_buckets():
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.TREND_ANALYSIS),
        ["月份", "销售额"],
        [
            {"月份": "2026-01", "销售额": 100},
            {"月份": "2026-03", "销售额": 120},
            {"月份": "2026-04", "销售额": 130},
        ],
        KnowledgeContext(query=""),
    )

    assert output.facts["temporal_continuity"]["grain"] == "month"
    assert output.facts["temporal_continuity"]["is_complete"] is False
    assert output.facts["temporal_continuity"]["gaps"][0]["intervals"] == 2
    assert any("不连续" in warning for warning in output.warnings)


def test_root_cause_reconciles_contributions_and_matches_knowledge() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.ROOT_CAUSE_ANALYSIS),
        ["区域", "销售额变化", "整体变化"],
        [
            {"区域": "华东区域", "销售额变化": -80, "整体变化": -50},
            {"区域": "华南区域", "销售额变化": 30, "整体变化": -50},
        ],
        KnowledgeContext(
            query="",
            documents=[
                KnowledgeDocument(content="华东区域促销结束，需核实影响。", source="运营记录"),
                KnowledgeDocument(content="天气变化是一般背景。", source="业务手册"),
            ],
        ),
    )
    assert output.facts["contribution_sum"] == -50
    assert output.facts["observed_change"] == -50
    assert output.facts["residual"] == 0
    assert output.facts["coverage"] == pytest.approx(1.0)
    assert output.facts["additivity_check"]["status"] == "passed"
    assert output.facts["ranked_candidates"][0]["contribution_pct_of_total_delta"] == pytest.approx(1.6)
    assert output.facts["matched_knowledge"][0]["matched_labels"] == ["华东区域"]
    assert len(output.facts["unmatched_knowledge_candidates"]) == 1


def test_root_cause_blocks_percentage_interpretation_for_offsetting_zero_change():
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.ROOT_CAUSE_ANALYSIS),
        ["渠道", "销售额变化", "整体变化"],
        [
            {"渠道": "线上", "销售额变化": 20, "整体变化": 0},
            {"渠道": "线下", "销售额变化": -20, "整体变化": 0},
        ],
        KnowledgeContext(query=""),
    )

    assert output.facts["additivity_check"]["status"] == "passed"
    assert all(
        item["contribution_pct_of_total_delta"] is None
        for item in output.facts["ranked_candidates"]
    )
    assert any("相互抵消" in warning for warning in output.warnings)


def test_root_cause_marks_non_additive_breakdown_as_failed():
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.ROOT_CAUSE_ANALYSIS),
        ["区域", "销售额变化", "整体变化"],
        [
            {"区域": "华东", "销售额变化": -20, "整体变化": -100},
            {"区域": "华南", "销售额变化": 10, "整体变化": -100},
        ],
        KnowledgeContext(query=""),
    )

    assert output.facts["additivity_check"]["status"] == "failed"
    assert any("禁止" in warning for warning in output.warnings)


def test_root_cause_does_not_match_single_character_group_labels_in_prose() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.ROOT_CAUSE_ANALYSIS),
        ["渠道", "销售额变化"],
        [{"渠道": "A", "销售额变化": -10}, {"渠道": "B", "销售额变化": 3}],
        KnowledgeContext(
            query="",
            documents=[KnowledgeDocument(content="A/B测试只是背景资料。", source="手册")],
        ),
    )
    assert output.facts["matched_knowledge"] == []


def test_decimal_conversion_preserves_exact_value() -> None:
    value = Decimal("12345678901234567890.123456789")
    assert AnalysisEngine._number(value) == value


def test_iso_week_labels_are_ordered_for_trend() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.TREND_ANALYSIS),
        ["周", "销售额"],
        [{"周": "2026-W01", "销售额": 10}, {"周": "2026-W02", "销售额": 12}],
        KnowledgeContext(query=""),
    )
    assert output.facts["absolute_change"] == 2


@pytest.mark.parametrize("value", ["2026-13", "2026/00", "13月", "0月"])
def test_invalid_month_labels_are_rejected(value: str) -> None:
    assert AnalysisEngine._time_key(value) is None


def test_constant_forecast_does_not_report_perfect_r_squared() -> None:
    output = AnalysisEngine().analyze(
        request(PrimaryIntent.FORECAST_ANALYSIS),
        ["月份", "销售额"],
        [{"月份": f"2026-{index:02d}", "销售额": 10} for index in range(1, 7)],
        KnowledgeContext(query=""),
    )
    assert output.facts["r_squared"] is None
    assert "R²不适用" in output.answer
    assert any("常数" in warning for warning in output.warnings)


def test_group_analysis_rejects_unlabelled_single_column_rows() -> None:
    with pytest.raises(AnalysisError, match="标签列"):
        AnalysisEngine().analyze(
            request(PrimaryIntent.COMPOSITION_ANALYSIS),
            ["销售额"],
            [{"销售额": 30}, {"销售额": 70}],
            KnowledgeContext(query=""),
        )
