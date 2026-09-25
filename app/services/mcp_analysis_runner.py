"""SKILL-driven general-purpose MCP ReAct executor.

对齐 New_Agent DeepAgentRunner 的"模型自主多轮调用工具"模式，但沿用本代码库
风格：httpx 直连 OpenAI 兼容接口 + 自制 streamable_http JSON-RPC 客户端，不引入
langchain 生态（pyproject 依赖仅 httpx + langgraph）。

通用性：本通道不绑定任何具体工具或业务（excel 分析只是其中一种 SKILL）。
- 提示词来源：请求 chat.skills 指定的平台 SKILL.md（DynamicSkillLoader 加载），
  SKILL 正文即工作流指导；无 SKILL 时走通用兜底提示词。
- 工具来源：请求 chat.mcp 的 streamable_http 服务，tools/list 动态发现，
  inputSchema 转 OpenAI function，由模型自主决定调用顺序与参数。
因此新增 MCP 或工具只需在平台侧配置 SKILL 绑定并随请求下发，无需改本文件。

与 extension_dispatcher 的分工：extension_dispatcher 保留"主回答之后的一次性
补充"语义（ENRICH / WEB_SEARCH），不改动；本通道是主执行——请求携带
streamable_http MCP 配置时接管回答。

为什么不能复用 extension_dispatcher：其 OptionalToolSelector.has_satisfiable_inputs
要求工具 required 参数全部存在于固定扩展 payload（question/columns/rows 等），
而多数 MCP 工具的 required 是 file_path/code/(spec, method_ref) 等，交集为空导致
工具在候选阶段被静默过滤；且其参数为静态同名提取、单次调用、30s 超时，无法承载
"读上一步结果 -> 构造下一步参数"的多轮工作流。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.observability.langfuse_client import trace_generation
from app.domain.models import ChatRequest, ExtensionExecution
from app.services.progress import emit_progress
from app.skills.dynamic import DynamicSkillLoader, LoadedSkill

logger = logging.getLogger(__name__)

# 单次工具结果回填给模型的截断上限：5001 工具链的 profile/verify 输出可达数十
# KB，不截断会在多轮循环中迅速撑爆上下文窗口。
_TOOL_RESULT_MAX_CHARS = 8 * 1024
# OpenAI function calling 对工具名的约束；不合规的 MCP 工具直接跳过并告警。
_OPENAI_FUNCTION_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# 工具返回文本常为未反转义的 JSON 片段，URL 后紧跟字面 \n、中文括号/标点；
# 排除集需覆盖 \ 与全角标点，否则提取出的链接会粘连垃圾字符（冒烟实测教训）。
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>)】〔\\，。；、！？：,;]+")


class McpToolDescriptor(BaseModel):
    """One tool discovered from a streamable_http MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    server_url: str


class McpAnalysisOutcome(BaseModel):
    answer: str = ""
    executions: list[ExtensionExecution] = Field(default_factory=list)
    artifact_urls: list[str] = Field(default_factory=list)
    turns_used: int = 0
    warnings: list[str] = Field(default_factory=list)


