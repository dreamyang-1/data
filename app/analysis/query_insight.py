from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

from app.analysis.engine import AnalysisOutput
from app.analysis.profiling import profile_dataset
from app.domain.models import CanonicalAnalysisRequest


MAX_DESCRIBED_COLUMNS = 4
MAX_SINGLE_ROW_VALUES = 6


def build_query_result_insight(
    request: CanonicalAnalysisRequest,
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    total_row_count: int,
    total_row_count_confirmed: bool,
    truncated: bool,
) -> AnalysisOutput | None:
    """Build safe facts that a presentation model may turn into prose.

    This is deliberately a result summarizer rather than a second analysis
    engine.  It only describes the validated query result and a bounded data
    profile; it does not infer causes, forecasts, or business recommendations.
    """

    if not rows or not columns:
        return None

    returned_row_count = len(rows)
    count_text = (
        f"本次查询共命中{total_row_count}条结果"
        if total_row_count_confirmed
        else f"本次接口返回{returned_row_count}条结果，完整结果行数尚未确认"
    )
    if total_row_count_confirmed and returned_row_count != total_row_count:
        count_text += f"，当前用于展示和概括的是{returned_row_count}条返回记录"

    described_columns = "、".join(columns[:MAX_DESCRIBED_COLUMNS])
    summary_points = [f"{count_text}，返回字段为{described_columns}。"]
    if total_row_count_confirmed and not truncated:
        summary_points.append("本次返回的是完整查询结果，没有发生结果截断。")
    profile = profile_dataset(columns, rows)

    if returned_row_count == 1:
        visible_values = [
            f"{column}为{_display_value(rows[0].get(column), column=column)}"
            for column in columns[:MAX_SINGLE_ROW_VALUES]
            if _number(rows[0].get(column)) is not None
        ]
        if visible_values:
            summary_points.append("这次结果的核心值是" + "，".join(visible_values) + "。")
    else:
        numeric_points = _numeric_summary_points(profile)
        summary_points.extend(numeric_points[:2])
        categorical_point = _categorical_summary_point(profile)
        if categorical_point:
            summary_points.append(categorical_point)

    null_columns = [
        f"{item['name']}缺失{item['null_count']}条"
        for item in profile.get("columns", [])
        if int(item.get("null_count") or 0) > 0
    ]
    if null_columns:
        summary_points.append("数据完整性方面，" + "，".join(null_columns[:3]) + "。")

    warnings: list[str] = []
    if truncated or (total_row_count_confirmed and returned_row_count < total_row_count):
        warnings.append(
            f"当前洞察基于{returned_row_count}条返回记录，不能代替对{total_row_count}条完整结果的全量统计。"
        )
    answer = "\n".join(summary_points)
    return AnalysisOutput(
        answer=answer,
        method="validated_query_result_summary",
        facts={
            "decision_source": "DETERMINISTIC_ALGORITHM",
            "llm_role": "PRESENTATION_ONLY",
            "algorithm_contract_version": "query-result-summary-v1",
            "intent": request.primary_intent.value,
            "row_count": total_row_count,
            "returned_row_count": returned_row_count,
            "total_row_count_confirmed": total_row_count_confirmed,
            "truncated": truncated,
            "columns": columns[:MAX_DESCRIBED_COLUMNS],
            "summary_points": summary_points,
            "data_profile": profile,
            "causality_established": False,
            "chart_specs": [],
        },
        warnings=warnings,
    )


def _numeric_summary_points(profile: dict[str, Any]) -> list[str]:
    points: list[str] = []
    for item in profile.get("columns", []):
        summary = item.get("numeric_summary")
        if not isinstance(summary, dict):
            continue
        column = str(item.get("name") or "数值字段")
        minimum = _display_number(summary.get("minimum"), column=column)
        maximum = _display_number(summary.get("maximum"), column=column)
        mean = _display_number(summary.get("mean"), column=column)
        if minimum == maximum:
            points.append(f"{column}在当前返回记录中均为{minimum}。")
        else:
            points.append(
                f"{column}在当前返回记录中的范围是{minimum}至{maximum}，平均值为{mean}。"
            )
    return points


def _categorical_summary_point(profile: dict[str, Any]) -> str | None:
    categorical = [
        item
        for item in profile.get("columns", [])
        if item.get("inferred_type") == "TEXT"
    ]
    if not categorical:
        return None
    descriptions = [
        f"{item['name']}包含{item['unique_count']}个不同值"
        for item in categorical[:2]
    ]
    return "从结果覆盖范围看，" + "，".join(descriptions) + "。"


def _display_value(value: Any, *, column: str) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    return _display_number(number, column=column)


def _display_number(value: Any, *, column: str) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    if _looks_like_rate(column) and 0 <= number <= 1:
        return f"{number:.2%}"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        normalized = value.strip().replace(",", "").replace("，", "")
        percent = normalized.endswith("%")
        if percent:
            normalized = normalized[:-1]
        try:
            number = float(Decimal(normalized))
        except (InvalidOperation, ValueError):
            return None
        number = number / 100 if percent else number
        return number if math.isfinite(number) else None
    return None


def _looks_like_rate(column: str) -> bool:
    lowered = column.casefold()
    return any(
        token in lowered
        for token in ("率", "比例", "占比", "rate", "ratio", "coverage")
    )
