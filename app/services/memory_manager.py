"""Governed extraction and persistence of explicit durable user preferences."""
from __future__ import annotations

import re

from app.domain.models import CanonicalAnalysisRequest
from app.stores.long_memory import (
    LongTermMemory,
    LongTermMemoryStore,
    MemoryCandidate,
    MemoryScope,
    MemoryType,
)


class MemoryManager:
    """Write only explicit defaults; storage remains the existing memory store."""

    _REMEMBER = re.compile(r"记住|以后(?:都)?默认|今后(?:都)?默认|设为默认")

    def __init__(self, store: LongTermMemoryStore) -> None:
        self.store = store

    async def remember_explicit_defaults(
        self,
        request: CanonicalAnalysisRequest,
        *,
        question: str,
        scope: MemoryScope,
        session_id: str,
        message_id: str,
        actor: str,
    ) -> list[LongTermMemory]:
        if not self._REMEMBER.search(question):
            return []
        clauses = [item.strip() for item in re.split(r"[，。；;]", question) if item.strip()]
        explicit_clauses: list[str] = []
        remember_seen = False
        for clause in clauses:
            if self._REMEMBER.search(clause):
                explicit_clauses.append(clause)
                remember_seen = "记住" in clause and len(clause) <= 6
            elif remember_seen:
                explicit_clauses.append(clause)
                remember_seen = False
        explicit_text = "，".join(explicit_clauses)
        confirmed: list[LongTermMemory] = []
        for memory_type, memory_key, value, summary in self._proposals(
            request, explicit_text
        ):
            candidate = await self.store.create_candidate(MemoryCandidate(
                scope=scope,
                memory_type=memory_type,
                memory_key=memory_key,
                summary=summary,
                value=value,
                confidence=1.0,
                source_session_id=session_id,
                source_message_id=message_id,
                created_by=actor,
            ))
            confirmed.append(await self.store.confirm(
                scope, candidate.memory_id, confirmed_by=actor
            ))
        return confirmed

    @staticmethod
    def _proposals(
        request: CanonicalAnalysisRequest, question: str
    ) -> list[tuple[MemoryType, str, dict, str]]:
        proposals: list[tuple[MemoryType, str, dict, str]] = []
        compact = re.sub(r"\s+", "", question)
        if request.metrics and any(word in compact for word in ("指标", "销售额", "订单量", "金额")):
            metric = request.metrics[0].canonical_name or request.metrics[0].input
            proposals.append((MemoryType.USER_PREFERENCE, "default_metric",
                              {"metric": metric}, f"默认指标：{metric}"))
        if request.dimensions and any(word in compact for word in ("维度", "拆分", "分组", "按")):
            dimensions = list(request.dimensions[:10])
            proposals.append((MemoryType.USER_PREFERENCE, "default_dimension",
                              {"dimensions": dimensions},
                              f"默认维度：{'、'.join(dimensions)}"))
        if request.comparison_type and request.comparison_type in compact:
            proposals.append((MemoryType.USER_PREFERENCE, "default_comparison",
                              {"comparison_type": request.comparison_type},
                              f"默认比较口径：{request.comparison_type}"))
        period_match = re.search(
            r"(最近(?:一|1)年|最近(?:一|1)个月|本月|本季度|本年|今年|去年)", compact
        )
        if period_match:
            period = period_match.group(1)
            proposals.append((MemoryType.USER_PREFERENCE, "default_time_period",
                              {"time_period": period}, f"默认时间范围：{period}"))
        format_match = re.search(r"(?:输出|展示|返回)(表格|图表|摘要|报告)", compact)
        if format_match:
            output_format = format_match.group(1)
            proposals.append((MemoryType.DISPLAY_PREFERENCE, "default_output_format",
                              {"output_format": output_format},
                              f"默认输出格式：{output_format}"))
        return list({item[1]: item for item in proposals}.values())
