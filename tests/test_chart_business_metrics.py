"""Chart grounding and visible business values, independently of column order."""
from copy import deepcopy
from decimal import Decimal
import xml.etree.ElementTree as ET

import pytest

from app.analysis import AnalysisEngine
from app.analysis.visualization import build_chart_specs, is_identifier_column, render_chart_svg
from app.domain.models import (
    CanonicalAnalysisRequest, ChatRequest, ExtensionExecution, KnowledgeContext,
    McpConfig, MetricRef, PrimaryIntent, ToolConfig,
)
from app.services.extension_dispatcher import ExtensionDispatcher

NS = {"svg": "http://www.w3.org/2000/svg"}


def svg(spec):
    payload = render_chart_svg(spec)
    assert payload is not None
    return ET.fromstring(payload)


def labels(root):
    return [node.text for node in root.findall("svg:text[@class='data-label']", NS)]


def chart(kind="BAR", metric="订单笔数", rows=None, **extra):
    return dict(chart_type=kind, title=f"{metric}对比", x_field="名称",
                y_fields=[metric], data=rows or [{"名称": "甲", metric: 35206}, {"名称": "乙", metric: 3762}], **extra)


@pytest.mark.parametrize("facts", [
    {"metric_column": "订单笔数"}, {"metric_columns": ["订单笔数"]},
    {"comparisons": [{"metric_column": "订单笔数"}]},
    {"metric_summaries": [{"metric_column": "订单笔数"}]}, {},
])
@pytest.mark.parametrize("code", [310000, "310000"])
def test_province_codes_never_become_business_measure(facts, code):
    rows = [{"省份名称": "上海市", "省份（编码）": code, "订单笔数": 35206},
            {"省份名称": "江苏省", "省份（编码）": 320000, "订单笔数": 3762}]
    before = deepcopy(rows)
    spec = build_chart_specs(PrimaryIntent.COMPARISON_ANALYSIS, list(rows[0]), rows, facts)[0]
    assert spec.y_fields == ["订单笔数"]
    assert spec.x_field == "省份名称"
    assert spec.title == "订单笔数对比"
    assert [row["订单笔数"] for row in spec.data] == [35206, 3762]
    assert labels(svg(spec)) == ["35,206", "3,762"]
    assert rows == before


@pytest.mark.parametrize("column", ["省份（编码）", "经销商编号", "province_id", "order_key",
                                    "product.code", "regionCode", "UUID", "排名", "订单号", "行政区代码"])
def test_identity_names_are_not_measures(column):
    assert is_identifier_column(column)
    rows = [{"名称": "甲", column: 123}, {"名称": "乙", column: 124}]
    assert not build_chart_specs(PrimaryIntent.COMPARISON_ANALYSIS, list(rows[0]), rows, {})
    assert render_chart_svg(chart(metric=column, rows=rows)) is None
    assert ExtensionDispatcher._visualization_call(chart(metric=column, rows=rows), {"generate_column_chart"}) is None


@pytest.mark.parametrize("column", ["订单笔数", "已合作医院数", "医院编码数量", "code_count", "paid_amount", "流动性"])
def test_business_counters_are_not_rejected_as_identity(column):
    assert not is_identifier_column(column)


@pytest.mark.parametrize("intent", [PrimaryIntent.TREND_ANALYSIS, PrimaryIntent.FORECAST_ANALYSIS,
                                    PrimaryIntent.COMPARISON_ANALYSIS, PrimaryIntent.COMPOSITION_ANALYSIS,
                                    PrimaryIntent.ANOMALY_ANALYSIS, PrimaryIntent.ROOT_CAUSE_ANALYSIS,
                                    PrimaryIntent.REPORT_GENERATION])
def test_measure_selection_protects_all_generated_chart_types(intent):
    rows = [{"业务日期": "2025-10", "省份（编码）": 310000, "订单笔数": 35206},
            {"业务日期": "2025-11", "省份（编码）": 320000, "订单笔数": 3762}]
    spec = build_chart_specs(intent, list(rows[0]), rows, {"metric_columns": ["订单笔数"]})[0]
    assert spec.y_fields == ["订单笔数"]
    assert spec.x_field == "业务日期"


