from __future__ import annotations

import math
import re
import unicodedata
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


_REQUESTED_CHARTS = {
    '柱状图': 'BAR', '柱形图': 'BAR', '条形图': 'BAR', '折线图': 'LINE',
    '饼图': 'PIE', '饼状图': 'PIE', '散点图': 'SCATTER',
    '树状图': 'TREE', '树形图': 'TREE', '层级图': 'TREE',
    'bar chart': 'BAR', 'line chart': 'LINE', 'pie chart': 'PIE',
    'scatter plot': 'SCATTER', 'tree diagram': 'TREE',
}


def requested_chart_specs(
    question: str, columns: list[str], rows: list[dict[str, Any]],
    facts: dict[str, Any], *, output_requirement: str = '',
) -> tuple[list[ChartSpec] | None, list[str]]:
    """Honor explicit presentation only; never change SQL, grouping or metrics.

    None preserves existing automatic charts. An empty list deliberately
    suppresses them when the requested chart is disabled or cannot be grounded.
    """
    text = question + '\n' + output_requirement
    tokens = '|'.join(re.escape(word) for word in _REQUESTED_CHARTS)
    text, negated = re.subn(r'(?:不要|不用|无需|不需要|别)(?:生成|绘制|展示|使用|用|画|以)?\s*(?:' + tokens + ')', '', text, flags=re.I)
    matches = list(re.finditer(tokens, text, flags=re.I))
    kinds = list(dict.fromkeys(_REQUESTED_CHARTS[match.group().lower()] for match in matches))
    unsupported = re.search(r'雷达图|热力图|桑基图|漏斗图|箱线图|地图|流程图|面积图|矩形树图|直方图', text)
    unsupported_notes = [f'当前绘图暂不支持{unsupported.group()}，已保留查询结果，未用其他图型替代。'] if unsupported else []
    if not kinds:
        if unsupported:
            return [], unsupported_notes
        if negated or re.search(r'不要(?:生成)?图(?:表|像|片)?|不用图|无需图|仅(?:用|要)?表格', text):
            return [], []
        return None, []
    if not rows:
        return [], ['本次没有可绘制的结果数据，未生成所要求的图表。']
    declared = [facts.get('metric_column'), *(facts.get('metric_columns') or [])]
    for key in ('comparisons', 'metric_summaries'):
        declared.extend(item.get('metric_column') for item in facts.get(key) or [] if isinstance(item, Mapping))
    eligible = [column for column in columns if not is_identifier_column(column)
                and not _looks_temporal(column, rows) and _column_is_numeric(column, rows)]
    metrics = list(dict.fromkeys(column for column in declared if column in eligible))
    if not any(declared):
        metrics = [column for column in eligible if column in text]
        if not metrics and len(eligible) == 1:
            metrics = eligible
    non_metrics = [column for column in columns if column not in metrics and not is_identifier_column(column)]
    labels = [column for column in non_metrics
              if not _column_is_numeric(column, rows) or _looks_temporal(column, rows)]
    if not labels and len(non_metrics) == 1:
        labels = non_metrics
    temporal = next((column for column in labels if _looks_temporal(column, rows)), None)
    categorical = next((column for column in labels if column != temporal), None)
    label = facts.get('label_column') if facts.get('label_column') in labels else categorical or temporal
    specs, notes = [], unsupported_notes
    for kind in kinds:
        if kind == 'TREE':
            # A result grouping tree, not inferred real-world organizational
            # relationships. Parent totals are intentionally not invented.
            if not labels or len(labels) > 6:
                notes.append('树状图需要1至6个可展示的分组或层级字段；当前结果不满足，已保留表格。')
                continue
            hierarchy = list(labels)
            order = re.search(r'(?:按照|按|依次)([^。；\n]*?)(?:分层|层级|层次|构建|绘制|展示|生成|树状图|树形图)', text)
            if order and all(column in order.group(1) for column in hierarchy):
                hierarchy.sort(key=order.group(1).index)
            paths = [[str(row.get(column) if row.get(column) is not None else '未提供') for column in hierarchy] for row in rows]
            if len({tuple(path) for path in paths}) != len(paths):
                notes.append('树状图的分组路径存在重复，无法唯一对应每条业务数值；未自行合并或累加结果。')
                continue
            for metric in metrics or ['']:
                data = [dict(path=path, **({metric: row.get(metric)} if metric else {}))
                        for path, row in zip(paths[:MAX_CHART_POINTS], rows[:MAX_CHART_POINTS])]
                specs.append(ChartSpec(chart_type='TREE', title=(metric + '分组树状图')[:200],
                    x_field='path', y_fields=[metric] if metric else [], data=data,
                    point_count=len(data), data_truncated=len(rows) > MAX_CHART_POINTS,
                    reason='依据用户指定图型和返回维度生成分组树；不推断组织隶属关系'))
            notes.append('树状图按查询返回的维度分组展示，不代表已核实的组织隶属关系；父节点未累加指标。')
            continue
        if not metrics:
            notes.append('所要求的图表缺少可唯一确认的业务数值字段；编码不能当作指标，已保留表格。')
            continue
        if kind == 'SCATTER':
            if len(metrics) < 2:
                notes.append('散点图需要两个可确认的业务数值字段，当前结果不足，未将编码或行号当作坐标。')
                continue
            for metric in metrics[1:]:
                specs.extend(_one(kind, metrics[0], metric, rows, f'{metrics[0]}与{metric}散点图'))
            continue
        if not label:
            notes.append('所要求的图表缺少类别或时间字段，未编造分组。')
            continue
        for metric in metrics:
            if kind == 'PIE' and (len({str(row.get(label)) for row in rows}) > 8
                    or any((_finite_number(row.get(metric)) or 0) < 0 for row in rows)
                    or not any((_finite_number(row.get(metric)) or 0) > 0 for row in rows)):
                notes.append(f'{metric}的数据含负值、无正值或超过8个类别，不适合当前饼图渲染；未替换成柱状图。')
                continue
            plotted = rows
            axis = (temporal or label) if kind == 'LINE' else label
            series = categorical if kind == 'LINE' and temporal else None
            group_fields = ([field for field in labels if field != temporal]
                            if kind == 'LINE' and temporal else labels)
            if len(group_fields) > 1 and not facts.get('label_column'):
                compound = ' / '.join(group_fields)
                plotted = [{**row, compound: ' / '.join(str(row.get(field) if row.get(field) is not None else '未提供')
                           for field in group_fields)} for row in rows]
                if kind == 'LINE' and temporal:
                    series = compound
                else:
                    axis = compound
            if kind == 'LINE' and len({(str(row.get(axis)), str(row.get(series)) if series else '') for row in plotted}) != len(plotted):
                notes.append(f'{metric}的时间/系列键重复，未把不同记录连接成一条折线。')
                continue
            specs.extend(_one(kind, axis, metric, plotted, f'{metric}{next(word for word, code in _REQUESTED_CHARTS.items() if code == kind)}',
                horizontal=kind == 'BAR' and '条形图' in text, series_field=series))
    if any(spec.data_truncated for spec in specs):
        notes.append(f'图表仅绘制前{MAX_CHART_POINTS}条返回记录，校验与分析仍使用全部可用数据。')
    if len(specs) > 3:
        notes.append('本次按现有展示上限绘制前3张图，其他指标仍保留在结果表和附件中。')
    return specs[:3], list(dict.fromkeys(notes))


