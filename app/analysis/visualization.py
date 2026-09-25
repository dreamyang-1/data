from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from xml.sax.saxutils import escape

from app.domain.models import ChartSpec, PrimaryIntent


MAX_CHART_POINTS = 200

_CHART_COLORS = (
    "#2563eb",
    "#16a34a",
    "#f97316",
    "#9333ea",
    "#dc2626",
    "#0891b2",
    "#ca8a04",
    "#4f46e5",
)


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
    if metric not in columns or not _column_is_numeric(metric, rows):
        metric = _first_numeric_column(columns, rows) or ""
    non_metrics = [column for column in columns if column != metric]
    temporal = next(
        (column for column in non_metrics if _looks_temporal(column, rows)),
        None,
    )
    categorical = next(
        (
            column
            for column in non_metrics
            if column != temporal and not _column_is_numeric(column, rows)
        ),
        None,
    )

    if intent in {PrimaryIntent.TREND_ANALYSIS, PrimaryIntent.FORECAST_ANALYSIS}:
        label = temporal or (non_metrics[0] if non_metrics else None)
        # A repeated time/series key cannot be rendered as a single trajectory.
        # Missing grouping evidence must never connect unrelated business rows.
        if label:
            keys = [
                (str(row.get(label)), str(row.get(categorical)) if categorical else "")
                for row in rows
            ]
            if len(keys) != len(set(keys)):
                return []
        return _one(
            "LINE", label, metric, rows, f"{metric}趋势",
            series_field=categorical if temporal else None,
        )
    if intent == PrimaryIntent.COMPARISON_ANALYSIS:
        label = categorical or temporal or (non_metrics[0] if non_metrics else None)
        return _one("BAR", label, metric, rows, f"{metric}对比")
    if intent == PrimaryIntent.COMPOSITION_ANALYSIS:
        label = categorical or temporal or (non_metrics[0] if non_metrics else None)
        return _one("PIE", label, metric, rows, f"{metric}占比")
    if intent == PrimaryIntent.ANOMALY_ANALYSIS:
        label = temporal or (non_metrics[0] if non_metrics else None)
        return _one("LINE", label, metric, rows, f"{metric}异常观察")
    if intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS:
        label = categorical or temporal or (non_metrics[0] if non_metrics else None)
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
    if intent == PrimaryIntent.REPORT_GENERATION and non_metrics and metric:
        label = temporal or categorical or non_metrics[0]
        chart_type = "LINE" if _looks_temporal(label, rows) else "BAR"
        return _one(chart_type, label, metric, rows, f"{metric}概览")
    return []


def render_chart_svg(
    spec: ChartSpec | Mapping[str, Any],
    *,
    width: int = 960,
    height: int = 520,
) -> bytes | None:
    """Render a validated ``ChartSpec`` as a self-contained, inert SVG.

    The web client already understands Markdown images but does not yet render
    the renderer-neutral ``chart_specs`` response field.  This renderer keeps
    chart selection and data in the analysis layer while providing a portable
    image for existing clients.  It never executes code or loads external
    resources.
    """

    raw = spec.model_dump(mode="json") if isinstance(spec, ChartSpec) else dict(spec)
    chart_type = str(raw.get("chart_type") or "").upper()
    title = str(raw.get("title") or "数据图表")
    x_field = str(raw.get("x_field") or "")
    y_fields = [str(item) for item in raw.get("y_fields") or [] if str(item)]
    series_field = str(raw.get("series_field") or "")
    rows = [dict(item) for item in raw.get("data") or [] if isinstance(item, Mapping)]
    if chart_type not in {"LINE", "BAR", "PIE", "SCATTER"}:
        return None
    if not rows or not x_field or not y_fields:
        return None

    width = max(640, min(int(width), 1600))
    height = max(360, min(int(height), 1000))
    if chart_type == "PIE":
        body = _render_pie(rows, x_field, y_fields[0], width, height)
    elif chart_type == "BAR":
        body = _render_bars(
            rows,
            x_field,
            y_fields[0],
            width,
            height,
            horizontal=bool(raw.get("horizontal")),
        )
    else:
        body = _render_cartesian(
            rows,
            x_field,
            y_fields[0],
            width,
            height,
            series_field=series_field or None,
            scatter=chart_type == "SCATTER",
        )
    if body is None:
        return None

    safe_title = escape(title)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="chart-title">'
        '<rect width="100%" height="100%" rx="12" fill="#ffffff"/>'
        f'<title id="chart-title">{safe_title}</title>'
        f'<text x="{width / 2:.1f}" y="34" text-anchor="middle" '
        'font-family="Microsoft YaHei, PingFang SC, sans-serif" font-size="20" '
        f'font-weight="600" fill="#1f2937">{safe_title}</text>'
        f'{body}'
        '</svg>'
    )
    return svg.encode("utf-8")


