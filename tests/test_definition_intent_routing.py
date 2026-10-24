import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models import ChatRequest, PlannerExtraction, PrimaryIntent, TrustedIdentity
from app.planning.compound_question import CompoundJudgment
from app.services.progress import progress_scope, emit_progress
from test_workflow import make_orchestrator


QUESTION = '当前语义模型中建立了哪些实体'
IDENTITY = TrustedIdentity(tenant_id='t', user_id='u')


def chat():
    result = ChatRequest(semantic_model_id=130, business_domain_ids=[296],
        application_id='app', conversation_id='definition-intent', message_id='m1', question=QUESTION)
    result._completed_question_execution = True
    return result


def direct_agent():
    agent = make_orchestrator()
    agent.settings.surface_asl_execution_enabled = True
    agent.analysis_synthesizer = SimpleNamespace(synthesize_definition=AsyncMock(return_value='已登记实体：医院、销售订单。'))
    agent.adapters.semantic.lookup_definitions = AsyncMock(side_effect=AssertionError('metric lookup is not a prerequisite'))
    agent.adapters.query.query_surface = AsyncMock(side_effect=AssertionError('definition must not query'))
    agent._classify = AsyncMock(side_effect=AssertionError('recognized intent must not be classified again'))
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['intent_judgment', 'task_intent', 'definition_child'])
async def test_recognized_definition_bypasses_query_normalization_without_metrics(source):
    agent, turn = direct_agent(), chat()
    if source == 'intent_judgment':
        turn._compound_judgment = CompoundJudgment(is_compound=False, intent='解释口径')
    elif source == 'task_intent':
        turn._planner_extraction = PlannerExtraction(intent=PrimaryIntent.METRIC_DEFINITION)
    else:
        turn._dag_definition_direct = True
    events = []
    with progress_scope(events.append):
        response = await agent._handle(turn, IDENTITY)
    assert response.status == 'COMPLETED' and response.intent == PrimaryIntent.METRIC_DEFINITION
    assert response.requested_business_domain_ids == [296] and response.semantic_model_id == 130
    assert response.analysis_plan is None and response.dataset_id is None and not response.missing_slots
    agent.analysis_synthesizer.synthesize_definition.assert_awaited_once_with(
        QUESTION, intent='METRIC_DEFINITION', definitions=[], semantic_model_id=130)
    agent.adapters.semantic.lookup_definitions.assert_not_awaited()
    agent.adapters.query.query_surface.assert_not_awaited()
    agent._classify.assert_not_awaited()
    assert not events


@pytest.mark.asyncio
async def test_surface_classifier_definition_does_not_fall_back_to_query_pipeline():
    from app.domain.models import CanonicalAnalysisRequest
    agent, turn = direct_agent(), chat()
    request = CanonicalAnalysisRequest(conversation_id=turn.conversation_id, tenant_id='t', user_id='u',
        original_question=QUESTION, primary_intent=PrimaryIntent.METRIC_DEFINITION, missing_slots=['metric'])
    response = await agent._handle_surface_query(turn, IDENTITY, request=request)
    assert response.status == 'COMPLETED' and not response.clarification_questions
    agent.adapters.query.query_surface.assert_not_awaited()


@pytest.mark.asyncio
async def test_definition_model_failure_does_not_trigger_metric_clarification():
    agent, turn = direct_agent(), chat()
    turn._compound_judgment = CompoundJudgment(is_compound=False, intent='解释口径')
    agent.analysis_synthesizer.synthesize_definition.side_effect = RuntimeError('model unavailable')
    response = await agent._handle(turn, IDENTITY)
    assert response.status == 'SAFE_FALLBACK' and response.intent == PrimaryIntent.METRIC_DEFINITION
    assert not response.clarification_questions and '指标' not in response.answer
    agent.adapters.semantic.lookup_definitions.assert_not_awaited()


@pytest.mark.asyncio
async def test_definition_model_receives_intent_completed_question_and_current_document(monkeypatch):
    from app.analysis.synthesis import QwenAnalysisSynthesizer, DEFINITION_ANSWER_PROMPT
    from test_analysis_synthesis import settings, transport_for
    reference = {'available': True, 'source': 'semantic_model_130.md', 'content': '实体：医院、销售订单'}
    load = AsyncMock(return_value=reference)
    monkeypatch.setattr('app.analysis.synthesis._semantic_reference', load)
    calls = []
    answer = await QwenAnalysisSynthesizer(settings(), transport_for({'answer': '医院、销售订单'}, calls)).synthesize_definition(
        QUESTION, semantic_model_id=130, intent='METRIC_DEFINITION')
    body = json.loads(calls[0]['messages'][1]['content'])
    assert body['intent'] == 'METRIC_DEFINITION' and body['completed_question'] == QUESTION
    assert body['semantic_reference'] == reference and len(calls) == 1
    assert answer == '医院、销售订单' and '问哪些实体就列实体' in DEFINITION_ANSWER_PROMPT
    assert 'tools' not in calls[0]
    load.assert_awaited_once()


def test_definition_stream_has_no_planning_or_query_stages(monkeypatch):
    from test_api import TestClient, build_test_app
    app = build_test_app()
    with TestClient(app) as client:
        agent = app.state.container.orchestrator
        original = agent.handle
        async def recognized(turn, identity):
            turn._compound_judgment = CompoundJudgment(is_compound=False, intent='解释口径')
            turn._completed_question_execution = True
            turn._intent_context_progress_emitted = True
            await emit_progress('INTENT_RECOGNITION', 'RUNNING', '意图：解释口径。', definition_direct=True)
            return await original(turn, identity)
        monkeypatch.setattr(agent, 'handle', recognized)
        agent.analysis_synthesizer = SimpleNamespace(synthesize_definition=AsyncMock(return_value='已登记实体：医院、销售订单。'))
        planner = agent.task_planner = SimpleNamespace(plan_question=AsyncMock(side_effect=AssertionError('must not plan')))
        agent.adapters.semantic.lookup_definitions = AsyncMock(side_effect=AssertionError('must not lookup metrics'))
        response = client.post('/agent_chat/stream', json={'semantic_model_id': 130, 'application_id': 'app',
            'conversation_id': 'definition-entities', 'message_id': 'm1', 'question': QUESTION})
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
    complete = next(event for event in events if event.get('type') == 'complete')
    assert complete['status'] == 'COMPLETED' and complete['intent'] == 'METRIC_DEFINITION'
    stages = {event.get('meta', {}).get('stage') for event in events if event.get('type') == 'message_chunk'}
    assert not {'TASK_PLANNING', 'ASL_GENERATION', 'SQL_EXECUTION', 'DATA_RETRIEVAL', 'RELIABILITY_CHECK', 'INSIGHT_ANALYSIS'} & stages
    assert 'FINAL_OUTPUT' in stages
    planner.plan_question.assert_not_awaited()
    assert '缺少要计算的业务指标' not in response.text
