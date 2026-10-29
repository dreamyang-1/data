"""Presentation-only units from the registered semantic catalog.

Units label already executed result columns and rendered chart values. They
never enter ASL, SQL or filter binding, and a catalog outage only leaves the
existing labels without a unit suffix.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

def _normalize(value: Any) -> str:
    text = str(value or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    if "=" in text:
        text = text.split("=", 1)[0]
    # A header that already carries a unit of its own is the same indicator.
    text = re.sub(r"[（(][^（()）]{1,12}[)）]$", "", text.strip())
    return re.sub(r"[\s_\-]+", "", text).casefold()


async def load_metric_units(settings: Any, semantic_model_id: int | None) -> dict[str, str]:
    """Use the same current-model semantic document as parameter extraction."""
    if type(semantic_model_id) is not int or semantic_model_id <= 0:
        return {}
    from app.domain.semantic_description import load_semantic_description

    reference = await load_semantic_description(settings, semantic_model_id)
    return metric_units_from_reference(reference)


def metric_units_from_reference(reference: Mapping[str, Any]) -> dict[str, str]:
    """Read indicator tables only; entity names never supply indicator units."""
    if not reference.get('available'):
        return {}
    candidates: dict[str, set[str]] = {}
    header = {}
    for line in str(reference.get('content') or '').splitlines():
        if not line.lstrip().startswith('|'):
            header = {}
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if any(name in cells for name in ('指标', '指标名称')) and any(name in cells for name in ('单位', '计量单位')):
            header = {name: index for index, name in enumerate(cells)}
            continue
        name_index = header.get('指标', header.get('指标名称'))
        unit_index = header.get('单位', header.get('计量单位'))
        if name_index is None or unit_index is None or max(name_index, unit_index) >= len(cells):
            continue
        unit = cells[unit_index]
        if not unit or unit in {'无', '-', '—', '未设置', 'N/A'} or re.fullmatch(r'[-: ]+', unit):
            continue
        labels = [cells[name_index]]
        for field in ('别名', '指标别名', '指标编码'):
            index = header.get(field)
            if index is not None and index < len(cells):
                labels.extend(re.split(r'[,，、;；]', cells[index]))
        for label in labels:
            if label and label not in {'无', '-', '—'}:
                candidates.setdefault(_normalize(label), set()).add(unit)
    return {name: next(iter(values)) for name, values in candidates.items() if len(values) == 1}


def table_with_units(table: str, units: Mapping[str, str] | None) -> str:
    """Annotate existing Markdown headers without rebuilding rows or summaries."""
    if not units:
        return table
    lines = table.splitlines()
    for index in range(len(lines) - 1):
        if not lines[index].lstrip().startswith('|'):
            continue
        cells = [cell.strip() for cell in lines[index].strip().strip('|').split('|')]
        separators = [cell.strip() for cell in lines[index + 1].strip().strip('|').split('|')]
        if len(cells) != len(separators) or not all(re.fullmatch(r':?-{3,}:?', cell) for cell in separators):
            continue
        lines[index] = '| ' + ' | '.join(header_with_unit(cell, unit_for_column(units, cell)) for cell in cells) + ' |'
    return '\n'.join(lines)


def unit_for_column(units: Mapping[str, str] | None, column: Any) -> str:
    if not units:
        return ""
    candidates = [column]
    tail = str(column or "").rsplit(".", 1)[-1]
    if tail != str(column or ""):
        candidates.append(tail)
    for candidate in candidates:
        for variant in (candidate, re.sub(r"[（(][^（()）]{1,12}[)）]$", "", str(candidate))):
            unit = units.get(_normalize(variant))
            if unit:
                return unit
    return ""


def header_with_unit(name: str, unit: str | None) -> str:
    unit = str(unit or "").strip()
    if not name or not unit:
        return name
    if f"（{unit}）" in name or f"({unit})" in name:
        return name
    if unit == "%" and "%" in name:
        return name
    return f"{name}（{unit}）"


def append_unit(value: str, unit: str | None) -> str:
    text = str(value or "")
    unit = str(unit or "").strip()
    if not text or not unit or text.endswith(unit):
        return text
    if unit == "%" and text.endswith("%"):
        return text
    return f"{text}{unit}"
