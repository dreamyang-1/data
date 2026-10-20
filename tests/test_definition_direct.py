import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpSemanticAdapter
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, MetricRef, PrimaryIntent, TrustedIdentity
from app.services.metric_definition_answer import definition_response


def request(**updates):
    return CanonicalAnalysisRequest(semantic_model_id=130, conversation_id='definition-direct',
        tenant_id='t', user_id='u', application_id='app',
        original_question='订单笔数的统计口径是什么？', primary_intent=PrimaryIntent.METRIC_DEFINITION,
        metrics=[MetricRef(input='订单笔数')], **updates)


def found(name='订单笔数'):
    return {'input': name, 'status': 'RESOLVED', 'definition': {
        'record_id': 'm-orders', 'semantic_model_id': 130, 'business_domain_id': 205,
        'metric_name': name, 'metric_code': 'orders', 'unit': '笔',
        'business_definition': {'description': '订单号去重计数'},
        'calculation_rule': {'calc_formula': 'COUNT(DISTINCT order_key)'}}}


def test_definition_answer_uses_vector_evidence_without_query_analysis():
    response = definition_response(request(), [found()])
    assert response.status == 'COMPLETED'
    assert '订单号去重计数' in response.answer
    assert response.analysis_plan is None and response.dataset_id is None
    assert [item.kind for item in response.evidence] == ['METRIC_DEFINITION']
    assert [step.stage for step in response.analysis_process] == ['KNOWLEDGE_RETRIEVAL']


@pytest.mark.parametrize('status', ['NOT_FOUND', 'AMBIGUOUS'])
def test_absent_or_ambiguous_definition_cannot_invent_an_answer(status):
    response = definition_response(request(), [{'input': '订单笔数', 'status': status}])
    assert response.status == 'SAFE_FALLBACK' and not response.evidence
    assert not response.dataset_id and 'COUNT' not in response.answer


def test_multiple_definition_results_keep_missing_topic_visible():
    response = definition_response(request(), [found(), {'input': '未登记指标', 'status': 'NOT_FOUND'}])
    assert response.status == 'PARTIAL_SUCCESS'
    assert '订单号去重计数' in response.answer and '未登记指标' in response.answer


def test_missing_definition_text_is_disclosed_instead_of_claiming_complete():
    item = found(); item['definition'].update(business_definition={}, calculation_rule={})
    response = definition_response(request(), [item])
    assert response.status == 'PARTIAL_SUCCESS' and '尚未登记' in response.answer


@pytest.mark.asyncio
async def test_http_definition_lookup_calls_only_vector_service_and_keeps_scope():
    client = SimpleNamespace(post=AsyncMock(return_value={'success': True,
        'semantic_model_id': 130, 'business_domain_ids': [205], 'results': [found()]}))
    settings = Settings(_env_file=None, env='test', asl_generator_base_url='http://vectors', semantic_base_url='http://sql')
    adapter = HttpSemanticAdapter(settings, client)
    result = await adapter.lookup_definitions(request(business_domain_ids=[205]), TrustedIdentity(tenant_id='t', user_id='u'))
    assert result == [found()]
    args = client.post.call_args.args
    assert args[:2] == ('http://vectors', '/vector/metric-definitions/lookup')
    assert args[2]['business_domain_ids'] == [205] and args[2]['metric_names'] == ['订单笔数']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['model', 'domain', 'record_model', 'record_domain', 'missing_topic'])
async def test_vector_definition_response_must_confirm_current_scope_and_topics(mutation):
    body = {'success': True, 'semantic_model_id': 130, 'business_domain_ids': [205], 'results': [found()]}
    if mutation == 'model': body['semantic_model_id'] = 131
    elif mutation == 'domain': body['business_domain_ids'] = []
    elif mutation == 'record_model': body['results'][0]['definition']['semantic_model_id'] = 131
    elif mutation == 'record_domain': body['results'][0]['definition']['business_domain_id'] = 206
    else: body['results'] = []
    adapter = HttpSemanticAdapter(Settings(_env_file=None, env='test'), SimpleNamespace(post=AsyncMock(return_value=body)))
    with pytest.raises(AdapterError):
        await adapter.lookup_definitions(request(business_domain_ids=[205]), TrustedIdentity(tenant_id='t', user_id='u'))


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


def test_stream_definition_skips_fixed_query_stages_and_models(monkeypatch):
    from test_api import TestClient, build_test_app
    app = build_test_app()
    with TestClient(app) as client:
        agent = app.state.container.orchestrator
        agent.adapters.semantic.lookup_definitions = AsyncMock(return_value=[found('销售额')])
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
    assert not complete['analysis_plan'] and all(e['kind'] == 'METRIC_DEFINITION' for e in complete['evidence'])
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
        return definition_response(request(), [found(chat.question)])
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
