"""MCP 文件分析通道（方案 A：架构对齐 New_Agent）的单元与集成测试。

覆盖：MCP 会话协议（initialize/list/call 会话头复用、SSE 分帧解析）、LLM 多轮
工具循环（成功串联、失败分类回填、轮数上限强制总结）、orchestrator 触发闸门
与响应组装/降级。全部使用 httpx.MockTransport，无外部依赖。
"""
import json

import httpx
import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    ChatRequest,
    ExtensionExecution,
    McpConfig,
    PrimaryIntent,
    SkillConfig,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.services.mcp_analysis_runner import (
    McpAnalysisOutcome,
    McpAnalysisRunner,
    McpSessionClient,
    _extract_artifact_urls,
    _tool_result_text,
    pick_primary_artifact,
)
from app.skills.dynamic import DynamicSkillLoader, LoadedSkill
from app.stores import InMemorySessionStore


MCP_URL = "http://mcp.test/mcp"
LLM_URL = "https://llm.test"
ARTIFACT_URL = "http://minio.test/analysis-content/out/result.png"


def mcp_settings(**overrides) -> Settings:
    defaults = dict(
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        mcp_analysis_enabled=True,
        mcp_analysis_model_base_url=LLM_URL,
        mcp_analysis_model_api_key="test-key",
        mcp_analysis_total_budget_seconds=60,
        mcp_analysis_max_turns=5,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def chat_request(**overrides) -> ChatRequest:
    defaults = dict(
        application_id="app",
        conversation_id="c1",
        message_id="m1",
        question="分析上传文件的数据质量并出图表",
        temp_file_paths=["uploads/a.xlsx"],
        mcp=[McpConfig(mcp_server_url=MCP_URL, connect_type="streamable_http")],
    )
    defaults.update(overrides)
    return ChatRequest(**defaults)


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def combined_transport(
    *,
    llm_script: list[dict],
    llm_seen: list | None = None,
    fail_tool: bool = False,
    sse_tool_result: bool = False,
    mcp_calls: list | None = None,
) -> httpx.MockTransport:
    """One transport serving both the MCP endpoint and the LLM endpoint."""
    seen = llm_seen if llm_seen is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(LLM_URL):
            payload = json.loads(request.content)
            seen.append(payload)
            message = llm_script[min(len(seen) - 1, len(llm_script) - 1)]
            return httpx.Response(200, json={"choices": [{"message": message}]})

        body = json.loads(request.content)
        method = body.get("method")
        if mcp_calls is not None:
            mcp_calls.append((method, request.headers.get("Mcp-Session-Id")))
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0", "id": body.get("id"),
                    "result": {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "serverInfo": {"name": "test-gateway", "version": "1.0"},
                    },
                },
                headers={"Mcp-Session-Id": "sess-123"},
            )
        if method == "notifications/initialized":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": {}})
        if method == "tools/list":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body.get("id"),
                "result": {"tools": [
                    {
                        "name": "analysis_profile",
                        "description": "表结构体检（算任何数字之前）",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"file_path": {"type": "string"}},
                            "required": ["file_path"],
                        },
                    },
                    {
                        "name": "analysis_make_chart",
                        "description": "生成 PNG 图表",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                                "x": {"type": "string"},
                            },
                            "required": ["file_path"],
                        },
                    },
                ]},
            })
        if method == "tools/call":
            if fail_tool:
                return httpx.Response(500, json={"detail": "boom"})
            text = json.dumps(
                {"code": 200, "msg": "图表已生成", "data": ARTIFACT_URL},
                ensure_ascii=False,
            )
            rpc = {
                "jsonrpc": "2.0", "id": body.get("id"),
                "result": {"content": [{"type": "text", "text": text}]},
            }
            if sse_tool_result:
                framed = f"event: message\ndata: {json.dumps(rpc, ensure_ascii=False)}\n\n"
                return httpx.Response(
                    200, content=framed.encode(),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json=rpc)
        return httpx.Response(400, json={"error": "unknown method"})

    return httpx.MockTransport(handler)


# ==================== MCP 会话协议 ====================


