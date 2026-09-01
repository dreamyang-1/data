"""Small, transport-agnostic runtime for controlled tool execution.

The runtime deliberately does not know HTTP, MCP, SQL or business semantics.
Callers keep those responsibilities and provide a guarded executor.  This
module only standardizes validation, bounded retry, timeout and execution
metadata so existing tools can migrate without changing their public API.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from app.domain.models import StrictModel


class ToolContext(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    user_id: str | None = Field(default=None, max_length=128)
    tenant_id: str | None = Field(default=None, max_length=128)
    application_id: str | None = Field(default=None, max_length=100)


class ToolRequest(StrictModel):
    tool_name: str = Field(min_length=1, max_length=100)
    kind: Literal["HTTP_TOOL", "MCP_TOOL", "INTERNAL_TOOL"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] | None = None
    timeout_seconds: float = Field(default=15, gt=0, le=300)
    max_attempts: int = Field(default=1, ge=1, le=3)
    idempotent: bool = True
    execution_id: str = Field(default_factory=lambda: str(uuid4()))


class ToolError(StrictModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    error_type: str | None = Field(default=None, max_length=100)
    status_code: int | None = Field(default=None, ge=0, le=999)
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(StrictModel):
    execution_id: str
    trace_id: str
    session_id: str
    tool_name: str
    kind: Literal["HTTP_TOOL", "MCP_TOOL", "INTERNAL_TOOL"]
    status: Literal["COMPLETED", "FAILED", "REJECTED"]
    output: Any = None
    error: ToolError | None = None
    attempts: int = Field(ge=0, le=3)
    latency_ms: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolRuntimeFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        error_type: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = ToolError(
            code=code,
            message=message,
            retryable=retryable,
            error_type=error_type,
            status_code=status_code,
            details=details or {},
        )


ToolExecutor = Callable[[dict[str, Any]], Awaitable[Any]]
ToolHook = Callable[[ToolContext, ToolRequest, ToolResult | None], Awaitable[None] | None]
ToolGuard = Callable[[ToolContext, ToolRequest], Awaitable[None] | None]
ResultValidator = Callable[[Any], Awaitable[None] | None]


class ToolRuntime:
    """Execute one already-selected tool under a finite, auditable policy."""

    def __init__(
        self,
        *,
        pre_execute: ToolHook | None = None,
        post_execute: ToolHook | None = None,
    ) -> None:
        self.pre_execute = pre_execute
        self.post_execute = post_execute

    async def execute(
        self,
        context: ToolContext,
        request: ToolRequest,
        executor: ToolExecutor,
        *,
        guard: ToolGuard | None = None,
        result_validator: ResultValidator | None = None,
    ) -> ToolResult:
        started = time.perf_counter()
        validation_error = self._validate_arguments(
            request.arguments, request.input_schema
        )
        if validation_error:
            return await self._finish(
                context,
                request,
                started,
                status="REJECTED",
                attempts=0,
                error=ToolError(
                    code="INVALID_TOOL_ARGUMENTS",
                    message=validation_error,
                    error_type="parameter_error",
                    status_code=400,
                ),
            )
        try:
            if guard is not None:
                await self._maybe_await(guard(context, request))
            if self.pre_execute is not None:
                await self._maybe_await(self.pre_execute(context, request, None))
        except ToolRuntimeFailure as exc:
            return await self._finish(
                context, request, started, status="REJECTED", attempts=0, error=exc.error
            )
        except Exception as exc:
            return await self._finish(
                context,
                request,
                started,
                status="REJECTED",
                attempts=0,
                error=ToolError(
                    code="TOOL_GUARD_REJECTED",
                    message=f"工具执行前检查未通过：{type(exc).__name__}",
                    error_type="permission_error",
                ),
            )

        allowed_attempts = request.max_attempts if request.idempotent else 1
        last_error: ToolError | None = None
        for attempt in range(1, allowed_attempts + 1):
            try:
                output = await asyncio.wait_for(
                    executor(request.arguments), timeout=request.timeout_seconds
                )
                if result_validator is not None:
                    await self._maybe_await(result_validator(output))
                return await self._finish(
                    context,
                    request,
                    started,
                    status="COMPLETED",
                    attempts=attempt,
                    output=output,
                )
            except asyncio.TimeoutError:
                last_error = ToolError(
                    code="TOOL_TIMEOUT",
                    message=f"工具执行超过 {request.timeout_seconds:g} 秒",
                    retryable=True,
                    error_type="timeout",
                    status_code=504,
                )
            except ToolRuntimeFailure as exc:
                last_error = exc.error
            except Exception as exc:
                last_error = ToolError(
                    code="TOOL_EXECUTION_FAILED",
                    message=f"工具执行失败：{type(exc).__name__}",
                    retryable=False,
                    error_type="unknown_error",
                    status_code=500,
                )
            if not last_error.retryable or attempt >= allowed_attempts:
                break
            await asyncio.sleep(min(0.05 * (2 ** (attempt - 1)), 0.2))
        return await self._finish(
            context,
            request,
            started,
            status="FAILED",
            attempts=attempt,
            error=last_error,
        )

    async def _finish(
        self,
        context: ToolContext,
        request: ToolRequest,
        started: float,
        *,
        status: Literal["COMPLETED", "FAILED", "REJECTED"],
        attempts: int,
        output: Any = None,
        error: ToolError | None = None,
    ) -> ToolResult:
        result = ToolResult(
            execution_id=request.execution_id,
            trace_id=context.trace_id,
            session_id=context.session_id,
            tool_name=request.tool_name,
            kind=request.kind,
            status=status,
            output=output,
            error=error,
            attempts=attempts,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        )
        if self.post_execute is not None:
            try:
                await self._maybe_await(self.post_execute(context, request, result))
            except Exception:
                # Observability hooks must never turn a valid tool result into a failure.
                pass
        return result

    @staticmethod
    async def _maybe_await(value: Awaitable[None] | None) -> None:
        if inspect.isawaitable(value):
            await value

    @classmethod
    def _validate_arguments(
        cls, arguments: Mapping[str, Any], schema: Mapping[str, Any] | None
    ) -> str | None:
        if not schema:
            return None
        required = schema.get("required", [])
        if not isinstance(required, list):
            return "工具 inputSchema.required 必须是数组"
        missing = [name for name in required if name not in arguments]
        if missing:
            return "工具缺少必填参数：" + ", ".join(str(item) for item in missing)
        properties = schema.get("properties", {})
        if properties is not None and not isinstance(properties, Mapping):
            return "工具 inputSchema.properties 必须是对象"
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "object": Mapping,
            "array": list,
        }
        for name, value in arguments.items():
            definition = properties.get(name) if isinstance(properties, Mapping) else None
            expected = definition.get("type") if isinstance(definition, Mapping) else None
            python_type = type_map.get(expected)
            if python_type is None:
                continue
            if expected in {"number", "integer"} and isinstance(value, bool):
                return f"工具参数 {name} 类型应为 {expected}"
            if not isinstance(value, python_type):
                return f"工具参数 {name} 类型应为 {expected}"
        return None
