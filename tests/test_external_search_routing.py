import asyncio
import json

import httpx
import pytest

from app.adapters import build_mock_adapters
from app.analysis import AnalysisError, AnalysisOutput
from app.config import Settings
from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ChatRequest,
    ExtensionExecution,
    McpConfig,
    PrimaryIntent,
    SkillConfig,
    ToolConfig,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.services.extension_dispatcher import ExtensionDispatcher
from app.services.tool_selector import OptionalToolSelector
from app.skills.dynamic import DynamicSkillLoader
from app.stores import InMemorySessionStore


def web_tool(name: str = "web_search_data") -> ToolConfig:
    return ToolConfig(
        name=name,
        description="联网搜索工具，用于查询互联网上的公开信息",
        url="https://search.example/web-search",
        inputSchema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer"},
                "summary": {"type": "boolean"},
            },
        },
    )


def search_response() -> dict:
    return {
        "code": 200,
        "msg": "联网搜索成功",
        "result": {
            "value": [
                {
                    "name": "上海交通大学医学院附属瑞金医院",
                    "url": "https://www.rjh.com.cn/",
                    "summary": "医院地址为上海市黄浦区瑞金二路197号。",
                }
            ]
        },
        "value": [
            {
                "name": "上海交通大学医学院附属瑞金医院",
                "url": "https://www.rjh.com.cn/",
                "summary": "医院地址为上海市黄浦区瑞金二路197号。",
            }
        ],
    }


def ranked_entity_search_response(label: str, index: int) -> dict:
    record = {
        "name": f"{label}官网与企业画像",
        "url": f"https://dealer-{index}.example/profile",
        "summary": f"{label}的公开信息：主营业务与官网资料。",
    }
    # New_Agent exposes both result.value and value.  The orchestrator must
    # merge these duplicate records instead of inflating evidence coverage.
    return {
        "code": 200,
        "msg": "联网搜索成功",
        "result": {"value": [record]},
        "value": [record],
    }


@pytest.mark.asyncio
async def test_web_search_true_uses_builtin_bocha_compatible_tool():
    sent = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        assert request.url == httpx.URL("https://api.bochaai.com/v1/web-search")
        assert request.headers["Authorization"] == "Bearer test-bocha-key"
        return httpx.Response(200, json=search_response())

    settings = Settings(bocha_api_key="test-bocha-key")
    dispatcher = ExtensionDispatcher(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院地址在哪里？",
        web_search=True,
    )
    result = await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question, "count": 5, "summary": True},
        required_capability="WEB_SEARCH",
    )

    assert result[0].status == "COMPLETED"
    assert sent == {"query": chat.question, "count": 5, "summary": True}


@pytest.mark.asyncio
async def test_web_search_http_200_error_body_is_not_reported_as_success():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": 503,
            "error_type": "network_service_error",
            "msg": "联网搜索失败：请求超时",
            "data": {"result": [], "value": []},
        })

    dispatcher = ExtensionDispatcher(
        settings=Settings(bocha_api_key="test-bocha-key"),
        transport=httpx.MockTransport(handler),
    )
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院地址在哪里？",
        web_search=True,
    )
    result = await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question, "count": 10, "summary": True},
        required_capability="WEB_SEARCH",
    )

    assert result[0].status == "FAILED"
    assert result[0].status_code == 503
    assert result[0].error_type == "network_service_error"
    assert result[0].output["code"] == 503


@pytest.mark.asyncio
async def test_web_search_http_auth_failure_uses_new_agent_error_contract():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    dispatcher = ExtensionDispatcher(
        settings=Settings(bocha_api_key="test-bocha-key"),
        transport=httpx.MockTransport(handler),
    )
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院地址在哪里？",
        web_search=True,
    )
    result = await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question, "count": 10, "summary": True},
        required_capability="WEB_SEARCH",
    )

    assert result[0].status == "FAILED"
    assert result[0].status_code == 401
    assert result[0].error_type == "auth_error"
    assert result[0].output == {
        "code": 401,
        "error_type": "auth_error",
        "msg": "联网搜索请求失败，HTTP 403",
        "data": {"result": [], "value": []},
    }


