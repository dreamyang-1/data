from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import CanonicalAnalysisRequest, DataQueryResult


class ValidatedQueryExample(BaseModel):
    """SQL-free semantic shape retained from a fully validated query."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    tenant_id: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=1000)
    intent: str = Field(min_length=1, max_length=100)
    semantic_model_id: int = Field(gt=0)
    business_domain_ids: list[int] = Field(default_factory=list, max_length=50)
    entity: str | None = Field(default=None, max_length=200)
    metric_ids: list[str] = Field(default_factory=list, max_length=20)
    metric_names: list[str] = Field(default_factory=list, max_length=20)
    fields: list[str] = Field(default_factory=list, max_length=50)
    dimensions: list[str] = Field(default_factory=list, max_length=50)
    filter_shapes: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    has_time_range: bool = False
    validation_layers: list[str] = Field(default_factory=list, max_length=3)
    quality_status: str = "PASS"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def semantic_payload(self) -> dict[str, Any]:
        # Deliberately no sql/asl/result rows. Recall is a semantic example only.
        return {
            "question": self.question,
            "intent": self.intent,
            "entity": self.entity,
            "metric_ids": self.metric_ids,
            "metric_names": self.metric_names,
            "fields": self.fields,
            "dimensions": self.dimensions,
            "filter_shapes": self.filter_shapes,
            "has_time_range": self.has_time_range,
        }


class ValidatedQueryRecall:
    def __init__(self, store: object, *, top_k: int = 3, max_items: int = 100) -> None:
        self.store = store
        self.top_k = top_k
        self.max_items = max_items

    @staticmethod
    def eligible(result: DataQueryResult) -> bool:
        report = result.semantic_validation_report
        return bool(
            report is not None
            and report.status == "PASS"
            and all(report.layers[name].status == "PASS" for name in ("syntax", "semantic", "business"))
            and result.dataset.quality_status.upper() == "PASS"
            and (result.dataset.total_row_count or result.dataset.row_count) > 0
            and result.sql.strip()
        )

    @classmethod
    def build(
        cls,
        request: CanonicalAnalysisRequest,
        result: DataQueryResult,
        *,
        semantic_model_id: int,
    ) -> ValidatedQueryExample:
        if not cls.eligible(result):
            raise ValueError("query does not meet validated-recall admission gates")
        filter_shapes = [
            {
                "field": str(item.get("field") or "")[:200],
                "operator": str(item.get("operator") or "")[:30],
                "value_type": type(item.get("value")).__name__,
            }
            for item in request.filters
            if isinstance(item, dict) and item.get("field")
        ]
        payload = {
            "tenant_id": request.tenant_id,
            "application_id": request.application_id,
            "question": request.rewritten_question or request.original_question,
            "intent": request.primary_intent.value,
            "semantic_model_id": semantic_model_id,
            "business_domain_ids": sorted(request.business_domain_ids),
            "entity": request.entity,
            "metric_ids": sorted(item.metric_id for item in request.metrics if item.metric_id),
            "metric_names": [item.canonical_name or item.input for item in request.metrics],
            "fields": request.fields,
            "dimensions": request.dimensions,
            "filter_shapes": filter_shapes,
            "has_time_range": request.time_range is not None,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ValidatedQueryExample(
            fingerprint=fingerprint,
            **payload,
            validation_layers=["syntax", "semantic", "business"],
            quality_status=result.dataset.quality_status.upper(),
        )

    async def remember(self, example: ValidatedQueryExample) -> None:
        await self.store.put_validated_query_example(
            example.model_dump(mode="json"), max_items=self.max_items
        )

    async def recall(
        self,
        request: CanonicalAnalysisRequest,
        *,
        semantic_model_id: int,
    ) -> list[ValidatedQueryExample]:
        values = await self.store.get_validated_query_examples(
            request.tenant_id,
            request.application_id,
            semantic_model_id,
            sorted(request.business_domain_ids),
            limit=self.max_items,
        )
        candidates: list[tuple[float, ValidatedQueryExample]] = []
        seen_fingerprints: set[str] = set()
        query_text = request.rewritten_question or request.original_question
        for value in values:
            try:
                example = ValidatedQueryExample.model_validate(value)
            except Exception:
                continue
            if example.fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(example.fingerprint)
            score = self._similarity(query_text, example.question)
            if example.intent == request.primary_intent.value:
                score += 0.20
            requested_metrics = {item.metric_id for item in request.metrics if item.metric_id}
            if requested_metrics and requested_metrics.intersection(example.metric_ids):
                score += 0.25
            if score >= 0.18:
                candidates.append((score, example))
        candidates.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        return [item[1] for item in candidates[: self.top_k]]

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        def tokens(text: str) -> set[str]:
            normalized = re.sub(r"\s+", "", text).casefold()
            chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
            result = {chinese[i : i + 2] for i in range(max(0, len(chinese) - 1))}
            result.update(re.findall(r"[a-z0-9_]{2,}", normalized))
            return result

        a, b = tokens(left), tokens(right)
        return len(a & b) / len(a | b) if a and b else 0.0