def _finite_number(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and math.isfinite(number) else None


def _short_label(value: Any, limit: int = 14) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _display_number(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if magnitude >= 10_000:
        return f"{value / 10_000:.2f}万"
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _chart_bounds(values: list[float]) -> tuple[float, float]:
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        padding = abs(low) * 0.1 or 1.0
        return low - padding, high + padding
    padding = (high - low) * 0.08
    return min(0.0, low - padding), high + padding


def _render_cartesian(
    rows: list[dict[str, Any]],
    x_field: str,
    y_field: str,
    width: int,
    height: int,
    *,
    series_field: str | None,
    scatter: bool,
) -> str | None:
    points = [
        (str(row.get(x_field, "")), _finite_number(row.get(y_field)), str(row.get(series_field, "")) if series_field else "")
        for row in rows[:MAX_CHART_POINTS]
    ]
    points = [(x, y, series) for x, y, series in points if x and y is not None]
    if not points:
        return None

    left, right, top, bottom = 78.0, width - 32.0, 62.0, height - 82.0
    plot_width, plot_height = right - left, bottom - top
    labels = list(dict.fromkeys(item[0] for item in points))
    label_index = {label: index for index, label in enumerate(labels)}
    y_values = [float(item[1]) for item in points]
    y_low, y_high = _chart_bounds(y_values)
    y_span = y_high - y_low
    x_denominator = max(1, len(labels) - 1)

    parts = []
    for tick in range(6):
        ratio = tick / 5
        y = bottom - ratio * plot_height
        value = y_low + ratio * y_span
        parts.append(
            f'<line x1="{left:.1f}" y1="{y:.1f}" x2="{right:.1f}" y2="{y:.1f}" '
            'stroke="#e5e7eb" stroke-width="1"/>'
            f'<text x="{left - 10:.1f}" y="{y + 4:.1f}" text-anchor="end" '
            'font-family="Microsoft YaHei, sans-serif" font-size="11" fill="#6b7280">'
            f'{escape(_display_number(value))}</text>'
        )
    parts.append(
        f'<line x1="{left:.1f}" y1="{top:.1f}" x2="{left:.1f}" y2="{bottom:.1f}" stroke="#9ca3af"/>'
        f'<line x1="{left:.1f}" y1="{bottom:.1f}" x2="{right:.1f}" y2="{bottom:.1f}" stroke="#9ca3af"/>'
    )

    max_x_labels = 12
    label_step = max(1, math.ceil(len(labels) / max_x_labels))
    for index, label in enumerate(labels):
        if index % label_step and index != len(labels) - 1:
            continue
        x = left + index / x_denominator * plot_width
        parts.append(
            f'<text x="{x:.1f}" y="{bottom + 24:.1f}" text-anchor="middle" '
            'font-family="Microsoft YaHei, sans-serif" font-size="11" fill="#4b5563">'
            f'{escape(_short_label(label, 12))}</text>'
        )

    groups: dict[str, list[tuple[str, float]]] = {}
    for label, value, series in points:
        groups.setdefault(series, []).append((label, float(value)))
    for series_index, (series, series_points) in enumerate(groups.items()):
        color = _CHART_COLORS[series_index % len(_CHART_COLORS)]
        coordinates = []
        for label, value in series_points:
            x = left + label_index[label] / x_denominator * plot_width
            y = bottom - (value - y_low) / y_span * plot_height
            coordinates.append((x, y, label, value))
        if not scatter and len(coordinates) > 1:
            serialized = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in coordinates)
            parts.append(
                f'<polyline points="{serialized}" fill="none" stroke="{color}" '
                'stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for x, y, label, value in coordinates:
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" stroke="#fff" stroke-width="1.5">'
                f'<title>{escape(label)}：{escape(_display_number(value))}</title></circle>'
            )
        if series:
            legend_x = left + series_index * 150
            parts.append(
                f'<circle cx="{legend_x:.1f}" cy="{height - 24:.1f}" r="5" fill="{color}"/>'
                f'<text x="{legend_x + 10:.1f}" y="{height - 20:.1f}" '
                'font-family="Microsoft YaHei, sans-serif" font-size="11" fill="#374151">'
                f'{escape(_short_label(series, 18))}</text>'
            )
    return "".join(parts)


def _render_bars(
    rows: list[dict[str, Any]],
    x_field: str,
    y_field: str,
    width: int,
    height: int,
    *,
    horizontal: bool,
) -> str | None:
    values = [
        (str(row.get(x_field, "")), _finite_number(row.get(y_field)))
        for row in rows[:30]
    ]
    values = [(label, float(value)) for label, value in values if label and value is not None]
    if not values:
        return None
    if horizontal:
        return _render_horizontal_bars(values, width, height)

    left, right, top, bottom = 70.0, width - 28.0, 62.0, height - 92.0
    plot_width, plot_height = right - left, bottom - top
    low, high = _chart_bounds([value for _, value in values])
    baseline_value = min(max(0.0, low), high)
    scale = lambda value: bottom - (value - low) / (high - low) * plot_height
    baseline = scale(baseline_value)
    slot = plot_width / len(values)
    bar_width = max(4.0, min(42.0, slot * 0.62))
    parts = [
        f'<line x1="{left:.1f}" y1="{baseline:.1f}" x2="{right:.1f}" y2="{baseline:.1f}" stroke="#9ca3af"/>'
    ]
    label_step = max(1, math.ceil(len(values) / 12))
    for index, (label, value) in enumerate(values):
        x = left + index * slot + (slot - bar_width) / 2
        y = min(baseline, scale(value))
        bar_height = max(1.0, abs(scale(value) - baseline))
        color = _CHART_COLORS[index % len(_CHART_COLORS)]
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" '
            f'rx="3" fill="{color}"><title>{escape(label)}：{escape(_display_number(value))}</title></rect>'
        )
        if index % label_step == 0 or index == len(values) - 1:
            parts.append(
                f'<text x="{x + bar_width / 2:.1f}" y="{bottom + 22:.1f}" text-anchor="middle" '
                'font-family="Microsoft YaHei, sans-serif" font-size="11" fill="#4b5563">'
                f'{escape(_short_label(label, 10))}</text>'
            )
    return "".join(parts)


