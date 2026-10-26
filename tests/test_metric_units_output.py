"""Final-output units: registered units label tables and chart values only."""
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import pytest

from app.analysis.units import (
    append_unit, header_with_unit, load_metric_units, unit_for_column,
)
from app.analysis.visualization import (
    render_chart_svg, requested_chart_specs, suggested_chart_specs,
)
from app.services.orchestrator import DataAnalysisOrchestrator

NS = {"svg": "http://www.w3.org/2000/svg"}


def labels(svg_bytes):
    root = ET.fromstring(svg_bytes)
    return [node.text for node in root.findall("svg:text[@class='data-label']", NS)]


def test_unit_lookup_matches_names_codes_and_tails():
    units = {"实付金额": "元", "订单笔数": "单", "支付到完成转化率": "%"}
    assert unit_for_column(units, "实付金额") == "元"
    assert unit_for_column(units, "t.实付金额") == "元"
    assert unit_for_column(units, "实付金额（元）") == "元"
    assert unit_for_column(units, "转化率") == ""


def test_header_and_label_never_duplicate_units():
    assert header_with_unit("实付金额", "元") == "实付金额（元）"
    assert header_with_unit("支付到完成转化率", "%") == "支付到完成转化率（%）"
    assert header_with_unit("支付到完成转化率(%)", "%") == "支付到完成转化率(%)"
    assert header_with_unit("实付金额", "") == "实付金额"
    assert append_unit("1,200", "元") == "1,200元"
    assert append_unit("45.0%", "%") == "45.0%"
    assert append_unit("1.20万", "元") == "1.20万元"


def test_table_headers_carry_registered_units():
    columns = ["月份", "实付金额", "订单笔数", "支付到完成转化率"]
    rows = [{"月份": "2026-01", "实付金额": 1200, "订单笔数": 3, "支付到完成转化率": 0.5}]
    table = DataAnalysisOrchestrator._markdown_result_table(
        columns, rows,
        units={"实付金额": "元", "订单笔数": "单", "支付到完成转化率": "%"},
    )
    header = table.splitlines()[0]
    assert "实付金额（元）" in header
    assert "订单笔数（单）" in header
    assert "支付到完成转化率（%）" in header
    assert "月份 |" in header


def test_table_headers_stay_unchanged_without_units():
    table = DataAnalysisOrchestrator._markdown_result_table(
        ["月份", "实付金额"], [{"月份": "2026-01", "实付金额": 1200}],
    )
    assert table.splitlines()[0] == "| 月份 | 实付金额 |"


def test_requested_chart_specs_use_registered_units():
    rows = [{"月份": "2026-01", "实付金额": 12000}, {"月份": "2026-02", "实付金额": 8000}]
    specs, _ = requested_chart_specs(
        "用折线图展示实付金额", list(rows[0]), rows,
        {"metric_columns": ["实付金额"], "metric_units": {"实付金额": "元"}},
    )
    assert specs and specs[0].unit == "元"
    assert labels(render_chart_svg(specs[0])) == ["12,000元", "8,000元"]


def test_bar_labels_keep_abbreviation_and_unit():
    rows = [{"名称": "甲", "实付金额": 12000}, {"名称": "乙", "实付金额": 8000}]
    specs, _ = requested_chart_specs(
        "用柱状图展示实付金额", list(rows[0]), rows,
        {"metric_columns": ["实付金额"], "metric_units": {"实付金额": "元"}},
    )
    assert specs and specs[0].unit == "元"
    assert labels(render_chart_svg(specs[0])) == ["1.20万元", "8,000元"]


def test_pie_and_tree_labels_include_units():
    pie_rows = [{"渠道": "直销", "订单笔数": 6}, {"渠道": "分销", "订单笔数": 4}]
    pie_specs, _ = requested_chart_specs(
        "用饼图展示订单笔数", list(pie_rows[0]), pie_rows,
        {"metric_columns": ["订单笔数"], "metric_units": {"订单笔数": "单"}},
    )
    assert pie_specs and pie_specs[0].unit == "单"
    pie_svg = render_chart_svg(pie_specs[0]).decode("utf-8")
    assert "6单" in pie_svg and "4单" in pie_svg

    tree_rows = [{"省份": "安徽", "医院": "甲医院", "订单笔数": 6}]
    tree_specs, _ = requested_chart_specs(
        "按省份、医院用树状图展示订单笔数", list(tree_rows[0]), tree_rows,
        {"metric_columns": ["订单笔数"], "metric_units": {"订单笔数": "单"}},
    )
    assert tree_specs and tree_specs[0].unit == "单"
    assert "6单" in render_chart_svg(tree_specs[0]).decode("utf-8")


def test_suggested_charts_include_units():
    rows = [{"月份": "2026-01", "实付金额": 1200}, {"月份": "2026-02", "实付金额": 800}]
    specs, _ = suggested_chart_specs(
        list(rows[0]), rows,
        [{"label_column": "月份", "value_column": "实付金额", "chart_type": "LINE", "title": "实付金额趋势"}],
        units={"实付金额": "元"},
    )
    assert specs and specs[0].unit == "元"
    assert labels(render_chart_svg(specs[0])) == ["1,200元", "800元"]


@pytest.mark.asyncio
async def test_units_loader_degrades_without_catalog_config():
    assert await load_metric_units(SimpleNamespace(), 6) == {}
    assert await load_metric_units(SimpleNamespace(mysql_host=""), None) == {}
