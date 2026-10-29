import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models import AgentResponse, CanonicalAnalysisRequest, MetricRef, PrimaryIntent, TrustedIdentity


def request(**updates):
    return CanonicalAnalysisRequest(semantic_model_id=130, conversation_id='definition-direct',
        tenant_id='t', user_id='u', application_id='app',
        original_question='订单笔数的统计口径是什么？', primary_intent=PrimaryIntent.METRIC_DEFINITION,
        metrics=[MetricRef(input='订单笔数')], **updates)


def model_response(answer):
    value = request()
    return AgentResponse(request_id=value.request_id, conversation_id=value.conversation_id,
        status='COMPLETED', intent=PrimaryIntent.METRIC_DEFINITION, answer=answer)


@pytest.mark.asyncio
async def test_definition_boundary_cannot_be_expanded_or_overridden_by_query_extraction(monkeypatch):
    from test_task_planning_stages import setup_planner
    planner, calls = setup_planner(monkeypatch, {'tasks': [{'question': '解释平均每单销售金额的统计口径',
        'definition_only': True, 'metric_targets': ['平均每单销售金额'], 'expected_output': '解释均值计算公式'}]},
        extract=lambda payload: {'tasks': [{'task_index': 0, 'extraction': {
            '意图': '指标查询', '指标': [{'name': '平均每单销售金额'}], '是否查询数据库': '是'}}]})
    result = await planner.plan_question('平均每单销售金额的统计口径是什么？', semantic_model_id=130)
    assert len(calls) == 2 and len(result.tasks) == 1
    assert result.tasks[0].intent == PrimaryIntent.METRIC_DEFINITION
    assert result.tasks[0].structured['是否查询数据库'] == '否'
    assert result.tasks[0].structured['指标'] == [{'name': '平均每单销售金额'}]


@pytest.mark.asyncio
async def test_mixed_dag_marks_definition_subtask_for_direct_answer():
    """混合计划：解释口径子任务必须打直答标记，查询子任务不打（Q1回归）。"""
    from test_workflow import make_orchestrator
    from app.domain.models import AtomicTask, ChatRequest, TaskPlan
    from app.planning.compound_question import CompoundQuestionRouter
    from app.services.progress import progress_scope
    agent = make_orchestrator()
    agent.task_planner = CompoundQuestionRouter(agent.settings)
    agent.analysis_synthesizer = SimpleNamespace(synthesize_combined=AsyncMock())
    flags = {}

    async def respond(child, identity):
        flags[child.question] = getattr(child, '_dag_definition_direct', False)
        return model_response('销售额说明')

    agent._handle = AsyncMock(side_effect=respond)
    tasks = [
        AtomicTask(task_id='task-1', question='2025年销售额排名前10的经销商有哪些',
            primary_intent=PrimaryIntent.METRIC_QUERY, extraction={'是否查询数据库': '是'}),
        AtomicTask(task_id='task-2', question='解释销售额排名中并列名次的处理规则',
            primary_intent=PrimaryIntent.METRIC_DEFINITION, extraction={'是否查询数据库': '否'}),
    ]
    chat = ChatRequest(semantic_model_id=130, application_id='app', conversation_id='mixed-dag',
        message_id='m1', question='2025年销售额排名前10的经销商有哪些？并列名次如何处理？')
    events = []
    with progress_scope(events.append):
        await agent._handle_task_plan(chat, TrustedIdentity(tenant_id='t', user_id='u'),
            TaskPlan(planner='DETERMINISTIC_RULE', tasks=tasks))
    assert flags.get('解释销售额排名中并列名次的处理规则') is True
    assert flags.get('2025年销售额排名前10的经销商有哪些') is False


def test_definition_direct_flag_overrides_rewritten_intent():
    """意图规范化把解释口径子任务意图改写成查询时，直答标记仍生效。"""
    from app.domain.models import ChatRequest
    from app.services.orchestrator import DataAnalysisOrchestrator
    chat = ChatRequest(semantic_model_id=130, application_id='app', conversation_id='c',
        message_id='m', question='解释销售额排名中并列名次的处理规则')
    chat._dag_definition_direct = True
    rewritten = request().model_copy(update={'primary_intent': PrimaryIntent.METRIC_QUERY, 'metrics': []})
    assert DataAnalysisOrchestrator._is_definition_direct(rewritten, chat) is True
    plain = ChatRequest(semantic_model_id=130, application_id='app', conversation_id='c',
        message_id='m', question='x')
    assert DataAnalysisOrchestrator._is_definition_direct(rewritten, plain) is False


