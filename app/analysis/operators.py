"""Composable deterministic business-analysis operators."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.analysis.multidimensional_attribution import (
    MultiDimensionalAttributionError,
    analyze_multidimensional_attribution,
)


class OperatorInputError(ValueError):
    pass


@dataclass(frozen=True)
class OperatorResult:
    method: str
    facts: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


class DeterministicOperator(Protocol):
    name: str

    def run(self, columns: list[str], rows: list[dict[str, Any]]) -> OperatorResult: ...


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _one_column(columns: list[str], signals: tuple[str, ...], label: str) -> str:
    exact = [column for column in columns if column.casefold() in signals]
    if len(exact) == 1:
        return exact[0]
    matches = [column for column in columns if any(s in column.casefold() for s in signals)]
    if len(matches) != 1:
        raise OperatorInputError(f"{label}必须且只能匹配一列")
    return matches[0]


class ContributionDecompositionOperator:
    name = "dimension_contribution_decomposition"

    def run(self, columns, rows) -> OperatorResult:
        contribution = _one_column(columns, ("贡献", "差额", "影响", "增量", "delta"), "贡献值")
        label_columns = [column for column in columns if column != contribution]
        label = label_columns[0] if len(label_columns) == 1 else _one_column(
            label_columns, ("区域", "渠道", "商品", "客户", "供应商", "dimension", "name"), "维度"
        )
        items = []
        seen = set()
        for row in rows:
            name = str(row.get(label, "")).strip()
            value = _number(row.get(contribution))
            if not name or value is None or name in seen:
                raise OperatorInputError("贡献分解要求唯一、非空维度和完整数值")
            seen.add(name)
            items.append((name, value))
        absolute_total = sum(abs(value) for _, value in items)
        if absolute_total == 0:
            raise OperatorInputError("所有贡献值均为0，无法形成贡献排序")
        ranked = sorted(items, key=lambda item: abs(item[1]), reverse=True)
        return OperatorResult(self.name, {
            "dimension_column": label,
            "contribution_column": contribution,
            "contribution_sum": sum(value for _, value in ranked),
            "ranked_candidates": [
                {"label": name, "contribution": value,
                 "absolute_contribution_share": abs(value) / absolute_total}
                for name, value in ranked
            ],
            "causality_established": False,
        }, ["贡献度分解不能单独证明因果关系"])


class PriceVolumeDecompositionOperator:
    name = "price_volume_decomposition"

    def run(self, columns, rows) -> OperatorResult:
        if len(rows) != 2:
            raise OperatorInputError("量价分解必须且只能包含基期和当前期两行")
        price = _one_column(columns, ("单价", "price"), "价格")
        quantity = _one_column(columns, ("销量", "销售数量", "数量", "quantity", "volume"), "数量")
        role = _one_column(columns, ("period_role", "期间角色", "对比角色"), "期间角色")
        roles = [str(row.get(role, "")).strip().casefold() for row in rows]
        base_set, current_set = {"base", "baseline", "基期", "上期"}, {"current", "本期", "当前期"}
        if sum(value in base_set for value in roles) != 1 or sum(value in current_set for value in roles) != 1:
            raise OperatorInputError("period_role 必须包含唯一基期和当前期")
        base = rows[next(i for i, value in enumerate(roles) if value in base_set)]
        current = rows[next(i for i, value in enumerate(roles) if value in current_set)]
        p0, q0, p1, q1 = map(_number, (base.get(price), base.get(quantity), current.get(price), current.get(quantity)))
        if any(value is None for value in (p0, q0, p1, q1)):
            raise OperatorInputError("价格和数量必须是完整数值")
        assert p0 is not None and q0 is not None and p1 is not None and q1 is not None
        base_value, current_value = p0 * q0, p1 * q1
        volume_effect = (q1 - q0) * p0
        price_effect = (p1 - p0) * q0
        interaction = (p1 - p0) * (q1 - q0)
        return OperatorResult(self.name, {
            "price_column": price, "quantity_column": quantity,
            "base_value": base_value, "current_value": current_value,
            "total_change": current_value - base_value,
            "volume_effect": volume_effect, "price_effect": price_effect,
            "interaction_effect": interaction,
            "reconciliation_residual": current_value - base_value - volume_effect - price_effect - interaction,
            "causality_established": False,
        }, ["量价效应是恒等式分解，不代表价格或数量变化的因果机制"])


class FunnelAnalysisOperator:
    name = "funnel_conversion_analysis"

    def run(self, columns, rows) -> OperatorResult:
        if len(rows) < 2:
            raise OperatorInputError("漏斗分析至少需要两个阶段")
        stage = _one_column(columns, ("阶段", "stage"), "漏斗阶段")
        order = _one_column(columns, ("阶段顺序", "stage_order", "step_order"), "阶段顺序")
        count = _one_column(columns, ("人数", "数量", "count", "users"), "阶段数量")
        parsed = []
        for row in rows:
            name, position, value = str(row.get(stage, "")).strip(), _number(row.get(order)), _number(row.get(count))
            if not name or position is None or value is None or value < 0:
                raise OperatorInputError("漏斗阶段、顺序和数量必须完整且数量非负")
            parsed.append((position, name, value))
        parsed.sort()
        if len({item[0] for item in parsed}) != len(parsed) or len({item[1] for item in parsed}) != len(parsed):
            raise OperatorInputError("漏斗阶段和阶段顺序必须唯一")
        if any(parsed[i][2] > parsed[i - 1][2] for i in range(1, len(parsed))):
            raise OperatorInputError("后续漏斗阶段数量不能大于前序阶段")
        transitions = []
        for previous, current in zip(parsed, parsed[1:]):
            rate = None if previous[2] == 0 else current[2] / previous[2]
            transitions.append({
                "from": previous[1], "to": current[1], "from_count": previous[2],
                "to_count": current[2], "conversion_rate": rate,
                "dropoff_count": previous[2] - current[2],
                "dropoff_rate": None if rate is None else 1 - rate,
            })
        return OperatorResult(self.name, {
            "stages": [{"stage": name, "count": value} for _, name, value in parsed],
            "transitions": transitions,
            "overall_conversion_rate": None if parsed[0][2] == 0 else parsed[-1][2] / parsed[0][2],
        })


class StructuralShiftOperator:
    name = "structural_share_shift"

    def run(self, columns, rows) -> OperatorResult:
        role = _one_column(columns, ("period_role", "期间角色", "对比角色"), "期间角色")
        value = _one_column(columns, ("金额", "销售额", "value", "metric"), "指标值")
        candidates = [column for column in columns if column not in {role, value}]
        if len(candidates) != 1:
            raise OperatorInputError("结构变化必须返回唯一维度列")
        dimension = candidates[0]
        grouped: dict[str, dict[str, float]] = {}
        for row in rows:
            label = str(row.get(dimension, "")).strip()
            period = str(row.get(role, "")).strip().casefold()
            number = _number(row.get(value))
            normalized = "base" if period in {"base", "baseline", "基期", "上期"} else "current" if period in {"current", "本期", "当前期"} else ""
            if not label or not normalized or number is None or number < 0 or normalized in grouped.setdefault(label, {}):
                raise OperatorInputError("每个维度必须且只能包含一条基期和当前期非负数值")
            grouped[label][normalized] = number
        if not grouped or any(set(values) != {"base", "current"} for values in grouped.values()):
            raise OperatorInputError("所有维度必须同时具备基期和当前期")
        base_total = sum(item["base"] for item in grouped.values())
        current_total = sum(item["current"] for item in grouped.values())
        if base_total <= 0 or current_total <= 0:
            raise OperatorInputError("基期和当前期合计必须大于0")
        shifts = sorted(({
            "label": label, "base_share": item["base"] / base_total,
            "current_share": item["current"] / current_total,
            "share_change": item["current"] / current_total - item["base"] / base_total,
        } for label, item in grouped.items()), key=lambda item: abs(item["share_change"]), reverse=True)
        return OperatorResult(self.name, {
            "dimension_column": dimension, "value_column": value,
            "base_total": base_total, "current_total": current_total,
            "shifts": shifts,
            "share_change_residual": sum(item["share_change"] for item in shifts),
        })


class MultiDimensionalAttributionOperator:
    def __init__(self, *, derived: bool) -> None:
        self.derived = derived
        self.name = (
            "multidimensional_ratio_attribution"
            if derived else "multidimensional_attribution"
        )

    def run(self, columns, rows) -> OperatorResult:
        try:
            facts = analyze_multidimensional_attribution(rows, derived=self.derived)
        except MultiDimensionalAttributionError as exc:
            raise OperatorInputError(str(exc)) from exc
        warnings = list(facts.pop("warnings"))
        warnings.append("多维归因识别的是可解释贡献，不代表已建立因果关系")
        return OperatorResult(self.name, facts, warnings)


class AnalysisOperatorRegistry:
    def __init__(self) -> None:
        self._operators: dict[str, DeterministicOperator] = {}
        for operator in (
            ContributionDecompositionOperator(), PriceVolumeDecompositionOperator(),
            FunnelAnalysisOperator(), StructuralShiftOperator(),
            MultiDimensionalAttributionOperator(derived=False),
            MultiDimensionalAttributionOperator(derived=True),
        ):
            self._operators[operator.name] = operator

    def execute(self, name: str, columns: list[str], rows: list[dict[str, Any]]) -> OperatorResult:
        try:
            operator = self._operators[name]
        except KeyError as exc:
            raise OperatorInputError(f"unknown analysis operator: {name}") from exc
        return operator.run(columns, rows)
