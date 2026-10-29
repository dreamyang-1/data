"""Composable deterministic business-analysis operators."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.analysis.multidimensional_attribution import (
    MultiDimensionalAttributionError,
    analyze_multidimensional_attribution,
)
from app.analysis.chain_tools.temporal_window import month_bucket_index, bucket_bounds


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

    def run(
        self, columns, rows, *, metric: str | None = None,
        dimension: str | None = None,
    ) -> OperatorResult:
        role = _one_column(columns, ("period_role", "期间角色", "对比角色"), "期间角色")
        if metric is None:
            value = _one_column(columns, ("金额", "销售额", "value", "metric"), "指标值")
        elif metric in columns:
            value = metric
        else:
            raise OperatorInputError("结构变化的绑定指标列未返回")
        candidates = [column for column in columns if column not in {role, value}]
        if dimension is not None:
            if dimension not in candidates:
                raise OperatorInputError("结构变化的绑定维度列未返回")
            candidates = [dimension]
        if len(candidates) != 1:
            display_columns = [
                column for column in candidates
                if not any(token in column.casefold() for token in ("编码", "代码", "编号", "标识"))
                and not column.casefold().endswith("id")
            ]
            if len(display_columns) != 1:
                raise OperatorInputError("结构变化必须返回唯一维度列")
            candidates = display_columns
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
        if not grouped or not any("base" in values for values in grouped.values()) or not any(
            "current" in values for values in grouped.values()
        ):
            raise OperatorInputError("结构变化必须同时包含基期和当前期数据")
        missing_members = [
            {"label": label, "missing_period": period}
            for label, values in grouped.items()
            for period in ("base", "current") if period not in values
        ]
        for values in grouped.values():
            values.setdefault("base", 0.0)
            values.setdefault("current", 0.0)
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
            "missing_period_members": missing_members,
        }, ([
            "完整分组结果中未出现的分组按该期0计入："
            + "、".join(item["label"] for item in missing_members)
        ] if missing_members else []))


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


_TEMPORAL_COLUMN_TOKENS = (
    "日期", "时间", "月份", "季度", "年份", "周期", "周",
    "date", "time", "month", "year", "quarter", "period", "week",
)


def _temporal_column(columns: list[str]) -> str | None:
    matches = [
        column for column in columns
        if any(token in column.lower() for token in _TEMPORAL_COLUMN_TOKENS)
    ]
    return matches[0] if len(matches) == 1 else None


def _metric_column(
    columns: list[str], rows: list[dict[str, Any]], temporal: str | None
) -> str:
    candidates = [
        column for column in columns
        if column != temporal
        and all(_number(row.get(column)) is not None for row in rows)
    ]
    if len(candidates) != 1:
        raise OperatorInputError("环比/同比序列必须且只能包含一个数值指标列")
    return candidates[0]


class PeriodOverPeriodOperator:
    """时间桶序列的逐期环比：每期差额与相对变化率。

    输入行须已按时间升序排列（由引擎的时间列校验保证）。首期环比
    涉及不完整首桶时由引擎侧剔除，本算子只负责纯计算。
    """

    name = "period_over_period_change"

    def run(self, columns, rows) -> OperatorResult:
        if len(rows) < 2:
            raise OperatorInputError("环比计算至少需要两个连续时间桶")
        temporal = _temporal_column(columns)
        if temporal is None:
            raise OperatorInputError("环比序列必须包含唯一的时间列")
        metric = _metric_column(columns, rows, temporal)
        series = []
        for row in rows:
            label = str(row.get(temporal, "")).strip()
            value = _number(row.get(metric))
            if not label or value is None:
                raise OperatorInputError(f"时间列或指标列“{metric}”包含空值或非数值")
            series.append((label, float(value)))
        changes = []
        zero_base_periods = []
        skipped_gaps = []
        for (base_label, base), (current_label, current) in zip(series, series[1:]):
            left, right = bucket_bounds(base_label), bucket_bounds(current_label)
            if left and right and left[1] != right[0]:
                skipped_gaps.append(current_label)
                continue
            rate = None if base == 0 else (current - base) / abs(base)
            if base == 0:
                zero_base_periods.append(current_label)
            changes.append({
                "from": base_label,
                "to": current_label,
                "base": base,
                "current": current,
                "absolute_change": current - base,
                "change_rate": rate,
            })
        facts = {
            "metric_column": metric,
            "period_count": len(changes),
            "changes": changes,
        }
        warnings = []
        if skipped_gaps:
            warnings.append("非相邻周期未计算环比：" + "、".join(skipped_gaps))
        if zero_base_periods:
            warnings.append(
                "以下期间基期为0，变化率不可计算，仅提供绝对差额："
                + "、".join(zero_base_periods)
            )
        return OperatorResult(self.name, facts, warnings)


class YearOverYearOperator:
    """逐月同比：本期月桶与上年同月桶对齐比较。

    上年同月没有数据时逐月标记不可计算并说明原因；全部缺失时整体
    拒算，不虚构基期值。
    """

    name = "year_over_year_change"

    def run(self, columns, rows) -> OperatorResult:
        temporal = _temporal_column(columns)
        if temporal is None:
            raise OperatorInputError("同比序列必须包含唯一的时间列")
        metric = _metric_column(columns, rows, temporal)
        buckets: dict[int, tuple[str, float]] = {}
        for row in rows:
            label = str(row.get(temporal, "")).strip()
            value = _number(row.get(metric))
            if not label or value is None:
                raise OperatorInputError(f"时间列或指标列“{metric}”包含空值或非数值")
            index = month_bucket_index(label)
            if index is None:
                raise OperatorInputError(
                    f"同比要求月桶标签带年份（如“2025-10”），无法解析：{label}"
                )
            if index in buckets:
                raise OperatorInputError(f"月桶“{label}”重复出现")
            buckets[index] = (label, float(value))
        changes = []
        missing_base = []
        for index in sorted(buckets):
            label, current = buckets[index]
            baseline = buckets.get(index - 12)
            if baseline is None:
                missing_base.append(label)
                changes.append({
                    "label": label,
                    "status": "NO_BASELINE",
                    "current": current,
                })
                continue
            base_label, base = baseline
            changes.append({
                "label": label,
                "status": "COMPUTABLE",
                "base_label": base_label,
                "base": base,
                "current": current,
                "absolute_change": current - base,
                "change_rate": None if base == 0 else (current - base) / abs(base),
            })
        computable = [item for item in changes if item["status"] == "COMPUTABLE"]
        facts = {
            "metric_column": metric,
            "month_count": len(changes),
            "computable_count": len(computable),
            "changes": changes,
        }
        warnings = []
        if not computable:
            facts["status"] = "NOT_COMPUTABLE"
            facts["reason"] = (
                "查询结果中没有上年同期数据，同比无法计算；"
                "需要数据源覆盖上年同月后才能比较"
            )
            return OperatorResult(self.name, facts, ["同比不可计算：" + facts["reason"]])
        if missing_base:
            warnings.append(
                "以下月份缺少上年同期基期，未参与同比：" + "、".join(missing_base)
            )
        return OperatorResult(self.name, facts, warnings)


class CumulativeContributionOperator:
    """分组贡献的帕累托计算：累计份额首次达到阈值的实体前缀集合。

    输入必须是完整分组结果（调用方负责拒绝 TopN 预览）。排序按指标
    降序、同值按标签稳定排序；阈值集合必须包含首次跨过阈值的边界
    实体。指标口径为净额时可能出现负值组：总量大于0的前提下，降序
    前缀不会触达负值组，负值组单独披露、不参与贡献排序。
    """

    name = "cumulative_contribution"

    def run(
        self, columns, rows, *, threshold: float = 0.8, metric: str | None = None,
    ) -> OperatorResult:
        if not 0 < threshold < 1:
            raise OperatorInputError("累计贡献阈值必须是0到1之间的比例")
        if len(rows) < 1:
            raise OperatorInputError("累计贡献至少需要一个分组")
        numeric_columns = [
            column for column in columns
            if all(_number(row.get(column)) is not None for row in rows)
        ]
        if metric is None:
            if len(numeric_columns) != 1:
                raise OperatorInputError("累计贡献结果必须且只能包含一个数值指标列")
            metric = numeric_columns[0]
        elif metric not in numeric_columns:
            raise OperatorInputError("累计贡献的绑定指标列必须包含完整数值")
        label_columns = [column for column in columns if column != metric]
        if len(label_columns) != 1:
            display_columns = [
                column for column in label_columns
                if not any(token in column.casefold() for token in ("编码", "代码", "编号", "标识"))
                and not column.casefold().endswith("id")
            ]
            if len(display_columns) != 1:
                raise OperatorInputError("累计贡献结果必须且只能包含一个分组标签列")
            label_columns = display_columns
        label = label_columns[0]
        items: list[tuple[str, float]] = []
        seen: set[str] = set()
        for row in rows:
            name = str(row.get(label, "")).strip()
            value = _number(row.get(metric))
            if not name or value is None or name in seen:
                raise OperatorInputError("累计贡献要求唯一、非空的分组标签和完整数值")
            seen.add(name)
            items.append((name, float(value)))
        total = sum(value for _, value in items)
        if total <= 0:
            raise OperatorInputError("分组合计不大于0，累计份额没有可解释的分母")
        # 降序 + 标签稳定次序，保证同值实体顺序可复现
        ranked = sorted(items, key=lambda item: (-item[1], item[0]))
        negatives = [(name, value) for name, value in ranked if value < 0]
        threshold_set: list[dict[str, Any]] = []
        cumulative = 0.0
        boundary_label: str | None = None
        boundary_share: float | None = None
        cumulative_share = 0.0
        for name, value in ranked:
            cumulative += value
            cumulative_share = cumulative / total
            threshold_set.append({
                "label": name,
                "value": value,
                "share": value / total,
                "cumulative_share": cumulative_share,
            })
            if cumulative_share >= threshold:
                boundary_label = name
                boundary_share = value / total
                break
        warnings: list[str] = []
        if negatives:
            warnings.append(
                f"{len(negatives)}个负值组（净额口径）未进入贡献前缀，合计"
                f"{sum(value for _, value in negatives):,.2f}，已在总量中抵减"
            )
        facts = {
            "metric_column": metric,
            "label_column": label,
            "threshold": threshold,
            "total": total,
            "group_count": len(items),
            "threshold_set_size": len(threshold_set),
            "threshold_set_coverage": cumulative_share,
            "boundary_label": boundary_label,
            "boundary_share": boundary_share,
            "threshold_set": threshold_set,
            "negative_group_count": len(negatives),
        }
        return OperatorResult(self.name, facts, warnings)


class AnalysisOperatorRegistry:
    def __init__(self) -> None:
        self._operators: dict[str, DeterministicOperator] = {}
        for operator in (
            ContributionDecompositionOperator(), PriceVolumeDecompositionOperator(),
            FunnelAnalysisOperator(), StructuralShiftOperator(),
            MultiDimensionalAttributionOperator(derived=False),
            MultiDimensionalAttributionOperator(derived=True),
            PeriodOverPeriodOperator(), YearOverYearOperator(),
            CumulativeContributionOperator(),
        ):
            self._operators[operator.name] = operator

    def execute(
        self, name: str, columns: list[str], rows: list[dict[str, Any]],
        **params: Any,
    ) -> OperatorResult:
        try:
            operator = self._operators[name]
        except KeyError as exc:
            raise OperatorInputError(f"unknown analysis operator: {name}") from exc
        return operator.run(columns, rows, **params)