def _render_horizontal_bars(
    values: list[tuple[str, float]], width: int, height: int
) -> str:
    left, right, top, bottom = 190.0, width - 42.0, 62.0, height - 38.0
    plot_width, plot_height = right - left, bottom - top
    max_value = max(abs(value) for _, value in values) or 1.0
    slot = plot_height / len(values)
    bar_height = max(4.0, min(28.0, slot * 0.62))
    parts = []
    for index, (label, value) in enumerate(values):
        y = top + index * slot + (slot - bar_height) / 2
        bar_width = max(1.0, abs(value) / max_value * plot_width)
        color = _CHART_COLORS[index % len(_CHART_COLORS)]
        parts.append(
            f'<text x="{left - 10:.1f}" y="{y + bar_height * .72:.1f}" text-anchor="end" '
            'font-family="Microsoft YaHei, sans-serif" font-size="11" fill="#4b5563">'
            f'{escape(_short_label(label, 18))}</text>'
            f'<rect x="{left:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" '
            f'rx="3" fill="{color}"><title>{escape(label)}：{escape(_display_number(value))}</title></rect>'
        )
    return "".join(parts)


def _render_pie(
    rows: list[dict[str, Any]],
    x_field: str,
    y_field: str,
    width: int,
    height: int,
) -> str | None:
    aggregated: dict[str, float] = {}
    for row in rows:
        label = str(row.get(x_field, ""))
        value = _finite_number(row.get(y_field))
        if label and value is not None and value > 0:
            aggregated[label] = aggregated.get(label, 0.0) + value
    values = sorted(aggregated.items(), key=lambda item: item[1], reverse=True)
    if len(values) > 8:
        values = values[:7] + [("其他", sum(value for _, value in values[7:]))]
    total = sum(value for _, value in values)
    if total <= 0:
        return None

    cx, cy = width * 0.35, height * 0.54
    radius = min(width, height) * 0.29
    angle = -math.pi / 2
    parts = []
    if len(values) == 1:
        label, value = values[0]
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="{_CHART_COLORS[0]}">'
            f'<title>{escape(label)}：100.0%（{escape(_display_number(value))}）</title></circle>'
            f'<rect x="{width * .68:.1f}" y="80.0" width="14" height="14" rx="2" fill="{_CHART_COLORS[0]}"/>'
            f'<text x="{width * .68 + 24:.1f}" y="92.0" '
            'font-family="Microsoft YaHei, sans-serif" font-size="12" fill="#374151">'
            f'{escape(_short_label(label, 16))} 100.0%</text>'
        )
        return "".join(parts)
    for index, (label, value) in enumerate(values):
        sweep = value / total * math.tau
        next_angle = angle + sweep
        x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        x2, y2 = cx + radius * math.cos(next_angle), cy + radius * math.sin(next_angle)
        large_arc = 1 if sweep > math.pi else 0
        color = _CHART_COLORS[index % len(_CHART_COLORS)]
        path = (
            f"M {cx:.1f} {cy:.1f} L {x1:.1f} {y1:.1f} "
            f"A {radius:.1f} {radius:.1f} 0 {large_arc} 1 {x2:.1f} {y2:.1f} Z"
        )
        percentage = value / total
        parts.append(
            f'<path d="{path}" fill="{color}" stroke="#fff" stroke-width="2">'
            f'<title>{escape(label)}：{percentage:.1%}（{escape(_display_number(value))}）</title></path>'
        )
        legend_y = 92 + index * 38
        parts.append(
            f'<rect x="{width * .68:.1f}" y="{legend_y - 12:.1f}" width="14" height="14" rx="2" fill="{color}"/>'
            f'<text x="{width * .68 + 24:.1f}" y="{legend_y:.1f}" '
            'font-family="Microsoft YaHei, sans-serif" font-size="12" fill="#374151">'
            f'{escape(_short_label(label, 16))} {percentage:.1%}</text>'
        )
        angle = next_angle
    return "".join(parts)