@pytest.mark.asyncio
async def test_session_reuses_mcp_session_id_across_calls():
    calls: list = []
    client = McpSessionClient(
        MCP_URL, None, tool_timeout=5, discovery_timeout=5,
        transport=combined_transport(llm_script=[], mcp_calls=calls),
    )
    await client.open()
    tools = await client.list_tools()
    assert [tool.name for tool in tools] == ["analysis_profile", "analysis_make_chart"]
    result = await client.call_tool("analysis_profile", {"file_path": "uploads/a.xlsx"})
    assert result["result"]["content"][0]["type"] == "text"
    await client.aclose()
    # initialize 不带会话头，其后所有请求复用服务端下发的 Mcp-Session-Id
    assert calls[0] == ("initialize", None)
    assert calls[1][0] == "notifications/initialized"
    assert all(session == "sess-123" for _, session in calls[1:])


@pytest.mark.asyncio
async def test_sse_framed_tool_result_is_parsed():
    client = McpSessionClient(
        MCP_URL, None, tool_timeout=5, discovery_timeout=5,
        transport=combined_transport(llm_script=[], sse_tool_result=True),
    )
    await client.open()
    result = await client.call_tool("analysis_make_chart", {"file_path": "uploads/a.xlsx"})
    await client.aclose()
    text = result["result"]["content"][0]["text"]
    assert json.loads(text)["data"] == ARTIFACT_URL


# ==================== LLM 多轮工具循环 ====================


@pytest.mark.asyncio
async def test_runner_drives_multi_turn_tool_loop():
    script = [
        {"tool_calls": [tool_call("call_1", "analysis_profile", {"file_path": "uploads/a.xlsx"})]},
        {"tool_calls": [tool_call("call_2", "analysis_make_chart", {"file_path": "uploads/a.xlsx", "x": "月份"})]},
        {"content": f"数据质量良好。图表：{ARTIFACT_URL}"},
    ]
    llm_seen: list = []
    runner = McpAnalysisRunner(
        mcp_settings(),
        transport=combined_transport(llm_script=script, llm_seen=llm_seen),
    )
    outcome = await runner.run(chat_request())

    assert outcome.answer.startswith("数据质量良好")
    assert outcome.turns_used == 3
    assert [execution.name for execution in outcome.executions] == [
        "analysis_profile", "analysis_make_chart",
    ]
    assert all(
        execution.status == "COMPLETED" for execution in outcome.executions
    )
    assert outcome.artifact_urls == [ARTIFACT_URL]

    # 系统提示注入文件清单与 file_path 契约；tools 转为 OpenAI function 格式
    first = llm_seen[0]
    assert "uploads/a.xlsx" in first["messages"][0]["content"]
    assert "file_path" in first["messages"][0]["content"]
    assert first["tools"][0]["function"]["name"] == "analysis_profile"
    assert first["tools"][0]["function"]["parameters"]["required"] == ["file_path"]
    # 第二轮：上一轮工具结果以 tool 角色回填，且携带 tool_call_id
    second_messages = llm_seen[1]["messages"]
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_call_id"] == "call_1"
    assert ARTIFACT_URL in second_messages[-1]["content"]
    # 最终轮不再携带工具调用的 assistant 消息以 content 收尾
    assert llm_seen[2]["messages"][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_tool_failure_is_classified_and_fed_back():
    script = [
        {"tool_calls": [tool_call("call_1", "analysis_profile", {"file_path": "uploads/a.xlsx"})]},
        {"content": "工具暂不可用，已按现有信息降级说明。"},
    ]
    llm_seen: list = []
    runner = McpAnalysisRunner(
        mcp_settings(),
        transport=combined_transport(
            llm_script=script, llm_seen=llm_seen, fail_tool=True
        ),
    )
    outcome = await runner.run(chat_request())

    execution = outcome.executions[0]
    assert execution.status == "FAILED"
    # HTTP 500 → New_Agent 5 类契约中的 business_logic_error（对齐 _http_failure 映射）
    assert execution.status_code == 422
    assert execution.error_type == "business_logic_error"
    # 失败以结构化 error_type 回填给模型自纠，循环未中断
    fed = json.loads(llm_seen[1]["messages"][-1]["content"])
    assert fed["error_type"] == "business_logic_error"
    assert outcome.answer == "工具暂不可用，已按现有信息降级说明。"


@pytest.mark.asyncio
async def test_turn_limit_forces_final_summary():
    script = [
        {"tool_calls": [tool_call("call_1", "analysis_profile", {"file_path": "uploads/a.xlsx"})]},
        {"tool_calls": [tool_call("call_2", "analysis_profile", {"file_path": "uploads/a.xlsx"})]},
        {"content": "已达轮数上限，基于现有结果总结。"},
    ]
    llm_seen: list = []
    runner = McpAnalysisRunner(
        mcp_settings(mcp_analysis_max_turns=2),
        transport=combined_transport(llm_script=script, llm_seen=llm_seen),
    )
    outcome = await runner.run(chat_request())

    assert outcome.turns_used == 2
    assert outcome.answer.startswith("已达轮数上限")
    assert any("轮数已达上限" in warning for warning in outcome.warnings)
    # 强制总结轮不再携带 tools 参数
    assert "tools" not in llm_seen[2]


@pytest.mark.asyncio
async def test_unknown_tool_call_is_rejected_and_fed_back():
    script = [
        {"tool_calls": [tool_call("call_1", "drop_database", {})]},
        {"content": "已终止非法调用。"},
    ]
    llm_seen: list = []
    runner = McpAnalysisRunner(
        mcp_settings(),
        transport=combined_transport(llm_script=script, llm_seen=llm_seen),
    )
    outcome = await runner.run(chat_request())

    execution = outcome.executions[0]
    assert execution.status == "REJECTED"
    assert execution.error_type == "parameter_error"
    fed = json.loads(llm_seen[1]["messages"][-1]["content"])
    assert "未知工具" in fed["msg"]


def test_tool_result_text_truncates_to_8kib():
    execution = ExtensionExecution(
        name="analysis_profile", kind="MCP_TOOL", status="COMPLETED",
        output={"result": {"content": [{"type": "text", "text": "x" * 20_000}]}},
    )
    assert len(_tool_result_text(execution)) == 8 * 1024


# ==================== orchestrator 触发闸门与响应组装 ====================


def make_orchestrator(settings: Settings) -> DataAnalysisOrchestrator:
    return DataAnalysisOrchestrator(
        settings=settings,
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )


def analysis_request() -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="c1", application_id="app", tenant_id="t", user_id="u",
        original_question="分析上传文件的数据质量", primary_intent=PrimaryIntent.DATA_QUALITY,
    )


