from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis

from app.domain.models import PendingState


def _key(tenant_id: str, conversation_id: str) -> str:
    # 按平台规范：租户命名空间 + 会话维度，避免跨租户冲突。
    return f"da:pending:{tenant_id}:session:{conversation_id}"


class RedisSessionStore:
    """生产级会话存储：基于 Redis，支持租户命名空间、CAS、TTL。

    - Key 形如 `da:pending:<tenant_id>:session:<conversation_id>`，避免跨租户污染。
    - 写入使用 WATCH/MULTI 实现真正的 CAS：版本号严格递增，过期写入被丢弃。
    - 自动 TTL，过期会话被 Redis 清理，避免悬空状态。
    - 不依赖"上层保证无并发"，多请求并发提交时只会保留最新版本。
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        client: redis.Redis | None = None,
    ) -> None:
        # 测试用 client 注入；生产用 redis_url 自动构造。
        self._redis = client if client is not None else redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_seconds

    async def get_pending(self, tenant_id: str, conversation_id: str) -> PendingState | None:
        raw: Any = await self._redis.get(_key(tenant_id, conversation_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return PendingState.model_validate(data)

    async def put_pending(self, state: PendingState) -> None:
        # 乐观锁：WATCH key → GET 当前值 → 比较版本号 → MULTI/SET/EXPIRE/EXEC。
        # 期间若 key 被修改，EXEC 返回 None，事务回滚，本次写入被丢弃（CAS 失败）。
        key = _key(state.request.tenant_id, state.request.conversation_id)
        payload = json.dumps(state.model_dump(mode="json"), ensure_ascii=False)
        async with self._redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                current = await pipe.get(key)
                if current:
                    try:
                        cur_version = json.loads(current).get("state_version", 0)
                    except (json.JSONDecodeError, TypeError):
                        cur_version = 0
                    if state.state_version <= cur_version:
                        # 版本号过期，放弃写入
                        await pipe.unwatch()
                        return
                pipe.multi()
                pipe.set(key, payload, ex=self._ttl)
                await pipe.execute()
            except redis.WatchError:
                # 被并发写入抢先，本版本丢弃
                return

    async def clear_pending(self, tenant_id: str, conversation_id: str) -> None:
        await self._redis.delete(_key(tenant_id, conversation_id))

    async def aclose(self) -> None:
        await self._redis.aclose()

