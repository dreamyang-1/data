from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.engine import AnalysisOutput
from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent


class InsightNature(StrEnum):
    FACT = "FACT"
    INTERPRETATION = "INTERPRETATION"
    PRIORITY = "PRIORITY"
    LIMITATION = "LIMITATION"


class InterpretedInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    statement: str
    nature: InsightNature
    priority: int = Field(ge=1, le=100)
    evidence_fields: list[str] = Field(default_factory=list)


class StructuredAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: PrimaryIntent
    headline: str
    overall_trend: str | None = None
    phases: list[dict[str, Any]] = Field(default_factory=list)
    turning_points: list[dict[str, Any]] = Field(default_factory=list)
    current_state: str | None = None
    recovery: dict[str, Any] | None = None
    abnormal_changes: list[dict[str, Any]] = Field(default_factory=list)
    business_priority: str | None = None
    insights: list[InterpretedInsight] = Field(default_factory=list)
    internal_diagnostics: dict[str, Any] = Field(default_factory=dict)


class AnswerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str
    key_facts: list[str] = Field(default_factory=list)
    interpretations: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    omitted_internal_fields: list[str] = Field(default_factory=list)

    def render(self) -> str:
        sections = [f"结论：{self.headline}"]
        if self.key_facts:
            sections.append("关键事实：" + "；".join(self.key_facts) + "。")
        if self.interpretations:
            sections.append("分析判断：" + "；".join(self.interpretations) + "。")
        if self.priorities:
            sections.append("建议关注：" + "；".join(self.priorities) + "。")
        if self.limitations:
            sections.append("分析边界：" + "；".join(self.limitations) + "。")
        return "\n".join(sections)


