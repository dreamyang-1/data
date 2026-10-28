"""Deliver only executed facts when optional root interpretation is unavailable."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


def incomplete_calculation(materials: list[dict[str, Any]]) -> bool:
    return any(item.get("calculation_incomplete") for item in materials)


def factual_fallback(materials: list[dict[str, Any]]) -> str:
    paragraphs = []
    for item in materials:
        summary = str(item.get("summary") or "").strip()
        if summary and item.get("status") in {"COMPLETED", "PARTIAL_SUCCESS"}:
            paragraphs.append(summary)
        elif item.get("calculation_incomplete"):
            paragraphs.append(summary or "请求的计算尚未完成，不能提供对应的增长率或分析结论。")
    return "\n\n".join(dict.fromkeys(paragraphs)) or "已保留本次可用结果，整体洞察暂不可用。"


async def deliver_root_insight(
    materials: list[dict[str, Any]], generate: Callable[[], Awaitable],
) -> tuple[str, Any | None]:
    # A failed tool must not be replaced with model arithmetic, even if that
    # arithmetic happens to produce the right number on a particular request.
    if incomplete_calculation(materials):
        return factual_fallback(materials), None
    return await generate()
