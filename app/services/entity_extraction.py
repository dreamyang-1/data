from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class EntityCandidate:
    text: str
    label: str
    score: float
    start: int
    end: int


class EntityCandidateExtractor(Protocol):
    async def extract(self, text: str) -> list[EntityCandidate]: ...


class HttpEntityCandidateExtractor:
    """Client for an independently deployed GLiNER service."""

    def __init__(
        self,
        *,
        url: str,
        threshold: float = 0.72,
        timeout_seconds: float = 0.8,
        maximum_candidates: int = 8,
        labels: tuple[str, ...] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("entity extractor URL must use http or https")
        self.url = url
        self.threshold = min(1.0, max(0.0, threshold))
        self.timeout_seconds = max(0.05, timeout_seconds)
        self.maximum_candidates = max(1, min(maximum_candidates, 32))
        self.labels = labels or GLiNERCandidateExtractor.DEFAULT_LABELS
        self.transport = transport

    async def extract(self, text: str) -> list[EntityCandidate]:
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            response = await client.post(
                self.url,
                json={
                    "text": text,
                    "labels": list(self.labels),
                    "threshold": self.threshold,
                },
            )
        response.raise_for_status()
        payload = response.json()
        values = payload.get("entities") if isinstance(payload, dict) else None
        return _validated_candidates(
            text,
            values,
            labels=self.labels,
            threshold=self.threshold,
            maximum_candidates=self.maximum_candidates,
        )


class GLiNERCandidateExtractor:
    """Lazy, bounded GLiNER adapter used only for candidate recall.

    Extracted spans are never authoritative query slots. QuestionRewriter may
    use them only as additional queries to the scoped Oagnet semantic catalog.
    """

    DEFAULT_LABELS = (
        "商品名称", "商品分类", "品牌", "生产厂家", "经销商", "供应商",
        "医院", "科室", "地区", "公司",
    )

    def __init__(
        self,
        *,
        model_name: str,
        threshold: float = 0.72,
        timeout_seconds: float = 0.8,
        maximum_candidates: int = 8,
        maximum_chars: int = 1000,
        labels: tuple[str, ...] | None = None,
    ) -> None:
        self.model_name = model_name
        self.threshold = min(1.0, max(0.0, threshold))
        self.timeout_seconds = max(0.05, timeout_seconds)
        self.maximum_candidates = max(1, min(maximum_candidates, 32))
        self.maximum_chars = max(32, min(maximum_chars, 4000))
        self.labels = labels or self.DEFAULT_LABELS
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()
        self._load_task: asyncio.Task[Any] | None = None

    async def extract(self, text: str) -> list[EntityCandidate]:
        source = text.strip()
        if not source or len(source) > self.maximum_chars:
            return []
        model = self._ready_model_or_start_loading()
        if model is None:
            return []
        raw = await asyncio.wait_for(
            asyncio.to_thread(
                model.predict_entities,
                source,
                list(self.labels),
                threshold=self.threshold,
            ),
            timeout=self.timeout_seconds,
        )
        return self._validate(source, raw)

    def _ready_model_or_start_loading(self) -> Any | None:
        if self._model is not None:
            return self._model
        if self._load_task is None:
            self._load_task = asyncio.create_task(self._load_model())
            self._load_task.add_done_callback(self._log_load_failure)
            return None
        if not self._load_task.done():
            return None
        self._model = self._load_task.result()
        return self._model

    async def _load_model(self) -> Any:
        async with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                from gliner import GLiNER
            except ImportError as exc:
                raise RuntimeError(
                    "GLiNER extractor is enabled but optional dependency 'gliner' is not installed"
                ) from exc
            self._model = await asyncio.to_thread(
                GLiNER.from_pretrained, self.model_name
            )
            return self._model

    @staticmethod
    def _log_load_failure(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("GLiNER model preload failed: %s", error)

    def _validate(self, source: str, values: Any) -> list[EntityCandidate]:
        return _validated_candidates(
            source,
            values,
            labels=self.labels,
            threshold=self.threshold,
            maximum_candidates=self.maximum_candidates,
        )


def _validated_candidates(
    source: str,
    values: Any,
    *,
    labels: tuple[str, ...],
    threshold: float,
    maximum_candidates: int,
) -> list[EntityCandidate]:
    if not isinstance(values, list):
        raise ValueError("GLiNER returned a non-list entity result")
    candidates: list[EntityCandidate] = []
    seen: set[tuple[int, int, str]] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            start, end = int(item["start"]), int(item["end"])
            text = str(item["text"]).strip()
            label = str(item["label"]).strip()
            score = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            label not in labels
            or score < threshold
            or not (0 <= start < end <= len(source))
            or source[start:end] != text
            or len(text) < 2
        ):
            continue
        key = (start, end, label)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(EntityCandidate(text, label, score, start, end))
    candidates.sort(key=lambda item: (-item.score, -(item.end - item.start), item.start))
    return candidates[:maximum_candidates]


def build_gliner_extractor_from_environment() -> tuple[str, EntityCandidateExtractor | None]:
    """Build the optional extractor without changing the core Settings contract."""
    mode = os.getenv("DATA_AGENT_ENTITY_EXTRACTOR_MODE", "off").strip().lower()
    if mode not in {"off", "shadow", "assist"}:
        raise RuntimeError(
            "DATA_AGENT_ENTITY_EXTRACTOR_MODE must be off, shadow or assist"
        )
    if mode == "off":
        return mode, None
    model_name = os.getenv(
        "DATA_AGENT_GLINER_MODEL", "urchade/gliner_multi-v2.1"
    ).strip()
    if not model_name:
        raise RuntimeError("DATA_AGENT_GLINER_MODEL must not be empty")
    try:
        threshold = float(os.getenv("DATA_AGENT_GLINER_THRESHOLD", "0.72"))
        timeout_seconds = float(os.getenv("DATA_AGENT_GLINER_TIMEOUT_SECONDS", "0.8"))
    except ValueError as exc:
        raise RuntimeError("GLiNER threshold and timeout must be numeric") from exc
    service_url = os.getenv("DATA_AGENT_ENTITY_EXTRACTOR_URL", "").strip()
    if service_url:
        return mode, HttpEntityCandidateExtractor(
            url=service_url,
            threshold=threshold,
            timeout_seconds=timeout_seconds,
        )
    return mode, GLiNERCandidateExtractor(
        model_name=model_name,
        threshold=threshold,
        timeout_seconds=timeout_seconds,
    )
