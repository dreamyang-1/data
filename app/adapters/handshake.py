"""Capability Handshake：启动期探测五个外部服务可用性。

设计原则：
- 启动期一次性探测，避免每次请求都探活的开销。
- 探测失败不阻断启动：对应能力标记为不可用，工作流在路由阶段让相关意图
  直接 safe_terminate，而不是让请求跑到运行时才失败。
- 探测有 2s 超时，避免某个服务挂起拖慢启动。
- HandshakeReport 挂载到 Container，可暴露给 /health 端点供运维查看。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.adapters.base import AdapterBundle

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HandshakeReport:
    """启动期能力探测结果。

    - 各能力 bool：True 表示对应外部服务健康。
    - probed_at: 探测时间戳，用于 /health 端点显示新鲜度。
    - raw_errors: 不可用能力的失败原因，便于排查。
    """

    semantic: bool
    policy: bool
    query: bool
    knowledge: bool
    analysis: bool
    probed_at: str
    raw_errors: dict[str, str] = field(default_factory=dict)

    def is_capable(self, capability: str) -> bool:
        """按能力名查询可用性。"""
        return getattr(self, capability, False)


async def _probe_one(name: str, fn: object) -> tuple[str, bool, str]:
    try:
        ok = await asyncio.wait_for(fn(), timeout=2.0)  # type: ignore[arg-type]
        return name, bool(ok), ""
    except asyncio.TimeoutError:
        return name, False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return name, False, str(exc)[:200]


async def probe_capabilities(adapters: AdapterBundle) -> HandshakeReport:
    """并发探测五个适配器的健康端点。

    - 任一服务不可用不会阻塞其他服务的探测。
    - 返回 HandshakeReport，由 Container 持有。
    """
    from datetime import datetime, timezone

    tasks = [
        _probe_one("semantic", adapters.semantic.health),
        _probe_one("policy", adapters.policy.health),
        _probe_one("query", adapters.query.health),
        _probe_one("knowledge", adapters.knowledge.health),
        _probe_one("analysis", adapters.analysis.health),
    ]
    results = await asyncio.gather(*tasks)
    raw_errors = {name: err for name, ok, err in results if not ok}
    if raw_errors:
        logger.warning("capability handshake partial failure: %s", raw_errors)
    return HandshakeReport(
        semantic=results[0][1],
        policy=results[1][1],
        query=results[2][1],
        knowledge=results[3][1],
        analysis=results[4][1],
        probed_at=datetime.now(timezone.utc).isoformat(),
        raw_errors=raw_errors,
    )
