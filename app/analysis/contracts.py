"""Canonical result-set contracts for composable analysis operators."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.domain.models import (
    AnalysisDataContractSpec,
    AnalysisOperator,
    CanonicalAnalysisRequest,
    PrimaryIntent,
)


@dataclass(frozen=True)
class ContractViolation:
    missing_roles: list[str]
    ambiguous_roles: dict[str, list[str]]
    invalid_numeric_roles: list[str]
    row_count_error: str | None

    @property
    def valid(self) -> bool:
        return not (
            self.missing_roles or self.ambiguous_roles
            or self.invalid_numeric_roles or self.row_count_error
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "missing_roles": self.missing_roles,
            "ambiguous_roles": self.ambiguous_roles,
            "invalid_numeric_roles": self.invalid_numeric_roles,
            "row_count_error": self.row_count_error,
        }


@dataclass(frozen=True)
class ExplicitObjectComparisonScope:
    """User-authorized finite object set for descriptive comparisons.

    This is intentionally separate from the SQL analysis-operator contracts:
    missing members may legitimately mean that an explicitly requested object
    has no matching row.  Two returned objects are enough for a descriptive
    comparison, while the analysis result must disclose every missing member.
    """

    filter_field: str
    requested_objects: tuple[str, ...]
    minimum_returned_objects: int = 2


def explicit_object_comparison_scope(
    request: CanonicalAnalysisRequest,
) -> ExplicitObjectComparisonScope | None:
    """Resolve an explicit object-name ``IN`` filter without guessing its role."""

    if (
        request.primary_intent != PrimaryIntent.COMPARISON_ANALYSIS
        or request.comparison_type != "对象间比较"
    ):
        return None

    dimensions = [
        _comparison_token(value)
        for value in [*request.dimensions, request.entity or ""]
        if value
    ]
    candidates: list[tuple[int, str, tuple[str, ...]]] = []
    for item in request.filters:
        field = str(item.get("field") or "").strip()
        values = item.get("value")
        if (
            not field
            or str(item.get("operator") or "").upper() != "IN"
            or not isinstance(values, list)
        ):
            continue
        objects = tuple(dict.fromkeys(
            str(value).strip()
            for value in values
            if not isinstance(value, (dict, list, tuple, set, bool))
            and str(value).strip()
        ))
        if len(objects) < 2:
            continue
        normalized_field = _comparison_token(field)
        score = 0
        for dimension in dimensions:
            if normalized_field in {
                dimension,
                f"{dimension}名称",
                f"{dimension}name",
            }:
                score = max(score, 6)
            elif dimension and dimension in normalized_field:
                score = max(score, 4)
        if normalized_field.endswith("名称") or normalized_field.endswith("name"):
            score = max(score, 2)
        if score:
            candidates.append((score, field, objects))

    if not candidates:
        return None
    highest = max(score for score, _, _ in candidates)
    best = [item for item in candidates if item[0] == highest]
    if len(best) != 1:
        return None
    _, field, objects = best[0]
    return ExplicitObjectComparisonScope(field, objects)


def ordered_entity_metric_ranking_request(
    request: CanonicalAnalysisRequest,
) -> bool:
    """Return whether a comparison is an ordered entity set, not a period delta.

    A full entity list sorted by one metric has no baseline/current roles.  It is
    safe to route to ranking validation only when the canonical request carries
    every part of that shape explicitly.  Named finite-object comparisons stay
    on their stricter scope-aware path.
    """

    return bool(
        request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
        and request.comparison_type == "对象间比较"
        and not re.search(
            r"(?:下降|减少|下跌)(?:幅度)?(?:最大|最多)",
            request.original_question or "",
        )
        and AnalysisOperator.SORT in request.operators
        and AnalysisOperator.GROUP_BY in request.operators
        and len(request.metrics) == 1
        and request.dimensions
        and explicit_object_comparison_scope(request) is None
    )


def _comparison_token(value: str) -> str:
    return re.sub(r"[\s_.:\-/]+", "", value).casefold()


_CONTRACTS = {
    "price_volume_decomposition": AnalysisDataContractSpec(
        operator="price_volume_decomposition", minimum_rows=2, maximum_rows=2,
        required_columns={
            "period_role": ["period_role", "期间角色", "对比角色"],
            "price": ["price", "单价"],
            "quantity": ["quantity", "volume", "销量", "销售数量", "数量"],
        },
    ),
    "funnel_conversion_analysis": AnalysisDataContractSpec(
        operator="funnel_conversion_analysis", minimum_rows=2,
        required_columns={
            "stage_order": ["stage_order", "step_order", "阶段顺序"],
            "stage": ["stage", "阶段"],
            "count": ["count", "users", "人数", "数量"],
        },
    ),
    "structural_share_shift": AnalysisDataContractSpec(
        operator="structural_share_shift", minimum_rows=2,
        required_columns={
            "period_role": ["period_role", "期间角色", "对比角色"],
            "dimension": ["dimension", "维度", "区域", "渠道", "商品", "客户", "供应商"],
            "value": ["value", "metric", "金额", "销售额"],
        },
    ),
    "dimension_contribution_decomposition": AnalysisDataContractSpec(
        operator="dimension_contribution_decomposition", minimum_rows=2,
        required_columns={
            "dimension": ["dimension", "维度", "区域", "渠道", "商品", "客户", "供应商"],
            "contribution": ["contribution", "delta", "贡献", "差额", "影响", "增量"],
        },
    ),
    "multidimensional_attribution": AnalysisDataContractSpec(
        operator="multidimensional_attribution", minimum_rows=4,
        required_columns={
            "dimension_name": ["dimension_name"],
            "element_value": ["element_value"],
            "baseline": ["baseline"],
            "current": ["current"],
        },
        instructions=[
            "每个dimension_name必须表示同一查询范围和同一指标总体的一种完整、互斥拆分",
            "每个维度元素只返回一行，baseline和current必须使用完全一致的过滤与指标口径",
            "dimension_name可表示单维或有业务意义的层级组合，但禁止返回原始明细或笛卡尔积",
        ],
    ),
    "multidimensional_ratio_attribution": AnalysisDataContractSpec(
        operator="multidimensional_ratio_attribution", minimum_rows=4,
        required_columns={
            "dimension_name": ["dimension_name"],
            "element_value": ["element_value"],
            "baseline_numerator": ["baseline_numerator"],
            "baseline_denominator": ["baseline_denominator"],
            "current_numerator": ["current_numerator"],
            "current_denominator": ["current_denominator"],
        },
        instructions=[
            "每个dimension_name必须表示同一查询范围下同一比率指标的一种完整、互斥拆分",
            "必须返回可加总的分子和分母，禁止只返回预先计算的比率、均值或百分比",
            "各维度的分子合计和分母合计必须分别一致，基期与当前期必须使用相同口径",
        ],
    ),
}


_MULTIDIMENSIONAL_SIGNALS = ("多维", "各维度", "多个维度", "逐层下钻", "维度下钻")
_RATIO_SIGNALS = ("率", "占比", "转化", "客单价")


def multidimensional_operator_for_question(question: str) -> str | None:
    compact = question.replace(" ", "")
    if not any(token in compact for token in _MULTIDIMENSIONAL_SIGNALS):
        return None
    return (
        "multidimensional_ratio_attribution"
        if any(token in compact for token in _RATIO_SIGNALS)
        else "multidimensional_attribution"
    )


def contract_for_request(request: CanonicalAnalysisRequest) -> AnalysisDataContractSpec | None:
    question = request.original_question.replace(" ", "")
    name = None
    if request.primary_intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS:
        if "量价" in question:
            name = "price_volume_decomposition"
        elif multidimensional := multidimensional_operator_for_question(question):
            name = multidimensional
        else:
            name = "dimension_contribution_decomposition"
    elif request.primary_intent == PrimaryIntent.COMPOSITION_ANALYSIS and "漏斗" in question:
        name = "funnel_conversion_analysis"
    elif request.primary_intent == PrimaryIntent.COMPOSITION_ANALYSIS and any(
        token in question for token in ("结构变化", "结构变动", "份额变化", "份额变动")
    ):
        name = "structural_share_shift"
    return _CONTRACTS.get(name)


def contract_instruction(contract: AnalysisDataContractSpec) -> str:
    return (
        "\n执行要求：本次分析必须返回符合以下结果契约的数据；每个语义角色只能对应一列，"
        "不得用普通汇总值替代。结果契约："
        + json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    )


def validate_contract(
    contract: AnalysisDataContractSpec, columns: list[str], rows: list[dict[str, Any]]
) -> ContractViolation:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for role, aliases in contract.required_columns.items():
        folded_aliases = {alias.casefold() for alias in aliases}
        exact = [column for column in columns if column.casefold() in folded_aliases]
        matches = exact or [
            column for column in columns
            if any(alias in column.casefold() for alias in folded_aliases)
        ]
        if not matches:
            missing.append(role)
        elif len(matches) > 1:
            ambiguous[role] = matches
        else:
            resolved[role] = matches[0]
    numeric_roles = {
        "price", "quantity", "count", "value", "contribution", "stage_order",
        "baseline", "current", "baseline_numerator", "baseline_denominator",
        "current_numerator", "current_denominator",
    }
    invalid_numeric = []
    for role in numeric_roles & resolved.keys():
        column = resolved[role]
        if any(not _finite_number(row.get(column)) for row in rows):
            invalid_numeric.append(role)
    row_error = None
    if len(rows) < contract.minimum_rows:
        row_error = f"expected at least {contract.minimum_rows} rows, got {len(rows)}"
    elif contract.maximum_rows is not None and len(rows) > contract.maximum_rows:
        row_error = f"expected at most {contract.maximum_rows} rows, got {len(rows)}"
    return ContractViolation(missing, ambiguous, invalid_numeric, row_error)


def _finite_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and abs(number) != float("inf")
