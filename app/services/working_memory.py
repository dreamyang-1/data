from __future__ import annotations

import re
from typing import Any

from app.domain.models import CanonicalAnalysisRequest


_RECALL_MARKER = re.compile(
    r"(?:回到|恢复|接着|继续)(?:刚才|之前|前面|上次)?|"
    r"(?:刚才|之前|前面|上次)(?:那个|那条|提到的|说的)?"
)
_LATEST_MARKER = re.compile(r"(?:刚才|上一个|上一条|最近)(?:那个|那条|的)?")
_GENERIC_TERMS = {
    "查询", "统计", "分析", "展示", "显示", "结果", "数据", "报告", "明细",
    "销售", "订单", "指标", "维度", "刚才", "之前", "前面", "上次", "那个",
}


def recalls_prior_task(question: str) -> bool:
    """True only for an explicit request to resume an earlier task branch."""
    return bool(_RECALL_MARKER.search(re.sub(r"\s+", "", question)))


def select_recalled_task_frame(
    question: str,
    frames: list[CanonicalAnalysisRequest],
) -> CanonicalAnalysisRequest | None:
    """Select a prior structured task, never raw chat, using bounded lexical recall.

    This is the data-agent equivalent of Letta recall memory: the active task stays
    small, while older task frames are retrieved only after an explicit recall cue.
    A weak or tied match is rejected so unrelated filters are never inherited.
    """
    if not frames or not recalls_prior_task(question):
        return None
    compact = re.sub(r"\s+", "", question)
    if _LATEST_MARKER.search(compact):
        return frames[0].model_copy(deep=True)

    unique_frames: list[CanonicalAnalysisRequest] = []
    seen: set[tuple[Any, ...]] = set()
    for frame in frames:
        signature = _frame_signature(frame)
        if signature not in seen:
            seen.add(signature)
            unique_frames.append(frame)

    scored: list[tuple[int, int, CanonicalAnalysisRequest]] = []
    for recency, frame in enumerate(unique_frames):
        terms = _frame_terms(frame)
        matched = [term for term in terms if term in compact]
        score = sum(min(len(term), 12) for term in matched)
        scored.append((score, -recency, frame))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not scored or scored[0][0] < 2:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][2].model_copy(deep=True)


def _frame_signature(frame: CanonicalAnalysisRequest) -> tuple[Any, ...]:
    return (
        re.sub(r"\s+", "", frame.rewritten_question or frame.original_question),
        frame.primary_intent.value,
        frame.entity,
        tuple(item.canonical_name or item.input for item in frame.metrics),
        tuple(frame.dimensions),
        tuple(frame.fields),
        tuple(sorted(repr(item) for item in frame.filters)),
        repr(frame.time_range.model_dump(mode="json") if frame.time_range else None),
    )


def _frame_terms(frame: CanonicalAnalysisRequest) -> set[str]:
    values: list[Any] = [
        frame.entity,
        *frame.dimensions,
        *frame.fields,
        *(item.canonical_name or item.input for item in frame.metrics),
    ]
    for item in frame.filters:
        if isinstance(item, dict):
            values.extend((item.get("field"), item.get("value")))
    source = frame.rewritten_question or frame.original_question
    values.extend(re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,30}|[\u4e00-\u9fff]{2,12}", source))
    return {
        text
        for value in values
        if isinstance(value, (str, int, float))
        for text in [str(value).strip()]
        if len(text) >= 2 and text not in _GENERIC_TERMS
    }
