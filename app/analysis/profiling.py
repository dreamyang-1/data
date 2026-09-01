from __future__ import annotations

import math
import statistics
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


MAX_PROFILE_COLUMNS = 100


def profile_dataset(columns: list[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a bounded, deterministic profile without exposing sample values."""
    row_count = len(rows)
    profiles: list[dict[str, Any]] = []
    for column in columns[:MAX_PROFILE_COLUMNS]:
        values = [row.get(column) for row in rows]
        present = [value for value in values if value is not None and value != ""]
        numeric = [_number(value) for value in present]
        is_numeric = bool(present) and all(value is not None for value in numeric)
        is_temporal = bool(present) and all(_is_temporal(value) for value in present)
        unique_count = len({_stable_key(value) for value in present})
        item: dict[str, Any] = {
            "name": column,
            "inferred_type": (
                "NUMBER" if is_numeric else "DATETIME" if is_temporal else "TEXT"
            ),
            "null_count": row_count - len(present),
            "null_rate": 0.0 if row_count == 0 else (row_count - len(present)) / row_count,
            "unique_count": unique_count,
            "unique_rate": 0.0 if not present else unique_count / len(present),
        }
        if is_numeric:
            numbers = [value for value in numeric if value is not None]
            item["numeric_summary"] = {
                "minimum": min(numbers),
                "maximum": max(numbers),
                "mean": statistics.fmean(numbers),
                "median": statistics.median(numbers),
                "standard_deviation": (
                    statistics.pstdev(numbers) if len(numbers) > 1 else 0.0
                ),
            }
        profiles.append(item)
    return {
        "row_count": row_count,
        "column_count": len(columns),
        "profiled_column_count": len(profiles),
        "columns_truncated": len(columns) > MAX_PROFILE_COLUMNS,
        "columns": profiles,
    }


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
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


def _is_temporal(value: Any) -> bool:
    if isinstance(value, (date, datetime)):
        return True
    if not isinstance(value, str):
        return False
    text = value.strip().replace("Z", "+00:00")
    try:
        datetime.fromisoformat(text)
        return True
    except ValueError:
        return False


def _stable_key(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return repr(value)
    return f"{type(value).__name__}:{value!s}"
