"""业务域的 jev 判定：候选取自语义规范的业务域总览表，达标时覆写拆分判断器的业务域。

候选表随 semantic_model_id 变化，域名取总览表第一列、选项描述取第二列
业务描述，无需手工维护。qwen 拆分判断器照旧先产出业务域，jev 对每个
（子）问题并行判定，置信度达标才覆写；调用失败、候选解析不到、低置信
一律保留 qwen 原值。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from app.observability.langfuse_client import structural_child

from .jev_client import JevChoiceResult, jev_choice

logger = logging.getLogger(__name__)

_DOMAIN_OVERVIEW_MARKER = "业务域总览"
_DOMAIN_TABLE_HEADER = "业务域名称"


def parse_domain_candidates(spec: str) -> dict[str, str]:
    """从业务域总览表解析 域名→业务描述；没有该表或表为空返回空 dict。"""
    marker = spec.find(_DOMAIN_OVERVIEW_MARKER)
    if marker < 0:
        return {}
    candidates: dict[str, str] = {}
    for line in spec[marker:].splitlines()[1:]:
        line = line.strip()
        if not line.startswith("|"):
            # 表格结束后不再往下找，避免把别的表误当候选
            if candidates:
                break
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0]
        if name == _DOMAIN_TABLE_HEADER or set(name) <= set("-: "):
            continue
        if name:
            candidates[name] = cells[1] or name
    return candidates


async def apply_jev_domain_override(
    settings, judgment, question: str, spec: str
):
    """对拆分判断结果做 jev 业务域终审，返回（可能覆写后的）判断结果。

    judgment 为 CompoundJudgment（frozen dataclass），覆写时返回新对象；
    开关关闭、无候选、jev 失败或低置信时原样返回。
    """
    if settings is None or not (
        getattr(settings, "jev_enabled", False)
        and getattr(settings, "jev_domain_enabled", False)
    ):
        return judgment
    candidates = parse_domain_candidates(spec or "")
    if not candidates:
        return judgment
    if judgment.is_compound:
        texts = [entry.question for entry in judgment.entries]
    else:
        texts = [question]

    # 每个（子）问题一次独立判定，并行发出；步骤 span 只归拢树形展示，
    # 复合问题的多个 jev-decision 收在同一个业务域判断节点下
    with structural_child(("intent_judge", "comprehension"), as_type="span", name="业务域判断"):
        tasks = [
            asyncio.create_task(
                jev_choice(
                    settings,
                    state_text=text,
                    question_name="business_domain",
                    instructions="判定用户问题所属的业务域",
                    criteria=candidates,
                )
            )
            for text in texts
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    min_confidence = settings.jev_min_confidence

    def adopt(result, current: str | None) -> str | None:
        if (
            isinstance(result, JevChoiceResult)
            and result.confidence >= min_confidence
            and result.choice != current
        ):
            logger.info(
                "node=compound_judge jev_domain_override domain=%s previous=%s "
                "confidence=%.2f",
                result.choice,
                current,
                result.confidence,
            )
            return result.choice
        return current

    if judgment.is_compound:
        entries = tuple(
            replace(entry, business_domain=adopt(res, entry.business_domain))
            for entry, res in zip(judgment.entries, results)
        )
        return replace(judgment, entries=entries)
    return replace(
        judgment, business_domain=adopt(results[0], judgment.business_domain)
    )