def test_dispatch_gate_requires_enabled_and_streamable_mcp():
    agent = make_orchestrator(mcp_settings())
    assert agent._should_dispatch_mcp_analysis(chat_request()) is True
    # temp_file_paths 已降为可选上下文：无上传文件但有 streamable_http MCP 仍触发
    assert agent._should_dispatch_mcp_analysis(chat_request(temp_file_paths=[])) is True
    # connect_type 默认 sse（DataAnalysis_Agent 调度器只认 streamable_http）
    assert agent._should_dispatch_mcp_analysis(
        chat_request(mcp=[McpConfig(mcp_server_url=MCP_URL)])
    ) is False
    # 未携带 MCP 配置
    assert agent._should_dispatch_mcp_analysis(chat_request(mcp=[])) is False
    # 灰度开关关闭
    closed = make_orchestrator(mcp_settings(mcp_analysis_enabled=False))
    assert closed._should_dispatch_mcp_analysis(chat_request()) is False
    assert closed.mcp_analysis_runner is None


class StubRunner:
    def __init__(self, outcome: McpAnalysisOutcome | None = None, error: Exception | None = None):
        self._outcome = outcome
        self._error = error

    async def run(self, chat: ChatRequest) -> McpAnalysisOutcome:
        if self._error is not None:
            raise self._error
        assert self._outcome is not None
        return self._outcome


