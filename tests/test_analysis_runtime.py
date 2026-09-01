import asyncio

import pytest

from app.domain.models import AnalysisPlan, AnalysisStep
from app.runtime import AnalysisPlanError, AnalysisRuntime, ToolContext


def plan(steps):
    return AnalysisPlan(
        objective="分析变化原因",
        methods=["dimension_breakdown"],
        conclusion_policy=["只输出有证据的结论"],
        steps=steps,
    )


def context():
    return ToolContext(session_id="session", trace_id="trace")


@pytest.mark.asyncio
async def test_analysis_runtime_runs_independent_steps_in_parallel_then_dependency():
    active = 0
    maximum_active = 0

    async def breakdown(inputs, _dependencies):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"dimension": inputs["dimension"], "change": -10}

    async def attribution(_inputs, dependencies):
        return {
            "contributors": [
                value.output["dimension"] for value in dependencies.values()
            ]
        }

    runtime_plan = plan([
        AnalysisStep(
            id="region", action="QUERY", tool="breakdown",
            inputs={"dimension": "region"}, parallel_group="dimensions",
            expected_output="区域变化贡献",
        ),
        AnalysisStep(
            id="category", action="QUERY", tool="breakdown",
            inputs={"dimension": "category"}, parallel_group="dimensions",
            expected_output="品类变化贡献",
        ),
        AnalysisStep(
            id="attribution", action="ANALYZE", tool="attribution",
            depends_on=["region", "category"], expected_output="贡献度排序",
        ),
    ])

    result = await AnalysisRuntime(max_parallel=2).execute(
        runtime_plan,
        context(),
        {"breakdown": breakdown, "attribution": attribution},
    )

    assert result.status == "COMPLETED"
    assert maximum_active == 2
    assert result.outputs["attribution"]["contributors"] == ["region", "category"]


@pytest.mark.asyncio
async def test_optional_dimension_failure_returns_partial_success():
    async def successful(_inputs, _dependencies):
        return {"change": -18}

    async def failed(_inputs, _dependencies):
        raise ValueError("dimension unavailable")

    result = await AnalysisRuntime().execute(
        plan([
            AnalysisStep(
                id="baseline", action="QUERY", tool="successful",
                expected_output="总体变化",
            ),
            AnalysisStep(
                id="optional-channel", action="QUERY", tool="failed",
                expected_output="渠道贡献", required=False,
            ),
        ]),
        context(),
        {"successful": successful, "failed": failed},
    )

    assert result.status == "PARTIAL_SUCCESS"
    assert result.outputs == {"baseline": {"change": -18}}
    assert result.failed_step_ids == ["optional-channel"]


@pytest.mark.asyncio
async def test_failed_dependency_is_skipped_and_required_plan_fails():
    async def failed(_inputs, _dependencies):
        raise ValueError("query failed")

    result = await AnalysisRuntime().execute(
        plan([
            AnalysisStep(
                id="query", action="QUERY", tool="failed",
                expected_output="查询数据",
            ),
            AnalysisStep(
                id="analysis", action="ANALYZE", tool="never",
                depends_on=["query"], expected_output="分析结果",
            ),
        ]),
        context(),
        {"failed": failed},
    )

    assert result.status == "FAILED"
    assert result.failed_step_ids == ["query"]
    assert result.skipped_step_ids == ["analysis"]
    assert result.step_results[1].error_code == "DEPENDENCY_FAILED"


def test_analysis_runtime_rejects_cycle_and_unknown_dependency():
    cyclic = [
        AnalysisStep(
            id="a", action="ANALYZE", tool="a", depends_on=["b"],
            expected_output="a",
        ),
        AnalysisStep(
            id="b", action="ANALYZE", tool="b", depends_on=["a"],
            expected_output="b",
        ),
    ]
    with pytest.raises(AnalysisPlanError, match="循环依赖"):
        AnalysisRuntime.execution_layers(cyclic)

    unknown = [AnalysisStep(
        id="a", action="ANALYZE", tool="a", depends_on=["missing"],
        expected_output="a",
    )]
    with pytest.raises(AnalysisPlanError, match="未知依赖"):
        AnalysisRuntime.execution_layers(unknown)


@pytest.mark.asyncio
async def test_unregistered_executor_is_never_dynamically_imported():
    result = await AnalysisRuntime().execute(
        plan([AnalysisStep(
            id="unsafe", action="ANALYZE", tool="os.system",
            expected_output="不得执行",
        )]),
        context(),
        {},
    )

    assert result.status == "FAILED"
    assert result.step_results[0].error_code == "ANALYSIS_EXECUTOR_NOT_REGISTERED"


@pytest.mark.asyncio
async def test_analysis_runtime_enforces_total_budget_and_never_starts_later_steps():
    later_started = False

    async def slow(_inputs, _dependencies):
        await asyncio.sleep(0.2)
        return {"unexpected": True}

    async def later(_inputs, _dependencies):
        nonlocal later_started
        later_started = True
        return {"unexpected": True}

    result = await AnalysisRuntime(max_duration_seconds=0.03).execute(
        plan([
            AnalysisStep(
                id="slow-query", action="QUERY", tool="slow",
                expected_output="慢查询结果", timeout_seconds=1,
            ),
            AnalysisStep(
                id="later-analysis", action="ANALYZE", tool="later",
                depends_on=["slow-query"], expected_output="后续分析",
            ),
        ]),
        context(),
        {"slow": slow, "later": later},
    )

    assert result.status == "FAILED"
    assert result.budget_exhausted is True
    assert result.budget_seconds == 0.03
    assert result.latency_ms < 150
    assert later_started is False
    assert result.failed_step_ids == ["slow-query"]
    assert result.skipped_step_ids == ["later-analysis"]
    assert all(
        item.error_code == "ANALYSIS_BUDGET_EXHAUSTED"
        for item in result.step_results
    )


@pytest.mark.asyncio
async def test_analysis_budget_preserves_completed_optional_results_as_partial_success():
    async def completed(_inputs, _dependencies):
        return {"baseline": 10}

    async def slow_optional(_inputs, _dependencies):
        await asyncio.sleep(0.2)

    result = await AnalysisRuntime(max_duration_seconds=0.03).execute(
        plan([
            AnalysisStep(
                id="baseline", action="QUERY", tool="completed",
                expected_output="基线",
            ),
            AnalysisStep(
                id="optional-detail", action="ANALYZE", tool="slow_optional",
                depends_on=["baseline"], expected_output="补充分析", required=False,
            ),
        ]),
        context(),
        {"completed": completed, "slow_optional": slow_optional},
    )

    assert result.status == "PARTIAL_SUCCESS"
    assert result.outputs == {"baseline": {"baseline": 10}}
    assert result.budget_exhausted is True
