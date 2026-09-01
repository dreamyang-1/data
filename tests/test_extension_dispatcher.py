import json

import httpx
import pytest

from app.domain.models import ChatRequest, McpConfig, SkillConfig, ToolConfig
from app.services.extension_dispatcher import ExtensionDispatcher
from app.services.tool_selector import OptionalToolSelector
from app.config import Settings
from app.skills.dynamic import DynamicSkillLoader


def chat(**updates):
    base = ChatRequest(
        application_id="app", conversation_id="conversation", message_id="message",
        question="分析本月销售趋势", semantic_model_id=6,
    )
    return base.model_copy(update=updates)


@pytest.mark.asyncio
async def test_explicit_skill_binding_calls_private_http_tool():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "insight": "ok"})

    dispatcher = ExtensionDispatcher(transport=httpx.MockTransport(handler))
    request = chat(
        skills=[SkillConfig(code="TREND_ANALYSIS", slug="trend_helper")],
        tools=[ToolConfig(name="trend_helper", url="http://192.168.1.20/tool")],
    )
    result = await dispatcher.execute(
        chat=request, intent="TREND_ANALYSIS", builtin_skill="trend_analysis",
        payload={"question": request.question, "rows": [{"value": 1}]},
    )

    assert result[0].status == "COMPLETED"
    assert result[0].output["insight"] == "ok"
    assert seen["question"] == request.question


@pytest.mark.asyncio
async def test_non_web_tool_business_code_is_not_treated_as_tool_status():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 503, "name": "商品编码503"})

    dispatcher = ExtensionDispatcher(transport=httpx.MockTransport(handler))
    request = chat(
        skills=[SkillConfig(code="CATALOG", slug="catalog_lookup")],
        tools=[ToolConfig(
            name="catalog_lookup",
            description="查询商品目录",
            url="https://tool.example/catalog",
        )],
    )
    result = await dispatcher.execute(
        chat=request,
        intent="DETAIL_QUERY",
        builtin_skill=None,
        payload={"question": request.question},
    )

    assert result[0].status == "COMPLETED"
    assert result[0].output == {"code": 503, "name": "商品编码503"}


@pytest.mark.asyncio
async def test_dynamic_skill_content_and_version_are_sent_to_bound_tool(tmp_path):
    skill_path = tmp_path / "A_TEST" / "trend_helper" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: trend\n---\nUse monthly comparison.", encoding="utf-8")
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    dispatcher = ExtensionDispatcher(
        transport=httpx.MockTransport(handler),
        skill_loader=DynamicSkillLoader(tmp_path),
    )
    request = chat(
        skills=[SkillConfig(code="A_TEST", slug="trend_helper")],
        tools=[ToolConfig(name="trend_helper", url="http://192.168.1.20/tool")],
    )
    result = await dispatcher.execute(
        chat=request,
        intent="TREND_ANALYSIS",
        builtin_skill="trend_analysis",
        payload={"question": request.question},
    )

    assert "Use monthly comparison." in seen["skill_context"]["instructions"]
    assert seen["skill_context"]["execution_boundary"]
    assert result[0].output["_skill"]["slug"] == "trend_helper"
    assert len(result[0].output["_skill"]["version"]) == 16


@pytest.mark.asyncio
async def test_unbound_tool_is_not_called():
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("unbound tool must not be called")

    dispatcher = ExtensionDispatcher(transport=httpx.MockTransport(handler))
    request = chat(
        tools=[ToolConfig(name="trend_helper", url="http://192.168.1.20/tool")]
    )
    assert await dispatcher.execute(
        chat=request, intent="TREND_ANALYSIS", builtin_skill="trend_analysis",
        payload={"question": request.question},
    ) == []


@pytest.mark.asyncio
async def test_model_can_select_one_optional_read_only_tool_and_schema_is_respected():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "selected_tools": ["inventory_lookup"]
                })}}]
            })
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"stock": 18})

    transport = httpx.MockTransport(handler)
    settings = Settings(
        env="test",
        intent_model_api_key="test-key",
        intent_model_base_url="https://model.example/v1",
    )
    dispatcher = ExtensionDispatcher(
        transport=transport,
        tool_selector=OptionalToolSelector(settings, transport=transport),
    )
    request = chat(
        question="查询商品 A100 的库存",
        tools=[ToolConfig(
            name="inventory_lookup",
            description="查询商品库存",
            url="https://tool.example/inventory",
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
        )],
    )
    result = await dispatcher.execute(
        chat=request, intent="METRIC_QUERY", builtin_skill=None,
        payload={"question": request.question, "rows": [{"private": "not sent"}]},
    )

    assert result[0].status == "COMPLETED"
    assert result[0].output == {"stock": 18}
    assert seen == {"query": request.question}


@pytest.mark.asyncio
async def test_core_query_tools_are_never_offered_for_autonomous_selection():
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("core query tools must neither reach the model nor be called")

    transport = httpx.MockTransport(handler)
    settings = Settings(env="test", intent_model_api_key="test-key")
    dispatcher = ExtensionDispatcher(
        transport=transport,
        tool_selector=OptionalToolSelector(settings, transport=transport),
    )
    request = chat(tools=[
        ToolConfig(
            name="自然语言提取AST", description="查询数据",
            url="http://192.168.1.49:8021/agent/query",
        ),
        ToolConfig(
            name="sql翻译器", description="查询数据",
            url="http://192.168.1.175:48000/api/ast-to-sql",
        ),
    ])

    assert await dispatcher.execute(
        chat=request, intent="METRIC_QUERY", builtin_skill=None,
        payload={"question": request.question},
    ) == []


