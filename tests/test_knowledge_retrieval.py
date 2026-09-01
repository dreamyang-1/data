import json

import pytest

from app.services.knowledge_retrieval import (
    RedisKnowledgeSearchCache,
    normalize_and_deduplicate_hits,
    normalize_relevance,
    unwrap_search_hits,
)


def test_unwraps_platform_response_and_nested_query_groups():
    payload = {"data": {"search_docs": [[{"page_content": "a"}], [{"page_content": "b"}]]}}
    assert [item["page_content"] for item in unwrap_search_hits(payload)] == ["a", "b"]


def test_deduplicates_blocks_rejects_cross_scope_and_limits_one_source():
    payload = [
        {"page_content": "第一块", "score": 0.2, "kb_name": "allowed", "metadata": {"source": "a.md", "block_id": "1"}},
        {"page_content": "重复块", "score": 0.1, "kb_name": "allowed", "metadata": {"source": "a.md", "block_id": "1"}},
        {"page_content": "第二块", "score": 0.3, "kb_name": "allowed", "metadata": {"source": "a.md", "block_id": "2"}},
        {"page_content": "第三块", "score": 0.4, "kb_name": "allowed", "metadata": {"source": "a.md", "block_id": "3"}},
        {"page_content": "越权", "score": 0.0, "kb_name": "other", "metadata": {"source": "x.md", "block_id": "x"}},
        {"page_content": "不同来源", "score": 0.5, "kb_name": "allowed", "metadata": {"source": "b.md", "block_id": "4"}},
    ]
    hits = normalize_and_deduplicate_hits(
        payload,
        allowed_knowledge_bases=["allowed"],
        final_top_k=5,
        excerpt_chars=100,
        max_per_source=2,
    )
    assert [hit.content for hit in hits] == ["第一块", "第二块", "不同来源"]
    assert hits[0].normalized_relevance == pytest.approx(0.9)


def test_distance_and_similarity_scores_are_normalized_consistently():
    assert normalize_relevance(0.2, score_type="distance") == pytest.approx(0.9)
    assert normalize_relevance(0.8, score_type="similarity") == pytest.approx(0.8)
    assert normalize_relevance(None, score_type="distance") is None


def test_low_relevance_hit_is_removed_by_confidence_gate():
    hits = normalize_and_deduplicate_hits(
        [{"page_content": "弱相关", "score": 1.4, "metadata": {"source": "a.md"}}],
        allowed_knowledge_bases=["kb"],
        final_top_k=5,
        excerpt_chars=100,
        min_normalized_relevance=0.5,
    )
    assert hits == []


def test_decorated_knowledge_base_name_is_normalized_to_authorized_id():
    hits = normalize_and_deduplicate_hits(
        [
            {
                "page_content": "grounded document excerpt",
                "score": 0.2,
                "kb_name": "Display Name(KB_ALLOWED)",
                "metadata": {"source": "guide.docx", "block_id": "1"},
            }
        ],
        allowed_knowledge_bases=["KB_ALLOWED"],
        final_top_k=5,
        excerpt_chars=100,
    )

    assert len(hits) == 1
    assert hits[0].kb_name == "KB_ALLOWED"


def test_decorated_knowledge_base_name_still_rejects_unauthorized_id():
    hits = normalize_and_deduplicate_hits(
        [
            {
                "page_content": "cross-scope excerpt",
                "score": 0.2,
                "kb_name": "Display Name(KB_OTHER)",
                "metadata": {"source": "guide.docx", "block_id": "1"},
            }
        ],
        allowed_knowledge_bases=["KB_ALLOWED"],
        final_top_k=5,
        excerpt_chars=100,
    )

    assert hits == []


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, _keys, key, token, *_args):
        if self.values.get(key) == token:
            if "EXPIRE" in script:
                return 1
            self.values.pop(key, None)
            return 1
        return 0


@pytest.mark.asyncio
async def test_redis_cache_key_includes_authorized_scope():
    cache = RedisKnowledgeSearchCache(FakeRedis(), ttl_seconds=300, prefix="test")
    base = {"query": ["销售额"], "knowledge_base_name": ["kb-a"], "cache_user_id": "u1"}
    await cache.put(base, {"data": [1]})
    assert await cache.get(base) == {"data": [1]}
    changed_user = dict(base, cache_user_id="u2")
    assert await cache.get(changed_user) is None


@pytest.mark.asyncio
async def test_cache_fill_lock_has_owner_safe_release():
    redis = FakeRedis()
    cache = RedisKnowledgeSearchCache(
        redis, ttl_seconds=300, prefix="test", lock_seconds=10, wait_seconds=0
    )
    payload = {"query": ["销售额"], "knowledge_base_name": ["kb"]}
    owner = await cache.acquire_fill_lock(payload)
    assert owner
    assert await cache.acquire_fill_lock(payload) is None
    await cache.release_fill_lock(payload, "not-the-owner")
    assert await cache.acquire_fill_lock(payload) is None
    await cache.release_fill_lock(payload, owner)
    assert await cache.acquire_fill_lock(payload)


@pytest.mark.asyncio
async def test_cache_fill_lock_renews_only_for_owner():
    redis = FakeRedis()
    cache = RedisKnowledgeSearchCache(
        redis, ttl_seconds=300, prefix="test", lock_seconds=10, wait_seconds=0
    )
    payload = {"query": ["sales"], "knowledge_base_name": ["kb"]}
    owner = await cache.acquire_fill_lock(payload)
    assert owner
    assert await cache.renew_fill_lock(payload, owner) is True
    assert await cache.renew_fill_lock(payload, "another-owner") is False
