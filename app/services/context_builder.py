"""Budgeted context assembly shared by model-facing agent operations."""
from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from app.domain.models import (
    AnalysisPlan,
    ExtensionExecution,
    HistoryMessage,
    StrictModel,
)
from app.services.history_compaction import compact_history


class ContextEnvelope(StrictModel):
    schema_version: str = "1.0"
    system_prompt: str = Field(default="", max_length=12000)
    current_question: str = Field(min_length=1, max_length=4000)
    task_state: dict[str, Any] = Field(default_factory=dict)
    analysis_plan: dict[str, Any] | None = None
    recent_history: list[HistoryMessage] = Field(default_factory=list, max_length=40)
    conversation_summary: dict[str, Any] | None = None
    long_term_memories: list[str] = Field(default_factory=list, max_length=20)
    tool_result_summaries: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    business_context: dict[str, Any] = Field(default_factory=dict)
    estimated_characters: int = Field(default=0, ge=0)
    omitted: dict[str, int] = Field(default_factory=dict)

    def prompt_payload(self) -> dict[str, Any]:
        """Return model-safe context without duplicating the system instruction."""
        return self.model_dump(
            mode="json",
            exclude={"system_prompt", "estimated_characters", "omitted"},
            exclude_none=True,
        )


class ContextBuilder:
    """Select useful context under a deterministic character budget."""

    def __init__(self, *, max_characters: int = 32_000, max_history_messages: int = 40):
        if not 1_000 <= max_characters <= 200_000:
            raise ValueError("max_characters must be between 1000 and 200000")
        if not 2 <= max_history_messages <= 100:
            raise ValueError("max_history_messages must be between 2 and 100")
        self.max_characters = max_characters
        self.max_history_messages = max_history_messages

    def build(
        self,
        *,
        current_question: str,
        system_prompt: str = "",
        task_state: dict[str, Any] | None = None,
        analysis_plan: AnalysisPlan | None = None,
        history: list[HistoryMessage] | None = None,
        conversation_summary: dict[str, Any] | None = None,
        long_term_memories: list[str] | None = None,
        tool_results: list[ExtensionExecution] | None = None,
        business_context: dict[str, Any] | None = None,
    ) -> ContextEnvelope:
        question = current_question[:4000]
        prompt = system_prompt[:12000]
        state = self._bounded(task_state or {}, depth=0)
        plan = (
            self._bounded(analysis_plan.model_dump(mode="json"), depth=0)
            if analysis_plan is not None else None
        )
        summary = self._bounded(conversation_summary, depth=0) if conversation_summary else None
        memories = [str(item)[:1000] for item in (long_term_memories or [])[:20]]
        tools = [self._tool_summary(item) for item in (tool_results or [])[:20]]
        business = self._bounded(business_context or {}, depth=0)
        compacted = compact_history(
            list(history or []),
            maximum_messages=self.max_history_messages,
            # Reserve capacity for old task/correction/clarification anchors.
            recent_messages=min(24, max(2, self.max_history_messages - 4)),
        )

        envelope = ContextEnvelope(
            system_prompt=prompt,
            current_question=question,
            task_state=state,
            analysis_plan=plan,
            conversation_summary=summary,
            business_context=business,
        )
        used = self._size(envelope.model_dump(mode="json", exclude={"estimated_characters"}))
        omitted = {"history": 0, "memories": 0, "tool_results": 0}

        # Stable priority: confirmed memory, recent/anchor history, then tool summaries.
        selected_memories: list[str] = []
        for item in memories:
            cost = self._size(item)
            if used + cost > self.max_characters:
                omitted["memories"] += 1
                continue
            selected_memories.append(item)
            used += cost

        selected_history: list[HistoryMessage] = []
        for item in reversed(compacted):
            cost = self._size(item.model_dump(mode="json"))
            if used + cost > self.max_characters:
                omitted["history"] += 1
                continue
            selected_history.append(item)
            used += cost
        selected_history.reverse()

        selected_tools: list[dict[str, Any]] = []
        for item in tools:
            cost = self._size(item)
            if used + cost > self.max_characters:
                omitted["tool_results"] += 1
                continue
            selected_tools.append(item)
            used += cost

        # If mandatory structured state itself exceeds the budget, preserve its
        # shape and clearly record the overage; never silently drop the question.
        return envelope.model_copy(update={
            "recent_history": selected_history,
            "long_term_memories": selected_memories,
            "tool_result_summaries": selected_tools,
            "estimated_characters": used,
            "omitted": omitted,
        })

    def select_history(self, history: list[HistoryMessage]) -> list[HistoryMessage]:
        """Compatibility helper for non-LLM child requests using the same policy."""
        envelope = self.build(current_question="内部子任务", history=history)
        return envelope.recent_history

    @classmethod
    def _tool_summary(cls, execution: ExtensionExecution) -> dict[str, Any]:
        output = execution.output or {}
        summary: dict[str, Any] = {
            "tool_name": execution.name,
            "kind": execution.kind,
            "status": execution.status,
            "execution_id": execution.execution_id,
            "latency_ms": execution.latency_ms,
            "error_type": execution.error_type,
            "result_metadata": cls._bounded(execution.result_metadata, 0),
        }
        if isinstance(output, dict):
            bulk_keys = {"rows", "data", "raw_rows", "raw_result"}
            summary["output_keys"] = [
                str(key)[:100]
                for key in output
                if str(key).casefold() not in bulk_keys
            ][:30]
            rows = output.get("rows") or output.get("data")
            if isinstance(rows, list):
                summary["output_row_count"] = len(rows)
            for key in ("count", "total", "row_count", "summary", "message"):
                value = output.get(key)
                if isinstance(value, (str, int, float, bool)):
                    summary[key] = value[:1000] if isinstance(value, str) else value
        return summary

    @classmethod
    def _bounded(cls, value: Any, depth: int) -> Any:
        if depth >= 5:
            return "<depth-limited>"
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:50]:
                folded = str(key).casefold()
                if folded in {"rows", "raw_rows", "raw_result", "prompt", "content"}:
                    result[str(key)] = cls._bulk_summary(item)
                else:
                    result[str(key)[:100]] = cls._bounded(item, depth + 1)
            return result
        if isinstance(value, list):
            return [cls._bounded(item, depth + 1) for item in value[:50]]
        if isinstance(value, str):
            return value[:4000]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:1000]

    @staticmethod
    def _bulk_summary(value: Any) -> dict[str, Any]:
        return {
            "item_count": len(value) if isinstance(value, (list, dict, str)) else None,
            "content_omitted": True,
        }

    @staticmethod
    def _size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))
