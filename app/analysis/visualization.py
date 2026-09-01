from __future__ import annotations

from typing import Any

from app.domain.models import ChartSpec, PrimaryIntent


MAX_CHART_POINTS = 200


def build_chart_specs(
    intent: PrimaryIntent,
    columns: list[str],
    rows: list[dict[str, Any]],
    facts: dict[str, Any],
) -> list[ChartSpec]:
    """Create renderer-neutral chart instructions from validated result rows."""
    if not rows:
        return []
    metric = str(facts.get("metric_column") or "")
    if metric not in columns:
        metric = _first_numeric_column(columns, rows) or ""
    label = next((column for column in columns if column != metric), None)

    if intent in {PrimaryIntent.TREND_ANALYSIS, PrimaryIntent.FORECAST_ANALYSIS}:
        return _one("LINE", label, metric, rows, f"{metric}趋势")
    if intent == PrimaryIntent.COMPARISON_ANALYSIS:
        return _one("BAR", label, metric, rows, f"{metric}对比")
    if intent == PrimaryIntent.COMPOSITION_ANALYSIS:
        return _one("PIE", label, metric, rows, f"{metric}占比")
    if intent == PrimaryIntent.ANOMALY_ANALYSIS:
        return _one("LINE", label, metric, rows, f"{metric}异常观察")
    if intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS:
        return _one("BAR", label, metric, rows, f"{metric}贡献分解", horizontal=True)
    if intent == PrimaryIntent.DATA_QUALITY:
        null_counts = facts.get("null_counts") or {}
        data = [
            {"field": str(name), "null_count": count}
            for name, count in null_counts.items()
            if count
        ]
        if not data:
            return []
        return [ChartSpec(
            chart_type="BAR",
            title="字段空值数量",
            x_field="field",
            y_fields=["null_count"],
            data=data[:MAX_CHART_POINTS],
            point_count=min(len(data), MAX_CHART_POINTS),
            data_truncated=len(data) > MAX_CHART_POINTS,
            reason="展示各字段空值分布，便于定位数据质量问题",
        )]
    if intent == PrimaryIntent.REPORT_GENERATION and label and metric:
        chart_type = "LINE" if _looks_temporal(label, rows) else "BAR"
        return _one(chart_type, label, metric, rows, f"{metric}概览")
    return []


def _one(
    chart_type: str,
    label: str | None,
    metric: str,
    rows: list[dict[str, Any]],
    title: str,
    *,
    horizontal: bool = False,
) -> list[ChartSpec]:
    if not label or not metric:
        return []
    selected = [
        {label: row.get(label), metric: row.get(metric)}
        for row in rows[:MAX_CHART_POINTS]
    ]
    return [ChartSpec(
        chart_type=chart_type,
        title=title,
        x_field=label,
        y_fields=[metric],
        data=selected,
        point_count=len(selected),
        data_truncated=len(rows) > MAX_CHART_POINTS,
        horizontal=horizontal,
        reason="依据分析意图和字段类型自动选择的确定性图表",
    )]


def _first_numeric_column(
    columns: list[str], rows: list[dict[str, Any]]
) -> str | None:
    for column in columns:
        values = [row.get(column) for row in rows if row.get(column) is not None]
        if values and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in values
        ):
            return column
    return None


def _looks_temporal(column: str, rows: list[dict[str, Any]]) -> bool:
    lowered = column.lower()
    return any(
        token in lowered
        for token in ("date", "time", "month", "year", "week", "日期", "时间", "月份", "年份", "周")
    )