def _one(
    chart_type: str,
    label: str | None,
    metric: str,
    rows: list[dict[str, Any]],
    title: str,
    *,
    horizontal: bool = False,
    series_field: str | None = None,
) -> list[ChartSpec]:
    if not label or not metric:
        return []
    selected_fields = [label, metric]
    if series_field and series_field not in selected_fields:
        selected_fields.append(series_field)
    selected = [
        {field: row.get(field) for field in selected_fields}
        for row in rows[:MAX_CHART_POINTS]
    ]
    return [ChartSpec(
        chart_type=chart_type,
        title=title,
        x_field=label,
        y_fields=[metric],
        series_field=series_field,
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
        if values and all(_number(value) is not None for value in values):
            return column
    return None


def _column_is_numeric(column: str, rows: list[dict[str, Any]]) -> bool:
    values = [row.get(column) for row in rows if row.get(column) is not None]
    return bool(values) and all(_number(value) is not None for value in values)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", "").replace("，", "")
        if normalized.endswith("%"):
            normalized = normalized[:-1]
        try:
            return float(Decimal(normalized))
        except (InvalidOperation, ValueError):
            return None
    return None


def _looks_temporal(column: str, rows: list[dict[str, Any]]) -> bool:
    lowered = column.lower()
    return any(
        token in lowered
        for token in ("date", "time", "month", "year", "week", "日期", "时间", "月份", "年份", "周")
    )
