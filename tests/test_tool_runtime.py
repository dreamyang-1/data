import asyncio

import pytest

from app.runtime import ToolContext, ToolRequest, ToolRuntime
from app.runtime.tool_runtime import ToolRuntimeFailure


def context() -> ToolContext:
    return ToolContext(session_id="conversation-1", trace_id="message-1")


@pytest.mark.asyncio
async def test_tool_runtime_rejects_missing_required_argument_without_execution():
    called = False

    async def executor(_):
        nonlocal called
        called = True

    result = await ToolRuntime().execute(
        context(),
        ToolRequest(
            tool_name="inventory",
            kind="HTTP_TOOL",
            arguments={},
            input_schema={
                "required": ["sku"],
                "properties": {"sku": {"type": "string"}},
            },
        ),
        executor,
    )

    assert result.status == "REJECTED"
    assert result.error.code == "INVALID_TOOL_ARGUMENTS"
    assert result.attempts == 0
    assert called is False


@pytest.mark.asyncio
async def test_tool_runtime_retries_retryable_failure_and_keeps_execution_identity():
    attempts = 0

    async def executor(_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ToolRuntimeFailure(
                "UPSTREAM_BUSY", "busy", retryable=True, status_code=503
            )
        return {"stock": 18}

    result = await ToolRuntime().execute(
        context(),
        ToolRequest(
            tool_name="inventory",
            kind="HTTP_TOOL",
            arguments={"sku": "A100"},
            max_attempts=2,
        ),
        executor,
    )

    assert result.status == "COMPLETED"
    assert result.output == {"stock": 18}
    assert result.attempts == 2
    assert result.execution_id
    assert result.trace_id == "message-1"
    assert result.session_id == "conversation-1"


@pytest.mark.asyncio
async def test_tool_runtime_does_not_retry_non_idempotent_tool():
    attempts = 0

    async def executor(_):
        nonlocal attempts
        attempts += 1
        raise ToolRuntimeFailure("UNCERTAIN", "unknown outcome", retryable=True)

    result = await ToolRuntime().execute(
        context(),
        ToolRequest(
            tool_name="unsafe_write",
            kind="INTERNAL_TOOL",
            arguments={},
            max_attempts=3,
            idempotent=False,
        ),
        executor,
    )

    assert result.status == "FAILED"
    assert result.attempts == 1
    assert attempts == 1


@pytest.mark.asyncio
async def test_tool_runtime_timeout_is_bounded_and_reported():
    async def executor(_):
        await asyncio.sleep(0.1)

    result = await ToolRuntime().execute(
        context(),
        ToolRequest(
            tool_name="slow_tool",
            kind="HTTP_TOOL",
            arguments={},
            timeout_seconds=0.01,
        ),
        executor,
    )

    assert result.status == "FAILED"
    assert result.error.code == "TOOL_TIMEOUT"
    assert result.error.retryable is True
    assert result.latency_ms < 100


@pytest.mark.asyncio
async def test_tool_runtime_hooks_do_not_change_result_contract():
    events = []

    async def pre(ctx, request, result):
        events.append(("pre", ctx.trace_id, request.tool_name, result))

    async def post(ctx, request, result):
        events.append(("post", ctx.trace_id, request.tool_name, result.status))

    result = await ToolRuntime(pre_execute=pre, post_execute=post).execute(
        context(),
        ToolRequest(tool_name="metric", kind="INTERNAL_TOOL", arguments={}),
        lambda _: asyncio.sleep(0, result={"value": 1}),
    )

    assert result.status == "COMPLETED"
    assert events == [
        ("pre", "message-1", "metric", None),
        ("post", "message-1", "metric", "COMPLETED"),
    ]
