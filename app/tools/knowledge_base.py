from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings


RetrievalMethod = Literal["semantic", "fulltext", "hybrid", "local", "hybrid_graph"]


class KnowledgeBaseSearchError(RuntimeError):
    """Raised when the platform knowledge-base service cannot return usable data."""


class KnowledgeChunk(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kb_name: str = ""
    page_content: str = ""
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    vector_id: str | None = None


class KnowledgeSearchResult(BaseModel):
    queries: list[str]
    knowledge_bases: list[str]
    chunks: list[KnowledgeChunk]

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def as_tool_output(self) -> dict[str, Any]:
        """Return a JSON-safe shape that an agent can consume directly."""
        return {
            "code": 200,
            "msg": "success",
            "data": {
                "queries": self.queries,
                "knowledge_bases": self.knowledge_bases,
                "chunks": [chunk.model_dump(mode="json") for chunk in self.chunks],
                "chunk_count": self.chunk_count,
            },
        }


class KnowledgeBaseSearchTool:
    """Read-only client for the platform's ``search_docs`` API.

    ``allowed_knowledge_bases`` must come from the trusted platform context, not
    from model-generated arguments. This prevents an agent from reading another
    tenant's knowledge base simply by guessing its identifier.
    """

    name = "search_user_knowledge_base"
    description = (
        "从当前用户已授权的平台知识库检索内部文档数据。适合获取分析所需的业务说明、"
        "报告片段、表格文本和数据来源；只读，不会修改知识库。"
    )

    def __init__(
        self,
        *,
        settings: Settings,
        allowed_knowledge_bases: Iterable[str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        allowed = {str(name).strip() for name in allowed_knowledge_bases if str(name).strip()}
        if not allowed:
            raise ValueError("allowed_knowledge_bases cannot be empty")
        self._settings = settings
        self._allowed = frozenset(allowed)
        self._client = client

    async def __call__(
        self,
        query: str | list[str],
        knowledge_base_names: list[str] | None = None,
        *,
        top_k: int = 20,
        score_threshold: float = 1.9,
        retrieval_method: RetrievalMethod = "hybrid",
        rrf_weight: float = -1.0,
        file_name: str = "",
        metadata: dict[str, Any] | None = None,
        label_list: list[str] | None = None,
        source_list: list[str] | None = None,
        search_filename: bool = True,
        max_hops: int = 1,
        knowledge_graph_identifier: list[str] | None = None,
    ) -> dict[str, Any]:
        queries = [query] if isinstance(query, str) else list(query)
        queries = [item.strip() for item in queries if isinstance(item, str) and item.strip()]
        if not queries:
            raise ValueError("query cannot be empty")
        if not 1 <= top_k <= 200:
            raise ValueError("top_k must be between 1 and 200")
        if not 0 <= score_threshold <= 2:
            raise ValueError("score_threshold must be between 0 and 2")

        targets = set(knowledge_base_names or self._allowed)
        unauthorized = targets - self._allowed
        if unauthorized:
            raise PermissionError(
                "knowledge base is not authorized for this user: " + ", ".join(sorted(unauthorized))
            )
        if not targets:
            raise ValueError("no knowledge base selected")

        payload = {
            "query": queries,
            "knowledge_base_name": sorted(targets),
            "top_k": top_k,
            "score_threshold": score_threshold,
            "file_name": file_name,
            "metadata": metadata or {},
            "retrieval_method": retrieval_method,
            "RRF_weight": rrf_weight,
            "label_list": label_list or [],
            "source_list": source_list or [],
            "search_filename": search_filename,
            "max_hops": max_hops,
            "knowledge_graph_identifier": knowledge_graph_identifier or [],
        }
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._settings.knowledge_base_api_key:
            headers["Authorization"] = (
                f"Bearer {self._settings.knowledge_base_api_key.get_secret_value()}"
            )

        url = (
            self._settings.knowledge_base_url.rstrip("/")
            + "/"
            + self._settings.knowledge_base_search_path.lstrip("/")
        )
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._settings.knowledge_base_timeout_seconds
        )
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
            docs = self._unwrap_documents(body)
            result = KnowledgeSearchResult(
                queries=queries,
                knowledge_bases=sorted(targets),
                chunks=[self._normalize_document(doc) for doc in docs],
            )
            return result.as_tool_output()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise KnowledgeBaseSearchError(f"knowledge-base search failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _unwrap_documents(body: Any) -> list[dict[str, Any]]:
        if isinstance(body, list):
            return [item for item in body if isinstance(item, dict)]
        if not isinstance(body, dict):
            raise TypeError("unexpected response type")
        if body.get("code", 200) != 200:
            raise ValueError(str(body.get("msg") or "knowledge-base service rejected request"))
        data = body.get("data", [])
        if isinstance(data, dict):
            data = data.get("chunks", [])
        if not isinstance(data, list):
            raise TypeError("response data is not a document list")
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _normalize_document(doc: dict[str, Any]) -> KnowledgeChunk:
        vector_id = doc.get("id") or doc.get("vector_id") or doc.get("vs_id")
        return KnowledgeChunk(
            kb_name=str(doc.get("kb_name") or doc.get("knowledge_base_name") or ""),
            page_content=str(doc.get("page_content") or doc.get("content") or ""),
            score=doc.get("score"),
            metadata=doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {},
            vector_id=str(vector_id) if vector_id is not None else None,
        )


def build_knowledge_base_search_tool(
    settings: Settings,
    allowed_knowledge_bases: Iterable[str],
    *,
    client: httpx.AsyncClient | None = None,
) -> KnowledgeBaseSearchTool:
    """Build a request-scoped knowledge tool with a trusted authorization scope."""
    return KnowledgeBaseSearchTool(
        settings=settings,
        allowed_knowledge_bases=allowed_knowledge_bases,
        client=client,
    )
