"""Execution waits for completion; cancellation and actual failures still propagate."""
import asyncio
import pytest

from app.config import Settings
from app.services.insight_delivery import deliver_root_insight


@pytest.mark.asyncio
async def test_root_insight_waits_and_preserves_all_result_selections():
    async def generate():
        await asyncio.sleep(.03)
        return '人员排名及所属公司', {'result_task_ids': ['task-3', 'task-4']}
    text, result = await deliver_root_insight([], generate)
    assert text and result['result_task_ids'] == ['task-3', 'task-4']


@pytest.mark.asyncio
async def test_root_insight_user_cancel_propagates():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def generate():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    task = asyncio.create_task(deliver_root_insight([], generate))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_root_insight_keeps_actual_error():
    async def generate():
        raise RuntimeError('upstream unavailable')
    with pytest.raises(RuntimeError, match='upstream unavailable'):
        await deliver_root_insight([], generate)


@pytest.mark.parametrize('name', [
    'request_timeout_seconds', 'asl_generation_timeout_seconds',
    'analysis_synthesis_timeout_seconds', 'http_dependency_timeout_seconds',
    'intent_model_timeout_seconds', 'multi_question_model_timeout_seconds',
    'model_gateway_timeout_seconds', 'mcp_file_analysis_total_budget_seconds',
    'mcp_file_analysis_tool_timeout_seconds', 'knowledge_base_timeout_seconds',
    'question_rewrite_timeout_seconds', 'jev_timeout_seconds',
])
def test_legacy_stage_timeout_environment_cannot_reenable_deadline(monkeypatch, name):
    monkeypatch.setenv('DATA_AGENT_' + name.upper(), '.001')
    assert not hasattr(Settings(_env_file=None), name)
