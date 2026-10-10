"""Small deterministic contracts for dependent query routing and delivery.

An upstream answer is prose, not a dataset schema.  Prefer the planner's typed
projection, and keep local result operations only when that projection and its
scope are actually present in the predecessor contract.
"""
from __future__ import annotations

import re
from typing import Any


def key_kind(value: str) -> str:
    value = re.sub(r"[\s（）()\[\]【】]", "", value.lower()).rsplit(".", 1)[-1]
    if re.search(r"(?:^|_)id$|编号$|标识$", value):
        return "id"
    if re.search(r"(?:^|_)code$|编码$|代码$", value):
        return "code"
    return "display"


def projection(extraction: dict[str, Any] | None) -> list[tuple[str, str]]:
    result = []
    for key in ("指标", "维度", "展示字段"):
        for item in (extraction or {}).get(key) or []:
            if isinstance(item, str):
                result.append(("", item))
            elif isinstance(item, dict):
                name = str(item.get("field") or item.get("name") or "").strip()
                if name:
                    result.append((str(item.get("entity") or ""), name))
    return result


def structured_query_changed(target: dict | None, sources: list[dict | None]) -> bool:
    """Unknown source coverage is not permission to reuse an unrelated table."""
    wanted = projection(target)
    if not wanted:
        return False  # legacy unstructured local-operation path
    for source in sources:
        available = projection(source)
        if not source or not all(
            any(name == old_name and owner == old_owner
                for old_owner, old_name in available)
            for owner, name in wanted
        ):
            continue
        if any(target.get(key) != source.get(key) for key in ("过滤条件", "时间粒度")
               if target.get(key)):
            continue
        # Aggregation grain changes require SQL, not projection of old aggregates.
        if target.get("指标") and target.get("维度") != source.get("维度"):
            continue
        return False
    return True


def sql_column_bindings(sql: str, columns: list[str]) -> dict[str, str]:
    """Capture only literal qualified projections, never infer join identities.

    This is provenance from already validated/executed SQL, not a SQL parser or
    security gate. Complex expressions are deliberately left unmapped.
    """
    pairs = re.findall(
        r"(?<![\w.])(`?[A-Za-z_]\w*`?\s*\.\s*`?[A-Za-z_]\w*`?)"
        r"\s+AS\s+(?:`([^`]+)`|\"([^\"]+)\"|([A-Za-z_]\w*))",
        sql, re.I,
    )
    result: dict[str, str] = {}
    conflicts: set[str] = set()
    for field, quoted, double, bare in pairs:
        alias = quoted or double or bare
        if alias not in columns:
            continue
        field = re.sub(r"[\s`]", "", field)
        if alias in result and result[alias] != field:
            conflicts.add(alias)
        result[alias] = field
    return {alias: field for alias, field in result.items() if alias not in conflicts}


def missing_display_fields(extraction: dict | None, columns: list[str]) -> list[str]:
    # Exact requested field/alias matching, with common *field* aliases (not
    # entity guessing). The live ASL service remains the authority for binding.
    aliases = (
        ("商品名称", "产品名称", "货品名称", "product_name", "goods_name"),
        ("销售员姓名", "销售人员姓名", "业务员姓名", "业务员名称", "salesperson_name"),
        ("医院名称", "hospital_name"),
        ("经销商名称", "dealer_name"),
    )
    def normalize(value):
        return re.sub(r"[\s`（）()]", "", value).lower().rsplit(".", 1)[-1]
    available = {normalize(str(column)) for column in columns}
    missing = []
    for item in (extraction or {}).get("展示字段") or []:
        name = str(item.get("field") or item.get("name") or "") if isinstance(item, dict) else str(item)
        accepted = {normalize(name)}
        for group in aliases:
            if normalize(name) in group:
                accepted.update(group)
        if not accepted & available:
            missing.append(name)
    return missing
