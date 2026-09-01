"""Conversation-aware temporal resolution and immutable result reuse.

This module contains deterministic policy only.  It never asks a model to pick
an omitted year or to infer arithmetic from prose, and it never executes SQL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ConversationControl,
    PrimaryIntent,
    ResolvedPeriodComparison,
    SlotProvenance,
    SlotSource,
    TemporalAnchor,
    TimeRange,
    TurnAdmissionDecision,
)


_WHY_PATTERN = re.compile(r"为什么|为何|怎么会|原因|归因")
_RECOVERY_PATTERN = re.compile(r"恢复|回升|反弹")
_DECLINE_PATTERN = re.compile(r"下降|减少|下跌")
_PAIR_PATTERN = re.compile(r"较|相比|对比|比较|(?<!不)比")


@dataclass(frozen=True)
class DerivedPeriodComparison:
    left_period: str
    right_period: str
    left_value: Decimal
    right_value: Decimal
    metric_column: str
    delta: Decimal
    change_rate: Decimal | None
    direction: str

    def answer(self) -> str:
        amount = abs(self.delta)
        unit = "元" if any(
            marker in self.metric_column
            for marker in ("金额", "销售额", "含税", "成交额", "收入", "利润")
        ) else ""
        rate = (
            f"，变动幅度为{abs(self.change_rate):.2f}%"
            if self.change_rate is not None else ""
        )
        return (
            f"{self.left_period[:4]}年{int(self.left_period[5:])}月较"
            f"{self.right_period[:4]}年{int(self.right_period[5:])}月"
            f"{self.direction}{amount:,.2f}{unit}{rate}。"
            f"其中，{self.right_period[:4]}年{int(self.right_period[5:])}月为"
            f"{self.right_value:,.2f}{unit}，"
            f"{self.left_period[:4]}年{int(self.left_period[5:])}月为"
            f"{self.left_value:,.2f}{unit}。"
        )


def build_temporal_anchor(
    request: CanonicalAnalysisRequest,
    columns: Sequence[str] = (),
    rows: Sequence[Mapping[str, Any]] = (),
) -> TemporalAnchor | None:
    periods = sorted(set(_period_values(columns, rows)))
    if request.time_range is None and not periods:
        return None
    grain = _request_grain(request)
    if periods:
        first = _period_start(periods[0])
        last = _period_start(periods[-1])
        range_start = first
        range_end = _next_month(last) - timedelta(days=1)
    elif request.time_range is not None:
        range_start = request.time_range.start
        range_end = request.time_range.end_exclusive - timedelta(days=1)
    else:
        return None
    if not periods and grain == "month":
        current = date(range_start.year, range_start.month, 1)
        end_month = date(range_end.year, range_end.month, 1)
        while current <= end_month and len(periods) < 500:
            periods.append(current.strftime("%Y-%m"))
            current = _next_month(current)
    return TemporalAnchor(
        range_start=range_start,
        range_end=range_end,
        grain=grain,
        available_periods=periods,
    )


def resolve_conversation_temporal_context(
    request: CanonicalAnalysisRequest,
    previous: CanonicalAnalysisRequest | None,
    decision: TurnAdmissionDecision,
    question: str,
) -> CanonicalAnalysisRequest:
    """Resolve omitted years against the active analytical episode."""
    if not decision.inherit_business_context or previous is None:
        return request
    references = decision.current_turn_facts.temporal_references
    if not references:
        if _WHY_PATTERN.search(question):
            request.query_resolution_type = "FOLLOWUP_ANALYSIS"
        return request

    anchor = previous.temporal_anchor or build_temporal_anchor(previous)
    if anchor is None:
        return request
    request.temporal_anchor = anchor.model_copy(deep=True)
    resolved = [
        _resolve_reference(item, anchor)
        for item in references
    ]
    if any(value is None for value in resolved):
        return request
    periods = [value for value in resolved if value is not None]
    request.resolved_periods = list(dict.fromkeys(periods))

    comparison: ResolvedPeriodComparison | None = None
    if len(periods) >= 2 and _PAIR_PATTERN.search(question):
        comparison = ResolvedPeriodComparison(
            left_period=periods[0],
            right_period=periods[1],
            operation=(
                "DECLINE" if _DECLINE_PATTERN.search(question)
                else "RECOVERY" if _RECOVERY_PATTERN.search(question)
                else "CHANGE"
            ),
        )
    elif len(periods) == 1 and _RECOVERY_PATTERN.search(question):
        preceding = _preceding_period(periods[0], anchor.available_periods)
        if preceding is not None:
            request.resolved_periods.append(preceding)
            comparison = ResolvedPeriodComparison(
                left_period=periods[0],
                right_period=preceding,
                operation="RECOVERY",
            )

    if comparison is not None:
        request.resolved_comparison = comparison
        request.comparison_type = "PERIOD_TO_PERIOD"
        request.followup_type = "RESULT_COMPARISON_FOLLOWUP"
        request.query_resolution_type = "DERIVED_RESULT_QUERY"
        request.primary_intent = PrimaryIntent.COMPARISON_ANALYSIS
        request.operators = list(dict.fromkeys([
            *request.operators,
            AnalysisOperator.AGGREGATE,
        ]))
        request.time_range = _periods_time_range(
            [comparison.left_period, comparison.right_period]
        )
    else:
        request.time_range = _periods_time_range(periods)
        request.query_resolution_type = (
            "FOLLOWUP_ANALYSIS"
            if _WHY_PATTERN.search(question)
            else "DIRECT_DATA_QUERY"
        )

    request.conversation_control = ConversationControl.FOLLOW_UP
    request.slot_provenance["time_range"] = SlotProvenance(
        value=request.time_range.model_dump(mode="json"),
        source=SlotSource.CURRENT_REFERENCE_RESOLUTION,
        source_turn=next(
            (
                value.source_turn
                for value in decision.current_turn_facts.explicit_slots.values()
                if value.source_turn
            ),
            None,
        ),
        source_thread=decision.previous_thread_id,
        confidence=1.0,
    )
    if comparison is not None:
        request.slot_provenance["comparison"] = SlotProvenance(
            value=comparison.model_dump(mode="json"),
            source=SlotSource.CURRENT_REFERENCE_RESOLUTION,
            source_thread=decision.previous_thread_id,
            confidence=1.0,
        )
    return request


def derive_period_comparison(
    request: CanonicalAnalysisRequest,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> DerivedPeriodComparison | None:
    comparison = request.resolved_comparison
    if comparison is None:
        return None
    time_column = _time_column(columns, rows)
    metric_column = _metric_column(request, columns, rows, time_column)
    if time_column is None or metric_column is None:
        return None
    values: dict[str, Decimal] = {}
    duplicates: set[str] = set()
    for row in rows:
        period = _as_period(row.get(time_column))
        value = _decimal(row.get(metric_column))
        if period is None or value is None:
            continue
        if period in values:
            duplicates.add(period)
        else:
            values[period] = value
    if duplicates.intersection({comparison.left_period, comparison.right_period}):
        return None
    left = values.get(comparison.left_period)
    right = values.get(comparison.right_period)
    if left is None or right is None:
        return None
    delta = left - right
    rate = (delta / abs(right) * Decimal("100")) if right != 0 else None
    direction = "下降" if delta < 0 else "上升" if delta > 0 else "持平"
    return DerivedPeriodComparison(
        left_period=comparison.left_period,
        right_period=comparison.right_period,
        left_value=left,
        right_value=right,
        metric_column=metric_column,
        delta=delta,
        change_rate=rate,
        direction=direction,
    )


def _resolve_reference(item: Mapping[str, Any], anchor: TemporalAnchor) -> str | None:
    month = int(item["month"])
    if item.get("year") is not None:
        return f"{int(item['year']):04d}-{month:02d}"
    candidates = [
        period for period in anchor.available_periods
        if period.endswith(f"-{month:02d}")
    ]
    if len(candidates) == 1:
        return candidates[0]
    years = {
        anchor.range_start.year,
        anchor.range_end.year,
    }
    if len(years) == 1:
        return f"{anchor.range_start.year:04d}-{month:02d}"
    return None


def _preceding_period(period: str, available: Sequence[str]) -> str | None:
    ordered = sorted(set(available))
    if period in ordered:
        index = ordered.index(period)
        if index > 0:
            return ordered[index - 1]
    start = _period_start(period)
    previous_end = start - timedelta(days=1)
    return previous_end.strftime("%Y-%m")


def _periods_time_range(periods: Sequence[str]) -> TimeRange:
    starts = sorted(_period_start(value) for value in periods)
    return TimeRange(start=starts[0], end_exclusive=_next_month(starts[-1]))


def _request_grain(request: CanonicalAnalysisRequest) -> str:
    for assumption in request.assumptions:
        if assumption.startswith("DEFAULT_TIME_GRANULARITY="):
            value = assumption.split("=", 1)[1]
            if value in {"day", "week", "month", "quarter", "year"}:
                return value
    return "month"


def _period_values(
    columns: Sequence[str], rows: Sequence[Mapping[str, Any]]
) -> list[str]:
    column = _time_column(columns, rows)
    if column is None:
        return []
    return [
        period
        for row in rows
        if (period := _as_period(row.get(column))) is not None
    ]


def _time_column(
    columns: Sequence[str], rows: Sequence[Mapping[str, Any]]
) -> str | None:
    preferred = [
        column for column in columns
        if any(marker in str(column) for marker in ("日期", "月份", "月", "时间", "period"))
    ]
    candidates = preferred or list(columns)
    matches = [
        column for column in candidates
        if any(_as_period(row.get(column)) is not None for row in rows)
    ]
    return matches[0] if len(matches) == 1 else None


def _metric_column(
    request: CanonicalAnalysisRequest,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    time_column: str | None,
) -> str | None:
    names = [
        value
        for metric in request.metrics
        for value in (metric.canonical_name, metric.input)
        if value
    ]
    exact = [
        column for column in columns
        if column != time_column and any(
            str(column) == name or str(column) in name or name in str(column)
            for name in names
        )
    ]
    if len(exact) == 1:
        return exact[0]
    numeric = [
        column for column in columns
        if column != time_column
        and any(_decimal(row.get(column)) is not None for row in rows)
    ]
    return numeric[0] if len(numeric) == 1 else None


def _as_period(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    text = str(value or "").strip()
    match = re.match(r"^(?P<year>(?:19|20)\d{2})[-/.年](?P<month>1[0-2]|0?[1-9])", text)
    if match is None:
        return None
    return f"{int(match.group('year')):04d}-{int(match.group('month')):02d}"


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _period_start(period: str) -> date:
    year, month = (int(value) for value in period.split("-", 1))
    return date(year, month, 1)


def _next_month(value: date) -> date:
    return date(value.year + 1, 1, 1) if value.month == 12 else date(
        value.year, value.month + 1, 1
    )
