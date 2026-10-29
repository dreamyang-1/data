"""最终输出增强：洞察驱动的业务化总结与自动图表建议。

覆盖三层契约：
- synthesis 只透传结构化图表建议，列名缺失或槽位不完整的建议被清洗丢弃；
- suggested_chart_specs 逐条按真实结果校验，虚构列名/非数值指标/明细数据
  一律拒绝渲染，绝不虚构图片；
- 洞察建议仅作为用户显式图型之后的次优来源，且总量受 3 张上限约束。
"""
from __future__ import annotations

from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.analysis.visualization import suggested_chart_specs, MAX_CHART_POINTS
from app.presentation.root_report import apply_insight_root_charts
from app.presentation.summary import brief_summary


COLUMNS = ["经销商名称", "月份", "销售额", "经销商编码"]
ROWS = [
    {"经销商名称": f"经销商{index}", "月份": f"2025-{index:02d}", "销售额": 100 + index, "经销商编码": f"D{index:03d}"}
    for index in range(1, 6)
]


def suggestion(**changes):
    entry = {"chart_type": "BAR", "label_column": "经销商名称", "value_column": "销售额", "title": "各经销商销售额对比"}
    entry.update(changes)
    return {key: value for key, value in entry.items() if value is not None}


class TestSuggestedChartSpecs:
    def test_valid_bar_suggestion_grounded_in_dataset(self):
        specs, notes = suggested_chart_specs(COLUMNS, ROWS, [suggestion()])
        assert len(specs) == 1 and not notes
        assert specs[0].chart_type == "BAR"
        assert specs[0].x_field == "经销商名称" and specs[0].y_fields == ["销售额"]
        assert specs[0].title == "各经销商销售额对比"
        assert [row["销售额"] for row in specs[0].data] == [row["销售额"] for row in ROWS]

    def test_fictional_value_column_is_dropped(self):
        specs, notes = suggested_chart_specs(COLUMNS, ROWS, [suggestion(value_column="利润额")])
        assert specs == []
        assert any("利润额" in note for note in notes)

    def test_non_numeric_value_column_is_dropped(self):
        specs, notes = suggested_chart_specs(COLUMNS, ROWS, [suggestion(value_column="月份")])
        assert specs == []

    def test_identifier_label_is_dropped(self):
        specs, notes = suggested_chart_specs(COLUMNS, ROWS, [suggestion(label_column="经销商编码")])
        assert specs == []

    def test_label_equal_to_value_is_dropped(self):
        specs, _ = suggested_chart_specs(COLUMNS, ROWS, [suggestion(label_column="销售额")])
        assert specs == []

    def test_missing_columns_are_rejected_without_specs(self):
        specs, notes = suggested_chart_specs(COLUMNS, ROWS, [suggestion(label_column="不存在维度")])
        assert specs == []
        assert any("不存在维度" in note for note in notes)

    def test_pie_with_more_than_eight_categories_downgrades_to_bar(self):
        many = [{"经销商名称": f"经销商{index}", "销售额": index} for index in range(9)]
        specs, notes = suggested_chart_specs(["经销商名称", "销售额"], many, [suggestion(chart_type="PIE")])
        assert len(specs) == 1 and specs[0].chart_type == "BAR"
        assert any("柱状图" in note for note in notes)

    def test_detail_rows_beyond_chart_points_are_refused(self):
        rows = [{"经销商名称": f"经销商{index}", "销售额": index} for index in range(MAX_CHART_POINTS + 1)]
        specs, notes = suggested_chart_specs(["经销商名称", "销售额"], rows, [suggestion()])
        assert specs == []
        assert any("明细数据" in note for note in notes)

    def test_unknown_chart_type_defaults_by_data_shape(self):
        temporal_rows = [{"月份": f"2025-{index:02d}", "销售额": 100 + index} for index in range(1, 7)]
        specs, _ = suggested_chart_specs(["月份", "销售额"], temporal_rows,
                                         [suggestion(chart_type="", label_column="月份")])
        assert specs[0].chart_type == "LINE"
        specs, _ = suggested_chart_specs(COLUMNS, ROWS, [suggestion(chart_type="面积图")])
        assert specs[0].chart_type == "BAR"

    def test_line_with_repeated_keys_is_refused(self):
        duplicated = [{"月份": "2025-01", "销售额": 1}, {"月份": "2025-01", "销售额": 2}]
        specs, notes = suggested_chart_specs(["月份", "销售额"], duplicated,
                                             [suggestion(chart_type="LINE", label_column="月份")])
        assert specs == []
        assert any("折线" in note for note in notes)

    def test_missing_title_gets_metric_fallback(self):
        specs, _ = suggested_chart_specs(COLUMNS, ROWS, [suggestion(title=None)])
        assert specs[0].title == "销售额概览"

    def test_empty_or_malformed_suggestions_are_ignored(self):
        assert suggested_chart_specs(COLUMNS, ROWS, None) == ([], [])
        assert suggested_chart_specs(COLUMNS, ROWS, []) == ([], [])
        specs, _ = suggested_chart_specs(COLUMNS, ROWS, ["柱状图", 42, suggestion()])
        assert len(specs) == 1

    def test_total_specifications_are_capped_at_three(self):
        entries = [suggestion(title=f"图{index}") for index in range(5)]
        specs, _ = suggested_chart_specs(COLUMNS, ROWS, entries)
        assert len(specs) == 3