def build_chart_specs(
    intent: PrimaryIntent,
    columns: list[str],
    rows: list[dict[str, Any]],
    facts: dict[str, Any],
) -> list[ChartSpec]:
    """Create renderer-neutral chart instructions from validated result rows."""
    if not rows:
        return []
    if intent == PrimaryIntent.DATA_QUALITY:
        return _build_metric_chart(intent, columns, rows, facts, "")
    # Comparison analyzers expose either a single metric, several metrics, or
    # per-metric summaries. Column position is never evidence of a measure.
    declared = [facts.get("metric_column")]
    declared.extend(facts.get("metric_columns") or [])
    for key in ("comparisons", "metric_summaries"):
        declared.extend(
            item.get("metric_column")
            for item in facts.get(key) or [] if isinstance(item, Mapping)
        )
    declared = list(dict.fromkeys(str(item) for item in declared if item))
    metrics = [
        column for column in declared
        if column in columns and not is_identifier_column(column)
        and _column_is_numeric(column, rows)
    ]
    if not declared:
        metric = _first_numeric_column(columns, rows)
        metrics = [metric] if metric else []
    chart_facts = {**facts, "metric_columns": metrics}
    return [
        chart for metric in metrics
        for chart in _build_metric_chart(intent, columns, rows, chart_facts, metric)
    ]


