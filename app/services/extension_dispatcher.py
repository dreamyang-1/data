"""Runtime for HTTP tools, MCP tools and skills supplied per request.

The request itself is the source of truth: no second allowlist is maintained by
the data agent. Skills enrich a matching HTTP/MCP capability with instructions;
when a selector is configured, every supplied capability is optional. Core deterministic results remain
authoritative; extension output is returned as separately labelled supplementary
output.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx

from app.config import Settings
from app.domain.models import ChatRequest, ExtensionExecution, ToolConfig
from app.skills.dynamic import DynamicSkillLoader, LoadedSkill
from app.services.tool_selector import OptionalToolSelector


class ExtensionDispatcher:
    def __init__(
        self,
        *,
        max_parallel: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        skill_loader: DynamicSkillLoader | None = None,
        tool_selector: OptionalToolSelector | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.max_parallel = max_parallel
        self.transport = transport
        self.skill_loader = skill_loader
        self.tool_selector = tool_selector
        self.builtin_web_search_tool = self._build_builtin_web_search_tool(settings)

    @staticmethod
    def _build_builtin_web_search_tool(settings: Settings | None) -> ToolConfig | None:
        if settings is None or settings.bocha_api_key is None:
            return None
        return ToolConfig(
            name="web_search_data",
            description="联网搜索工具，用于查询互联网上的公开信息",
            url=settings.bocha_search_url,
            http_method="post",
            headers={
                "Authorization": (
                    "Bearer " + settings.bocha_api_key.get_secret_value()
                ),
                "Content-Type": "application/json",
            },
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer"},
                    "summary": {"type": "boolean"},
                },
            },
            timeout=settings.bocha_search_timeout_seconds,
        )

    @staticmethod
    def _bound_slugs(chat: ChatRequest, intent: str, builtin_skill: str | None) -> list[str]:
        # Match New_Agent request semantics: all caller-supplied configurations
        # are available in this request. ``code`` and ``authority`` are platform
        # metadata; the data agent does not apply another authorization layer.
        return [item.slug for item in chat.skills]

    async def execute(
        self,
        *,
        chat: ChatRequest,
        intent: str,
        builtin_skill: str | None,
        payload: dict[str, Any],
        required_capability: str | None = None,
    ) -> list[ExtensionExecution]:
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")
        if len(encoded_payload) > 1024 * 1024:
            return [ExtensionExecution(
                name="extension_dispatch", kind="HTTP_TOOL", status="REJECTED",
                error="扩展工具输入超过1 MiB安全上限",
            )]
        payload = json.loads(encoded_payload)
        runtime_tools = list(chat.tools)
        if (
            chat.web_search
            and self.builtin_web_search_tool is not None
            and not any(
                item.name == self.builtin_web_search_tool.name
                for item in runtime_tools
            )
        ):
            runtime_tools.append(self.builtin_web_search_tool)
        tool_map = {item.name: item for item in runtime_tools}
        skill_map = {item.slug: item for item in chat.skills}
        loaded_skills: dict[str, LoadedSkill] = {}
        if self.skill_loader is not None:
            for slug, config in skill_map.items():
                loaded = self.skill_loader.load(config)
                if loaded is not None:
                    loaded_skills[slug] = loaded
        autonomous = self.tool_selector is not None
        mcp_tools = (
            await self._discover_mcp_tools(chat)
            if autonomous or required_capability is not None
            else []
        )
        candidates = runtime_tools + mcp_tools
        descriptions = {
            name: self._capability_description(tool_map.get(name), loaded)
            for name, loaded in loaded_skills.items()
        }
        if required_capability == "WEB_SEARCH":
            slugs = [
                tool.name
                for tool in candidates
                if OptionalToolSelector.is_safe_candidate(tool)
                and OptionalToolSelector.has_satisfiable_inputs(tool, payload)
                and self._is_web_search_capability(
                    tool,
                    loaded_skills.get(tool.name),
                )
            ][:1]
        elif autonomous:
            slugs = await self.tool_selector.select(
                question=chat.question,
                intent=intent,
                tools=candidates,
                available_arguments=payload,
                capability_descriptions=descriptions,
            )
        else:
            # Compatibility for explicitly constructed dispatchers: without a
            # selector, skills retain their legacy explicit-binding semantics.
            slugs = self._bound_slugs(chat, intent, builtin_skill)
        if not slugs:
            return []
        constrained_arguments = autonomous or required_capability is not None
        mcp_tool_map = {item.name: item for item in mcp_tools}
        selected_http = [(slug, tool_map[slug]) for slug in slugs if slug in tool_map]
        selected_mcp = [
            slug.removeprefix("mcp:") for slug in slugs
            if slug.startswith("mcp:")
            and (
                not constrained_arguments
                or slug in mcp_tool_map
            )
        ]
        if len(selected_http) + len(selected_mcp) > self.max_parallel:
            return [ExtensionExecution(
                name="extension_dispatch",
                kind="HTTP_TOOL",
                status="REJECTED",
                error="本轮扩展调用数量超过安全上限",
            )]

        import asyncio

        calls = [
            self._call_http(
                name,
                config,
                self._payload_with_skill(
                    self._tool_arguments(config, payload)
                    if constrained_arguments else payload,
                    loaded_skills.get(name),
                ),
                loaded_skills.get(name),
            )
            for name, config in selected_http
        ]
        for tool_name in selected_mcp:
            if not chat.mcp:
                calls.append(self._missing_mcp(tool_name))
            else:
                # A bound MCP tool is attempted on configured servers in order;
                # the first successful result wins.
                skill_slug = f"mcp:{tool_name}"
                loaded = loaded_skills.get(skill_slug)
                discovered_tool = mcp_tool_map.get(skill_slug)
                calls.append(
                    self._call_mcp_servers(
                        tool_name,
                        chat,
                        self._payload_with_skill(
                            self._tool_arguments(
                                mcp_tool_map[f"mcp:{tool_name}"], payload
                            ) if constrained_arguments else payload,
                            loaded,
                        ),
                        loaded,
                        is_web_search=self._is_web_search_capability(
                            discovered_tool,
                            loaded,
                            fallback_name=tool_name,
                        ),
                    )
                )
        return list(await asyncio.gather(*calls)) if calls else []

    @staticmethod
    def _is_web_search_capability(
        tool: ToolConfig | None,
        skill: LoadedSkill | None,
        *,
        fallback_name: str = "",
    ) -> bool:
        """Recognize the existing New_Agent web-search contract, including MCP.

        A generic ``search`` capability is intentionally insufficient: it may be
        an internal knowledge-base or database search.  The request must expose
        an explicitly internet/web-labelled read-only capability.
        """
        tool_name = tool.name if tool is not None else fallback_name
        name = tool_name.removeprefix("mcp:").casefold()
        if name in {"web_search_data", "web_search", "internet_search", "联网搜索"}:
            return True
        label = f"{tool_name} {tool.description if tool is not None else ''}"
        if skill is not None:
            label += f" {skill.content}"
        normalized = " ".join(label.casefold().split())
        return any(
            marker in normalized
            for marker in (
                "web search",
                "internet search",
                "search the web",
                "联网搜索",
                "互联网搜索",
                "网络公开信息",
            )
        )

    @staticmethod
    def _capability_description(
        tool: ToolConfig | None, skill: LoadedSkill
    ) -> str:
        prefix = tool.description if tool is not None else ""
        return f"{prefix}\nSkill instructions:\n{skill.content}".strip()

    async def _discover_mcp_tools(self, chat: ChatRequest) -> list[ToolConfig]:
        import asyncio

        discovered = await asyncio.gather(*[
            self._list_mcp_server(server.mcp_server_url, server.headers)
            if server.connect_type == "streamable_http" else self._empty_mcp_list()
            for server in chat.mcp
        ])
        unique: dict[str, ToolConfig] = {}
        for tools in discovered:
            for tool in tools:
                unique.setdefault(tool.name, tool)
        return list(unique.values())

    @staticmethod
    async def _empty_mcp_list() -> list[ToolConfig]:
        return []

    async def _list_mcp_server(
        self, url: str, headers: dict[str, str] | None
    ) -> list[ToolConfig]:
        request_headers = {
            "Accept": "application/json, text/event-stream", **(headers or {})
        }
        try:
            async with httpx.AsyncClient(timeout=15, transport=self.transport) as client:
                initialized = await client.post(
                    url,
                    headers=request_headers,
                    json={
                        "jsonrpc": "2.0", "id": str(uuid4()), "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26", "capabilities": {},
                            "clientInfo": {
                                "name": "youo-data-analysis-agent", "version": "0.2.0"
                            },
                        },
                    },
                )
                initialized.raise_for_status()
                session_id = initialized.headers.get("Mcp-Session-Id")
                if session_id:
                    request_headers["Mcp-Session-Id"] = session_id
                await client.post(
                    url, headers=request_headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                listed = await client.post(
                    url, headers=request_headers,
                    json={
                        "jsonrpc": "2.0", "id": str(uuid4()),
                        "method": "tools/list", "params": {},
                    },
                )
                listed.raise_for_status()
                value = self._bounded_output(listed)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "MCP tool discovery unavailable: %s", type(exc).__name__
            )
            return []
        raw_tools = value.get("result", {}).get("tools", [])
        result: list[ToolConfig] = []
        for item in raw_tools if isinstance(raw_tools, list) else []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            try:
                result.append(ToolConfig(
                    name=f"mcp:{item['name']}",
                    description=str(item.get("description") or ""),
                    url=url,
                    http_method="post",
                    inputSchema=item.get("inputSchema") or {},
                ))
            except ValueError:
                continue
        return result

    @staticmethod
    def _tool_arguments(config: ToolConfig, payload: dict[str, Any]) -> dict[str, Any]:
        schema = config.input_schema or {}
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return payload
        aliases = {
            "query": "question",
            "business_domain_id": "business_domain_ids",
        }
        arguments: dict[str, Any] = {}
        for name in properties:
            source = aliases.get(name, name)
            if source not in payload or payload[source] is None:
                continue
            value = payload[source]
            if name == "business_domain_id" and isinstance(value, list):
                if len(value) != 1:
                    continue
                value = value[0]
            arguments[name] = value
        return arguments

    @staticmethod
    def _payload_with_skill(
        payload: dict[str, Any], skill: LoadedSkill | None
    ) -> dict[str, Any]:
        if skill is None:
            return payload
        value = dict(payload)
        value["skill_context"] = skill.tool_context()
        return value

    async def _call_http(
        self,
        name: str,
        config: ToolConfig,
        payload: dict[str, Any],
        skill: LoadedSkill | None = None,
    ) -> ExtensionExecution:
        is_web_search = self._is_web_search_capability(config, skill)
        try:
            async with httpx.AsyncClient(
                timeout=config.timeout, transport=self.transport
            ) as client:
                if config.http_method == "get":
                    response = await client.get(config.url, params=payload, headers=config.headers)
                else:
                    response = await client.post(config.url, json=payload, headers=config.headers)
                if (
                    is_web_search and response.status_code != 200
                ):
                    return self._http_failure(
                        name, response.status_code, web_search=True
                    )
                response.raise_for_status()
                value = self._bounded_output(response)
                if skill is not None:
                    value["_skill"] = skill.audit_context()
                failure = (
                    self._application_failure(value)
                    if is_web_search or bool(value.get("error_type"))
                    else None
                )
                if failure is not None:
                    status_code, error_type = failure
                    message = str(
                        value.get("msg")
                        or value.get("message")
                        or f"HTTP工具返回失败状态：{status_code}"
                    )[:500]
                    return ExtensionExecution(
                        name=name,
                        kind="HTTP_TOOL",
                        status="FAILED",
                        output=value,
                        error=message,
                        status_code=status_code,
                        error_type=error_type,
                    )
            return ExtensionExecution(
                name=name,
                kind="HTTP_TOOL",
                status="COMPLETED",
                output=value,
                status_code=200,
            )
        except httpx.HTTPStatusError as exc:
            return self._http_failure(
                name, exc.response.status_code, web_search=is_web_search
            )
        except (httpx.TimeoutException, httpx.RequestError):
            return self._transport_failure(name, web_search=is_web_search)
        except (ValueError, json.JSONDecodeError):
            return self._transport_failure(
                name,
                "联网搜索响应解析失败" if is_web_search else "HTTP工具响应解析失败",
                web_search=is_web_search,
            )
        except Exception as exc:
            return ExtensionExecution(
                name=name, kind="HTTP_TOOL", status="FAILED",
                error=f"HTTP工具调用失败：{type(exc).__name__}",
                status_code=500,
                error_type="unknown_error",
            )

    @staticmethod
    def _application_failure(value: dict[str, Any]) -> tuple[int, str] | None:
        """Interpret the application-level status used by New_Agent tools."""
        raw_error_type = value.get("error_type")
        if raw_error_type:
            try:
                status_code = int(value.get("code", 500))
            except (TypeError, ValueError):
                status_code = 500
            return status_code, str(raw_error_type)[:100]
        if "code" not in value:
            return None
        try:
            status_code = int(value.get("code", 500))
        except (TypeError, ValueError):
            status_code = 500
        if status_code == 200:
            return None
        return status_code, "unknown_error"

    @staticmethod
    def _http_failure(
        name: str, http_status: int, *, web_search: bool = False
    ) -> ExtensionExecution:
        if http_status == 400:
            status_code, error_type = 400, "parameter_error"
        elif http_status in {401, 403}:
            status_code, error_type = 401, "auth_error"
        elif http_status in {404, 408, 599} or http_status > 500:
            status_code, error_type = 503, "network_service_error"
        elif http_status == 500:
            status_code, error_type = 422, "business_logic_error"
        elif 402 <= http_status < 500:
            status_code, error_type = 422, "business_logic_error"
        else:
            status_code, error_type = 500, "unknown_error"
        label = "联网搜索" if web_search else "HTTP工具"
        message = f"{label}请求失败，HTTP {http_status}"
        return ExtensionExecution(
            name=name,
            kind="HTTP_TOOL",
            status="FAILED",
            output={
                "code": status_code,
                "error_type": error_type,
                "msg": message,
                "data": {"result": [], "value": []},
            },
            error=message,
            status_code=status_code,
            error_type=error_type,
        )

    @staticmethod
    def _transport_failure(
        name: str,
        message: str | None = None,
        *,
        web_search: bool = False,
    ) -> ExtensionExecution:
        if message is None:
            message = "联网搜索网络请求失败" if web_search else "HTTP工具网络请求失败"
        return ExtensionExecution(
            name=name,
            kind="HTTP_TOOL",
            status="FAILED",
            output={
                "code": 503,
                "error_type": "network_service_error",
                "msg": message,
                "data": {"result": [], "value": []},
            },
            error=message,
            status_code=503,
            error_type="network_service_error",
        )

    async def _missing_mcp(self, tool_name: str) -> ExtensionExecution:
        return ExtensionExecution(
            name=tool_name, kind="MCP_TOOL", status="REJECTED",
            error="未配置MCP服务",
        )

    async def _call_mcp_servers(
        self,
        tool_name: str,
        chat: ChatRequest,
        payload: dict[str, Any],
        skill: LoadedSkill | None = None,
        *,
        is_web_search: bool = False,
    ) -> ExtensionExecution:
        last_error = "MCP工具不可用"
        for server in chat.mcp:
            if server.connect_type != "streamable_http":
                last_error = "当前数据智能体仅支持streamable_http MCP调用"
                continue
            try:
                value = await self._call_streamable_mcp(
                    server.mcp_server_url, server.headers, tool_name, payload
                )
                if skill is not None:
                    value["_skill"] = skill.audit_context()
                if is_web_search:
                    failure = self._mcp_application_failure(value)
                    if failure is not None:
                        status_code, error_type = failure
                        return ExtensionExecution(
                            name=tool_name,
                            kind="MCP_TOOL",
                            status="FAILED",
                            output=value,
                            error=f"MCP联网搜索返回失败状态：{status_code}",
                            status_code=status_code,
                            error_type=error_type,
                        )
                return ExtensionExecution(
                    name=tool_name,
                    kind="MCP_TOOL",
                    status="COMPLETED",
                    output=value,
                    status_code=200,
                )
            except Exception as exc:
                last_error = f"MCP工具调用失败：{type(exc).__name__}"
        return ExtensionExecution(
            name=tool_name, kind="MCP_TOOL", status="FAILED", error=last_error
        )

    @classmethod
    def _mcp_application_failure(
        cls, value: dict[str, Any]
    ) -> tuple[int, str] | None:
        direct = cls._application_failure(value)
        if direct is not None:
            return direct
        result = value.get("result")
        content = result.get("content") if isinstance(result, dict) else value.get("content")
        if not isinstance(content, list):
            return None
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                failure = cls._application_failure(decoded)
                if failure is not None:
                    return failure
        return None

    async def _call_streamable_mcp(
        self,
        url: str,
        headers: dict[str, str] | None,
        tool_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request_headers = {"Accept": "application/json, text/event-stream", **(headers or {})}
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            initialized = await client.post(
                url,
                headers=request_headers,
                json={
                    "jsonrpc": "2.0", "id": str(uuid4()), "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "youo-data-analysis-agent", "version": "0.2.0"},
                    },
                },
            )
            initialized.raise_for_status()
            session_id = initialized.headers.get("Mcp-Session-Id")
            if session_id:
                request_headers["Mcp-Session-Id"] = session_id
            await client.post(
                url, headers=request_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            called = await client.post(
                url,
                headers=request_headers,
                json={
                    "jsonrpc": "2.0", "id": str(uuid4()), "method": "tools/call",
                    "params": {"name": tool_name, "arguments": payload},
                },
            )
            called.raise_for_status()
            return self._bounded_output(called)

    @staticmethod
    def _bounded_output(response: httpx.Response) -> dict[str, Any]:
        if len(response.content) > 1024 * 1024:
            raise ValueError("extension response exceeds 1 MiB")
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
