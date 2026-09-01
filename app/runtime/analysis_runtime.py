"""Controlled executor for structured analysis plans.

Only explicitly registered executors can run.  The runtime supports bounded
parallel layers, dependencies and partial success; it never evaluates model-
generated Python or imports a tool named by the plan.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from pydantic import Field

from app.domain.models import AnalysisPlan, AnalysisStep, StrictModel
from app.runtime.tool_runtime import ToolContext, ToolRequest, ToolRuntime


class AnalysisStepResult(StrictModel):
    step_id: str
    action: str
    tool: str
    status: Literal["COMPLETED", "FAILED", "SKIPPED"]
    output: Any = None
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=500)
    attempts: int = Field(default=0, ge=0, le=3)
    latency_ms: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnalysisRuntimeResult(StrictModel):
    status: Literal["COMPLETED", "PARTIAL_SUCCESS", "FAILED"]
    step_results: list[AnalysisStepResult] = Field(default_factory=list, max_length=20)
    outputs: dict[str, Any] = Field(default_factory=dict)
    failed_step_ids: list[str] = Field(default_factory=list, max_length=20)
    skipped_step_ids: list[str] = Field(default_factory=list, max_length=20)
    latency_ms: int = Field(default=0, ge=0)
    budget_exhausted: bool = False
    budget_seconds: float | None = Field(default=None, gt=0, le=900)


AnalysisExecutor = Callable[
    [dict[str, Any], Mapping[str, AnalysisStepResult]], Awaitable[Any]
]


class AnalysisPlanError(ValueError):
    pass


class AnalysisRuntime:
    def __init__(
        self,
        *,
        max_parallel: int = 4,
        max_duration_seconds: float = 120,
        tool_runtime: ToolRuntime | None = None,
    ):
        if not 1 <= max_parallel <= 16:
            raise ValueError("max_parallel must be between 1 and 16")
        if not 0 < max_duration_seconds <= 900:
            raise ValueError("max_duration_seconds must be between 0 and 900")
        self.max_parallel = max_parallel
        self.max_duration_seconds = max_duration_seconds
        self.tool_runtime = tool_runtime or ToolRuntime()

    async def execute(
        self,
        plan: AnalysisPlan,
        context: ToolContext,
        executors: Mapping[str, AnalysisExecutor],
    ) -> AnalysisRuntimeResult:
        started = time.perf_counter()
        layers = self.execution_layers(plan.steps)
        semaphore = asyncio.Semaphore(self.max_parallel)
        results: dict[str, AnalysisStepResult] = {}
        budget_exhausted = False

        async def run(step: AnalysisStep) -> AnalysisStepResult:
            failed_dependencies = [
                dependency for dependency in step.depends_on
                if results[dependency].status != "COMPLETED"
            ]
            if failed_dependencies:
                return AnalysisStepResult(
                    step_id=step.id,
                    action=step.action,
                    tool=step.tool,
                    status="SKIPPED",
                    error_code="DEPENDENCY_FAILED",
                    error_message="依赖步骤未成功：" + ", ".join(failed_dependencies),
                    metadata={"failed_dependencies": failed_dependencies},
                )
            executor = executors.get(step.tool)
            if executor is None:
                return AnalysisStepResult(
                    step_id=step.id,
                    action=step.action,
                    tool=step.tool,
                    status="FAILED",
                    error_code="ANALYSIS_EXECUTOR_NOT_REGISTERED",
                    error_message=f"分析执行器未注册：{step.tool}",
                )
            dependency_results = {
                dependency: results[dependency] for dependency in step.depends_on
            }

            async def invoke(arguments: dict[str, Any]) -> Any:
                async with semaphore:
                    return await executor(arguments, dependency_results)

            tool_result = await self.tool_runtime.execute(
                context,
                ToolRequest(
                    tool_name=step.tool,
                    kind="INTERNAL_TOOL",
                    arguments=step.inputs,
                    timeout_seconds=step.timeout_seconds,
                    max_attempts=step.max_attempts,
                    idempotent=True,
                ),
                invoke,
            )
            error = tool_result.error
            return AnalysisStepResult(
                step_id=step.id,
                action=step.action,
                tool=step.tool,
                status=("COMPLETED" if tool_result.status == "COMPLETED" else "FAILED"),
                output=tool_result.output,
                error_code=error.code if error else None,
                error_message=error.message if error else None,
                attempts=tool_result.attempts,
                latency_ms=tool_result.latency_ms,
                metadata={
                    "execution_id": tool_result.execution_id,
                    "trace_id": tool_result.trace_id,
                    "parallel_group": step.parallel_group,
                    "expected_output": step.expected_output,
                    "required": step.required,
                    **({"error_details": error.details} if error and error.details else {}),
                },
            )

        for layer_index, layer in enumerate(layers):
            elapsed = time.perf_counter() - started
            remaining = self.max_duration_seconds - elapsed
            if remaining <= 0:
                budget_exhausted = True
                self._mark_budget_exhausted(
                    results,
                    [step for pending in layers[layer_index:] for step in pending],
                    running=False,
                )
                break
            try:
                completed = await asyncio.wait_for(
                    asyncio.gather(*(run(step) for step in layer)),
                    timeout=remaining,
                )
                results.update((item.step_id, item) for item in completed)
            except asyncio.TimeoutError:
                budget_exhausted = True
                self._mark_budget_exhausted(results, layer, running=True)
                self._mark_budget_exhausted(
                    results,
                    [step for pending in layers[layer_index + 1:] for step in pending],
                    running=False,
                )
                break

        ordered = [results[step.id] for step in plan.steps]
        failed = [item.step_id for item in ordered if item.status == "FAILED"]
        skipped = [item.step_id for item in ordered if item.status == "SKIPPED"]
        required_failed = any(
            results[step.id].status != "COMPLETED" and step.required
            for step in plan.steps
        )
        completed_count = sum(item.status == "COMPLETED" for item in ordered)
        status = (
            "COMPLETED"
            if not failed and not skipped
            else "PARTIAL_SUCCESS"
            if completed_count and not required_failed
            else "FAILED"
        )
        return AnalysisRuntimeResult(
            status=status,
            step_results=ordered,
            outputs={
                item.step_id: item.output for item in ordered
                if item.status == "COMPLETED"
            },
            failed_step_ids=failed,
            skipped_step_ids=skipped,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            budget_exhausted=budget_exhausted,
            budget_seconds=self.max_duration_seconds,
        )

    @staticmethod
    def _mark_budget_exhausted(
        results: dict[str, AnalysisStepResult],
        steps: list[AnalysisStep],
        *,
        running: bool,
    ) -> None:
        for step in steps:
            if step.id in results:
                continue
            results[step.id] = AnalysisStepResult(
                step_id=step.id,
                action=step.action,
                tool=step.tool,
                status="FAILED" if running else "SKIPPED",
                error_code="ANALYSIS_BUDGET_EXHAUSTED",
                error_message=(
                    "分析总执行时间预算已耗尽，当前步骤已取消"
                    if running else "分析总执行时间预算已耗尽，步骤未启动"
                ),
                metadata={
                    "parallel_group": step.parallel_group,
                    "expected_output": step.expected_output,
                    "required": step.required,
                },
            )

    @staticmethod
    def execution_layers(steps: list[AnalysisStep]) -> list[list[AnalysisStep]]:
        if not steps:
            return []
        ids = [step.id for step in steps]
        if len(ids) != len(set(ids)):
            raise AnalysisPlanError("分析步骤 ID 重复")
        known = set(ids)
        for step in steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise AnalysisPlanError(
                    f"步骤 {step.id} 引用了未知依赖：{', '.join(sorted(unknown))}"
                )
            if step.id in step.depends_on:
                raise AnalysisPlanError(f"步骤 {step.id} 不能依赖自身")
        remaining = {step.id: step for step in steps}
        completed: set[str] = set()
        layers: list[list[AnalysisStep]] = []
        while remaining:
            ready = [
                step for step in steps
                if step.id in remaining and set(step.depends_on).issubset(completed)
            ]
            if not ready:
                raise AnalysisPlanError("分析计划存在循环依赖")
            layers.append(ready)
            completed.update(step.id for step in ready)
            for step in ready:
                remaining.pop(step.id)
        return layers
