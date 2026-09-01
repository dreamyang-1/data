"""Bounded deterministic discovery of noteworthy dataset patterns."""
from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_NUMERIC_COLUMNS = 10
MAX_CATEGORY_COLUMNS = 20
MAX_SERIES_POINTS = 200


def discover_insights(columns: list[str], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return []
    numeric = [c for c in columns if _numeric_series(c, rows) is not None and not _identifier(c)]
    numeric = numeric[:MAX_NUMERIC_COLUMNS]
    temporal = [c for c in columns if _temporal_series(c, rows)]
    findings: list[dict[str, Any]] = []

    for column in columns[:MAX_CATEGORY_COLUMNS]:
        if column in numeric or column in temporal or _identifier(column):
            continue
        values = [str(row.get(column)).strip() for row in rows if row.get(column) not in (None, "")]
        counts = Counter(values)
        if 2 <= len(counts) <= 50 and values:
            label, count = counts.most_common(1)[0]
            share = count / len(values)
            if share >= 0.5:
                findings.append(_finding("DOMINANT_CATEGORY", column, share, min(1.0, len(values) / 30), {
                    "label": label, "count": count, "share": share,
                    "non_null_count": len(values), "category_count": len(counts),
                }))

    for column in numeric:
        values = _numeric_series(column, rows)
        assert values is not None
        if len(values) >= 5:
            median = statistics.median(values)
            mad = statistics.median(abs(value - median) for value in values)
            if mad > 0:
                outliers = [value for value in values if abs(0.6745 * (value - median) / mad) >= 3.5]
                if outliers:
                    findings.append(_finding(
                        "ROBUST_OUTLIERS", column,
                        min(1.0, len(outliers) / len(values) * 5), min(1.0, len(values) / 30),
                        {"outlier_count": len(outliers), "sample_count": len(values), "median": median, "mad": mad},
                    ))

    if temporal:
        time_column = temporal[0]
        for column in numeric:
            pairs = [(_time_key(row.get(time_column)), _number(row.get(column))) for row in rows]
            if any(key is None or value is None for key, value in pairs):
                continue
            pairs.sort(key=lambda item: item[0])
            values = _bounded([float(value) for _, value in pairs])
            if len(values) < 3:
                continue
            deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
            non_zero = [value for value in deltas if value != 0]
            consistency = 1.0 if not non_zero else max(
                sum(value > 0 for value in non_zero), sum(value < 0 for value in non_zero)
            ) / len(non_zero)
            scale = max(statistics.median(abs(value) for value in values), 1e-12)
            normalized_change = (values[-1] - values[0]) / scale
            if abs(normalized_change) >= 0.05 and consistency >= 0.6:
                findings.append(_finding("TIME_TREND", column, min(1.0, abs(normalized_change)), consistency, {
                    "time_column": time_column, "start": values[0], "end": values[-1],
                    "absolute_change": values[-1] - values[0], "normalized_change": normalized_change,
                    "direction": "UP" if normalized_change > 0 else "DOWN",
                    "direction_consistency": consistency, "point_count": len(values),
                }))

    for index, left in enumerate(numeric):
        for right in numeric[index + 1:]:
            pairs = [(_number(row.get(left)), _number(row.get(right))) for row in rows]
            complete = [(float(a), float(b)) for a, b in pairs if a is not None and b is not None]
            if len(complete) < 6:
                continue
            coefficient = _pearson(complete)
            if coefficient is not None and abs(coefficient) >= 0.7:
                findings.append(_finding("CORRELATION", f"{left}|{right}", abs(coefficient), min(1.0, len(complete) / 30), {
                    "left": left, "right": right, "coefficient": coefficient,
                    "sample_count": len(complete), "causality_established": False,
                }))

    findings.sort(key=lambda item: item["priority_score"], reverse=True)
    return findings[:20]


def _finding(kind: str, subject: str, impact: float, confidence: float, evidence: dict) -> dict:
    impact, confidence = max(0.0, min(1.0, impact)), max(0.0, min(1.0, confidence))
    return {"type": kind, "subject": subject, "impact": impact, "confidence": confidence,
            "priority_score": impact * confidence, "evidence": evidence,
            "producer": "DETERMINISTIC_INSIGHT_DISCOVERY"}


def _numeric_series(column: str, rows: list[dict[str, Any]]) -> list[float] | None:
    values = [_number(row.get(column)) for row in rows]
    return [float(value) for value in values] if values and all(value is not None for value in values) else None


def _temporal_series(column: str, rows: list[dict[str, Any]]) -> bool:
    values = [row.get(column) for row in rows]
    return bool(values) and all(_time_key(value) is not None for value in values)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(Decimal(value.strip().replace(",", ""))) if isinstance(value, str) else float(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _time_key(value: Any) -> tuple[int, int, int, int, int, int] | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, parsed.second


def _identifier(column: str) -> bool:
    folded = column.casefold()
    return folded == "id" or folded.endswith("_id") or any(token in folded for token in ("编号", "序号"))


def _bounded(values: list[float]) -> list[float]:
    if len(values) <= MAX_SERIES_POINTS:
        return values
    indexes = [round(i * (len(values) - 1) / (MAX_SERIES_POINTS - 1)) for i in range(MAX_SERIES_POINTS)]
    return [values[index] for index in indexes]


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    left, right = [item[0] for item in pairs], [item[1] for item in pairs]
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in pairs)
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_scale == 0 or right_scale == 0:
        return None
    return max(-1.0, min(1.0, numerator / (left_scale * right_scale)))