@pytest.mark.asyncio
async def test_mutating_or_unsatisfied_tool_is_not_offered_for_selection():
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("unsafe or unusable tools must not reach the model")

    transport = httpx.MockTransport(handler)
    settings = Settings(env="test", intent_model_api_key="test-key")
    dispatcher = ExtensionDispatcher(
        transport=transport,
        tool_selector=OptionalToolSelector(settings, transport=transport),
    )
    request = chat(tools=[
        ToolConfig(
            name="delete_order", description="删除订单",
            url="https://tool.example/delete",
        ),
        ToolConfig(
            name="inventory_lookup", description="查询库存",
            url="https://tool.example/inventory",
            inputSchema={"required": ["sku"], "properties": {"sku": {"type": "string"}}},
        ),
    ])

    assert await dispatcher.execute(
        chat=request, intent="METRIC_QUERY", builtin_skill=None,
        payload={"question": request.question},
    ) == []


@pytest.mark.asyncio
async def test_caller_supplied_http_tool_does_not_require_a_second_allowlist():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    dispatcher = ExtensionDispatcher(transport=httpx.MockTransport(handler))
    request = chat(
        skills=[SkillConfig(code="TREND_ANALYSIS", slug="public_tool")],
        tools=[ToolConfig(name="public_tool", url="https://example.com/tool")],
    )
    result = await dispatcher.execute(
        chat=request, intent="TREND_ANALYSIS", builtin_skill="trend_analysis",
        payload={"question": request.question},
    )
    assert result[0].status == "COMPLETED"


@pytest.mark.asyncio
async def test_bound_streamable_http_mcp_tool_is_called():
    methods = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "session-1"},
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202, json={})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0", "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "done"}]},
            },
        )

    dispatcher = ExtensionDispatcher(transport=httpx.MockTransport(handler))
    request = chat(
        skills=[SkillConfig(code="TREND_ANALYSIS", slug="mcp:diagnose")],
        mcp=[McpConfig(
            mcp_server_url="http://192.168.1.21/mcp",
            connect_type="streamable_http",
        )],
    )
    result = await dispatcher.execute(
        chat=request, intent="TREND_ANALYSIS", builtin_skill="trend_analysis",
        payload={"question": request.question},
    )
    assert result[0].status == "COMPLETED"
    assert methods == ["initialize", "notifications/initialized", "tools/call"]


@pytest.mark.asyncio
async def test_model_selects_discovered_mcp_tool_without_explicit_skill_binding():
    methods = []
    called_arguments = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.host == "model.example":
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "selected_tools": ["mcp:inventory_lookup"]
                })}}]
            })
        methods.append(payload["method"])
        if payload["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "session-1"},
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202, json={})
        if payload["method"] == "tools/list":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": payload["id"],
                "result": {"tools": [{
                    "name": "inventory_lookup",
                    "description": "查询商品库存",
                    "inputSchema": {
                        "type": "object", "required": ["query"],
                        "properties": {"query": {"type": "string"}},
                    },
                }]},
            })
        called_arguments.update(payload["params"]["arguments"])
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": payload["id"],
            "result": {"content": [{"type": "text", "text": "18"}]},
        })

    transport = httpx.MockTransport(handler)
    settings = Settings(
        env="test", intent_model_api_key="test-key",
        intent_model_base_url="https://model.example/v1",
    )
    dispatcher = ExtensionDispatcher(
        transport=transport,
        tool_selector=OptionalToolSelector(settings, transport=transport),
    )
    request = chat(
        question="查询商品 A100 的库存",
        mcp=[McpConfig(
            mcp_server_url="https://mcp.example/mcp",
            connect_type="streamable_http",
        )],
    )
    result = await dispatcher.execute(
        chat=request, intent="METRIC_QUERY", builtin_skill=None,
        payload={"question": request.question, "rows": [{"secret": 1}]},
    )

    assert result[0].status == "COMPLETED"
    assert methods == [
        "initialize", "notifications/initialized", "tools/list",
        "initialize", "notifications/initialized", "tools/call",
    ]
    assert called_arguments == {"query": request.question}


@pytest.mark.asyncio
async def test_skill_instructions_participate_in_optional_tool_selection(tmp_path):
    skill_path = tmp_path / "A_TEST" / "inventory_lookup" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "Use this skill only for warehouse inventory questions.", encoding="utf-8"
    )
    model_prompt = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            model_prompt.update(json.loads(request.content))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "selected_tools": ["inventory_lookup"]
                })}}]
            })
        sent = json.loads(request.content)
        assert "warehouse inventory" in sent["skill_context"]["instructions"]
        return httpx.Response(200, json={"stock": 18})

    transport = httpx.MockTransport(handler)
    settings = Settings(
        env="test", intent_model_api_key="test-key",
        intent_model_base_url="https://model.example/v1",
    )
    dispatcher = ExtensionDispatcher(
        transport=transport,
        tool_selector=OptionalToolSelector(settings, transport=transport),
        skill_loader=DynamicSkillLoader(tmp_path),
    )
    request = chat(
        question="查询仓库库存",
        skills=[SkillConfig(code="A_TEST", slug="inventory_lookup")],
        tools=[ToolConfig(
            name="inventory_lookup", description="查询库存",
            url="https://tool.example/inventory",
        )],
    )
    result = await dispatcher.execute(
        chat=request, intent="METRIC_QUERY", builtin_skill=None,
        payload={"question": request.question},
    )

    assert result[0].status == "COMPLETED"
    assert "Skill instructions" in model_prompt["messages"][1]["content"]