@pytest.mark.asyncio
async def test_non_web_http_tool_business_code_is_not_treated_as_search_error():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 123, "value": "业务分类编码"})

    dispatcher = ExtensionDispatcher(transport=httpx.MockTransport(handler))
    result = await dispatcher._call_http(
        "catalog_lookup",
        ToolConfig(
            name="catalog_lookup",
            description="查询内部商品目录",
            url="https://catalog.example/query",
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
        ),
        {"query": "测试商品"},
    )

    assert result.status == "COMPLETED"
    assert result.output == {"code": 123, "value": "业务分类编码"}
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_web_search_false_does_not_expose_builtin_tool():
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("web_search=false must not call the built-in tool")

    dispatcher = ExtensionDispatcher(
        settings=Settings(bocha_api_key="test-bocha-key"),
        transport=httpx.MockTransport(handler),
    )
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院地址在哪里？",
    )

    assert await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question},
        required_capability="WEB_SEARCH",
    ) == []


@pytest.mark.asyncio
async def test_web_search_true_exposes_but_does_not_force_builtin_tool():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model.example":
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "selected_tools": [],
                })}}],
            })
        raise AssertionError("an optional web tool must not be called when not selected")

    transport = httpx.MockTransport(handler)
    settings = Settings(
        bocha_api_key="test-bocha-key",
        intent_model_api_key="test-model-key",
        intent_model_base_url="https://model.example/v1",
    )
    dispatcher = ExtensionDispatcher(
        settings=settings,
        transport=transport,
        tool_selector=OptionalToolSelector(settings, transport=transport),
    )
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="查询本月销售额",
        web_search=True,
    )

    assert await dispatcher.execute(
        chat=chat,
        intent="METRIC_QUERY",
        builtin_skill=None,
        payload={"question": chat.question},
    ) == []


@pytest.mark.asyncio
async def test_web_search_missing_key_degrades_without_network_call():
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("a missing key must not trigger an HTTP call")

    dispatcher = ExtensionDispatcher(
        settings=Settings(bocha_api_key=None),
        transport=httpx.MockTransport(handler),
    )
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院地址在哪里？",
        web_search=True,
    )

    assert await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question},
        required_capability="WEB_SEARCH",
    ) == []


@pytest.mark.asyncio
async def test_explicit_web_tool_has_priority_over_builtin_tool():
    called_hosts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        called_hosts.append(request.url.host)
        return httpx.Response(200, json=search_response())

    dispatcher = ExtensionDispatcher(
        settings=Settings(bocha_api_key="test-bocha-key"),
        transport=httpx.MockTransport(handler),
    )
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院地址在哪里？",
        web_search=True,
        tools=[web_tool()],
    )
    result = await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question},
        required_capability="WEB_SEARCH",
    )

    assert result[0].status == "COMPLETED"
    assert called_hosts == ["search.example"]


@pytest.mark.asyncio
async def test_required_web_search_reuses_new_agent_arguments_without_model_selection():
    sent = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=search_response())

    dispatcher = ExtensionDispatcher(transport=httpx.MockTransport(handler))
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院地址在哪里？",
        tools=[web_tool()],
    )
    result = await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question, "count": 5, "summary": True},
        required_capability="WEB_SEARCH",
    )

    assert result[0].status == "COMPLETED"
    assert sent == {"query": chat.question, "count": 5, "summary": True}


@pytest.mark.asyncio
async def test_web_search_skill_can_label_a_generic_bound_http_tool(tmp_path):
    skill_path = tmp_path / "PUBLIC" / "public_lookup" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("使用联网搜索查询互联网公开信息。", encoding="utf-8")

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=search_response())

    dispatcher = ExtensionDispatcher(
        transport=httpx.MockTransport(handler),
        skill_loader=DynamicSkillLoader(tmp_path),
    )
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院在哪里？",
        skills=[SkillConfig(code="PUBLIC", slug="public_lookup")],
        tools=[ToolConfig(
            name="public_lookup",
            description="只读公开信息查询",
            url="https://search.example/search",
            inputSchema={
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
        )],
    )
    result = await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question},
        required_capability="WEB_SEARCH",
    )

    assert result[0].status == "COMPLETED"
    assert result[0].output["_skill"]["slug"] == "public_lookup"


