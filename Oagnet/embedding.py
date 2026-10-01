"""Embedding 服务 - 基于 DashScope 兼容 OpenAI 接口的 text-embedding-v4。

提供单条/批量文本向量化，自动分批避免超出API限制。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

from openai import OpenAI

from config import (
    BASE_URL,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_MAX_CONCURRENCY,
    EMBEDDING_MAX_RETRIES,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_API_KEY,
    EMBEDDING_TIMEOUT_SECONDS,
    require_runtime_secret,
)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=require_runtime_secret(
                EMBEDDING_MODEL_API_KEY,
                "OAGNET_EMBEDDING_API_KEY/DASHSCOPE_API_KEY",
            ),
            base_url=BASE_URL,
            timeout=EMBEDDING_TIMEOUT_SECONDS,
            max_retries=EMBEDDING_MAX_RETRIES,
        )
    return _client


def embed_query(text: str) -> list[float]:
    """单条查询向量化"""
    client = _get_client()
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[text])
    return resp.data[0].embedding


def embed_documents(
    texts: Sequence[str], batch_size: int = EMBEDDING_BATCH_SIZE
) -> list[list[float]]:
    """批量文档向量化，自动分批调用

    DashScope text-embedding-v4 单批最多10条，留余量取6
    """
    if not texts:
        return []
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    client = _get_client()
    batches = [
        list(texts[i : i + batch_size])
        for i in range(0, len(texts), batch_size)
    ]

    def embed_batch(batch: list[str]) -> list[list[float]]:
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        # 按index排序保证顺序一致
        resp.data.sort(key=lambda d: d.index)
        resp.data.sort(key=lambda item: item.index)
        if len(resp.data) != len(batch):
            raise RuntimeError(
                "Embedding response size mismatch: "
                f"expected={len(batch)}, actual={len(resp.data)}"
            )
        return [item.embedding for item in resp.data]

    workers = min(EMBEDDING_MAX_CONCURRENCY, len(batches))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = executor.map(embed_batch, batches)
        return [vector for batch_vectors in results for vector in batch_vectors]


if __name__ == "__main__":
    # 自检：测试向量维度
    v = embed_query("今年小程序渠道的支付GMV是多少？")
    print(f"向量维度: {len(v)}")
    print(f"前5维: {v[:5]}")