def test_multiple_metrics_use_separate_charts_without_mixing_units():
    rows = [{"名称": "甲", "编号": 1, "金额": 12, "数量": 3},
            {"名称": "乙", "编号": 2, "金额": 18, "数量": 4}]
    specs = build_chart_specs(PrimaryIntent.COMPARISON_ANALYSIS, list(rows[0]), rows,
                              {"metric_columns": ["金额", "数量"], "label_column": "名称"})
    assert [spec.y_fields for spec in specs] == [["金额"], ["数量"]]
    assert not build_chart_specs(PrimaryIntent.COMPARISON_ANALYSIS, list(rows[0]), rows, {})
    assert not build_chart_specs(PrimaryIntent.COMPARISON_ANALYSIS, list(rows[0]), rows,
                                  {"metric_column": "未返回的指标"})
    unsupported = chart()
    unsupported["y_fields"] = ["金额", "数量"]
    assert render_chart_svg(unsupported) is None
    assert ExtensionDispatcher._visualization_call(unsupported, {"generate_column_chart"}) is None


@pytest.mark.parametrize("kind", ["BAR", "LINE", "SCATTER"])
def test_visible_labels_include_zero_negative_and_exact_decimal(kind):
    rows = [{"名称": 1, "订单笔数": 0}, {"名称": 2, "订单笔数": -3762},
            {"名称": 100, "订单笔数": Decimal("38080291.829")}]
    assert labels(svg(chart(kind=kind, rows=rows))) == ["0", "-3,762", "38,080,291.829"]


def test_horizontal_negative_bars_extend_left_of_zero():
    root = svg(chart(rows=[{"名称": "甲", "订单笔数": -10}, {"名称": "乙", "订单笔数": 20}], horizontal=True))
    baseline = float(root.find("svg:line", NS).get("x1"))
    bars = [node for node in root.findall("svg:rect", NS) if node.find("svg:title", NS) is not None]
    assert float(bars[0].get("x")) < baseline
    assert float(bars[1].get("x")) == pytest.approx(baseline, abs=.1)
    assert labels(root) == ["-10", "20"]


def test_scatter_uses_actual_numerical_x_distances_not_row_order():
    root = svg(chart(kind="SCATTER", rows=[{"名称": x, "订单笔数": 10} for x in [1, 2, 100]]))
    positions = [float(node.get("cx")) for node in root.findall("svg:circle", NS)]
    assert (positions[2] - positions[1]) / (positions[1] - positions[0]) == pytest.approx(98, rel=.02)
    assert render_chart_svg({**chart(kind="SCATTER"), "x_field": "省份（编码）"}) is None


def test_bar_renderer_does_not_silently_drop_points_after_thirty():
    rows = [{"名称": f"科室{i}", "订单笔数": i} for i in range(200)]
    spec = build_chart_specs(PrimaryIntent.COMPARISON_ANALYSIS, list(rows[0]), rows, {})[0]
    root = svg(spec)
    assert len(labels(root)) == 200
    assert labels(root)[-1] == "199"
    assert spec.horizontal


def test_pie_has_exact_values_and_never_hides_negative_or_extra_categories():
    root = svg(chart(kind="PIE", rows=[{"名称": "甲", "订单笔数": 60}, {"名称": "乙", "订单笔数": 40}]))
    text = " ".join(node.text or "" for node in root.findall("svg:text", NS))
    assert "甲 60.0%（60）" in text and "乙 40.0%（40）" in text
    for rows in [[{"名称": "甲", "订单笔数": -1}, {"名称": "乙", "订单笔数": 5}],
                 [{"名称": str(i), "订单笔数": i + 1} for i in range(9)]]:
        assert render_chart_svg(chart(kind="PIE", rows=rows)) is None
        spec = build_chart_specs(PrimaryIntent.COMPOSITION_ANALYSIS, list(rows[0]), rows,
                                 {"metric_columns": ["订单笔数"], "label_column": "名称"})[0]
        assert spec.chart_type == "BAR"
        assert len(spec.data) == len(rows)
        assert ExtensionDispatcher._visualization_call(chart(kind="PIE", rows=rows), {"generate_pie_chart"}) is None


def test_percent_conversion_is_consistent_across_local_and_mcp_rendering():
    spec = chart(metric="覆盖率", rows=[{"名称": "甲", "覆盖率": "40.5%"}, {"名称": "乙", "覆盖率": .5}])
    assert labels(svg(spec)) == ["40.5%", "0.5"]
    prepared = ExtensionDispatcher._visualization_call(spec, {"generate_column_chart"})
    assert [row["value"] for row in prepared[1]["data"]] == [.405, .5]


