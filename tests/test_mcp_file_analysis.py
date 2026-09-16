from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import bind_chat_spreadsheet
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    ChatRequest,
    ExtensionExecution,
    McpConfig,
    PrimaryIntent,
    ToolConfig,
    TrustedIdentity,
)
from app.services.mcp_file_analysis import (
    McpFileAnalysisOutcome,
    McpFileAnalysisRunner,
)
from app.services.orchestrator import DataAnalysisOrchestrator


def chat(**updates) -> ChatRequest:
    values = {
        "application_id": "app",
        "conversation_id": "conversation",
        "message_id": "message",
        "question": "分析一下",
        "semantic_model_id": 81,
        "temp_file_paths": ["uploads/E-Commerce.xlsx"],
        "mcp": [McpConfig(
            mcp_server_url="https://mcp.example/sse",
            connect_type="sse",
        )],
    }
    values.update(updates)
    return ChatRequest(**values)


class FakeDispatcher:
    def __init__(self, tools):
        self.tools = tools
        self.calls = []

    async def discover_mcp_tools(self, _chat, *, timeout_seconds=None):
        del timeout_seconds
        return self.tools

    async def call_discovered_mcp_tool(self, *, chat, tool, arguments):
        self.calls.append((chat, tool, arguments))
        return ExtensionExecution(
            name=tool.name.removeprefix("mcp:"),
            kind="MCP_TOOL",
            status="COMPLETED",
            output={
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "rows": 250,
                        "report_url": "https://files.example/report.xlsx",
                    }),
                }],
            },
        )


class FailingImporter:
    async def import_object(self, **_kwargs):
        raise ValueError("mixed column types")


class ResolvingUploadReference:
    def __init__(self):
        self.calls = []

    async def resolve(self, references, *, conversation_id, question):
        self.calls.append((references, conversation_id, question))
        return ["uploads/E-Commerce.xlsx"]


def settings(**updates) -> Settings:
    values = {
        "env": "test",
        "adapter_mode": "mock",
        "intent_model_api_key": "test-key",
        "intent_model_max_retries": 0,
    }
    values.update(updates)
    return Settings(**values)


def test_image_artifact_urls_are_normalized_to_markdown_images():
    image_url = "https://cdn.example.com/charts/sales-trend.png"
    document_url = "https://files.example.com/report.xlsx"

    assert McpFileAnalysisRunner._normalize_artifact_markdown(
        image_url,
        [image_url],
    ) == f"![图表]({image_url})"
    assert McpFileAnalysisRunner._normalize_artifact_markdown(
        f"[销售趋势]({image_url})",
        [image_url],
    ) == f"![销售趋势]({image_url})"
    assert McpFileAnalysisRunner._normalize_artifact_markdown(
        f"![销售趋势]({image_url})",
        [image_url],
    ) == f"![销售趋势]({image_url})"
    assert McpFileAnalysisRunner._normalize_artifact_markdown(
        document_url,
        [document_url],
    ) == document_url


@pytest.mark.asyncio
async def test_model_drives_uploaded_file_mcp_and_file_argument_is_injected(monkeypatch):
    tool = ToolConfig(
        name="mcp:analysis_profile",
        description="Analyze an Excel workbook",
        url="https://mcp.example/sse",
        inputSchema={
            "type": "object",
            "required": ["file_path"],
            "properties": {
                "file_path": {"type": "string"},
                "question": {"type": "string"},
            },
        },
    )
    dispatcher = FakeDispatcher([tool])
    runner = McpFileAnalysisRunner(settings(), dispatcher)  # type: ignore[arg-type]
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "analysis_profile",
                    "arguments": '{"question":"分析销售数据"}',
                },
            }],
        },
        {"role": "assistant", "content": "文件共有250行，已完成分析。"},
    ]

    async def fake_chat(_messages, _tools):
        return messages.pop(0)

    monkeypatch.setattr(runner, "_chat", fake_chat)
    outcome = await runner.run(chat())

    assert outcome.applicable is True
    assert outcome.usable is True
    assert outcome.answer == "文件共有250行，已完成分析。"
    assert dispatcher.calls[0][2] == {
        "question": "分析销售数据",
        "file_path": "uploads/E-Commerce.xlsx",
    }
    assert outcome.artifact_urls == ["https://files.example/report.xlsx"]


