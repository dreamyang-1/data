"""按语义模型ID拉取平台生成的语义描述文件，本地维护文档兜底。

语义模型在平台发布后由 DSL 转换服务生成 semantic_model_{id}.md，
拆分/提取与洞察综合注入上下文时优先取这份，拉取失败回退本地
《语义描述文件.md》，与历史行为保持一致。
"""
import time
from pathlib import Path
from typing import Any

import httpx

_LOCAL_DESCRIPTION_PATH = Path(__file__).resolve().parents[2] / "语义描述文件.md"
_LOCAL_SOURCE = "语义描述文件.md"
_REMOTE_SOURCE_TEMPLATE = "semantic_model_{model_id}.md"

# (读取时间, 内容)；拉取失败也缓存空内容，避免每轮请求都等超时
_cache: dict[int, tuple[float, str]] = {}


def read_local_description() -> str:
    try:
        return _LOCAL_DESCRIPTION_PATH.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return ""


def _reference(content: str, source: str) -> dict[str, Any]:
    return {"source": source, "available": bool(content), "content": content}


async def load_semantic_description(
    settings: Any, semantic_model_id: int | None
) -> dict[str, Any]:
    """返回 semantic_reference 结构，content 同时供任务规划注入使用。"""
    local = read_local_description()
    base_url = (getattr(settings, "semantic_description_base_url", "") or "").rstrip("/")
    if not semantic_model_id or not base_url:
        return _reference(local, _LOCAL_SOURCE)

    now = time.monotonic()
    ttl = getattr(settings, "semantic_description_cache_ttl_seconds", 30)
    cached = _cache.get(semantic_model_id)
    if cached is not None and now - cached[0] < ttl:
        if cached[1]:
            return _reference(
                cached[1], _REMOTE_SOURCE_TEMPLATE.format(model_id=semantic_model_id)
            )
        return _reference(local, _LOCAL_SOURCE)

    url = f"{base_url}/files/semantic_model_{semantic_model_id}.md"
    content = ""
    try:
        async with httpx.AsyncClient(
            timeout=getattr(settings, "semantic_description_timeout_seconds", 3.0)
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.text.strip()
    except httpx.HTTPError:
        content = ""
    _cache[semantic_model_id] = (now, content)
    if content:
        return _reference(
            content, _REMOTE_SOURCE_TEMPLATE.format(model_id=semantic_model_id)
        )
    return _reference(local, _LOCAL_SOURCE)


def clear_cache() -> None:
    _cache.clear()