@pytest.mark.asyncio
async def test_required_web_search_discovers_and_calls_mcp_capability():
    methods = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-1"},
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202, json={})
        if payload["method"] == "tools/list":
            return httpx.Response(200, json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"tools": [{
                    "name": "web_search_data",
                    "description": "联网搜索互联网公开信息",
                    "inputSchema": {
                        "required": ["query"],
                        "properties": {"query": {"type": "string"}},
                    },
                }]},
            })
        return httpx.Response(200, json={
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "content": [{"type": "text", "text": json.dumps(search_response(), ensure_ascii=False)}]
            },
        })

    dispatcher = ExtensionDispatcher(transport=httpx.MockTransport(handler))
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院地址在哪里？",
        mcp=[McpConfig(
            mcp_server_url="https://mcp.example/mcp",
            connect_type="streamable_http",
        )],
    )
    result = await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question},
        required_capability="WEB_SEARCH",
    )

    assert result[0].status == "COMPLETED"
    assert methods == [
        "initialize", "notifications/initialized", "tools/list",
        "initialize", "notifications/initialized", "tools/call",
    ]


@pytest.mark.asyncio
async def test_required_web_search_mcp_error_contract_is_not_reported_as_success():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-1"},
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202, json={})
        if payload["method"] == "tools/list":
            return httpx.Response(200, json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"tools": [{
                    "name": "web_search_data",
                    "description": "联网搜索互联网公开信息",
                    "inputSchema": {
                        "required": ["query"],
                        "properties": {"query": {"type": "string"}},
                    },
                }]},
            })
        return httpx.Response(200, json={
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {"content": [{
                "type": "text",
                "text": json.dumps({
                    "code": 503,
                    "error_type": "network_service_error",
                    "msg": "联网搜索失败：请求超时",
                }, ensure_ascii=False),
            }]},
        })

    dispatcher = ExtensionDispatcher(transport=httpx.MockTransport(handler))
    chat = ChatRequest(semantic_model_id=81,
        application_id="app",
        conversation_id="c",
        message_id="m",
        question="瑞金医院地址在哪里？",
        mcp=[McpConfig(
            mcp_server_url="https://mcp.example/mcp",
            connect_type="streamable_http",
        )],
    )
    result = await dispatcher.execute(
        chat=chat,
        intent="OUT_OF_SCOPE",
        builtin_skill=None,
        payload={"question": chat.question},
        required_capability="WEB_SEARCH",
    )

    assert result[0].status == "FAILED"
    assert result[0].status_code == 503
    assert result[0].error_type == "network_service_error"


@pytest.mark.asyncio
async def test_public_hospital_address_is_answered_before_out_of_scope_shortcut():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=search_response())

    settings = Settings(env="test", adapter_mode="mock", intent_model_enabled=False)
    agent = DataAnalysisOrchestrator(
        settings=settings,
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        extension_dispatcher=ExtensionDispatcher(
            transport=httpx.MockTransport(handler),
        ),
    )
    response = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app",
            conversation_id="address",
            message_id="m1",
            question="瑞金医院地址在哪里？",
            tools=[web_tool()],
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert response.intent != PrimaryIntent.OUT_OF_SCOPE
    assert "瑞金二路197号" in response.answer
    assert "https://www.rjh.com.cn/" in response.answer
    assert response.evidence[0].kind == "WEB_SEARCH_RESULT"
    assert response.extension_executions[0].name == "web_search_data"