class InsightInterpretationLayer:
    """Convert deterministic statistics into evidence-grounded business meaning."""

    def interpret(
        self,
        request: CanonicalAnalysisRequest,
        analysis: AnalysisOutput,
    ) -> StructuredAnalysisResult:
        if (
            request.primary_intent == PrimaryIntent.TREND_ANALYSIS
            and {"start", "end", "absolute_change", "period_changes"}
            <= set(analysis.facts)
        ):
            return self._interpret_trend(request, analysis)
        insights = [
            InterpretedInsight(
                category="deterministic_result",
                statement=analysis.answer,
                nature=InsightNature.FACT,
                priority=100,
                evidence_fields=["answer"],
            )
        ]
        insights.extend(
            InterpretedInsight(
                category="limitation",
                statement=warning,
                nature=InsightNature.LIMITATION,
                priority=80,
                evidence_fields=["warnings"],
            )
            for warning in analysis.warnings
        )
        return StructuredAnalysisResult(
            intent=request.primary_intent,
            headline=analysis.answer,
            insights=insights,
            internal_diagnostics={"method": analysis.method},
        )

    def _interpret_trend(
        self,
        request: CanonicalAnalysisRequest,
        analysis: AnalysisOutput,
    ) -> StructuredAnalysisResult:
        facts = analysis.facts
        start = float(facts["start"])
        end = float(facts["end"])
        change = float(facts["absolute_change"])
        change_rate = facts.get("change_rate")
        period_changes = list(facts.get("period_changes") or [])
        labels, values = self._reconstruct_series(start, period_changes)
        phases = self._phases(period_changes)
        turning_points = list(facts.get("change_points") or [])
        direction = "上升" if change > 0 else "下降" if change < 0 else "基本持平"
        magnitude = self._magnitude(change_rate)
        metric = str(facts.get("metric_column") or "指标")
        start_label = labels[0] if labels else "期初"
        end_label = labels[-1] if labels else "期末"
        rate_text = self._percent(abs(change_rate)) if change_rate is not None else None

        latest_delta = period_changes[-1]["absolute_change"] if period_changes else 0
        recovery = self._recovery(labels, values, change, latest_delta)
        current_state = self._current_state(direction, latest_delta, recovery)
        headline = self._headline(
            metric=metric,
            direction=direction,
            magnitude=magnitude,
            turning_points=turning_points,
            current_state=current_state,
        )

        overall_statement = (
            f"{metric}从{start_label}的{self._number(start)}变为"
            f"{end_label}的{self._number(end)}，整体{direction}"
            + (f"{rate_text}" if rate_text else f"{self._number(abs(change))}")
        )
        phase_statement = self._phase_statement(phases)
        current_statement = current_state
        if recovery and recovery.get("recovery_ratio") is not None:
            current_statement += (
                f"，已收复此前回撤的{self._percent(recovery['recovery_ratio'])}"
            )
        priority = self._business_priority(direction, current_state, change_rate)
        abnormal_changes = self._abnormal_changes(period_changes)
        insights = [
            InterpretedInsight(
                category="overall_trend",
                statement=overall_statement,
                nature=InsightNature.FACT,
                priority=100,
                evidence_fields=["start", "end", "absolute_change", "change_rate"],
            ),
        ]
        if phase_statement:
            insights.append(InterpretedInsight(
                category="phases",
                statement=phase_statement,
                nature=InsightNature.FACT,
                priority=90,
                evidence_fields=["period_changes", "change_points"],
            ))
        insights.extend([
            InterpretedInsight(
                category="current_state",
                statement=current_statement,
                nature=InsightNature.INTERPRETATION,
                priority=85,
                evidence_fields=["period_changes", "start", "end"],
            ),
            InterpretedInsight(
                category="business_priority",
                statement=priority,
                nature=InsightNature.PRIORITY,
                priority=70,
                evidence_fields=["change_rate", "period_changes"],
            ),
        ])
        if abnormal_changes:
            abnormal_summary = "；".join(
                f"{item['from']}至{item['to']}变化{abs(float(item['delta'])):,.2f}"
                for item in abnormal_changes[:2]
            )
            insights.append(InterpretedInsight(
                category="abnormal_changes",
                statement=f"相较常规波动，需重点核查：{abnormal_summary}。",
                nature=InsightNature.FACT,
                priority=85,
                evidence_fields=["period_changes"],
            ))
        limitation = (
            "当前结果只能验证时间变化，未包含产品、医院、经销商或地区贡献拆分，"
            "不能据此认定具体业务原因"
        )
        insights.append(InterpretedInsight(
            category="limitation",
            statement=limitation,
            nature=InsightNature.LIMITATION,
            priority=60,
            evidence_fields=["dimensions"],
        ))
        return StructuredAnalysisResult(
            intent=request.primary_intent,
            headline=headline,
            overall_trend=direction,
            phases=phases,
            turning_points=turning_points,
            current_state=current_state,
            recovery=recovery,
            abnormal_changes=abnormal_changes,
            business_priority=priority,
            insights=insights,
            internal_diagnostics={
                "method": analysis.method,
                "trend_diagnostics": facts.get("trend_diagnostics"),
                "minimum": facts.get("minimum"),
                "maximum": facts.get("maximum"),
            },
        )

    @staticmethod
    def _reconstruct_series(
        start: float, period_changes: list[dict[str, Any]]
    ) -> tuple[list[str], list[float]]:
        if not period_changes:
            return [], [start]
        labels = [str(period_changes[0]["from"])]
        values = [start]
        for item in period_changes:
            labels.append(str(item["to"]))
            values.append(values[-1] + float(item["absolute_change"]))
        return labels, values

    @staticmethod
    def _phases(period_changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        phases: list[dict[str, Any]] = []
        for item in period_changes:
            delta = float(item["absolute_change"])
            direction = "上升" if delta > 0 else "下降" if delta < 0 else "持平"
            if phases and phases[-1]["direction"] == direction:
                phases[-1]["end"] = str(item["to"])
                phases[-1]["absolute_change"] += delta
                phases[-1]["period_count"] += 1
            else:
                phases.append({
                    "start": str(item["from"]),
                    "end": str(item["to"]),
                    "direction": direction,
                    "absolute_change": delta,
                    "period_count": 1,
                })
        return phases

    @staticmethod
    def _recovery(
        labels: list[str], values: list[float], overall_change: float, latest_delta: float
    ) -> dict[str, Any] | None:
        if overall_change >= 0 or latest_delta <= 0 or len(values) < 3:
            return None
        trough_index = min(range(len(values)), key=values.__getitem__)
        if trough_index == 0 or trough_index == len(values) - 1:
            return None
        peak_before = max(values[:trough_index])
        trough = values[trough_index]
        drawdown = peak_before - trough
        recovered = values[-1] - trough
        return {
            "trough_period": labels[trough_index],
            "trough_value": trough,
            "recovered_amount": recovered,
            "recovery_ratio": None if drawdown <= 0 else recovered / drawdown,
            "remaining_gap_to_prior_peak": peak_before - values[-1],
        }

    @staticmethod
    def _current_state(
        overall_direction: str,
        latest_delta: float,
        recovery: dict[str, Any] | None,
    ) -> str:
        if overall_direction == "下降" and latest_delta > 0:
            ratio = recovery.get("recovery_ratio") if recovery else None
            if ratio is not None and ratio < 0.5:
                return "当前处于低位修复阶段，尚不能判断为趋势反转"
            return "当前出现回升，但仍需更多连续周期确认是否反转"
        if overall_direction == "上升" and latest_delta < 0:
            return "当前从前期高位回落，但尚未改变区间整体上升格局"
        if overall_direction == "下降":
            return "当前仍处于下行阶段"
        if overall_direction == "上升":
            return "当前延续上行状态"
        return "当前处于区间震荡状态"

    @staticmethod
    def _headline(
        *, metric: str, direction: str, magnitude: str,
        turning_points: list[dict[str, Any]], current_state: str,
    ) -> str:
        turning = "，期间出现方向转折" if turning_points else ""
        return f"{metric}整体{magnitude}{direction}{turning}；{current_state}。"

    @staticmethod
    def _business_priority(
        direction: str, current_state: str, change_rate: float | None
    ) -> str:
        if direction == "下降" and change_rate is not None and change_rate <= -0.2:
            if "修复" in current_state or "回升" in current_state:
                return "优先确认回升能否连续，并按产品、医院和经销商拆分定位主要缺口"
            return "优先排查主要下滑区间，并按产品、医院和经销商拆分贡献"
        return "持续观察下一周期变化，并对变化较大的业务维度做拆分核验"

    @staticmethod
    def _abnormal_changes(
        period_changes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if len(period_changes) < 4:
            return []
        magnitudes = sorted(abs(float(item["absolute_change"])) for item in period_changes)
        median = magnitudes[len(magnitudes) // 2]
        if median <= 0:
            return []
        return [
            item for item in period_changes
            if abs(float(item["absolute_change"])) >= 2.5 * median
        ][:3]

    @classmethod
    def _phase_statement(cls, phases: list[dict[str, Any]]) -> str:
        return "；".join(
            f"{item['start']}至{item['end']}{item['direction']}"
            f"{cls._number(abs(float(item['absolute_change'])))}"
            for item in phases[:4]
        )

    @staticmethod
    def _magnitude(change_rate: float | None) -> str:
        if change_rate is None:
            return ""
        value = abs(change_rate)
        return "明显" if value >= 0.2 else "温和" if value >= 0.05 else "小幅"

    @staticmethod
    def _percent(value: float) -> str:
        return f"{value:.2%}"

    @staticmethod
    def _number(value: float) -> str:
        if not math.isfinite(value):
            return str(value)
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}".rstrip("0").rstrip(".")


class AnswerPlanner:
    """Select decision-relevant insights and suppress diagnostic field dumps."""

    _INTERNAL_FIELDS = [
        "robust_slope_per_period",
        "normalized_slope",
        "direction_consistency",
        "coefficient_of_variation",
        "minimum",
        "maximum",
    ]

    def plan(self, result: StructuredAnalysisResult) -> AnswerPlan:
        ordered = sorted(result.insights, key=lambda item: item.priority, reverse=True)
        facts = [item.statement for item in ordered if item.nature == InsightNature.FACT][:3]
        interpretations = [
            item.statement for item in ordered
            if item.nature == InsightNature.INTERPRETATION
        ][:2]
        priorities = [
            item.statement for item in ordered if item.nature == InsightNature.PRIORITY
        ][:1]
        limitations = [
            item.statement for item in ordered if item.nature == InsightNature.LIMITATION
        ][:1]
        return AnswerPlan(
            headline=result.headline,
            key_facts=facts,
            interpretations=interpretations,
            priorities=priorities,
            limitations=limitations,
            omitted_internal_fields=list(self._INTERNAL_FIELDS),
        )