@pytest.mark.asyncio
async def test_run_mcp_analysis_assembles_response():
    agent = make_orchestrator(mcp_settings())
    report_url = "http://minio.test/analysis-content/out/report.html"
    agent.mcp_analysis_runner = StubRunner(outcome=McpAnalysisOutcome(
        answer=f"分析完成，报告：{report_url}",
        executions=[
            ExtensionExecution(
                name="analysis_profile", kind="MCP_TOOL",
                status="COMPLETED", status_code=200,
            ),
            ExtensionExecution(
                name="analysis_make_report", kind="MCP_TOOL",
                status="FAILED", error="报告生成超时", status_code=504,
                error_type="network_service_error",
            ),
        ],
        artifact_urls=[report_url],
        turns_used=4,
        warnings=["MCP 分析时间预算用尽，回答基于已获得的工具结果"],
    ))
    response = await agent._run_mcp_analysis(
        chat_request(), TrustedIdentity(tenant_id="t", user_id="u"), analysis_request()
    )

    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.DATA_QUALITY
    assert response.answer.startswith("分析完成")
    assert response.result_file_url == report_url
    assert [item.name for item in response.extension_executions] == [
        "analysis_profile", "analysis_make_report",
    ]
    assert response.analysis_process[0].stage == "DETERMINISTIC_ANALYSIS"
    assert response.analysis_process[0].status == "COMPLETED"
    assert response.analysis_process[1].status == "FAILED"
    assert response.reliability is not None
    assert response.reliability.level == "HIGH"
    assert response.reliability.warnings


@pytest.mark.asyncio
async def test_run_mcp_analysis_truncates_long_execution_lists():
    agent = make_orchestrator(mcp_settings())
    executions = [
        ExtensionExecution(
            name="analysis_profile", kind="MCP_TOOL", status="COMPLETED",
            status_code=200,
        )
        for _ in range(15)
    ]
    agent.mcp_analysis_runner = StubRunner(outcome=McpAnalysisOutcome(
        answer="完成", executions=executions, turns_used=15,
    ))
    response = await agent._run_mcp_analysis(
        chat_request(), TrustedIdentity(tenant_id="t", user_id="u"), analysis_request()
    )
    # AgentResponse 契约：extension_executions ≤ 10、analysis_process ≤ 20
    assert len(response.extension_executions) == 10
    assert len(response.analysis_process) == 15
    assert any("仅保留前 10 条明细" in w for w in response.reliability.warnings)


@pytest.mark.asyncio
async def test_run_mcp_analysis_falls_back_on_runner_error():
    agent = make_orchestrator(mcp_settings())
    agent.mcp_analysis_runner = StubRunner(error=RuntimeError("boom"))
    response = await agent._run_mcp_analysis(
        chat_request(), TrustedIdentity(tenant_id="t", user_id="u"), analysis_request()
    )
    assert response.status == "SAFE_FALLBACK"
    assert "RuntimeError" in response.answer


@pytest.mark.asyncio
async def test_run_mcp_analysis_falls_back_when_nothing_usable():
    agent = make_orchestrator(mcp_settings())
    agent.mcp_analysis_runner = StubRunner(outcome=McpAnalysisOutcome(
        answer="", executions=[], warnings=["MCP 服务未发现任何可用工具"],
    ))
    response = await agent._run_mcp_analysis(
        chat_request(), TrustedIdentity(tenant_id="t", user_id="u"), analysis_request()
    )
    assert response.status == "SAFE_FALLBACK"
    assert "未发现任何可用工具" in response.answer


def test_url_extraction_stops_at_literal_escapes_and_cjk_punctuation():
    # 冒烟实测场景：工具返回未反转义 JSON 片段，URL 后紧跟字面 \n 与〔注：…〕
    text = (
        "http://minio.test/out/abc_cleaned.xlsx\\n〔注：清洗脚本仅供审计存档〕\\n"
        "详情见 http://minio.test/out/abc_report.html，请下载。"
    )
    urls = _extract_artifact_urls(text)
    assert urls == [
        "http://minio.test/out/abc_cleaned.xlsx",
        "http://minio.test/out/abc_report.html",
    ]


def test_pick_primary_artifact_prefers_final_report_html():
    # 首个 URL 是中间产物（cleaned.xlsx），不能盲取 artifact_urls[0]
    urls = [
        "http://minio.test/out/abc_cleaned.xlsx",
        "http://minio.test/out/abc_chart.png",
        "http://minio.test/out/abc_report.html",
    ]
    assert pick_primary_artifact(urls) == "http://minio.test/out/abc_report.html"
    # 多个 HTML 时取最后生成的
    assert pick_primary_artifact(
        ["http://m.test/1.html", "http://m.test/2.html"]
    ) == "http://m.test/2.html"


