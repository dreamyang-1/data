from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


class MultiDimensionalAttributionError(ValueError):
    pass


def _number(value: Any, field: str) -> float:
    if value is None or isinstance(value, bool):
        raise MultiDimensionalAttributionError(f"{field}必须是完整数值")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MultiDimensionalAttributionError(f"{field}必须是完整数值") from exc
    if not math.isfinite(result):
        raise MultiDimensionalAttributionError(f"{field}必须是有限数值")
    return result


def _js_component(before: float, after: float, before_total: float, after_total: float) -> float:
    if min(before, after, before_total, after_total) < 0 or before_total <= 0 or after_total <= 0:
        return 0.0
    p, q = before / before_total, after / after_total
    middle = (p + q) / 2
    left = 0.0 if p == 0 else p * math.log(p / middle)
    right = 0.0 if q == 0 else q * math.log(q / middle)
    return 0.5 * (left + right)


def analyze_multidimensional_attribution(
    rows: list[dict[str, Any]], *, derived: bool, issue_type: str | None = None,
    explanatory_threshold: float = 0.70, element_threshold: float = 0.02,
    maximum_dimensions: int = 3, maximum_values_per_dimension: int = 500,
) -> dict[str, Any]:
    if issue_type not in {None, "drop", "rise"}:
        raise MultiDimensionalAttributionError("issue_type只能是drop或rise")
    if not 0 < element_threshold < explanatory_threshold <= 1:
        raise MultiDimensionalAttributionError("归因阈值必须满足0 < element_threshold < explanatory_threshold <= 1")
    if maximum_dimensions < 1 or maximum_values_per_dimension < 2:
        raise MultiDimensionalAttributionError("维度数量限制必须为正，单维度至少允许2个元素")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        dimension = str(row.get("dimension_name", "")).strip()
        element = str(row.get("element_value", "")).strip()
        if not dimension or not element:
            raise MultiDimensionalAttributionError("dimension_name和element_value不能为空")
        key = (dimension, element)
        if key in seen:
            raise MultiDimensionalAttributionError("同一维度元素只能出现一行，需先聚合")
        seen.add(key)
        parsed = {"element": element}
        fields = (
            ("baseline_numerator", "baseline_denominator", "current_numerator", "current_denominator")
            if derived else ("baseline", "current")
        )
        parsed.update({field: _number(row.get(field), field) for field in fields})
        grouped[dimension].append(parsed)
    if not grouped:
        raise MultiDimensionalAttributionError("多维归因数据不能为空")

    dimension_details: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    comparable: list[dict[str, Any]] = []
    reconcilable: list[dict[str, Any]] = []
    for dimension, elements in grouped.items():
        if len(elements) > maximum_values_per_dimension:
            dimension_details[dimension] = {
                "status": "skipped", "reason": "HIGH_CARDINALITY_DIMENSION",
                "value_count": len(elements), "candidates": [],
            }
            warnings.append(f"维度“{dimension}”超过{maximum_values_per_dimension}个值，需先使用更粗粒度下钻")
            continue
        if derived:
            bn = sum(item["baseline_numerator"] for item in elements)
            bd = sum(item["baseline_denominator"] for item in elements)
            cn = sum(item["current_numerator"] for item in elements)
            cd = sum(item["current_denominator"] for item in elements)
            if bd <= 0 or cd <= 0:
                dimension_details[dimension] = {
                    "status": "skipped", "reason": "NON_POSITIVE_DENOMINATOR",
                    "value_count": len(elements), "candidates": [],
                }
                warnings.append(f"维度“{dimension}”的分母合计非正，无法执行比率归因")
                continue
            baseline_total, current_total = bn / bd, cn / cd
            total_delta = current_total - baseline_total
            reconciliation_values = (bn, bd, cn, cd)
            absolute_baseline_numerator = sum(abs(item["baseline_numerator"]) for item in elements)
            absolute_current_numerator = sum(abs(item["current_numerator"]) for item in elements)
            absolute_baseline_denominator = sum(abs(item["baseline_denominator"]) for item in elements)
            absolute_current_denominator = sum(abs(item["current_denominator"]) for item in elements)
        else:
            baseline_total = sum(item["baseline"] for item in elements)
            current_total = sum(item["current"] for item in elements)
            total_delta = current_total - baseline_total
            reconciliation_values = (baseline_total, current_total)
            absolute_baseline_total = sum(abs(item["baseline"]) for item in elements)
            absolute_current_total = sum(abs(item["current"]) for item in elements)
        direction = issue_type or ("drop" if total_delta < 0 else "rise" if total_delta > 0 else "flat")
        if direction == "flat" or (direction == "drop" and total_delta >= 0) or (
            direction == "rise" and total_delta <= 0
        ):
            detail = {
                "status": "no_anomaly_direction",
                "reason": "ZERO_TOTAL_DELTA" if direction == "flat" else "ISSUE_DIRECTION_MISMATCH",
                "value_count": len(elements), "baseline_total": baseline_total,
                "current_total": current_total, "total_delta": total_delta,
                "reconciliation_values": reconciliation_values, "candidates": [],
            }
            dimension_details[dimension] = detail
            reconcilable.append({"dimension": dimension, **detail})
            continue

        candidates = []
        all_effects = []
        for item in elements:
            if derived:
                numerator_change = item["current_numerator"] - item["baseline_numerator"]
                denominator_change = item["current_denominator"] - item["baseline_denominator"]
                numerator_effect = 0.5 * numerator_change * (1 / bd + 1 / cd)
                denominator_effect = -0.5 * denominator_change * (bn + cn) / (bd * cd)
                raw_effect = numerator_effect + denominator_effect
                before_value = 0.0 if item["baseline_denominator"] == 0 else item["baseline_numerator"] / item["baseline_denominator"]
                after_value = 0.0 if item["current_denominator"] == 0 else item["current_numerator"] / item["current_denominator"]
                surprise = _js_component(
                    abs(item["baseline_numerator"]), abs(item["current_numerator"]),
                    absolute_baseline_numerator, absolute_current_numerator,
                )
                surprise += _js_component(
                    abs(item["baseline_denominator"]), abs(item["current_denominator"]),
                    absolute_baseline_denominator, absolute_current_denominator,
                )
            else:
                before_value, after_value = item["baseline"], item["current"]
                raw_effect = after_value - before_value
                surprise = _js_component(
                    abs(before_value), abs(after_value),
                    absolute_baseline_total, absolute_current_total,
                )
            all_effects.append(raw_effect)
            if (direction == "drop" and raw_effect >= -1e-9) or (direction == "rise" and raw_effect <= 1e-9):
                continue
            ep = 0.0 if abs(total_delta) <= 1e-12 else raw_effect / total_delta
            if ep > element_threshold:
                candidates.append({
                    "element": item["element"], "baseline": before_value, "current": after_value,
                    "effect": raw_effect, "explanatory_power": ep, "surprise": surprise,
                    **({
                        "numerator_effect": numerator_effect,
                        "denominator_effect": denominator_effect,
                    } if derived else {}),
                })
        candidates.sort(key=lambda item: (-item["surprise"], -item["explanatory_power"], item["element"]))
        selected, cumulative = [], 0.0
        for candidate in candidates:
            selected.append(candidate)
            cumulative += candidate["explanatory_power"]
            if cumulative >= explanatory_threshold:
                break
        selected_elements = {item["element"] for item in selected}
        selected_rows = [item for item in elements if item["element"] in selected_elements]
        if derived:
            baseline_proportion = sum(abs(item["baseline_numerator"]) for item in selected_rows) / max(absolute_baseline_numerator, 1e-12)
            current_proportion = sum(abs(item["current_numerator"]) for item in selected_rows) / max(absolute_current_numerator, 1e-12)
        else:
            baseline_proportion = sum(abs(item["baseline"]) for item in selected_rows) / max(absolute_baseline_total, 1e-12)
            current_proportion = sum(abs(item["current"]) for item in selected_rows) / max(absolute_current_total, 1e-12)
        accepted = bool(selected and cumulative >= explanatory_threshold)
        if accepted and max(baseline_proportion, current_proportion) >= 0.98:
            accepted = False
            reason = "CANDIDATES_COVER_NEARLY_ALL_VALUE"
        else:
            reason = "THRESHOLD_REACHED" if accepted else "EXPLANATORY_POWER_BELOW_THRESHOLD"
        detail = {
            "status": "candidate" if accepted else "no_root_cause", "reason": reason,
            "value_count": len(elements), "baseline_total": baseline_total,
            "current_total": current_total, "total_delta": total_delta,
            "total_surprise": sum(item["surprise"] for item in candidates),
            "explanatory_power": cumulative, "candidates": selected,
            "reconciliation_values": reconciliation_values,
            "reconciliation_residual": total_delta - sum(all_effects),
            "unexplained_residual": total_delta - sum(
                candidate["effect"] for candidate in candidates
            ),
        }
        dimension_details[dimension] = detail
        reconcilable.append({"dimension": dimension, **detail})
        if accepted:
            comparable.append({"dimension": dimension, **detail})

    if len(reconcilable) > 1:
        signatures = [item["reconciliation_values"] for item in reconcilable]
        width = len(signatures[0])
        mismatched = any(
            max(values) - min(values) > 0.02 * max(max(abs(value) for value in values), 1.0)
            for values in ([signature[index] for signature in signatures] for index in range(width))
        )
        if mismatched:
            for item in reconcilable:
                detail = dimension_details[item["dimension"]]
                detail["status"] = "skipped"
                detail["reason"] = "TOTAL_MISMATCH_ACROSS_DIMENSIONS"
            comparable = []
            warnings.append("各维度的基期/当前期总量不一致，可能存在漏值或过滤口径差异，已停止跨维度归因")

    comparable.sort(key=lambda item: (-item["total_surprise"], -item["explanatory_power"], item["dimension"]))
    selected_dimensions = comparable[:maximum_dimensions]
    observed_delta = next(
        (item.get("total_delta") for item in dimension_details.values() if "total_delta" in item),
        0.0,
    )
    resolved_issue_type = issue_type or (
        "drop" if observed_delta < 0 else "rise" if observed_delta > 0 else None
    )
    status = (
        "success" if selected_dimensions and not warnings else
        "partial" if selected_dimensions else
        "no_anomaly_direction" if dimension_details and all(
            item["status"] == "no_anomaly_direction" for item in dimension_details.values()
        ) else "no_root_cause"
    )
    return {
        "status": status,
        "derived_metric": derived,
        "issue_type": resolved_issue_type,
        "root_causes": {
            item["dimension"]: [candidate["element"] for candidate in item["candidates"]]
            for item in selected_dimensions
        },
        "ranked_dimensions": [item["dimension"] for item in comparable],
        "dimension_details": dimension_details,
        "warnings": warnings,
        "causality_established": False,
    }
