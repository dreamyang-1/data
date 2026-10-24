import asyncio
from uuid import uuid4

import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import AgentResponse, AtomicTask, ChatRequest, PrimaryIntent, TaskPlan, TrustedIdentity
from app.planning import CompoundQuestionRouter
from app.services.orchestrator import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


def service_for(handler):
    settings = Settings(_env_file=None, env='test', multi_question_model_enabled=False)
    service = DataAnalysisOrchestrator(
        settings=settings, classifier=None, adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(7200, 7200), task_planner=CompoundQuestionRouter(settings),
    )
    service._handle = handler
    return service


async def execute(service, dependencies):
    plan = TaskPlan(planner='DETERMINISTIC_RULE', tasks=[
        AtomicTask(task_id=f'task-{index}', question=f'解释指标{index}口径',
                   primary_intent=PrimaryIntent.METRIC_DEFINITION, depends_on=deps)
        for index, deps in enumerate(dependencies, 1)
    ])
    return await service._handle_task_plan(
        ChatRequest(semantic_model_id=81, conversation_id='ready', message_id='m1',
                    question='解释各项指标', application_id='app'),
        TrustedIdentity(tenant_id='t', user_id='u'), plan,
    )


def completed(chat):
    return AgentResponse(request_id=uuid4(), conversation_id=chat.conversation_id,
                         status='COMPLETED', intent=PrimaryIntent.METRIC_DEFINITION, answer='完成')


@pytest.mark.asyncio
async def test_ready_branch_does_not_wait_for_unrelated_slow_branch():
    slow_started, dependent_finished = asyncio.Event(), asyncio.Event()
    order = []

    async def handle(chat, identity):
        if '指标1' in chat.question:
            await slow_started.wait()
            order.append(1)
        elif '指标2' in chat.question:
            slow_started.set()
            await dependent_finished.wait()
            order.append(2)
        else:
            assert order == [1]
            order.append(3)
            dependent_finished.set()
        return completed(chat)

    result = await asyncio.wait_for(execute(service_for(handle), [[], [], ['task-1']]), 3)
    assert order == [1, 3, 2]
    assert [task.status for task in result.task_results] == ['COMPLETED'] * 3


@pytest.mark.asyncio
async def test_fan_in_waits_for_both_dependencies():
    started = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    finished = set()

    async def handle(chat, identity):
        if '指标3' in chat.question:
            assert finished == {1, 2}
        else:
            index = 1 if '指标1' in chat.question else 2
            started[index - 1].set()
            await release.wait()
            finished.add(index)
        return completed(chat)

    root = asyncio.create_task(execute(service_for(handle), [[], [], ['task-1', 'task-2']]))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 3)
        assert not root.done()
        release.set()
        result = await asyncio.wait_for(root, 3)
        assert all(task.status == 'COMPLETED' for task in result.task_results)
    finally:
        root.cancel()
        await asyncio.gather(root, return_exceptions=True)


@pytest.mark.asyncio
async def test_root_cancellation_cleans_up_running_and_waiting_tasks():
    started = [asyncio.Event(), asyncio.Event()]
    cancelled = set()

    async def handle(chat, identity):
        assert '指标3' not in chat.question
        index = 1 if '指标1' in chat.question else 2
        started[index - 1].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(index)

    root = asyncio.create_task(execute(service_for(handle), [[], [], ['task-1']]))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 3)
    finally:
        root.cancel()
        await asyncio.gather(root, return_exceptions=True)
    assert cancelled == {1, 2}


@pytest.mark.asyncio
async def test_scheduled_tasks_preserve_progress_order_and_task_identity():
    from app.services.progress import emit_progress, progress_scope
    events = []

    async def handle(chat, identity):
        await emit_progress('ASL_GENERATION', 'COMPLETED', 'parsed')
        await asyncio.sleep(0)
        await emit_progress('SQL_EXECUTION', 'COMPLETED', 'queried')
        return completed(chat)

    with progress_scope(events.append):
        await emit_progress('INTENT_RECOGNITION', 'COMPLETED', 'intent')
        await emit_progress('TASK_PLANNING', 'COMPLETED', 'plan')
        await execute(service_for(handle), [[], [], ['task-1', 'task-2']])
        await emit_progress('RELIABILITY_CHECK', 'COMPLETED', 'checked')
        await emit_progress('INSIGHT_ANALYSIS', 'COMPLETED', 'analyzed')
    assert [event['stage'] for event in events[:2]] == ['INTENT_RECOGNITION', 'TASK_PLANNING']
    assert [event['stage'] for event in events[-2:]] == ['RELIABILITY_CHECK', 'INSIGHT_ANALYSIS']
    for task_id in ['task-1', 'task-2', 'task-3']:
        stages = [event['stage'] for event in events if event.get('task_id') == task_id]
        assert stages == ['ASL_GENERATION', 'SQL_EXECUTION', 'METADATA_RETRIEVAL']
    dependent_start = next(i for i, event in enumerate(events)
                          if event.get('task_id') == 'task-3')
    assert all(i < dependent_start for i, event in enumerate(events)
               if event.get('task_id') in {'task-1', 'task-2'})
