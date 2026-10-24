"""Internal analysis requirements shared by planning, execution and presentation.

This is not an API schema. Requirements travel inside the existing analysis
binding and never grant semantic access or invent business filters.
"""
from __future__ import annotations

from datetime import date, timedelta
import copy
import re
from typing import Any

from app.analysis.chain_tools.temporal_window import (
    has_pop_signal, has_yoy_signal, normalize_grain,
    resolve_target_window, shift_calendar_months,
)
from app.domain.models import PrimaryIntent
from app.analysis.contracts import multidimensional_operator_for_question


METHODS = {
    PrimaryIntent.TREND_ANALYSIS: ["trend_change_calculation", "turning_points", "continuous_runs"],
    PrimaryIntent.COMPARISON_ANALYSIS: ["baseline_alignment", "absolute_and_rate_difference", "ratio_and_index"],
    PrimaryIntent.COMPOSITION_ANALYSIS: ["part_whole_reconciliation", "share_calculation", "cumulative_contribution", "concentration"],
    PrimaryIntent.ANOMALY_ANALYSIS: ["series_readiness_check", "robust_outlier_detection", "iqr_detection", "z_score_detection", "adjacent_jump_detection"],
    PrimaryIntent.ROOT_CAUSE_ANALYSIS: ["target_change_confirmation", "dimension_contribution_decomposition", "positive_negative_offset", "effect_reconciliation"],
    PrimaryIntent.FORECAST_ANALYSIS: ["forecast_readiness_check", "deterministic_model_selection", "backtest"],
}


def analysis_intent(intent: PrimaryIntent, question: str) -> PrimaryIntent:
    from app.analysis.intents import resolve_analysis_intent
    from app.analysis.tool_selection import independent_intent
    explicit = independent_intent(question)
    if explicit and intent == PrimaryIntent.METRIC_QUERY:
        return explicit
    return resolve_analysis_intent(intent, question)


def trend_comparison_modes(question: str) -> list[str]:
    from app.analysis.tool_selection import select_tools
    selection = select_tools(PrimaryIntent.TREND_ANALYSIS, question)
    selected = selection['capabilities'] if selection['mode'] == 'SELECTED' else None
    return [label for label, key in (('环比', 'period_over_period_change'), ('同比', 'year_over_year_change'))
            if (selected is None or key in selected) and not re.search(
        r'(?:不要|无需|不做|不用|不计算|不需要|不看)(?:进行|计算|分析|查看|做)?' + label, question)]


def build_requirements(intent: PrimaryIntent, question: str, structured: dict[str, Any], *, today: date) -> dict[str, Any]:
    intent = analysis_intent(intent, question)
    methods = list(METHODS[intent])
    if intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS:
        if "量价" in question:
            methods = ["baseline_alignment", "price_volume_decomposition", "effect_reconciliation"]
        elif multidimensional_operator_for_question(question):
            methods = ["baseline_current_alignment", "dimension_surprise_ranking", "explanatory_power_threshold", "root_cause_drilldown"]
    elif intent == PrimaryIntent.COMPOSITION_ANALYSIS:
        if "漏斗" in question:
            methods = ["funnel_stage_validation", "stage_conversion_calculation", "dropoff_ranking"]
        elif any(token in question for token in ("结构变化", "结构变动", "份额变化", "份额变动")):
            methods = ["period_alignment", "share_calculation", "structural_share_shift"]
    granularity = structured.get("时间粒度") or {}
    grain = normalize_grain(granularity.get("unit"))
    if grain is None and intent in {PrimaryIntent.TREND_ANALYSIS, PrimaryIntent.ANOMALY_ANALYSIS, PrimaryIntent.FORECAST_ANALYSIS}:
        if re.search(r'每日|每天|按日|分日|日度', question):
            grain = 'day'
        elif re.search(r'每周|按周|分周|周度', question):
            grain = 'week'
        elif re.search(r'季度|分季', question):
            grain = 'quarter'
        elif re.search(r'月', question):
            grain = 'month'
    target = resolve_target_window(str(granularity.get("time_range") or ""), grain, today=today)
    if intent == PrimaryIntent.TREND_ANALYSIS:
        # Full playbook is now the default; an explicit exclusion still wins.
        if '环比' in trend_comparison_modes(question):
            methods.append("period_over_period_change")
        if '同比' in trend_comparison_modes(question):
            methods.append("year_over_year_change")
    from app.analysis.tool_selection import select_tools
    selection = select_tools(intent, question)
    if selection['mode'] == 'SELECTED':
        methods = list(selection['capabilities'])
    result: dict[str, Any] = {
        "version": 1, "intent": intent.value, "methods": methods, "tool_selection": selection,
        "grain": grain, "reference_date": today.isoformat(), "timezone": "Asia/Shanghai",
        "time_anchor": granularity.get("anchor"),
        "minimum_points": 6 if intent == PrimaryIntent.FORECAST_ANALYSIS else 5 if intent == PrimaryIntent.ANOMALY_ANALYSIS else 2,
        "missing_policy": "REPORT_NOT_COMPUTABLE", "windows": [],
        "input_shape": {
            PrimaryIntent.TREND_ANALYSIS: "ordered_time_series",
            PrimaryIntent.COMPARISON_ANALYSIS: "comparable_groups_or_periods",
            PrimaryIntent.COMPOSITION_ANALYSIS: "non_overlapping_parts_and_total",
            PrimaryIntent.ANOMALY_ANALYSIS: "comparable_observations",
            PrimaryIntent.ROOT_CAUSE_ANALYSIS: "baseline_current_by_dimension",
            PrimaryIntent.FORECAST_ANALYSIS: "ordered_history",
        }[intent],
        "metric_names": [item.get("name") if isinstance(item, dict) else item for item in structured.get("指标") or []],
        "filters": copy.deepcopy(structured.get("过滤条件") or []),
    }
    if target:
        start, end = target
        result["target_window"] = {"start": start.isoformat(), "end_exclusive": (end + timedelta(days=1)).isoformat()}
        retrieval_start = start
        # A preceding complete bucket is useful only when the target begins on
        # a bucket boundary. Rolling partial months keep their exact day bounds.
        if intent == PrimaryIntent.TREND_ANALYSIS and "period_over_period_change" in methods:
            size = {"month": 1, "quarter": 3, "half_year": 6, "year": 12}.get(grain)
            aligned = ((grain == "month" and start.day == 1)
                       or (grain == "quarter" and start.day == 1 and start.month in {1, 4, 7, 10})
                       or (grain == "year" and start.month == 1 and start.day == 1)
                       or (grain == "half_year" and start.day == 1 and start.month in {1, 7}))
            if size and aligned:
                retrieval_start = shift_calendar_months(start, -size)
            elif grain == "day":
                retrieval_start = start - timedelta(days=1)
            elif grain == "week" and start.weekday() == 0:
                retrieval_start = start - timedelta(weeks=1)
        result["windows"].append({"role": "target", "start": retrieval_start.isoformat(), "end": end.isoformat()})
        if "year_over_year_change" in methods:
            # Two bounded queries, not one nineteen-month covering interval.
            result["windows"].append({"role": "yoy_baseline", "start": shift_calendar_months(start, -12).isoformat(), "end": shift_calendar_months(end, -12).isoformat()})
    return result
