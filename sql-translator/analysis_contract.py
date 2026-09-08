"""Validation of analysis result contracts at the SQL execution boundary."""
from __future__ import annotations

import math
import re
from datetime import date, datetime


ALLOWED_OPERATORS = {
    "dimension_contribution_decomposition",
    "price_volume_decomposition",
    "funnel_conversion_analysis",
    "structural_share_shift",
    "multidimensional_attribution",
    "multidimensional_ratio_attribution",
}


def normalize_contract(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("analysis_contract must be an object")
    operator = value.get("operator") or value.get("analysis_operator")
    required = value.get("required_columns")
    if operator not in ALLOWED_OPERATORS:
        raise ValueError("analysis_contract operator is invalid")
    if not isinstance(required, dict) or not required:
        raise ValueError("analysis_contract required_columns must be non-empty")
    if any(
        not isinstance(role, str) or not role
        or not isinstance(aliases, list) or not aliases
        or any(not isinstance(alias, str) or not alias for alias in aliases)
        for role, aliases in required.items()
    ):
        raise ValueError("analysis_contract aliases are invalid")
    minimum = value.get("minimum_rows", 1)
    maximum = value.get("maximum_rows")
    if type(minimum) is not int or minimum < 1:
        raise ValueError("analysis_contract minimum_rows is invalid")
    if maximum is not None and (type(maximum) is not int or maximum < minimum):
        raise ValueError("analysis_contract maximum_rows is invalid")
    return {
        "operator": operator,
        "required_columns": required,
        "minimum_rows": minimum,
        "maximum_rows": maximum,
    }


def validate_result(contract, columns, rows):
    contract = normalize_contract(contract)
    if contract is None:
        return None
    columns = columns if isinstance(columns, list) else []
    rows = rows if isinstance(rows, list) else []
    resolved, missing, ambiguous = _resolve_columns(contract, columns)
    numeric = {"price", "quantity", "count", "value", "contribution", "stage_order"}
    invalid_numeric = [
        role for role in numeric & resolved.keys()
        if any(not _finite(row.get(resolved[role])) for row in rows if isinstance(row, dict))
    ]
    row_error = None
    if len(rows) < contract["minimum_rows"]:
        row_error = f"expected at least {contract['minimum_rows']} rows, got {len(rows)}"
    elif contract["maximum_rows"] is not None and len(rows) > contract["maximum_rows"]:
        row_error = f"expected at most {contract['maximum_rows']} rows, got {len(rows)}"
    violations = {
        "missing_roles": missing,
        "ambiguous_roles": ambiguous,
        "invalid_numeric_roles": invalid_numeric,
        "row_count_error": row_error,
    }
    return {
        "contract_version": "1.0",
        "operator": contract["operator"],
        "contract_satisfied": not any((missing, ambiguous, invalid_numeric, row_error)),
        "resolved_columns": resolved,
        "violations": violations,
        "producer": "SQL_TRANSLATOR",
    }


def normalize_result(contract, columns, rows):
    """Safely reduce price/quantity detail rows into two comparable month rows."""
    contract = normalize_contract(contract)
    columns = columns if isinstance(columns, list) else []
    rows = rows if isinstance(rows, list) else []
    metadata = {
        "applied": False,
        "producer": "SQL_TRANSLATOR",
    }
    if contract is None or contract["operator"] != "price_volume_decomposition":
        metadata["reason"] = "operator_not_supported"
        return columns, rows, metadata

    resolved, missing, ambiguous = _resolve_columns(contract, columns)
    if "period_role" in resolved:
        metadata["reason"] = "period_role_already_present"
        return columns, rows, metadata
    if any(role in missing or role in ambiguous for role in ("price", "quantity")):
        metadata["reason"] = "price_or_quantity_column_unresolved"
        return columns, rows, metadata

    excluded = {resolved["price"], resolved["quantity"]}
    temporal = [
        column for column in columns
        if column not in excluded and _is_temporal_column(column, rows)
    ]
    if len(temporal) != 1:
        metadata["reason"] = "temporal_column_missing" if not temporal else "temporal_column_ambiguous"
        metadata["temporal_candidates"] = [str(column) for column in temporal]
        return columns, rows, metadata

    time_column = temporal[0]
    buckets = {}
    for row in rows:
        if not isinstance(row, dict):
            metadata["reason"] = "invalid_row"
            return columns, rows, metadata
        month = _parse_month(row.get(time_column))
        price = row.get(resolved["price"])
        quantity = row.get(resolved["quantity"])
        if month is None or not _finite(price) or not _finite(quantity):
            metadata["reason"] = "invalid_time_or_numeric_value"
            return columns, rows, metadata
        price, quantity = float(price), float(quantity)
        if price < 0 or quantity < 0:
            metadata["reason"] = "negative_price_or_quantity"
            return columns, rows, metadata
        total = buckets.setdefault(month, {"quantity": 0.0, "amount": 0.0})
        total["quantity"] += quantity
        total["amount"] += price * quantity

    periods = sorted(buckets)
    if len(periods) != 2:
        metadata["reason"] = "expected_exactly_two_months"
        metadata["periods"] = [_month_label(month) for month in periods]
        return columns, rows, metadata
    if any(buckets[month]["quantity"] <= 0 for month in periods):
        metadata["reason"] = "non_positive_month_quantity"
        return columns, rows, metadata

    normalized = []
    for role, month in zip(("BASE", "CURRENT"), periods):
        total = buckets[month]
        normalized.append({
            "period_role": role,
            "price": total["amount"] / total["quantity"],
            "quantity": total["quantity"],
        })
    metadata.update({
        "applied": True,
        "method": "two_month_weighted_price_volume",
        "source_row_count": len(rows),
        "source_time_column": str(time_column),
        "periods": [_month_label(month) for month in periods],
        "aggregation": {"price": "quantity_weighted_average", "quantity": "sum"},
    })
    return ["period_role", "price", "quantity"], normalized, metadata


def _resolve_columns(contract, columns):
    resolved, missing, ambiguous = {}, [], {}
    for role, aliases in contract["required_columns"].items():
        folded = {alias.casefold() for alias in aliases}
        exact = [column for column in columns if str(column).casefold() in folded]
        matches = exact or [
            column for column in columns
            if any(alias in str(column).casefold() for alias in folded)
        ]
        if not matches:
            missing.append(role)
        elif len(matches) > 1:
            ambiguous[role] = matches
        else:
            resolved[role] = matches[0]
    return resolved, missing, ambiguous


def _is_temporal_column(column, rows):
    name = str(column).casefold()
    name_hint = bool(re.search(r"(^|[_\s])(date|time|month)([_\s]|$)|_at$", name)) or any(
        token in name for token in ("日期", "时间", "月份")
    )
    values = [row.get(column) for row in rows if isinstance(row, dict)]
    return bool(values) and all(_parse_month(value) is not None for value in values) and (
        name_hint or any(isinstance(value, (date, datetime)) for value in values)
    )


def _parse_month(value):
    if isinstance(value, datetime):
        return value.year, value.month
    if isinstance(value, date):
        return value.year, value.month
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = re.match(r"^(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?(?:[ T].*)?$", text)
    if not match:
        match = re.match(r"^(\d{4})年(\d{1,2})月(?:\d{1,2}日)?(?:\s.*)?$", text)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return (year, month) if 1 <= month <= 12 else None


def _month_label(month):
    return f"{month[0]:04d}-{month[1]:02d}"


def _finite(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)
