"""Constrained selection of caller-provided, read-only HTTP tools."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.domain.models import ToolConfig


logger = logging.getLogger(__name__)


class ToolSelectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_tools: list[str] = Field(default_factory=list)


class OptionalToolSelector:
    """Let the model choose among usable tools, then enforce deterministic gates."""

    _CORE_PATHS = {
        "/agent/query",
        "/api/ast-to-sql",
        "/api/translate",
        "/api/execute",
    }
    _MUTATION_TERMS = (
        "create", "update", "delete", "remove", "write", "send", "submit",
        "approve", "cancel", "pay", "purchase", "upload", "创建", "新增",
        "修改", "更新", "删除", "移除", "写入", "发送", "提交", "审批",
        "取消", "支付", "购买", "上传",
    )
    _READ_TERMS = (
        "query", "search", "lookup", "read", "get", "list", "find", "analyze",
        "查询", "搜索", "检索", "读取", "获取", "查看", "分析", "统计",
    )

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    @classmethod
    def is_safe_candidate(cls, tool: ToolConfig) -> bool:
        path = urlparse(tool.url).path.rstrip("/").lower() or "/"
        if path in cls._CORE_PATHS:
            return False
        label = f"{tool.name} {tool.description}".lower()
        if any(term in label for term in cls._MUTATION_TERMS):
            return False
        return tool.http_method == "get" or any(term in label for term in cls._READ_TERMS)

    @staticmethod
    def has_satisfiable_inputs(
        tool: ToolConfig, available_arguments: dict[str, Any]
    ) -> bool:
        schema = tool.input_schema or {}
        required = schema.get("required", [])
        if not isinstance(required, list):
            return False
        available = {key for key, value in available_arguments.items() if value is not None}
        if "question" in available:
            available.add("query")
        domains = available_arguments.get("business_domain_ids")
        if isinstance(domains, list) and len(domains) == 1:
            available.add("business_domain_id")
        return all(isinstance(name, str) and name in available for name in required)

    async def select(
        self,
        *,
        question: str,
        intent: str,
        tools: list[ToolConfig],
        available_arguments: dict[str, Any],
        capability_descriptions: dict[str, str] | None = None,
    ) -> list[str]:
        if not self.settings.autonomous_tool_selection_enabled:
            return []
        candidates = [
            tool for tool in tools
            if self.is_safe_candidate(tool)
            and self.has_satisfiable_inputs(tool, available_arguments)
        ]
        if not candidates or not self.settings.intent_model_api_key:
            return []

        schema = ToolSelectionOutput.model_json_schema()
        catalog = [
            {
                "name": tool.name,
                "description": (capability_descriptions or {}).get(
                    tool.name, tool.description
                )[:4000],
                "http_method": tool.http_method,
                "input_schema": tool.input_schema or {},
            }
            for tool in candidates
        ]
        prompt = {
            "question": question,
            "intent": intent,
            "available_arguments": sorted(available_arguments),
            "candidate_tools": catalog,
        }
        body = {
            "model": self.settings.intent_model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You select optional read-only tools for a data-analysis agent. "
                        "Select a tool only when it adds information needed to answer the "
                        "current question. The core data query has already run, so do not "
                        "select tools that merely repeat SQL/AST generation. Return an empty "
                        "list when no tool is necessary. Never invent a tool name. Return only "
                        "JSON conforming to this schema: "
                        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "temperature": 0,
            "enable_thinking": self.settings.intent_model_enable_thinking,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.intent_model_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.intent_model_base_url.rstrip("/"),
                timeout=self.settings.intent_model_timeout_seconds,
                transport=self.transport,
            ) as client:
                for attempt in range(self.settings.intent_model_max_retries + 1):
                    try:
                        response = await client.post("/chat/completions", headers=headers, json=body)
                        response.raise_for_status()
                        break
                    except (httpx.TimeoutException, httpx.NetworkError):
                        if attempt >= self.settings.intent_model_max_retries:
                            raise
                        await asyncio.sleep(0.2 * (2**attempt))
                    except httpx.HTTPStatusError as exc:
                        retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                        if not retryable or attempt >= self.settings.intent_model_max_retries:
                            raise
                        await asyncio.sleep(0.2 * (2**attempt))
            content = response.json()["choices"][0]["message"]["content"]
            chosen = ToolSelectionOutput.model_validate_json(content).selected_tools
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("optional tool selection unavailable: %s", type(exc).__name__)
            return []

        allowed = {tool.name for tool in candidates}
        selected: list[str] = []
        for name in chosen:
            if name in allowed and name not in selected:
                selected.append(name)
            if len(selected) >= self.settings.autonomous_tool_selection_max_tools:
                break
        return selected