def _build_metric_chart(
    intent: PrimaryIntent,
    columns: list[str],
    rows: list[dict[str, Any]],
    facts: dict[str, Any],
    metric: str,
) -> list[ChartSpec]:
    non_metrics = [
        column for column in columns
        if column not in (facts.get("metric_columns") or [metric])
    ]
    temporal = next(
        (column for column in non_metrics if _looks_temporal(column, rows)),
        None,
    )
    categorical = next(
        (
            column
            for column in non_metrics
            if column != temporal and not is_identifier_column(column)
            and not _column_is_numeric(column, rows)
        ),
        None,
    )
    confirmed_label = str(facts.get("label_column") or "")
    if confirmed_label in non_metrics and confirmed_label != temporal:
        categorical = confirmed_label
    label = categorical or temporal or (non_metrics[0] if non_metrics else None)

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
        return _one("BAR", label, metric, rows, f"{metric}对比")
    if intent == PrimaryIntent.COMPOSITION_ANALYSIS:
        values = [_finite_number(row.get(metric)) for row in rows]
        if any(value is not None and value < 0 for value in values):
            return _one("BAR", label, metric, rows, f"{metric}对比")
        categories = {str(row.get(label)) for row in rows} if label else set()
        chart_type = "BAR" if len(categories) > 8 else "PIE"
        return _one(chart_type, label, metric, rows, f"{metric}占比")
    if intent == PrimaryIntent.ANOMALY_ANALYSIS:
        label = temporal or (non_metrics[0] if non_metrics else None)
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
    if chart_type == 'TREE':
        return _render_tree(raw, width=max(640, min(int(width), 1600)))
    if chart_type not in {"LINE", "BAR", "PIE", "SCATTER"}:
        return None
    if (not rows or not x_field or len(y_fields) != 1
            or is_identifier_column(y_fields[0])
            or (chart_type == "SCATTER" and is_identifier_column(x_field))):
        return None

    width = max(640, min(int(width), 1600))
    height = max(360, min(int(height), 1000))
    horizontal = bool(raw.get("horizontal")) or (chart_type == "BAR" and len(rows) > 12)
    if chart_type == "BAR" and horizontal:
        height = max(height, min(len(rows), MAX_CHART_POINTS) * 28 + 110)
    if chart_type == "PIE":
        body = _render_pie(rows, x_field, y_fields[0], width, height)
    elif chart_type == "BAR":
        body = _render_bars(
            rows,
            x_field,
            y_fields[0],
            width,
            height,
            horizontal=horizontal,
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


def _render_tree(raw: dict[str, Any], *, width: int) -> bytes | None:
    rows = raw.get('data') or []
    field = raw.get('x_field')
    metrics = raw.get('y_fields') or []
    if not rows or len(metrics) > 1 or any(is_identifier_column(str(metric)) for metric in metrics):
        return None
    paths = [row.get(field) for row in rows[:MAX_CHART_POINTS] if isinstance(row, Mapping)]
    if len(paths) != len(rows) or any(not isinstance(path, list) or not 1 <= len(path) <= 6 for path in paths):
        return None
    paths = [tuple(str(item) for item in path) for path in paths]
    if len(set(paths)) != len(paths):
        return None
    nodes = {(): '查询结果'}
    for path in paths:
        for depth in range(1, len(path) + 1):
            nodes.setdefault(path[:depth], path[depth - 1])
    levels = max(map(len, paths)) + 1
    width = max(width, levels * 170 + 60)
    height = max(360, len(paths) * 64 + 110)
    positions = {path: 94 + index * 64 for index, path in enumerate(paths)}
    for path in sorted(nodes, key=len, reverse=True):
        if path not in positions:
            children = [child for child in nodes if len(child) == len(path) + 1 and child[:-1] == path]
            positions[path] = sum(positions[child] for child in children) / len(children)
    parts = []
    for path in nodes:
        x, y = 20 + len(path) * 170, positions[path]
        if path:
            parent_y = positions[path[:-1]]
            parts.append(f'<path d="M {x - 25} {parent_y} H {x - 13} V {y} H {x}" fill="none" stroke="#94a3b8"/>')
        label = escape(nodes[path])
        parts.append(f'<rect x="{x}" y="{y - 23}" width="145" height="46" rx="6" fill="#eff6ff" stroke="#93c5fd"/>'
                     f'<text x="{x + 8}" y="{y - 4}" font-size="12" fill="#1f2937"><title>{label}</title>{escape(_short_label(nodes[path], 12))}</text>')
    if metrics:
        metric = metrics[0]
        for path, row in zip(paths, rows):
            value = row.get(metric)
            if _finite_number(value) is not None:
                parts.append(f'<text x="{28 + len(path) * 170}" y="{positions[path] + 14}" font-size="12" fill="#1d4ed8">{escape(_value_label(value))}</text>')
    title = escape(str(raw.get('title') or '结果分组树状图'))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="chart-title">'
            f'<rect width="100%" height="100%" fill="white"/><title id="chart-title">{title}</title>'
            f'<g font-family="Microsoft YaHei, PingFang SC, sans-serif"><text x="20" y="34" font-size="20" fill="#1f2937">{title}</text>{"".join(parts)}</g></svg>').encode('utf-8')


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