@pytest.mark.asyncio
async def test_default_orchestrator_honors_web_search_flag_without_request_tool():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=search_response())

    settings = Settings(
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        bocha_api_key="test-bocha-key",
    )
    agent = DataAnalysisOrchestrator(
        settings=settings,
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )
    agent.extension_dispatcher.transport = httpx.MockTransport(handler)
    response = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app",
            conversation_id="builtin-address",
            message_id="m1",
            question="瑞金医院地址在哪里？",
            web_search=True,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert "瑞金二路197号" in response.answer
    assert response.extension_executions[0].name == "web_search_data"


class MixedProfileClassifier:
    def classify(self, question, identity, conversation_id):
        return CanonicalAnalysisRequest(
            conversation_id=conversation_id,
            application_id="app",
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            original_question=question,
            primary_intent=PrimaryIntent.DETAIL_QUERY,
            entity="经销商",
            fields=["经销商名称", "业务规模"],
            dimensions=["经销商"],
            operators=[AnalysisOperator.TOP_N],
            ranking_limit=3,
            intent_confidence=1,
        )


class FailingProfileClassifier:
    def classify(self, question, identity, conversation_id):
        return CanonicalAnalysisRequest(
            conversation_id=conversation_id,
            application_id="app",
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            original_question=question,
            primary_intent=PrimaryIntent.DETAIL_QUERY,
            entity="经销商",
            fields=["经销商名称"],
            dimensions=["经销商"],
            operators=[AnalysisOperator.TOP_N],
            ranking_limit=3,
            intent_confidence=1,
        )


class FailingProfileAnalysisEngine:
    def analyze_ranking(self, request, columns, rows, knowledge):
        raise AnalysisError("画像标签存在歧义")


class RankedProfileAnalysisEngine:
    labels = ["经销商甲", "经销商乙", "经销商丙"]

    def analyze_ranking(self, request, columns, rows, knowledge):
        rankings = [
            {"rank": index, "label": label, "value": 4 - index, "profile": {}}
            for index, label in enumerate(self.labels, 1)
        ]
        return AnalysisOutput(
            answer="数据库排名：" + "、".join(self.labels),
            method="validated_top_n_ranking",
            facts={"rankings": rankings, "chart_specs": []},
        )


@pytest.mark.asyncio
async def test_mixed_dealer_profile_keeps_internal_query_then_adds_web_supplement():
    sent_queries: list[str] = []
    active = 0
    max_active = 0

    async def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        query = json.loads(http_request.content)["query"]
        sent_queries.append(query)
        label = next(
            label for label in RankedProfileAnalysisEngine.labels
            if f'"{label}"' in query
        )
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200,
            json=ranked_entity_search_response(
                label, RankedProfileAnalysisEngine.labels.index(label) + 1
            ),
        )

    settings = Settings(env="test", adapter_mode="mock", intent_model_enabled=False)
    agent = DataAnalysisOrchestrator(
        settings=settings,
        classifier=MixedProfileClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        analysis_engine=RankedProfileAnalysisEngine(),
        extension_dispatcher=ExtensionDispatcher(
            transport=httpx.MockTransport(handler),
        ),
    )
    response = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app",
            conversation_id="mixed",
            message_id="m1",
            question="限定上海和心血管内科，筛选经销商并展示TOP3画像",
            tools=[web_tool()],
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert any(item.kind == "QUERY_RESULT" for item in response.evidence)
    assert any(item.kind == "WEB_SEARCH_RESULT" for item in response.evidence)
    assert "公开信息补充" in response.answer
    assert "数据库排名：经销商甲、经销商乙、经销商丙" in response.answer
    assert max_active == 3
    assert len(sent_queries) == 3
    assert all("企业画像" in query and "公开信息" in query for query in sent_queries)
    for label in RankedProfileAnalysisEngine.labels:
        assert any(f'"{label}"' in query for query in sent_queries)
    web_evidence = next(
        item for item in response.evidence if item.kind == "WEB_SEARCH_RESULT"
    )
    assert web_evidence.payload["ranking_labels"] == RankedProfileAnalysisEngine.labels
    assert web_evidence.payload["covered_ranking_labels"] == RankedProfileAnalysisEngine.labels
    assert web_evidence.payload["ranking_label_coverage"] == 1
    assert len(web_evidence.payload["records"]) == 3
    assert {
        record["entity_label"] for record in web_evidence.payload["records"]
    } == set(RankedProfileAnalysisEngine.labels)