def test_pick_primary_artifact_falls_back_to_last_url():
    assert pick_primary_artifact([]) is None
    assert pick_primary_artifact(
        ["http://m.test/a.png", "http://m.test/b.xlsx"]
    ) == "http://m.test/b.xlsx"


# ==================== SKILL 驱动的通用提示词 ====================


def _skill(slug: str, content: str, code: str = "A_TEST") -> LoadedSkill:
    return LoadedSkill(
        code=code, slug=slug, content=content, sha256="0" * 64, modified_ns=0
    )


def test_system_prompt_injects_skill_content_and_boundary():
    prompt = McpAnalysisRunner._system_prompt(
        chat_request(temp_file_paths=[]),
        [_skill("excel数据分析skill", "八步流程：先 profile 再 clean")],
    )
    assert "## 技能：excel数据分析skill" in prompt
    assert "八步流程：先 profile 再 clean" in prompt
    assert "执行边界" in prompt
    # 无上传文件时不注入“可用文件”段
    assert "可用文件" not in prompt


def test_system_prompt_fallback_is_generic_without_hardcoded_tools():
    prompt = McpAnalysisRunner._system_prompt(chat_request(temp_file_paths=[]), [])
    # 通用兜底：不写死任何具体工具名，也无技能指导段
    assert "analysis_profile" not in prompt
    assert "analysis_make_chart" not in prompt
    assert "技能指导" not in prompt
    assert "通用约束" in prompt


def test_system_prompt_includes_files_when_present():
    prompt = McpAnalysisRunner._system_prompt(
        chat_request(temp_file_paths=["uploads/a.xlsx"]), []
    )
    assert "可用文件" in prompt
    assert "uploads/a.xlsx" in prompt
    assert "file_path" in prompt


def test_system_prompt_concatenates_multiple_skills():
    prompt = McpAnalysisRunner._system_prompt(
        chat_request(temp_file_paths=[]),
        [_skill("skill-one", "第一个技能正文", "A"), _skill("skill-two", "第二个技能正文", "B")],
    )
    assert "## 技能：skill-one" in prompt
    assert "## 技能：skill-two" in prompt
    assert "第一个技能正文" in prompt
    assert "第二个技能正文" in prompt


@pytest.mark.asyncio
async def test_runner_loads_skill_md_into_system_prompt(tmp_path):
    # 造平台目录结构 <root>/<code>/<slug>/SKILL.md，用真实 DynamicSkillLoader 加载
    skill_dir = tmp_path / "A_TEST" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\n---\n# 演示技能\n先调用工具A再调用工具B。",
        encoding="utf-8",
    )
    script = [{"content": "完成。"}]
    llm_seen: list = []
    runner = McpAnalysisRunner(
        mcp_settings(),
        transport=combined_transport(llm_script=script, llm_seen=llm_seen),
        skill_loader=DynamicSkillLoader(tmp_path),
    )
    outcome = await runner.run(
        chat_request(skills=[SkillConfig(code="A_TEST", slug="demo-skill")])
    )
    assert outcome.answer == "完成。"
    system_prompt = llm_seen[0]["messages"][0]["content"]
    assert "## 技能：demo-skill" in system_prompt
    assert "先调用工具A再调用工具B" in system_prompt


@pytest.mark.asyncio
async def test_runner_warns_when_skill_missing(tmp_path):
    # 空 skills_root：SKILL 加载失败记 warning，但不中断，走通用兜底
    script = [{"content": "完成。"}]
    runner = McpAnalysisRunner(
        mcp_settings(),
        transport=combined_transport(llm_script=script),
        skill_loader=DynamicSkillLoader(tmp_path),
    )
    outcome = await runner.run(
        chat_request(skills=[SkillConfig(code="A_NONE", slug="missing")])
    )
    assert outcome.answer == "完成。"
    assert any("未加载到" in w for w in outcome.warnings)


def test_skills_root_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_AGENT_SKILLS_ROOT", str(tmp_path))
    assert Settings(env="test", adapter_mode="mock").skills_root == tmp_path
