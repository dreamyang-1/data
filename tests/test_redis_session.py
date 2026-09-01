"""Redis 会话存储的契约测试，基于 fakeredis。"""
import pytest
import fakeredis.aioredis

from app.domain.models import (
    CanonicalAnalysisRequest,
    PendingState,
    PrimaryIntent,
    TrustedIdentity,
)
from app.stores.redis import RedisSessionStore


def make_pending(version: int = 1, conversation_id: str = "c1") -> PendingState:
    return PendingState(
        request=CanonicalAnalysisRequest(
            conversation_id=conversation_id,
            tenant_id="t1",
            user_id="u1",
            original_question="本月销售额",
            primary_intent=PrimaryIntent.METRIC_QUERY,
            missing_slots=[],
        ),
        clarification_rounds=version,
        state_version=version,
    )


@pytest.mark.asyncio
async def test_put_and_get_pending_roundtrip():
    store = RedisSessionStore("redis://localhost/0", 60, client=fakeredis.aioredis.FakeRedis())
    state = make_pending(version=1)
    await store.put_pending(state)
    got = await store.get_pending("t1", "c1")
    assert got is not None
    assert got.state_version == 1
    assert got.request.conversation_id == "c1"


@pytest.mark.asyncio
async def test_cas_rejects_stale_version():
    store = RedisSessionStore("redis://localhost/0", 60, client=fakeredis.aioredis.FakeRedis())
    # 先写入版本 2
    await store.put_pending(make_pending(version=2))
    # 用版本 1（过期）写入，应被 CAS 拒绝
    await store.put_pending(make_pending(version=1))
    got = await store.get_pending("t1", "c1")
    assert got is not None
    assert got.state_version == 2  # 版本号未变


@pytest.mark.asyncio
async def test_clear_pending_removes_state():
    store = RedisSessionStore("redis://localhost/0", 60, client=fakeredis.aioredis.FakeRedis())
    await store.put_pending(make_pending(version=1))
    await store.clear_pending("t1", "c1")
    got = await store.get_pending("t1", "c1")
    assert got is None


@pytest.mark.asyncio
async def test_isolation_between_tenants():
    store = RedisSessionStore("redis://localhost/0", 60, client=fakeredis.aioredis.FakeRedis())
    await store.put_pending(make_pending(version=1, conversation_id="c1"))
    # 不同租户不应能读到 c1 的状态
    got = await store.get_pending("other-tenant", "c1")
    assert got is None