@pytest.mark.asyncio
async def test_web_enrichment_is_not_called_when_profile_analysis_stops_safely():
    call_count = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=search_response())

    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=FailingProfileClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        analysis_engine=FailingProfileAnalysisEngine(),
        extension_dispatcher=ExtensionDispatcher(
            transport=httpx.MockTransport(handler),
        ),
    )
    response = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app",
            conversation_id="mixed-analysis-fallback",
            message_id="m1",
            question="限定上海和心血管内科，筛选经销商并展示TOP3画像",
            tools=[web_tool()],
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "SAFE_FALLBACK"
    assert call_count == 0
    assert any(item.kind == "QUERY_RESULT" for item in response.evidence)
    assert not any(item.kind == "WEB_SEARCH_RESULT" for item in response.evidence)
    assert response.extension_executions == []
    assert response.reliability.level == "FAIL"


@pytest.mark.asyncio
async def test_ranked_web_failure_does_not_change_database_ranking():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "unavailable"})

    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=MixedProfileClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        analysis_engine=RankedProfileAnalysisEngine(),
        extension_dispatcher=ExtensionDispatcher(
            transport=httpx.MockTransport(handler),
        ),
    )
    response = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app",
            conversation_id="ranked-web-failure",
            message_id="m1",
            question="限定上海和心血管内科，筛选经销商并展示TOP3画像",
            tools=[web_tool()],
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert "数据库排名：经销商甲、经销商乙、经销商丙" in response.answer
    assert not any(item.kind == "WEB_SEARCH_RESULT" for item in response.evidence)
    assert len(response.extension_executions) == 3
    assert all(item.status == "FAILED" for item in response.extension_executions)
    assert response.reliability.level != "FAIL"


def test_internal_contact_query_is_not_misrouted_as_pure_external_search():
    question = "提供上海地区做BD品牌产品的经销商及联系方式"
    assert DataAnalysisOrchestrator._external_search_mode(question) is None
    assert (
        DataAnalysisOrchestrator._external_search_mode(
            "限定上海和心血管内科，筛选经销商并展示TOP3画像"
        )
        == "ENRICH"
    )


def test_public_address_answer_prefers_independent_consensus_over_first_result():
    output = {
        "code": 200,
        "value": [
            {
                "name": "某医院地址在哪里",
                "url": "https://content.example/a",
                "summary": "该医院位于上海市徐汇区华山路197号。",
            },
            {
                "name": "医院资料",
                "url": "https://source-one.example/hospital",
                "summary": "总院地址：上海市黄浦区瑞金二路197号。",
            },
            {
                "name": "就医指南",
                "url": "https://source-two.example/guide",
                "summary": "医院地址为上海市黄浦区瑞金二路197号。",
            },
        ],
    }
    answer, records = DataAnalysisOrchestrator._external_search_material([
        ExtensionExecution(
            name="web_search_data",
            kind="HTTP_TOOL",
            status="COMPLETED",
            output=output,
        )
    ])

    assert answer.startswith(
        "多个独立公开来源一致指向：上海市黄浦区瑞金二路197号。"
    )
    assert "瑞金二路197号" in records[0]["snippet"]
    assert any("华山路197号" in item["snippet"] for item in records)


def test_public_address_consensus_does_not_guess_on_tie_or_single_source():
    assert DataAnalysisOrchestrator._consensus_chinese_address([
        {
            "url": "https://one.example/a",
            "snippet": "地址：上海市黄浦区瑞金二路197号。",
        },
        {
            "url": "https://two.example/b",
            "snippet": "地址：上海市徐汇区华山路197号。",
        },
    ]) == ""