@pytest.mark.asyncio
async def test_chart_only_mcp_does_not_take_over_uploaded_file_analysis():
    dispatcher = FakeDispatcher([ToolConfig(
        name="mcp:generate_line_chart",
        description="Generate line chart from supplied points",
        url="https://mcp.example/sse",
        inputSchema={
            "type": "object",
            "properties": {"data": {"type": "array"}},
        },
    )])
    runner = McpFileAnalysisRunner(settings(), dispatcher)  # type: ignore[arg-type]

    outcome = await runner.run(chat())

    assert outcome.applicable is False
    assert outcome.usable is False
    assert dispatcher.calls == []


def test_untrusted_file_reference_is_rejected_before_mcp_call():
    tool = ToolConfig(
        name="mcp:excel_analyze",
        description="Excel data analysis",
        url="https://mcp.example/sse",
        inputSchema={
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
        },
    )

    prepared, error = McpFileAnalysisRunner._prepare_arguments(
        tool,
        {"file_path": "uploads/someone-elses-file.xlsx"},
        trusted_file_refs={"uploads/E-Commerce.xlsx"},
        original_file_refs=["uploads/E-Commerce.xlsx"],
    )

    assert prepared == {}
    assert "未经本轮文件" in str(error)


@pytest.mark.asyncio
async def test_local_import_failure_can_continue_to_configured_file_mcp():
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(file_importer=FailingImporter())
            )
        )
    )
    current = chat()

    await bind_chat_spreadsheet(
        request,
        current,
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert current.dataset_id is None
    assert current._file_inspection["status"] == (
        "LOCAL_IMPORT_FAILED_MCP_AVAILABLE"
    )

    without_mcp = chat(mcp=[])
    with pytest.raises(HTTPException) as exc_info:
        await bind_chat_spreadsheet(
            request,
            without_mcp,
            TrustedIdentity(tenant_id="tenant", user_id="user"),
        )
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_no_parse_is_resolved_before_local_import_and_mcp_fallback():
    resolver = ResolvingUploadReference()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(
                    file_importer=FailingImporter(),
                    upload_file_resolver=resolver,
                )
            )
        )
    )
    current = chat(temp_file_paths=["no-parse"])

    await bind_chat_spreadsheet(
        request,
        current,
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert resolver.calls == [
        (["no-parse"], "conversation", current.question)
    ]
    assert current.temp_file_paths == ["uploads/E-Commerce.xlsx"]
    assert current._file_inspection["status"] == (
        "LOCAL_IMPORT_FAILED_MCP_AVAILABLE"
    )


@pytest.mark.asyncio
async def test_no_parse_fails_clearly_when_platform_resolver_is_unavailable():
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(
                    file_importer=FailingImporter(),
                    upload_file_resolver=None,
                )
            )
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await bind_chat_spreadsheet(
            request,
            chat(temp_file_paths=["no-parse"]),
            TrustedIdentity(tenant_id="tenant", user_id="user"),
        )

    assert exc_info.value.status_code == 422
    assert "MinIO" in exc_info.value.detail


@pytest.mark.asyncio
async def test_orchestrator_uses_completed_mcp_result_and_falls_back_when_unusable():
    class Runner:
        def __init__(self, outcome):
            self.outcome = outcome

        async def run(self, _chat):
            return self.outcome

    request = CanonicalAnalysisRequest(
        request_id=uuid4(),
        conversation_id="conversation",
        tenant_id="tenant",
        user_id="user",
        original_question="分析一下",
        primary_intent=PrimaryIntent.CHAT,
    )
    success = McpFileAnalysisOutcome(
        applicable=True,
        answer="分析完成。",
        executions=[ExtensionExecution(
            name="excel_analyze",
            kind="MCP_TOOL",
            status="COMPLETED",
            output={"content": [{"type": "text", "text": "ok"}]},
        )],
        turns_used=2,
    )
    orchestrator = object.__new__(DataAnalysisOrchestrator)
    orchestrator.mcp_file_analysis_runner = Runner(success)

    response = await orchestrator._run_mcp_file_analysis(chat(), request)

    assert response is not None
    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.DATA_QUALITY
    assert response.extension_executions[0].name == "excel_analyze"

    orchestrator.mcp_file_analysis_runner = Runner(
        McpFileAnalysisOutcome(applicable=True, answer="无工具证据")
    )
    assert await orchestrator._run_mcp_file_analysis(chat(), request) is None