def _parse_rpc_body(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON-RPC body from either application/json or SSE framing.

    与 extension_dispatcher._bounded_output 同构：5001 网关返回 application/json，
    SSE 分支保留以兼容其它 MCP 服务实现。
    """
    if len(response.content) > 1024 * 1024:
        raise ValueError("MCP response exceeds 1 MiB")
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        data_lines = [
            line[5:].strip() for line in response.text.splitlines()
            if line.startswith("data:") and line[5:].strip()
        ]
        if not data_lines:
            raise ValueError("MCP response contains no data event")
        value = json.loads(data_lines[-1])
    else:
        value = response.json()
    if not isinstance(value, dict):
        return {"value": value}
    return value


def _classify_exception(exc: Exception) -> tuple[int, str]:
    """Map transport/HTTP failures onto the New_Agent 5-class error contract."""
    if isinstance(exc, httpx.TimeoutException):
        return 504, "network_service_error"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 400:
            return 400, "parameter_error"
        if status in {401, 403}:
            return 401, "auth_error"
        if status == 500:
            return 422, "business_logic_error"
        if status in {404, 408, 599} or status > 500:
            return 503, "network_service_error"
        if 402 <= status < 500:
            return 422, "business_logic_error"
        return 500, "unknown_error"
    if isinstance(exc, (httpx.RequestError, ValueError, json.JSONDecodeError)):
        return 503, "network_service_error"
    return 500, "unknown_error"


class McpSessionClient:
    """One streamable_http MCP server: initialize once, call tools many times.

    与 extension_dispatcher._call_streamable_mcp 的差异：那边每次调用都新建连接
    并重新 initialize；这里整个分析会话复用一个连接与 Mcp-Session-Id，工具调用
    超时按 mcp_analysis_tool_timeout_seconds（默认 180s，对齐 New_Agent 已验证值，
    远大于 extension_dispatcher 的 30s）。
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None,
        *,
        tool_timeout: float,
        discovery_timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self._tool_timeout = tool_timeout
        self._discovery_timeout = discovery_timeout
        self._headers = {
            "Accept": "application/json, text/event-stream", **(headers or {})
        }
        self._client = httpx.AsyncClient(timeout=tool_timeout, transport=transport)
        self._session_id: str | None = None

    def _request_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def open(self) -> None:
        response = await self._client.post(
            self.url,
            headers=self._headers,
            timeout=self._discovery_timeout,
            json={
                "jsonrpc": "2.0", "id": str(uuid4()), "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {
                        "name": "youo-data-analysis-agent", "version": "0.3.0"
                    },
                },
            },
        )
        response.raise_for_status()
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id
        await self._client.post(
            self.url,
            headers=self._request_headers(),
            timeout=self._discovery_timeout,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

    async def list_tools(self) -> list[McpToolDescriptor]:
        response = await self._client.post(
            self.url,
            headers=self._request_headers(),
            timeout=self._discovery_timeout,
            json={
                "jsonrpc": "2.0", "id": str(uuid4()),
                "method": "tools/list", "params": {},
            },
        )
        response.raise_for_status()
        body = _parse_rpc_body(response)
        raw_tools = body.get("result", {}).get("tools", [])
        descriptors: list[McpToolDescriptor] = []
        for item in raw_tools if isinstance(raw_tools, list) else []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"])
            if not _OPENAI_FUNCTION_NAME.match(name):
                logger.warning("跳过不合规的 MCP 工具名: %s", name)
                continue
            descriptors.append(McpToolDescriptor(
                name=name,
                description=str(item.get("description") or "")[:4000],
                input_schema=item.get("inputSchema") or {},
                server_url=self.url,
            ))
        return descriptors

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            self.url,
            headers=self._request_headers(),
            json={
                "jsonrpc": "2.0", "id": str(uuid4()), "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        response.raise_for_status()
        return _parse_rpc_body(response)

    async def aclose(self) -> None:
        await self._client.aclose()


class McpAnalysisRunner:
    """Drive one request through an autonomous, SKILL-guided MCP tool loop.

    提示词由 chat.skills 指定的 SKILL.md 动态提供，工具由 chat.mcp 动态发现；
    不绑定任何具体业务（excel 分析只是一种 SKILL）。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        skill_loader: DynamicSkillLoader | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.skill_loader = skill_loader

    async def run(self, chat: ChatRequest) -> McpAnalysisOutcome:
        warnings: list[str] = []
        servers = [
            server for server in (chat.mcp or [])
            if server.connect_type == "streamable_http"
        ]
        if not servers:
            warnings.append("请求未携带 streamable_http MCP 服务，MCP 分析通道不可用")
            return McpAnalysisOutcome(warnings=warnings)

        clients: dict[str, McpSessionClient] = {}
        tools: list[McpToolDescriptor] = []
        try:
            for server in servers:
                client = McpSessionClient(
                    server.mcp_server_url,
                    server.headers,
                    tool_timeout=self.settings.mcp_analysis_tool_timeout_seconds,
                    discovery_timeout=self.settings.mcp_analysis_discovery_timeout_seconds,
                    transport=self.transport,
                )
                try:
                    await client.open()
                    discovered = await client.list_tools()
                except Exception as exc:
                    logger.warning(
                        "MCP 分析服务 %s 不可用: %s",
                        server.mcp_server_url, type(exc).__name__,
                    )
                    warnings.append(
                        f"MCP 服务 {server.mcp_server_url} 连接失败：{type(exc).__name__}"
                    )
                    await client.aclose()
                    continue
                clients[server.mcp_server_url] = client
                known = {tool.name for tool in tools}
                tools.extend(tool for tool in discovered if tool.name not in known)
            if not tools:
                warnings.append("MCP 服务未发现任何可用工具")
                return McpAnalysisOutcome(warnings=warnings)
            loaded_skills = self._load_skills(chat, warnings)
            await emit_progress(
                "MCP_ANALYSIS", "RUNNING",
                f"已连接 MCP 服务，发现 {len(tools)} 个工具、加载 {len(loaded_skills)} 个 SKILL。",
                tools=[tool.name for tool in tools],
                skills=[skill.slug for skill in loaded_skills],
            )
            return await self._loop(chat, tools, clients, warnings, loaded_skills)
        finally:
            for client in clients.values():
                await client.aclose()

    def _load_skills(
        self, chat: ChatRequest, warnings: list[str]
    ) -> list[LoadedSkill]:
        """按请求 chat.skills 加载平台 SKILL.md 作为提示词来源。

        skill_loader 未配置、请求未带 skills、或某个 SKILL 加载失败时跳过并记入
        warnings，走通用兜底提示词——不因 SKILL 缺失而中断 MCP 执行。
        """
        if self.skill_loader is None or not chat.skills:
            return []
        loaded: list[LoadedSkill] = []
        for config in chat.skills:
            skill = self.skill_loader.load(config)
            if skill is None:
                warnings.append(f"SKILL {config.code}/{config.slug} 未加载到，已跳过")
                continue
            loaded.append(skill)
        return loaded

    async def _loop(
        self,
        chat: ChatRequest,
        tools: list[McpToolDescriptor],
        clients: dict[str, McpSessionClient],
        warnings: list[str],
        loaded_skills: list[LoadedSkill] | None = None,
    ) -> McpAnalysisOutcome:
        tool_map = {tool.name: tool for tool in tools}
        openai_tools = self._to_openai_tools(tools)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._system_prompt(chat, loaded_skills or []),
            },
            {"role": "user", "content": chat.question},
        ]
        executions: list[ExtensionExecution] = []
        artifact_urls: list[str] = []
        answer = ""
        turns = 0
        budget_exhausted = False
        deadline = time.monotonic() + self.settings.mcp_analysis_total_budget_seconds
        while turns < self.settings.mcp_analysis_max_turns:
            turns += 1
            if time.monotonic() >= deadline:
                budget_exhausted = True
                break
            message = await self._chat(messages, openai_tools)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                answer = str(message.get("content") or "").strip()
                break
            messages.append(message)
            for call in tool_calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                descriptor = tool_map.get(name)
                await emit_progress(
                    "MCP_TOOL_CALL", "RUNNING",
                    f"第 {turns} 轮调用 MCP 工具：{name}", tool=name, turn=turns,
                )
                if descriptor is None:
                    execution = ExtensionExecution(
                        name=name[:100] or "unknown_tool", kind="MCP_TOOL",
                        status="REJECTED",
                        error=f"未知工具 {name}，仅可调用已发现的工具",
                        error_type="parameter_error",
                    )
                    content = json.dumps(
                        {"error_type": "parameter_error", "msg": execution.error},
                        ensure_ascii=False,
                    )
                else:
                    execution = await self._execute_tool(
                        clients[descriptor.server_url], descriptor, arguments
                    )
                    content = _tool_result_text(execution)
                    for url in _extract_artifact_urls(content):
                        if url not in artifact_urls:
                            artifact_urls.append(url)
                executions.append(execution)
                await emit_progress(
                    "MCP_TOOL_CALL",
                    "COMPLETED" if execution.status == "COMPLETED" else "FAILED",
                    f"MCP 工具 {execution.name} 执行{execution.status}。",
                    tool=execution.name, tool_status=execution.status,
                    error_type=execution.error_type,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": content,
                })
        if not answer:
            reason = (
                "时间预算已用尽" if budget_exhausted else "工具调用轮数已达上限"
            )
            warnings.append(f"MCP 分析{reason}，回答基于已获得的工具结果")
            messages.append({
                "role": "user",
                "content": (
                    f"{reason}，请立即基于已获得的工具结果输出最终分析结论"
                    "（含产物下载链接），不要再调用任何工具。"
                ),
            })
            final = await self._chat(messages, tools=None)
            answer = str(final.get("content") or "").strip()
        await emit_progress(
            "MCP_ANALYSIS", "COMPLETED",
            f"MCP 分析完成：{turns} 轮对话、{len(executions)} 次工具调用。",
            turns=turns, tool_calls=len(executions),
        )
        return McpAnalysisOutcome(
            answer=answer,
            executions=executions,
            artifact_urls=artifact_urls,
            turns_used=turns,
            warnings=warnings,
        )

    async def _execute_tool(
        self,
        client: McpSessionClient,
        descriptor: McpToolDescriptor,
        arguments: dict[str, Any],
    ) -> ExtensionExecution:
        name = descriptor.name[:100]
        try:
            body = await client.call_tool(descriptor.name, arguments)
        except Exception as exc:
            status_code, error_type = _classify_exception(exc)
            return ExtensionExecution(
                name=name, kind="MCP_TOOL", status="FAILED",
                error=f"MCP工具调用失败：{type(exc).__name__}"[:500],
                status_code=status_code, error_type=error_type,
            )
        if "error" in body:
            rpc_error = body.get("error") if isinstance(body.get("error"), dict) else {}
            message = str(rpc_error.get("message") or "MCP 服务返回协议错误")[:500]
            return ExtensionExecution(
                name=name, kind="MCP_TOOL", status="FAILED", output=body,
                error=message, status_code=502, error_type="business_logic_error",
            )
        return ExtensionExecution(
            name=name, kind="MCP_TOOL", status="COMPLETED",
            output=body, status_code=200,
        )

    @staticmethod
    def _to_openai_tools(tools: list[McpToolDescriptor]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema
                    or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]

    @staticmethod
    def _system_prompt(chat: ChatRequest, loaded_skills: list[LoadedSkill]) -> str:
        """SKILL 驱动的系统提示词：技能指导来自平台 SKILL.md，无 SKILL 时通用兜底。

        不写死任何具体工具名或业务工作流——工作流指导由 SKILL.md 正文承载
        （如 excel 分析的八步流程），工具能力由各 MCP 工具 description 承载，
        模型据此自主规划调用顺序。
        """
        parts: list[str] = [
            "你是通过 MCP 工具完成任务的智能体：依据各工具的 description 自主"
            "规划调用顺序和参数，逐步完成用户请求。"
        ]
        if loaded_skills:
            skill_sections = "\n\n".join(
                f"## 技能：{skill.slug}\n{skill.content}" for skill in loaded_skills
            )
            parts.append(f"# 技能指导\n{skill_sections}")
            boundary = loaded_skills[0].tool_context().get("execution_boundary")
            if boundary:
                parts.append(f"# 执行边界\n{boundary}")
        if chat.temp_file_paths:
            files = "\n".join(f"- {path}" for path in chat.temp_file_paths)
            parts.append(
                "# 可用文件\n以下为本次会话已上传的文件（MinIO 对象路径）；"
                f"当工具需要 file_path 参数时，把对应路径原样传入：\n{files}"
            )
        parts.append(
            "# 通用约束\n"
            "- 所有数字必须逐字引用工具返回的真实值，禁止编造或凭印象估算；\n"
            "- 工具返回的报告/图表/文件下载链接必须原样放在最终回答里展示给用户；\n"
            "- 工具调用失败时，根据返回的 error_type 调整参数重试或改变策略，不要空转；\n"
            "- 完成后用中文输出结构化结论。"
        )
        return "\n\n".join(parts)

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        api_key = (
            self.settings.mcp_analysis_model_api_key
            or self.settings.intent_model_api_key
        )
        if api_key is None:
            raise RuntimeError(
                "未配置 MCP 分析模型 API Key（mcp_analysis_model_api_key / intent_model_api_key）"
            )
        body: dict[str, Any] = {
            "model": self.settings.mcp_analysis_model_name,
            "messages": messages,
            "temperature": 0,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        with trace_generation(
            name="MCP分析规划",
            model=body.get("model"),
            messages=body.get("messages"),
            tools=body.get("tools"),
        ) as gen:
            async with httpx.AsyncClient(
                base_url=self.settings.mcp_analysis_model_base_url.rstrip("/"),
                timeout=self.settings.mcp_analysis_model_timeout_seconds,
                transport=self.transport,
            ) as client:
                for attempt in range(3):
                    try:
                        response = await client.post(
                            "/chat/completions", headers=headers, json=body
                        )
                        response.raise_for_status()
                        payload = response.json()
                        choices = payload.get("choices")
                        if not isinstance(choices, list) or not choices:
                            raise ValueError("模型响应缺少 choices")
                        message = choices[0].get("message")
                        if not isinstance(message, dict):
                            raise ValueError("模型响应缺少 message")
                        gen.set_response(payload)
                        return message
                    except (httpx.TimeoutException, httpx.NetworkError) as exc:
                        if attempt >= 2:
                            raise
                        logger.warning(
                            "MCP 分析模型调用超时/网络错误（第 %s 次）: %s",
                            attempt + 1, type(exc).__name__,
                        )
                    except httpx.HTTPStatusError as exc:
                        status = exc.response.status_code
                        if status != 429 and status < 500:
                            raise
                        if attempt >= 2:
                            raise
                        logger.warning(
                            "MCP 分析模型返回 %s（第 %s 次），准备重试", status, attempt + 1
                        )
                    await asyncio.sleep(0.2 * (2 ** attempt))
            raise RuntimeError("MCP 分析模型调用失败")


def _tool_result_text(execution: ExtensionExecution) -> str:
    """Serialize one tool outcome for the next model turn, bounded to 8 KiB.

    失败时回填结构化 error_type/msg（New_Agent 5 类契约），让模型自纠而非中断。
    """
    if execution.status != "COMPLETED" or not execution.output:
        return json.dumps(
            {
                "error_type": execution.error_type or "unknown_error",
                "msg": execution.error or "工具调用失败",
            },
            ensure_ascii=False,
        )[:_TOOL_RESULT_MAX_CHARS]
    body = execution.output
    result = body.get("result") if isinstance(body, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        texts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if texts:
            return "\n".join(texts)[:_TOOL_RESULT_MAX_CHARS]
    return json.dumps(body, ensure_ascii=False, default=str)[:_TOOL_RESULT_MAX_CHARS]


def _extract_artifact_urls(text: str) -> list[str]:
    """Collect download URLs emitted by the tool chain (报告/图表产物链接)."""
    return _URL_PATTERN.findall(text)[:5]


def pick_primary_artifact(urls: list[str]) -> str | None:
    """从产物链接中选出主交付物：报告 HTML 优先，否则取最后产出。

    工具链会先后产出中间产物（如 cleaned.xlsx，明确标注仅供审计存档）
    与最终报告（*_report.html），首个 URL 往往是中间产物，不能直接
    作为 result_file_url；多个 HTML 时取最后生成的（更接近最终交付）。
    """
    if not urls:
        return None
    for url in reversed(urls):
        if url.endswith(".html"):
            return url
    return urls[-1]