class TestParseReportChartSuggestions:
    def parse(self, final_answer):
        content = __import__("json").dumps({"claims": [{"statement": "分析正文"}], "final_answer": final_answer},
                                           ensure_ascii=False)
        return QwenAnalysisSynthesizer._parse_report(content, set()).final_answer

    def test_valid_suggestions_are_kept_and_normalized(self):
        final = self.parse({"chart_suggestions": [
            {"chart_type": " BAR ", "label_column": " 经销商名称 ", "value_column": "销售额", "title": "对比", "task_id": "t2"},
        ]})
        assert final["chart_suggestions"] == [
            {"chart_type": "BAR", "label_column": "经销商名称", "value_column": "销售额", "title": "对比", "task_id": "t2"}
        ]

    def test_incomplete_entries_are_dropped(self):
        final = self.parse({"chart_suggestions": [
            "柱状图",
            {"label_column": "经销商名称"},
            {"value_column": "销售额"},
            {"chart_type": "BAR", "label_column": "", "value_column": "销售额"},
        ]})
        assert "chart_suggestions" not in final

    def test_extra_fields_and_overlength_are_trimmed(self):
        entries = [suggestion(title=f"图{index}", extra="注入") for index in range(5)]
        final = self.parse({"chart_suggestions": entries})
        kept = final["chart_suggestions"]
        assert len(kept) == 3 and all(set(item) <= {"chart_type", "label_column", "value_column", "title"} for item in kept)


class TestBusinessOverviewLength:
    def test_relaxed_limits_keep_more_business_sentences(self):
        overview = "。" .join(f"第{index}句围绕业务层面总结本次查询结果的关键差异、结构特点和值得关注的业务含义，并结合原始问题说明数据口径与覆盖范围" for index in range(1, 7)) + "。"
        assert len(brief_summary(overview)) <= 240
        relaxed = brief_summary(overview, limit=400, max_sentences=5)
        assert len(relaxed) > 240
        assert relaxed.count("。") == 5

    def test_default_stays_compact(self):
        overview = "。" .join(f"第{index}句" for index in range(1, 6)) + "。"
        assert brief_summary(overview).count("。") == 3


class TestApplyInsightRootCharts:
    def material(self, task_id="t1", columns=COLUMNS, rows=ROWS):
        return {
            "task_id": task_id, "status": "COMPLETED",
            "question": "各经销商销售额", "depends_on": [],
            "facts": {"query_data": {"columns": columns, "rows": rows}},
            "presentation": {"table": "| 表 |", "chart": "", "notes": []},
        }

    @staticmethod
    def render(*, chart_specs):
        return [f"![图表]({chart_specs[0]['title']}.svg)"] if chart_specs else []

    def test_suggestion_is_rendered_into_presentation(self):
        material = self.material()
        specs = apply_insight_root_charts("问题", [material], {"t1"}, [suggestion()], self.render)
        assert len(specs) == 1
        assert "图表" in material["presentation"]["chart"]

    def test_task_id_filters_matched_materials(self):
        first, second = self.material("t1"), self.material("t2")
        specs = apply_insight_root_charts("问题", [first, second], {"t1", "t2"},
                                          [suggestion(task_id="t2")], self.render)
        assert len(specs) == 1
        assert first["presentation"]["chart"] == ""
        assert "图表" in second["presentation"]["chart"]

    def test_task_id_less_suggestion_requires_single_selection(self):
        single = apply_insight_root_charts("问题", [self.material()], {"t1"}, [suggestion()], self.render)
        assert len(single) == 1
        first, second = self.material("t1"), self.material("t2")
        both = apply_insight_root_charts("问题", [first, second], {"t1", "t2"}, [suggestion()], self.render)
        assert both == []
        assert first["presentation"]["chart"] == "" and second["presentation"]["chart"] == ""

    def test_suggestions_for_unselected_tasks_are_ignored(self):
        material = self.material("t1")
        specs = apply_insight_root_charts("问题", [material], {"t1"}, [suggestion(task_id="t9")], self.render)
        assert specs == [] and material["presentation"]["chart"] == ""

    def test_total_is_capped_at_three_across_materials(self):
        materials = [self.material("t1"), self.material("t2"), self.material("t3")]
        specs = apply_insight_root_charts("问题", materials, {"t1", "t2", "t3"},
                                          [suggestion(task_id=f"t{index}") for index in (1, 2, 3, 4)],
                                          self.render)
        assert len(specs) == 3

    def test_invalid_suggestion_only_leaves_a_note(self):
        material = self.material()
        specs = apply_insight_root_charts("问题", [material], {"t1"}, [suggestion(value_column="利润额")], self.render)
        assert specs == [] and material["presentation"]["chart"] == ""
        assert any("利润额" in note for note in material["presentation"]["notes"])