def test_pie_single_nonzero_category_is_a_full_circle_and_keeps_zero_legend():
    root = svg(chart(kind="PIE", rows=[{"名称": "甲", "订单笔数": 5}, {"名称": "乙", "订单笔数": 0}]))
    assert root.find("svg:circle", NS) is not None
    text = " ".join(node.text or "" for node in root.findall("svg:text", NS))
    assert "甲 100.0%（5）" in text and "乙 0.0%（0）" in text
    assert render_chart_svg(chart(kind="PIE", rows=[{"名称": "甲", "订单笔数": 0}])) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "Infinity", True])
def test_invalid_numeric_values_are_never_plotted(value):
    spec = chart(rows=[{"名称": "甲", "订单笔数": value}, {"名称": "乙", "订单笔数": 3}])
    assert labels(svg(spec)) == ["3"]
    assert ExtensionDispatcher._visualization_call(spec, {"generate_column_chart"})[1]["data"] == [{"category": "乙", "value": 3}]


def test_labels_and_categories_are_inert_svg_text():
    root = svg(chart(rows=[{"名称": '<script>alert("x")</script>', "订单笔数": 2}]))
    assert root.find("svg:script", NS) is None
    assert any((node.text or "").startswith('<script>alert("x")</script>') for node in root.findall(".//svg:title", NS))


@pytest.mark.parametrize(("question", "label", "code", "names", "metric", "values"), [
    ("对比下2025年四季度上海地区和江苏地区的销售订单数量", "省份名称", "省份（编码）", ["上海市", "江苏省"], "订单笔数", [35206, 3762]),
    ("对比上海市和北京市的含税销售总额", "省份名称", "省份（编码）", ["上海市", "北京市"], "含税销售总额", [Decimal("123456.789"), Decimal("54321.234")]),
    ("对比经销商甲和经销商乙的已合作医院数", "经销商名称", "经销商（编码）", ["经销商甲", "经销商乙"], "已合作医院数", [49, 30]),
    ("对比医院甲和医院乙的区域医院覆盖率", "医院名称", "医院（编码）", ["医院甲", "医院乙"], "区域医院覆盖率", ["40.5%", "24.79%"]),
])
def test_original_question_and_three_related_questions_through_analysis_engine(question, label, code, names, metric, values):
    request = CanonicalAnalysisRequest(
        conversation_id="business-chart-tests", tenant_id="tenant", user_id="user",
        original_question=question, primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
        comparison_type="对象间比较", metrics=[MetricRef(input=metric)], dimensions=[label],
        filters=[{"field": label, "operator": "IN", "value": names}],
    )
    rows = [{label: name, code: 310000 + i * 10000, metric: values[i]} for i, name in enumerate(names)]
    output = AnalysisEngine().analyze(request, list(rows[0]), rows, KnowledgeContext(query=""))
    spec = output.facts["chart_specs"][0]
    assert spec["y_fields"] == [metric]
    assert spec["x_field"] == label
    assert code not in spec["title"]
    assert len(labels(svg(spec))) == 2
    call = ExtensionDispatcher._visualization_call(spec, {"generate_column_chart"})
    assert call[1]["axisYTitle"] == metric
    assert [row["value"] for row in call[1]["data"]] == [ExtensionDispatcher._finite_number(value) for value in values]


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False])
async def test_mcp_labels_only_use_supported_tool_schema(monkeypatch, declared):
    dispatcher = ExtensionDispatcher()
    calls = []
    schema = {"properties": {"showDataLabels": {"type": "boolean"}}} if declared else {}

    async def discover(_chat):
        return [ToolConfig(name="mcp:generate_column_chart", url="https://charts.example/sse", inputSchema=schema)]

    async def call(name, _chat, arguments):
        calls.append(arguments)
        return ExtensionExecution(name=name, kind="MCP_TOOL", status="COMPLETED")

    monkeypatch.setattr(dispatcher, "_discover_mcp_tools", discover)
    monkeypatch.setattr(dispatcher, "_call_mcp_servers", call)
    request = ChatRequest(conversation_id="labels", message_id="turn", application_id="1",
                          semantic_model_id=81, question="test",
                          mcp=[McpConfig(mcp_server_url="https://charts.example/sse")])
    await dispatcher.execute_visualizations(chat=request, chart_specs=[chart()])
    assert calls[0].get("showDataLabels") is (True if declared else None)

