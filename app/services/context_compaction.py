"""Deterministic structured compaction for long analytical conversations."""
from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from app.domain.models import CanonicalAnalysisRequest, HistoryMessage, StrictModel


class ConversationSummary(StrictModel):
    schema_version: str = "1.0"
    active_goal: str = Field(default="", max_length=1000)
    intent: str | None = Field(default=None, max_length=80)
    confirmed_metrics: list[str] = Field(default_factory=list, max_length=20)
    confirmed_dimensions: list[str] = Field(default_factory=list, max_length=20)
    confirmed_filters: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    confirmed_time_range: dict[str, Any] | None = None
    user_corrections: list[str] = Field(default_factory=list, max_length=20)
    key_conclusions: list[str] = Field(default_factory=list, max_length=20)
    unresolved_items: list[str] = Field(default_factory=list, max_length=20)
    source_message_count: int = Field(ge=0)
    compacted_message_count: int = Field(ge=0)


class ContextCompactor:
    """Build a fact-preserving summary without an additional model call."""

    _CORRECTION = re.compile(
        r"不是|改成|换成|更正|纠正|排除|不要|只看|口径|按.+(?:分析|拆分)"
    )
    _CLARIFICATION = re.compile(
        r"请(?:补充|确认|提供)|还需要|需要确认|缺少|哪个指标|时间范围|哪个维度"
    )
    _CONCLUSION = re.compile(
        r"查询成功|分析结论|总体|整体|上升|下降|增长|减少|排名|异常|触底|恢复|修复"
    )

    def __init__(
        self,
        *,
        trigger_messages: int = 24,
        retained_recent_messages: int = 12,
        max_excerpt_characters: int = 500,
    ) -> None:
        if trigger_messages < 4:
            raise ValueError("trigger_messages must be at least 4")
        if not 2 <= retained_recent_messages < trigger_messages:
            raise ValueError("retained_recent_messages must be below trigger_messages")
        self.trigger_messages = trigger_messages
        self.retained_recent_messages = retained_recent_messages
        self.max_excerpt_characters = max_excerpt_characters

    def compact(
        self,
        history: list[HistoryMessage],
        *,
        request: CanonicalAnalysisRequest | None = None,
        prior_summary: ConversationSummary | None = None,
    ) -> ConversationSummary | None:
        if len(history) <= self.trigger_messages and prior_summary is None:
            return None

        old_history = history[:-self.retained_recent_messages]
        corrections = list(prior_summary.user_corrections) if prior_summary else []
        conclusions = list(prior_summary.key_conclusions) if prior_summary else []
        unresolved = list(prior_summary.unresolved_items) if prior_summary else []

        for message in old_history:
            excerpt = self._excerpt(message.content)
            if message.role == "user" and self._CORRECTION.search(excerpt):
                corrections.append(excerpt)
            elif message.role == "assistant" and self._CLARIFICATION.search(excerpt):
                unresolved.append(excerpt)
            elif message.role == "assistant" and self._CONCLUSION.search(excerpt):
                conclusions.append(excerpt)

        if request is not None:
            goal = request.rewritten_question or request.original_question
            intent = request.primary_intent.value
            metrics = [item.canonical_name or item.input for item in request.metrics]
            dimensions = list(request.dimensions)
            filters = [dict(item) for item in request.filters]
            time_range = (
                request.time_range.model_dump(mode="json")
                if request.time_range is not None else None
            )
            # A resolved structured request supersedes old clarification text.
            if not request.missing_slots:
                unresolved = []
        else:
            goal = prior_summary.active_goal if prior_summary else ""
            intent = prior_summary.intent if prior_summary else None
            metrics = list(prior_summary.confirmed_metrics) if prior_summary else []
            dimensions = list(prior_summary.confirmed_dimensions) if prior_summary else []
            filters = list(prior_summary.confirmed_filters) if prior_summary else []
            time_range = prior_summary.confirmed_time_range if prior_summary else None

        return ConversationSummary(
            active_goal=goal[:1000],
            intent=intent,
            confirmed_metrics=self._unique(metrics, 20),
            confirmed_dimensions=self._unique(dimensions, 20),
            confirmed_filters=filters[:50],
            confirmed_time_range=time_range,
            user_corrections=self._unique(corrections, 20),
            key_conclusions=self._unique(conclusions, 20),
            unresolved_items=self._unique(unresolved, 20),
            source_message_count=len(history),
            compacted_message_count=len(old_history),
        )

    def retained_history(self, history: list[HistoryMessage]) -> list[HistoryMessage]:
        if len(history) <= self.trigger_messages:
            return list(history)
        return list(history[-self.retained_recent_messages:])

    def _excerpt(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()[: self.max_excerpt_characters]

    @staticmethod
    def _unique(values: list[str], limit: int) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result[-limit:]
