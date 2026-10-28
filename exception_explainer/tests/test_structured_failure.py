import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.domain.models import AgentResponse, CanonicalAnalysisRequest, PlannerExtraction, PrimaryIntent, SemanticAmbiguity
from exception_explainer import integration, semantic_reference
from exception_explainer.diagnostics import begin, end
from test_explainer import mock_model


SCOPE = {'semantic_model_id': 130, 'business_domain_ids': [], 'scope_mode': 'MODEL_WIDE'}
SETTINGS = SimpleNamespace(intent_model_enabled=True, intent_model_api_key=SecretStr('test-key'),
                           intent_model_base_url='https://model.invalid/v1', intent_model_name='test')


def blocked_request(*, model=130, kind='context', phrase='时间字段'):
    request = CanonicalAnalysisRequest(conversation_id='test', tenant_id='test', user_id='test',
        original_question='查询2025年销售员及所属公司', primary_intent=PrimaryIntent.DETAIL_QUERY,
        semantic_model_id=model, missing_slots=['semantic_ambiguity'],
        semantic_ambiguities=[SemanticAmbiguity(type=kind, phrase=phrase,
            question='结构化参数【time_context】：尚未取得已登记的时间字段', affected_slots=['context'])])
    request._planner_extraction = PlannerExtraction(structured={'实体': ['业务员'], '指标': [],
        '时间范围': '2025年', '过滤条件': [], 'rows': [{'name': 'private-result'}]})
    return request


@pytest.mark.asyncio
async def test_structured_pending_gets_model_reason_and_reference_without_changing_choices(monkeypatch):
    captured = mock_model(monkeypatch)
    async def reference(settings, scope):
        assert scope == SCOPE
        return {'available': True, 'content': '业务日期对应销售订单的订单日期；公司通过业务员关联。'}
    monkeypatch.setattr(integration, 'load_reference', reference)
    state, token = begin('查询2025年销售员及所属公司', scope=SCOPE)
    try:
        request = blocked_request()
        integration.record_parameter_blocker(request, source_stage='STRUCTURED_ASL_BINDING')
        response = AgentResponse(request_id=uuid4(), conversation_id='test', status='NEEDS_CLARIFICATION',
            intent=PrimaryIntent.DETAIL_QUERY, answer='请确认时间字段：订单日期、发货日期',
            missing_slots=['semantic_ambiguity'], clarification_questions=['请选择时间字段'],
            clarification_items=[{'slot': 'semantic_ambiguity', 'title': '时间字段', 'question': '请选择时间字段',
                                  'options': ['订单日期', '发货日期']}], dag_resume_token='existing-resume')
        result = await integration.explain_response(state, SETTINGS, response)
    finally:
        end(state, token)
    assert len(captured) == 1
    evidence = json.loads(captured[0]['messages'][1]['content'])
    assert evidence['semantic_reference']['content'].startswith('业务日期')
    blocker = next(e['detail'] for e in evidence['evidence'] if e['kind'] == 'structured_blocker')
    assert blocker['parameters']['时间范围'] == '2025年'
    assert blocker['missing_slots'] == ['semantic_ambiguity']
    assert 'private-result' not in json.dumps(evidence)
    assert result.answer != response.answer and response.answer not in result.answer
    assert '前后两次' in result.answer
    assert result.model_dump(exclude={'answer'}) == response.model_dump(exclude={'answer'})


@pytest.mark.asyncio
async def test_recovered_blocker_does_not_explain_success(monkeypatch):
    async def forbidden(*args): raise AssertionError('recovered blocker')
    monkeypatch.setattr(integration, '_explain', forbidden)
    state, token = begin('问题', scope=SCOPE)
    try:
        integration.record_parameter_blocker(blocked_request(), source_stage='STRUCTURED_ASL_BINDING')
        response = AgentResponse(request_id=uuid4(), conversation_id='test', status='COMPLETED',
                                 intent=PrimaryIntent.DETAIL_QUERY, answer='查询成功')
        assert await integration.explain_response(state, SETTINGS, response) is response
    finally:
        end(state, token)


@pytest.mark.asyncio
async def test_long_original_explanation_is_preserved_when_model_unavailable():
    state = integration.Diagnostics('问题', scope=SCOPE)
    state.blockers.append({'structured': True})
    answer = '系统没有提取出销售额指标。' * 60
    response = AgentResponse(request_id=uuid4(), conversation_id='test', status='NEEDS_CLARIFICATION',
                             intent=PrimaryIntent.DETAIL_QUERY, answer=answer)
    settings = SimpleNamespace(intent_model_enabled=False, intent_model_api_key=None)
    result = await integration.explain_response(state, settings, response)
    assert result is response and result.answer == answer


def test_other_model_blocker_is_not_collected():
    state, token = begin('问题', scope=SCOPE)
    try:
        integration.record_parameter_blocker(blocked_request(model=81), source_stage='STRUCTURED_ASL_BINDING')
        assert not state.blockers
    finally:
        end(state, token)


