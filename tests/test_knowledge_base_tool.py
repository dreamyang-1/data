import httpx
import pytest

from app.config import Settings
from app.tools import KnowledgeBaseSearchError, build_knowledge_base_search_tool


def settings() -> Settings:
    return Settings(
        env="test",
        intent_model_enabled=False,
        knowledge_base_url="http://kb.test",
        knowledge_base_api_key="secret",
    )


@pytest.mark.asyncio
async def test_searches_only_authorized_knowledge_base_and_normalizes_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert request.url.path == "/knowledge_base/search_docs"
        assert request.headers["Authorization"] == "Bearer secret"
        assert body["knowledge_base_name"] == ["KB_USER_1"]
        assert body["score_threshold"] == 0.8
        return httpx.Response(
            200,
            json=[
                {
                    "kb_name": "KB_USER_1",
                    "page_content": "华东区销售额为 120 万元",
                    "score": 0.12,
                    "metadata": {"source": "sales.xlsx", "sheet": "月报"},
                    "id": "chunk-1",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tool = build_knowledge_base_search_tool(
            settings(), ["KB_USER_1"], client=client
        )
        result = await tool(
            "华东区销售额",
            ["KB_USER_1"],
            top_k=10,
            score_threshold=0.8,
        )

    assert result["data"]["chunk_count"] == 1
    assert result["data"]["chunks"][0]["metadata"]["source"] == "sales.xlsx"
    assert result["data"]["chunks"][0]["vector_id"] == "chunk-1"


@pytest.mark.asyncio
async def test_rejects_knowledge_base_outside_trusted_scope_without_http_call():
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tool = build_knowledge_base_search_tool(settings(), ["KB_USER_1"], client=client)
        with pytest.raises(PermissionError):
            await tool("内部数据", ["KB_OTHER_TENANT"])
    assert called is False


@pytest.mark.asyncio
async def test_converts_remote_failure_to_domain_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tool = build_knowledge_base_search_tool(settings(), ["KB_USER_1"], client=client)
        with pytest.raises(KnowledgeBaseSearchError):
            await tool("内部数据")
