from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import Awaitable, Callable, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ErrorKind(StrEnum):
    """适配器错误五分类，对应 README §2.6 Tool 错误五分类。"""

    NETWORK = "NETWORK"            # 连接失败、DNS、TLS 等基础设施层
    TIMEOUT = "TIMEOUT"             # 调用超时
    AUTH = "AUTH"                   # 401/403、签名错误、租户未授权
    CONTRACT = "CONTRACT"           # 4xx 业务错误、Schema 不一致、字段缺失
    UPSTREAM = "UPSTREAM"           # 5xx 上游服务故障
    UNPARSEABLE = "UNPARSEABLE"     # 响应无法解析


class AdapterError(Exception):
    """适配器统一错误。`retryable` 控制重试策略；`kind` 决定降级路径。"""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        source: str,
        retryable: bool,
        status_code: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.source = source
        self.retryable = retryable
        self.status_code = status_code
        self.__cause__ = cause

    def to_fallback_reason(self) -> str:
        """用户可见的兜底话术，不暴露内部细节。"""
        mapping = {
            ErrorKind.NETWORK: "数据服务暂时无法访问，请稍后重试。",
            ErrorKind.TIMEOUT: "数据查询超时，请缩小范围或稍后再试。",
            ErrorKind.AUTH: "当前身份暂无对应数据访问权限。",
            ErrorKind.CONTRACT: "数据服务返回结构异常，已停止查询。",
            ErrorKind.UPSTREAM: "数据服务临时不可用，请稍后重试。",
            ErrorKind.UNPARSEABLE: "无法解析数据服务响应，已停止查询。",
        }
        return mapping.get(self.kind, "数据服务异常，已安全终止。")


def classify_http_error(exc: Exception, source: str) -> AdapterError:
    """把 httpx 异常映射成 AdapterError。"""
    if isinstance(exc, httpx.TimeoutException):
        return AdapterError(ErrorKind.TIMEOUT, str(exc), source=source, retryable=True)
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return AdapterError(ErrorKind.NETWORK, str(exc), source=source, retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return AdapterError(
                ErrorKind.AUTH, f"forbidden ({status})", source=source, retryable=False, status_code=status
            )
        if 400 <= status < 500:
            return AdapterError(
                ErrorKind.CONTRACT, f"client error ({status})", source=source, retryable=False, status_code=status
            )
        if 500 <= status < 600:
            return AdapterError(
                ErrorKind.UPSTREAM, f"server error ({status})", source=source, retryable=True, status_code=status
            )
    return AdapterError(ErrorKind.UNPARSEABLE, str(exc), source=source, retryable=False)


async def safe_call(
    fn: Callable[[], Awaitable[T]],
    *,
    source: str,
    retries: int = 2,
    base_delay: float = 0.2,
    timeout: float = 10.0,
) -> T:
    """三级兜底：重试 → 降级（抛 AdapterError）→ 调用方安全终止。

    - 网络层/超时/5xx 在 `retries` 次内指数退避重试。
    - 4xx 与不可重试错误立即抛出，避免无效重试。
    - 重试用尽仍失败则抛 AdapterError，由 orchestrator 转为 SAFE_FALLBACK。
    """
    last_error: AdapterError | None = None
    for attempt in range(retries + 1):
        try:
            return await asyncio.wait_for(fn(), timeout=timeout)
        except AdapterError as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            last_error = classify_http_error(exc, source)
        # 统一重试决策：不可重试或重试用尽时抛出。
        if not last_error.retryable or attempt == retries:
            raise last_error
        delay = base_delay * (2 ** attempt)
        logger.warning(
            "adapter %s attempt %d failed (%s), retrying in %.2fs",
            source, attempt + 1, last_error.kind, delay,
        )
        await asyncio.sleep(delay)
    # 理论不可达；保险返回最后一次错误。
    assert last_error is not None
    raise last_error