def test_stream_definition_uses_answer_model_without_query_stages(monkeypatch):
    from test_api import TestClient, build_test_app
    app = build_test_app()
    with TestClient(app) as client:
        agent = app.state.container.orchestrator
        agent.analysis_synthesizer = SimpleNamespace(synthesize_definition=AsyncMock(return_value='销售额说明'))
        query = agent.adapters.query.query = AsyncMock(side_effect=AssertionError('definition must not execute SQL'))
        recovery = agent._recover_live_published_metrics = AsyncMock(side_effect=AssertionError('definition must not recover SQL metrics'))
        response = client.post('/agent_chat/stream', json={'semantic_model_id': 130, 'application_id': 'app',
            'conversation_id': 'definition-stream', 'message_id': 'm1', 'question': '销售额的口径是什么？'})
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
    complete = next(event for event in events if event.get('type') == 'complete')
    assert complete['status'] == 'COMPLETED' and complete['intent'] == 'METRIC_DEFINITION'
    stages = {event.get('meta', {}).get('stage') for event in events if event.get('type') == 'message_chunk'}
    assert not {'ASL_GENERATION', 'SQL_TRANSLATION', 'SQL_EXECUTION', 'DATA_RETRIEVAL', 'RELIABILITY_CHECK', 'INSIGHT_ANALYSIS'}.intersection(stages)
    assert 'FINAL_OUTPUT' in stages
    assert not complete['analysis_plan'] and complete['answer'] == '销售额说明'
    agent.analysis_synthesizer.synthesize_definition.assert_awaited_once()
    query.assert_not_awaited(); recovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_definition_dag_returns_each_answer_without_root_insight():
    from test_workflow import make_orchestrator
    from app.domain.models import AtomicTask, ChatRequest, TaskPlan
    from app.planning.compound_question import CompoundQuestionRouter
    from app.services.progress import progress_scope
    agent = make_orchestrator()
    agent.task_planner = CompoundQuestionRouter(agent.settings)
    synthesis = AsyncMock(side_effect=AssertionError('definition must not synthesize data insight'))
    agent.analysis_synthesizer = SimpleNamespace(synthesize_combined=synthesis)
    async def explain(chat, identity):
        assert not getattr(chat, '_dag_pure_computation', False)
        return model_response(chat.question)
    agent._handle = AsyncMock(side_effect=explain)
    tasks = [AtomicTask(task_id='task-1', question='解释订单笔数口径', primary_intent=PrimaryIntent.METRIC_DEFINITION,
                extraction={'是否查询数据库': '否'}),
             AtomicTask(task_id='task-2', question='解释平均每单销售金额口径', depends_on=['task-1'],
                primary_intent=PrimaryIntent.METRIC_DEFINITION, extraction={'是否查询数据库': '否'})]
    chat = ChatRequest(semantic_model_id=130, application_id='app', conversation_id='definition-dag',
        message_id='m1', question='分别解释订单笔数与平均每单销售金额的口径')
    events = []
    with progress_scope(events.append):
        response = await agent._handle_task_plan(chat, TrustedIdentity(tenant_id='t', user_id='u'),
            TaskPlan(planner='DETERMINISTIC_RULE', tasks=tasks))
    assert response.status == 'COMPLETED' and len(response.task_results) == 2
    assert '解释订单笔数口径' in response.answer and '解释平均每单销售金额口径' in response.answer
    assert not {'ASL_GENERATION', 'SQL_EXECUTION', 'DATA_RETRIEVAL', 'RELIABILITY_CHECK', 'INSIGHT_ANALYSIS'}.intersection(e['stage'] for e in events)
    synthesis.assert_not_awaited()


def test_definition_child_crosses_mixed_progress_barriers_without_visible_query_node():
    from app.api import _CompositeChildProgressOrderer
    orderer = _CompositeChildProgressOrderer()
    def event(task_id, index, stage, **kwargs):
        return {'task_id': task_id, 'task_index': index, 'task_count': 2,
            'stage': stage, 'status': 'COMPLETED', 'message': 'done', 'is_child_task': True, **kwargs}
    assert orderer.push(event('definition', 0, 'DATA_RETRIEVAL', metadata_only=True, task_terminal=True)) == []
    visible = orderer.push(event('query', 1, 'ASL_GENERATION'))
    visible += orderer.push(event('query', 1, 'SQL_EXECUTION'))
    visible += orderer.push(event('query', 1, 'DATA_RETRIEVAL', task_terminal=True))
    visible += orderer.push(event('query', 1, 'RELIABILITY_CHECK'))
    visible += orderer.push(event('query', 1, 'INSIGHT_ANALYSIS'))
    assert [e['stage'] for e in visible] == ['ASL_GENERATION', 'SQL_EXECUTION', 'DATA_RETRIEVAL', 'RELIABILITY_CHECK', 'INSIGHT_ANALYSIS']
    assert all(e['task_id'] == 'query' for e in visible)
