"""Model-driven MCP execution for user-uploaded data files.

The generic platform agent registers request-scoped MCP tools before model
execution and lets the model call them for several turns.  DataAnalysis Agent
keeps its V1 semantic/SQL path authoritative, but uses the same execution model
for a narrower case: the current request explicitly carries an uploaded file
and one configured MCP server advertises a file-analysis capability.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.domain.models import ChatRequest, ExtensionExecution, ToolConfig
from app.observability.langfuse_client import trace_generation
from app.services.extension_dispatcher import ExtensionDispatcher
from app.services.progress import emit_progress


logger = logging.getLogger(__name__)

_FUNCTION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_URL = re.compile(r"https?://[^\s\"'<>)，。；、！？：,;]+")
_FILE_TOOL_MARKERS = (
    "excel",
    "spreadsheet",
    "workbook",
    "csv",
    "file analysis",
    "data analysis",
    "analysis_profile",
    "analysis_clean",
    "analysis_scan",
    "表格",
    "文件分析",
    "数据分析",
)
_FILE_ARGUMENT_NAMES = {
    "file_path",
    "file_paths",
    "file",
    "files",
    "path",
    "paths",
    "object_name",
    "object_names",
    "file_url",
    "file_urls",
    "input_file",
    "input_files",
}
_MAX_TOOL_RESULT_CHARS = 12 * 1024


class McpFileAnalysisOutcome(BaseModel):
    applicable: bool = False
    answer: str = ""
    executions: list[ExtensionExecution] = Field(default_factory=list)
    artifact_urls: list[str] = Field(default_factory=list)
    turns_used: int = 0
    warnings: list[str] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(
            self.answer.strip()
            and any(item.status == "COMPLETED" for item in self.executions)
        )


class McpFileAnalysisRunner:
    """Run an uploaded-file request through configured MCP tools."""

    def __init__(
        self,
        settings: Settings,
        dispatcher: ExtensionDispatcher,
        *,
        model_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.dispatcher = dispatcher
        self.model_transport = model_transport

    async def run(self, chat: ChatRequest) -> McpFileAnalysisOutcome:
        if not chat.temp_file_paths or not chat.mcp:
            return McpFileAnalysisOutcome()
        warnings: list[str] = []
        try:
            tools = await self.dispatcher.discover_mcp_tools(
                chat,
                timeout_seconds=(
                    self.settings.mcp_file_analysis_discovery_timeout_seconds
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("MCP file tool discovery failed: %s", type(exc).__name__)
            return McpFileAnalysisOutcome(
                warnings=[f"MCP工具发现失败：{type(exc).__name__}"]
            )

        tools = [tool for tool in tools if self._valid_tool(tool)]
        if not any(self._is_file_analysis_tool(tool) for tool in tools):
            return McpFileAnalysisOutcome(
                warnings=["已配置的MCP未提供文件分析工具"]
            )

        await emit_progress(
            "MCP_ANALYSIS",
            "RUNNING",
            f"已连接平台配置的MCP服务，发现{len(tools)}个可用工具，正在分析上传文件。",
            tools=[tool.name.removeprefix("mcp:") for tool in tools],
        )
        try:
            return await asyncio.wait_for(
                self._tool_loop(chat, tools, warnings),
                timeout=self.settings.mcp_file_analysis_total_budget_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            warnings.append("MCP文件分析超过本轮时间预算")
        except Exception as exc:
            logger.warning("MCP file analysis failed: %s", type(exc).__name__)
            warnings.append(f"MCP文件分析失败：{type(exc).__name__}")
        return McpFileAnalysisOutcome(applicable=True, warnings=warnings)

    async def _tool_loop(
        self,
        chat: ChatRequest,
        tools: list[ToolConfig],
        warnings: list[str],
    ) -> McpFileAnalysisOutcome:
        tool_map = {tool.name.removeprefix("mcp:"): tool for tool in tools}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(chat)},
            {"role": "user", "content": chat.question},
        ]
        executions: list[ExtensionExecution] = []
        artifacts: list[str] = []
        trusted_file_refs = set(chat.temp_file_paths)
        answer = ""
        turns = 0
        for turns in range(1, self.settings.mcp_file_analysis_max_turns + 1):
            message = await self._chat(messages, self._openai_tools(tools))
            calls = message.get("tool_calls") or []
            if not calls:
                answer = str(message.get("content") or "").strip()
                break
            messages.append(message)
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else {}
                function = function if isinstance(function, dict) else {}
                name = str(function.get("name") or "")
                descriptor = tool_map.get(name)
                arguments = self._decode_arguments(function.get("arguments"))
                await emit_progress(
                    "MCP_TOOL_CALL",
                    "RUNNING",
                    f"正在调用MCP工具：{name or '未知工具'}。",
                    tool=name,
                    turn=turns,
                )
                if descriptor is None:
                    execution = ExtensionExecution(
                        name=(name or "unknown_tool")[:100],
                        kind="MCP_TOOL",
                        status="REJECTED",
                        error="模型请求了未注册的MCP工具",
                        error_type="parameter_error",
                    )
                else:
                    prepared, argument_error = self._prepare_arguments(
                        descriptor,
                        arguments,
                        trusted_file_refs=trusted_file_refs,
                        original_file_refs=chat.temp_file_paths,
                    )
                    if argument_error:
                        execution = ExtensionExecution(
                            name=name[:100],
                            kind="MCP_TOOL",
                            status="REJECTED",
                            error=argument_error,
                            error_type="parameter_error",
                        )
                    else:
                        try:
                            execution = await asyncio.wait_for(
                                self.dispatcher.call_discovered_mcp_tool(
                                    chat=chat,
                                    tool=descriptor,
                                    arguments=prepared,
                                ),
                                timeout=self.settings.mcp_file_analysis_tool_timeout_seconds,
                            )
                        except asyncio.CancelledError:
                            raise
                        except TimeoutError:
                            execution = ExtensionExecution(
                                name=name[:100],
                                kind="MCP_TOOL",
                                status="FAILED",
                                error="MCP工具调用超时",
                                status_code=504,
                                error_type="network_service_error",
                            )
                executions.append(execution)
                tool_text = self._execution_text(execution)
                for ref in self._extract_file_references(tool_text):
                    trusted_file_refs.add(ref)
                    if ref.startswith(("http://", "https://")) and ref not in artifacts:
                        artifacts.append(ref)
                await emit_progress(
                    "MCP_TOOL_CALL",
                    "COMPLETED" if execution.status == "COMPLETED" else "FAILED",
                    (
                        f"MCP工具{execution.name}执行完成。"
                        if execution.status == "COMPLETED"
                        else f"MCP工具{execution.name}未成功，正在选择其他可用方式。"
                    ),
                    tool=execution.name,
                    tool_status=execution.status,
                    error_type=execution.error_type,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": tool_text,
                    }
                )

        if not answer and any(item.status == "COMPLETED" for item in executions):
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "请停止调用工具，严格依据已有工具结果给出中文总结；"
                        "保留工具返回的下载链接，不得补造数字。"
                    ),
                }
            )
            final = await self._chat(messages, None)
            answer = str(final.get("content") or "").strip()

        outcome = McpFileAnalysisOutcome(
            applicable=True,
            answer=answer,
            executions=executions,
            artifact_urls=artifacts[:10],
            turns_used=turns,
            warnings=warnings,
        )
        await emit_progress(
            "MCP_ANALYSIS",
            "COMPLETED" if outcome.usable else "DEGRADED",
            (
                f"MCP文件分析完成，共调用{len(executions)}次工具。"
                if outcome.usable
                else "MCP未产出可验证的文件分析结果，正在回退到数据智能体现有分析链路。"
            ),
            turns=turns,
            tool_calls=len(executions),
        )
        return outcome

    def _system_prompt(self, chat: ChatRequest) -> str:
        files = "\n".join(f"- {item}" for item in chat.temp_file_paths)
        return (
            "你是数据分析智能体的文件工具调度器。平台已把用户明确启用的MCP工具"
            "注册到本轮请求。请根据每个工具的名称、说明和JSON Schema自主选择工具，"
            "可以连续调用多个工具完成分析。\n\n"
            f"用户上传的文件引用如下：\n{files}\n\n"
            "约束：调用需要文件参数的工具时使用上述原始引用，或使用前序工具明确返回的"
            "新文件引用；先读取/分析文件再下结论；所有数字必须来自工具结果；工具失败时"
            "可修正参数或选择其他工具；最终用中文直接回答，并原样保留下载链接。用户问题"
            "按UTF-8原文提供，除非原文确实包含替换字符“�”，不得声称存在乱码或编码问题。"
            "只要工具结果已经满足用户问题，就直接给出结果，不得声称用户需求没有完整传达，"
            "也不要要求用户重新说明已经明确提出的需求。"
        )

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        api_key = self.settings.intent_model_api_key
        if api_key is None:
            raise RuntimeError("MCP文件分析模型未配置API Key")
        body: dict[str, Any] = {
            "model": self.settings.intent_model_name,
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
            name="MCP文件分析规划",
            model=body["model"],
            messages=messages,
            tools=tools,
        ) as generation:
            async with httpx.AsyncClient(
                base_url=self.settings.intent_model_base_url.rstrip("/"),
                timeout=self.settings.mcp_file_analysis_model_timeout_seconds,
                transport=self.model_transport,
            ) as client:
                for attempt in range(self.settings.intent_model_max_retries + 1):
                    try:
                        response = await client.post(
                            "/chat/completions", headers=headers, json=body
                        )
                        response.raise_for_status()
                        payload = response.json()
                        choices = payload.get("choices")
                        if not isinstance(choices, list) or not choices:
                            raise ValueError("模型响应缺少choices")
                        message = choices[0].get("message")
                        if not isinstance(message, dict):
                            raise ValueError("模型响应缺少message")
                        generation.set_response(payload)
                        return message
                    except (httpx.TimeoutException, httpx.NetworkError):
                        if attempt >= self.settings.intent_model_max_retries:
                            raise
                        await asyncio.sleep(0.2 * (2**attempt))
        raise RuntimeError("MCP文件分析模型调用失败")

    @staticmethod
    def _valid_tool(tool: ToolConfig) -> bool:
        return bool(
            tool.name.startswith("mcp:")
            and _FUNCTION_NAME.fullmatch(tool.name.removeprefix("mcp:"))
        )

    @staticmethod
    def _is_file_analysis_tool(tool: ToolConfig) -> bool:
        label = f"{tool.name} {tool.description}".casefold()
        if any(marker in label for marker in _FILE_TOOL_MARKERS):
            return True
        schema = tool.input_schema or {}
        properties = schema.get("properties")
        return bool(
            isinstance(properties, dict)
            and any(McpFileAnalysisRunner._is_file_argument(name) for name in properties)
        )

    @staticmethod
    def _openai_tools(tools: list[ToolConfig]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name.removeprefix("mcp:"),
                    "description": tool.description,
                    "parameters": tool.input_schema
                    or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]

    @staticmethod
    def _decode_arguments(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        try:
            decoded = json.loads(str(value or "{}"))
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @classmethod
    def _prepare_arguments(
        cls,
        tool: ToolConfig,
        arguments: dict[str, Any],
        *,
        trusted_file_refs: set[str],
        original_file_refs: list[str],
    ) -> tuple[dict[str, Any], str | None]:
        prepared = dict(arguments)
        schema = tool.input_schema or {}
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        for name in properties:
            if not cls._is_file_argument(name):
                continue
            value = prepared.get(name)
            is_array = cls._expects_array(properties.get(name)) or name.endswith("s")
            if value in (None, "", []):
                if name in required or len(original_file_refs) == 1:
                    prepared[name] = (
                        list(original_file_refs)
                        if is_array
                        else original_file_refs[0]
                    )
                continue
            values = value if isinstance(value, list) else [value]
            if any(str(item) not in trusted_file_refs for item in values):
                return {}, f"MCP工具{name}参数引用了未经本轮文件或前序工具确认的文件"
        return prepared, None

    @staticmethod
    def _is_file_argument(name: str) -> bool:
        normalized = name.casefold()
        return bool(
            normalized in _FILE_ARGUMENT_NAMES
            or ("file" in normalized and any(
                marker in normalized for marker in ("path", "url", "name", "key")
            ))
        )

    @staticmethod
    def _expects_array(schema: Any) -> bool:
        return isinstance(schema, dict) and schema.get("type") == "array"

    @staticmethod
    def _execution_text(execution: ExtensionExecution) -> str:
        if execution.status != "COMPLETED" or execution.output is None:
            return json.dumps(
                {
                    "error_type": execution.error_type or "unknown_error",
                    "msg": execution.error or "工具调用失败",
                },
                ensure_ascii=False,
            )
        value = execution.output
        content = value.get("content")
        if not isinstance(content, list):
            result = value.get("result")
            content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            texts = [
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)[:_MAX_TOOL_RESULT_CHARS]
        return json.dumps(value, ensure_ascii=False, default=str)[:_MAX_TOOL_RESULT_CHARS]

    @staticmethod
    def _extract_file_references(text: str) -> list[str]:
        references = list(_URL.findall(text))
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            value = None

        def visit(item: Any, key: str = "") -> None:
            if isinstance(item, dict):
                for child_key, child in item.items():
                    visit(child, str(child_key))
            elif isinstance(item, list):
                for child in item:
                    visit(child, key)
            elif isinstance(item, str) and (
                McpFileAnalysisRunner._is_file_argument(key)
                or item.startswith(("http://", "https://"))
            ):
                references.append(item)

        visit(value)
        return list(dict.fromkeys(ref.strip() for ref in references if ref.strip()))[:20]


def pick_primary_artifact(urls: list[str]) -> str | None:
    if not urls:
        return None
    for url in reversed(urls):
        if re.search(r"\.(?:html?|xlsx|csv|pdf)(?:\?|$)", url, re.I):
            return url
    return urls[-1]