def _value_label(value: Any) -> str:
    """Visible labels retain business precision, unlike abbreviated axis ticks."""
    text = str(value).strip().replace(",", "").replace("，", "")
    suffix = "%" if text.endswith("%") else ""
    try:
        number = Decimal(text.removesuffix("%"))
        if not number.is_finite():
            return ""
        formatted = format(number, ",f")
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted + suffix
    except (InvalidOperation, ValueError):
        return ""


def _value_text(x: float, y: float, value: str, *, anchor: str = "middle") -> str:
    return (
        f'<text class="data-label" x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
        'font-family="Microsoft YaHei, sans-serif" font-size="12" fill="#374151" '
        'paint-order="stroke" stroke="#fff" stroke-width="3" stroke-linejoin="round">'
        f'{escape(value)}</text>'
    )


def _chart_bounds(values: list[float], *, include_zero: bool = False) -> tuple[float, float]:
    low = min(0.0, min(values)) if include_zero else min(values)
    high = max(0.0, max(values)) if include_zero else max(values)
    if math.isclose(low, high):
        padding = abs(low) * 0.1 or 1.0
        return low - padding, high + padding
    padding = (high - low) * 0.08
    if include_zero:
        return low - padding if low < 0 else 0.0, high + padding if high > 0 else 0.0
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
        (str(row.get(x_field, "")), _finite_number(row.get(y_field)),
         str(row.get(series_field, "")) if series_field else "", _value_label(row.get(y_field)))
        for row in rows[:MAX_CHART_POINTS]
    ]
    points = [item for item in points if item[0] and item[1] is not None
              and (not scatter or _finite_number(item[0]) is not None)]
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
    if scatter:
        x_values = [float(_finite_number(label)) for label in labels]
        x_low, x_high = _chart_bounds(x_values)
        x_position = lambda label: left + (float(_finite_number(label)) - x_low) / (x_high - x_low) * plot_width
    else:
        x_position = lambda label: left + label_index[label] / x_denominator * plot_width

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
    axis_labels = (
        [_display_number(x_low + (x_high - x_low) * index / 5) for index in range(6)]
        if scatter else labels
    )
    for index, label in enumerate(axis_labels):
        if index % label_step and index != len(labels) - 1:
            continue
        x = left + index / 5 * plot_width if scatter else x_position(label)
        parts.append(
            f'<text x="{x:.1f}" y="{bottom + 24:.1f}" text-anchor="middle" '
            'font-family="Microsoft YaHei, sans-serif" font-size="11" fill="#4b5563">'
            f'{escape(_short_label(label, 12))}</text>'
        )

    groups: dict[str, list[tuple[str, float, str]]] = {}
    for label, value, series, value_label in points:
        groups.setdefault(series, []).append((label, float(value), value_label))
    for series_index, (series, series_points) in enumerate(groups.items()):
        color = _CHART_COLORS[series_index % len(_CHART_COLORS)]
        coordinates = []
        for label, value, value_label in series_points:
            x = x_position(label)
            y = bottom - (value - y_low) / y_span * plot_height
            coordinates.append((x, y, label, value_label))
        if not scatter and len(coordinates) > 1:
            serialized = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in coordinates)
            parts.append(
                f'<polyline points="{serialized}" fill="none" stroke="{color}" '
                'stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for x, y, label, value_label in coordinates:
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" stroke="#fff" stroke-width="1.5">'
                f'<title>{escape(label)}：{escape(value_label)}</title></circle>'
            )
            parts.append(_value_text(
                x, max(top + 12, y - 10), value_label,
                anchor="start" if x <= left else "end" if x >= right else "middle",
            ))
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
        (str(row.get(x_field, "")), _finite_number(row.get(y_field)), _value_label(row.get(y_field)))
        for row in rows[:MAX_CHART_POINTS]
    ]
    values = [(label, float(value), text) for label, value, text in values if label and value is not None]
    if not values:
        return None
    if horizontal:
        return _render_horizontal_bars(values, width, height)

    left, right, top, bottom = 70.0, width - 28.0, 62.0, height - 92.0
    plot_width, plot_height = right - left, bottom - top
    low, high = _chart_bounds([value for _, value, _ in values], include_zero=True)
    baseline_value = min(max(0.0, low), high)
    scale = lambda value: bottom - (value - low) / (high - low) * plot_height
    baseline = scale(baseline_value)
    slot = plot_width / len(values)
    bar_width = max(4.0, min(42.0, slot * 0.62))
    parts = [
        f'<line x1="{left:.1f}" y1="{baseline:.1f}" x2="{right:.1f}" y2="{baseline:.1f}" stroke="#9ca3af"/>'
    ]
    for tick in range(6):
        value = low + (high - low) * tick / 5
        parts.append(
            f'<text x="{left - 8:.1f}" y="{scale(value) + 4:.1f}" text-anchor="end" '
            'font-family="Microsoft YaHei, sans-serif" font-size="11" fill="#6b7280">'
            f'{escape(_display_number(value))}</text>'
        )
    label_step = max(1, math.ceil(len(values) / 12))
    for index, (label, value, text) in enumerate(values):
        x = left + index * slot + (slot - bar_width) / 2
        y = min(baseline, scale(value))
        bar_height = max(1.0, abs(scale(value) - baseline))
        color = _CHART_COLORS[index % len(_CHART_COLORS)]
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" '
            f'rx="3" fill="{color}"><title>{escape(label)}：{escape(text)}</title></rect>'
        )
        label_y = max(top + 12, scale(value) - 8) if value >= 0 else min(bottom - 2, scale(value) + 16)
        parts.append(_value_text(x + bar_width / 2, label_y, text))
        if index % label_step == 0 or index == len(values) - 1:
            parts.append(
                f'<text x="{x + bar_width / 2:.1f}" y="{bottom + 22:.1f}" text-anchor="middle" '
                'font-family="Microsoft YaHei, sans-serif" font-size="11" fill="#4b5563">'
                f'{escape(_short_label(label, 10))}</text>'
            )
    return "".join(parts)


