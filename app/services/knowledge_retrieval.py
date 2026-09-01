from __future__ import annotations

import hashlib
import json
import logging
import re
import asyncio
import secrets
from dataclasses import dataclass
from typing import Any, Iterable, Protocol


logger = logging.getLogger(__name__)


class KnowledgeSearchCache(Protocol):
    async def get(self, key_payload: dict[str, Any]) -> Any | None: ...
    async def put(self, key_payload: dict[str, Any], value: Any) -> None: ...
    async def acquire_fill_lock(self, key_payload: dict[str, Any]) -> str | None: ...
    async def release_fill_lock(self, key_payload: dict[str, Any], token: str) -> None: ...
    async def renew_fill_lock(self, key_payload: dict[str, Any], token: str) -> bool: ...
    async def wait_for_value(self, key_payload: dict[str, Any]) -> Any | None: ...


class RedisKnowledgeSearchCache:
    """Small fail-open cache for post-Milvus search responses.

    The key includes the complete authorized search scope. Full documents and
    datasets are never cached here; callers must cache only the bounded search
    response returned by the knowledge API.
    """

    _RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
    _RENEW_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

    def __init__(
        self,
        redis,
        *,
        ttl_seconds: int,
        prefix: str,
        lock_seconds: int = 10,
        wait_seconds: float = 0.5,
    ) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.prefix = prefix.rstrip(":")
        self.lock_seconds = lock_seconds
        self.wait_seconds = wait_seconds

    def _key(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"{self.prefix}:kb-cache:{hashlib.sha256(canonical).hexdigest()}"

    def _lock_key(self, payload: dict[str, Any]) -> str:
        return self._key(payload).replace(":kb-cache:", ":kb-cache-lock:")

    async def get(self, key_payload: dict[str, Any]) -> Any | None:
        try:
            raw = await self.redis.get(self._key(key_payload))
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("knowledge cache read failed; continuing without cache: %s", exc)
            return None

    async def put(self, key_payload: dict[str, Any], value: Any) -> None:
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            # Do not let an unexpectedly large upstream response turn Redis into
            # document storage.  One MiB is already much larger than normal Top-K.
            if len(encoded.encode("utf-8")) > 1024 * 1024:
                logger.warning("knowledge response too large for Redis cache")
                return
            await self.redis.set(
                self._key(key_payload), encoded, ex=self.ttl_seconds
            )
        except Exception as exc:
            logger.warning("knowledge cache write failed; continuing without cache: %s", exc)

    async def acquire_fill_lock(self, key_payload: dict[str, Any]) -> str | None:
        token = secrets.token_urlsafe(24)
        try:
            acquired = await self.redis.set(
                self._lock_key(key_payload), token, ex=self.lock_seconds, nx=True
            )
            return token if acquired else None
        except Exception as exc:
            logger.warning("knowledge cache lock failed; bypassing lock: %s", exc)
            # A Redis outage must not block the knowledge service. Empty token
            # means caller may continue but must not attempt lock deletion.
            return ""

    async def release_fill_lock(self, key_payload: dict[str, Any], token: str) -> None:
        if not token:
            return
        try:
            await self.redis.eval(
                self._RELEASE_LOCK_SCRIPT, 1, self._lock_key(key_payload), token
            )
        except Exception as exc:
            logger.warning("knowledge cache lock release failed: %s", exc)

    async def renew_fill_lock(self, key_payload: dict[str, Any], token: str) -> bool:
        if not token:
            return False
        try:
            renewed = await self.redis.eval(
                self._RENEW_LOCK_SCRIPT,
                1,
                self._lock_key(key_payload),
                token,
                self.lock_seconds,
            )
            return bool(renewed)
        except Exception as exc:
            logger.warning("knowledge cache lock renewal failed: %s", exc)
            return False

    async def wait_for_value(self, key_payload: dict[str, Any]) -> Any | None:
        if self.wait_seconds <= 0:
            return None
        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        delay = 0.025
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(delay)
            value = await self.get(key_payload)
            if value is not None:
                return value
            delay = min(delay * 1.7, 0.1)
        return None


@dataclass(frozen=True)
class NormalizedKnowledgeHit:
    content: str
    source: str | None
    block_id: str | None
    kb_name: str | None
    raw_score: float | None
    normalized_relevance: float | None
    retrieval_rank: int
    metadata: dict[str, Any]


def unwrap_search_hits(payload: Any) -> list[dict[str, Any]]:
    """Accept the response variants used by the current knowledge service."""
    value = payload
    if isinstance(value, dict):
        data = value.get("data", value)
        value = data.get("search_docs", []) if isinstance(data, dict) else []
    if not isinstance(value, list):
        return []
    flattened: list[dict[str, Any]] = []
    stack = list(value)
    while stack:
        item = stack.pop(0)
        if isinstance(item, list):
            stack[0:0] = item
        elif isinstance(item, dict):
            flattened.append(item)
    return flattened


def normalize_and_deduplicate_hits(
    payload: Any,
    *,
    allowed_knowledge_bases: Iterable[str],
    final_top_k: int,
    excerpt_chars: int,
    score_type: str = "distance",
    min_normalized_relevance: float = 0.0,
    max_per_source: int = 2,
) -> list[NormalizedKnowledgeHit]:
    """Validate scope, normalize scores, deduplicate and preserve source diversity."""
    allowed = {str(item).strip() for item in allowed_knowledge_bases if str(item).strip()}
    if not allowed or final_top_k <= 0:
        return []
    seen: set[str] = set()
    source_counts: dict[str, int] = {}
    output: list[NormalizedKnowledgeHit] = []
    for rank, hit in enumerate(unwrap_search_hits(payload), start=1):
        metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        content = hit.get("page_content", hit.get("content", ""))
        if not isinstance(content, str) or not content.strip():
            continue
        content = content.strip()
        kb_name = str(
            hit.get("kb_name")
            or metadata.get("kb_name")
            or metadata.get("knowledge_base_name")
            or ""
        ).strip() or None
        # Explicit cross-scope hits are rejected. Older platform responses omit
        # kb_name; those are accepted only because the request itself was scoped.
        if kb_name is not None and kb_name not in allowed:
            # The current platform decorates identifiers as
            # "display-name(identifier)". Validate and restore the canonical
            # identifier instead of rejecting a legitimate scoped result.
            decorated = re.search(r"\(([^()]*)\)$", kb_name)
            canonical = decorated.group(1).strip() if decorated else ""
            if canonical not in allowed:
                logger.warning("discarding cross-scope knowledge hit: kb=%s", kb_name)
                continue
            kb_name = canonical
        if kb_name is None and len(allowed) == 1:
            kb_name = next(iter(allowed))
        source = str(
            metadata.get("source")
            or metadata.get("file_name")
            or hit.get("source")
            or ""
        ).strip() or None
        block_id = str(metadata.get("block_id") or hit.get("id") or "").strip() or None
        normalized_text = re.sub(r"\s+", "", content).casefold()
        fingerprint_input = (
            f"block:{kb_name}:{block_id}" if block_id else f"text:{kb_name}:{normalized_text}"
        )
        fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
        if fingerprint in seen:
            continue
        source_key = f"{kb_name or ''}:{source or fingerprint}"
        if source_counts.get(source_key, 0) >= max_per_source:
            continue
        raw_score = _float_or_none(hit.get("score"))
        normalized_score = normalize_relevance(raw_score, score_type=score_type)
        if (
            normalized_score is not None
            and normalized_score < min_normalized_relevance
        ):
            continue
        seen.add(fingerprint)
        source_counts[source_key] = source_counts.get(source_key, 0) + 1
        output.append(NormalizedKnowledgeHit(
            content=content[:excerpt_chars],
            source=source,
            block_id=block_id,
            kb_name=kb_name,
            raw_score=raw_score,
            normalized_relevance=normalized_score,
            retrieval_rank=rank,
            metadata=dict(metadata),
        ))
        if len(output) >= final_top_k:
            break
    return output


def normalize_relevance(score: float | None, *, score_type: str) -> float | None:
    if score is None:
        return None
    if score_type == "similarity":
        return max(0.0, min(1.0, score))
    # Current platform documents cosine-like distance in [0, 2], lower is better.
    return max(0.0, min(1.0, 1.0 - score / 2.0))


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