def test_other_domain_blocker_is_not_collected():
    state, token = begin('问题', scope={**SCOPE, 'business_domain_ids': [296], 'scope_mode': 'EXPLICIT_DOMAINS'})
    try:
        request = blocked_request()
        request.business_domain_ids = [297]
        integration.record_parameter_blocker(request, source_stage='STRUCTURED_ASL_BINDING')
        assert not state.blockers
    finally:
        end(state, token)


@pytest.mark.parametrize('mode', ['available', 'unavailable', 'exception'])
@pytest.mark.asyncio
async def test_reference_uses_current_model_and_degrades_without_guessing(monkeypatch, mode):
    async def load(settings, model):
        assert model == 130
        if mode == 'exception': raise ConnectionError('private')
        return {'available': mode == 'available', 'source': 'semantic_model_130.md',
                'content': '# 语义\n业务日期、订单日期\npassword=private\n'}
    monkeypatch.setattr(semantic_reference, 'load_semantic_description', load)
    result = await semantic_reference.load_reference(None, SCOPE)
    assert result['available'] == (mode == 'available')
    assert 'private' not in json.dumps(result)
    if mode == 'available': assert '订单日期' in result['content']


@pytest.mark.asyncio
async def test_explicit_domain_does_not_include_other_domains_or_overview(monkeypatch):
    async def load(*args):
        return {'available': True, 'content': '# 总览\nsecret-overview\n'
                '### 业务域 ：医药销售域\n#### 指标\n销售总额\n'
                '### 业务域 ：薪资域\nprivate-payroll\n## 通用附录\nprivate-appendix'}
    monkeypatch.setattr(semantic_reference, 'load_semantic_description', load)
    monkeypatch.setattr(semantic_reference, '_domain_name', lambda *_: '医药销售域')
    scope = {**SCOPE, 'business_domain_ids': [296], 'scope_mode': 'EXPLICIT_DOMAINS'}
    result = await semantic_reference.load_reference(None, scope)
    assert result['available'] and '销售总额' in result['content']
    assert 'private' not in result['content'] and 'secret-overview' not in result['content']
    monkeypatch.setattr(semantic_reference, '_domain_name', lambda *_: '无法确认的域')
    assert not (await semantic_reference.load_reference(None, scope))['available']


@pytest.mark.parametrize('scope', [None, {**SCOPE, 'semantic_model_id': '130'},
    {**SCOPE, 'business_domain_ids': [296, 297], 'scope_mode': 'EXPLICIT_DOMAINS'}])
@pytest.mark.asyncio
async def test_invalid_or_multiple_scope_never_loads_a_document(monkeypatch, scope):
    async def forbidden(*args): raise AssertionError('must not load')
    monkeypatch.setattr(semantic_reference, 'load_semantic_description', forbidden)
    assert not (await semantic_reference.load_reference(None, scope))['available']


@pytest.mark.parametrize('path', ['/agent_chat', '/agent_chat/stream'])
def test_actual_clarification_gate_and_api_explain_structured_stop(monkeypatch, path):
    from test_api import TestClient, build_test_app
    captured = mock_model(monkeypatch, content={
        'explanation': '查询已经包含2025年，但整理后的条件还没有绑定到具体的时间字段，因此停在解析校验。',
        'evidence_ids': ['E1'], 'certainty': 'confirmed'})
    async def reference(settings, scope):
        assert scope['semantic_model_id'] == 130
        return {'available': True, 'content': '订单日期是销售订单的时间字段。'}
    monkeypatch.setattr(integration, 'load_reference', reference)
    app = build_test_app(intent_model_enabled=True, intent_model_api_key='test-key')
    original = []
    class Workflow:
        async def ainvoke(self, state):
            request = blocked_request(kind='time_anchor')
            request.semantic_ambiguities[0].candidates = ['订单日期', '发货日期']
            response = await app.state.container.orchestrator._request_clarification(
                request, 1, source_stage='STRUCTURED_ASL_BINDING')
            original.append(response.model_dump(mode='json'))
            return {'response': response}
    with TestClient(app) as client:
        object.__setattr__(app.state.container, 'workflow', Workflow())
        result = client.post(path, json={'semantic_model_id': 130, 'application_id': 'app1',
            'conversation_id': str(uuid4()), 'message_id': str(uuid4()), 'question': '查询2025年销售员及所属公司'})
    assert result.status_code == 200
    assert len(captured) == 1 and '没有绑定到具体的时间字段' in result.text
    payload = json.loads(captured[0]['messages'][1]['content'])
    assert any(e['kind'] == 'structured_blocker' for e in payload['evidence'])
    if path == '/agent_chat':
        body = result.json()
        assert body['status'] == 'NEEDS_CLARIFICATION'
        assert body['clarification_items'] == original[0]['clarification_items']
        assert body['missing_slots'] == original[0]['missing_slots']
    else:
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
        assert any(e.get('type') == 'answer' and '没有绑定到具体的时间字段' in e.get('content', '') for e in events)
        assert not any(e.get('meta', {}).get('stage') == 'SQL_EXECUTION' for e in events)