def _render_horizontal_bars(
    values: list[tuple[str, float, str]], width: int, height: int
) -> str:
    left, right, top, bottom = 190.0, width - 110.0, 62.0, height - 38.0
    plot_width, plot_height = right - left, bottom - top
    low, high = _chart_bounds([value for _, value, _ in values], include_zero=True)
    scale = lambda value: left + (value - low) / (high - low) * plot_width
    baseline = scale(0)
    slot = plot_height / len(values)
    bar_height = max(4.0, min(28.0, slot * 0.62))
    parts = [f'<line x1="{baseline:.1f}" y1="{top:.1f}" x2="{baseline:.1f}" y2="{bottom:.1f}" stroke="#9ca3af"/>']
    for index, (label, value, text) in enumerate(values):
        y = top + index * slot + (slot - bar_height) / 2
        bar_width = max(1.0, abs(scale(value) - baseline))
        x = min(baseline, scale(value))
        color = _CHART_COLORS[index % len(_CHART_COLORS)]
        parts.append(
            f'<text x="{left - 10:.1f}" y="{y + bar_height * .72:.1f}" text-anchor="end" '
            'font-family="Microsoft YaHei, sans-serif" font-size="11" fill="#4b5563">'
            f'{escape(_short_label(label, 18))}</text>'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" '
            f'rx="3" fill="{color}"><title>{escape(label)}：{escape(text)}</title></rect>'
        )
        # Values sit inside negative bars to avoid colliding with category names.
        label_x = scale(value) + 6
        parts.append(_value_text(label_x, y + bar_height * .72, text, anchor="start"))
    return "".join(parts)


def _render_pie(
    rows: list[dict[str, Any]],
    x_field: str,
    y_field: str,
    width: int,
    height: int,
) -> str | None:
    aggregated: dict[str, Decimal] = {}
    for row in rows[:MAX_CHART_POINTS]:
        label = str(row.get(x_field, ""))
        value = _decimal_number(row.get(y_field))
        if value is not None and value < 0:
            return None
        if label and value is not None and value >= 0:
            aggregated[label] = aggregated.get(label, Decimal(0)) + value
    values = sorted(aggregated.items(), key=lambda item: item[1], reverse=True)
    if len(values) > 8:
        return None  # Never silently hide categories under an invented aggregate.
    total = sum(value for _, value in values)
    if total <= 0:
        return None

    cx, cy = width * 0.35, height * 0.54
    radius = min(width, height) * 0.29
    angle = -math.pi / 2
    parts = []
    if values[0][1] == total:
        label, value = values[0]
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="{_CHART_COLORS[0]}">'
            f'<title>{escape(label)}：100.0%（{escape(_value_label(value))}）</title></circle>'
        )
        for index, (label, value) in enumerate(values):
            parts.append(_pie_legend(index, label, value, total, width))
        return "".join(parts)
    for index, (label, value) in enumerate(values):
        sweep = float(value / total) * math.tau
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
            f'<title>{escape(label)}：{percentage:.1%}（{escape(_value_label(value))}）</title></path>'
        )
        parts.append(_pie_legend(index, label, value, total, width))
        angle = next_angle
    return "".join(parts)


def _pie_legend(index: int, label: str, value: Decimal, total: Decimal, width: int) -> str:
    legend_y = 92 + index * 38
    color = _CHART_COLORS[index % len(_CHART_COLORS)]
    return (
        f'<rect x="{width * .68:.1f}" y="{legend_y - 12:.1f}" width="14" height="14" rx="2" fill="{color}"/>'
        f'<text x="{width * .68 + 24:.1f}" y="{legend_y:.1f}" '
        'font-family="Microsoft YaHei, sans-serif" font-size="12" fill="#374151">'
        f'{escape(_short_label(label, 16))} {value / total:.1%}（{escape(_value_label(value))}）</text>'
    )


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
        horizontal=horizontal or (chart_type == "BAR" and len(selected) > 12),
        reason="依据分析意图和字段类型自动选择的确定性图表",
    )]


def _first_numeric_column(
    columns: list[str], rows: list[dict[str, Any]]
) -> str | None:
    candidates = [column for column in columns
                  if not is_identifier_column(column) and not _looks_temporal(column, rows)
                  and _column_is_numeric(column, rows)]
    return candidates[0] if len(candidates) == 1 else None


def _column_is_numeric(column: str, rows: list[dict[str, Any]]) -> bool:
    values = [row.get(column) for row in rows if row.get(column) is not None]
    return bool(values) and all(_finite_number(value) is not None for value in values)


def _number(value: Any) -> float | None:
    number = _decimal_number(value)
    if number is None:
        return None
    try:
        converted = float(number)
        return converted if math.isfinite(converted) else None
    except (ValueError, OverflowError):
        return None


def _decimal_number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip().replace(",", "").replace("，", "")
    percent = normalized.endswith("%")
    try:
        number = Decimal(normalized.removesuffix("%"))
        if not number.is_finite():
            return None
        return number / 100 if percent else number
    except (InvalidOperation, ValueError):
        return None


def is_identifier_column(column: str) -> bool:
    """Names of identity columns, not counters such as code_count/编码数量."""
    name = unicodedata.normalize("NFKC", column).strip()
    if re.search(r"(?:编码|编号|代码|标识|主键|序号|排名|身份证号)\)?$", name):
        return True
    if re.search(r"(?:订单|经销商|医院|商品|产品|客户|业务员|销售员|省份|科室)(?:号|ID|id)$", name):
        return True
    return bool(
        re.search(r"(?:^|[._\s(])(?:id|code|key|uuid)\)?$", name, re.I)
        or re.search(r"(?:Id|ID|Code|Key|UUID)$", name)
    )


def _looks_temporal(column: str, rows: list[dict[str, Any]]) -> bool:
    lowered = column.lower()
    return any(
        token in lowered
        for token in ("date", "time", "month", "year", "week", "日期", "时间", "月份", "年份", "周")
    )
