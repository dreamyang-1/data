import asyncio
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from exception_explainer.diagnostics import Diagnostics, begin, end, redact
from exception_explainer.model import ModelConfig, explain
from exception_explainer import integration


def mock_model(monkeypatch, *, content=None, error=None):
    captured = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            captured.append(kwargs['json'])
            if error:
                raise error
            evidence = json.loads(kwargs['json']['messages'][1]['content'])['evidence']
            output = content or {'explanation': '系统前后两次对查询类型的判断不一致，因此任务还没有进入查询阶段，需要维护人员核对这两步的衔接。',
                                 'evidence_ids': [evidence[-1]['id']], 'certainty': 'confirmed'}
            return httpx.Response(200, request=httpx.Request('POST', 'https://model.invalid'),
                json={'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)}}]})
    monkeypatch.setattr('exception_explainer.model.httpx.AsyncClient', Client)
    return captured


@pytest.mark.asyncio
async def test_parallel_request_and_child_task_isolation(caplog):
    caplog.set_level(logging.INFO)
    ready = asyncio.Event()
    async def request(number):
        state, token = begin(f'问题{number}')
        try:
            async def child():
                await ready.wait()
                logging.getLogger('app.child').warning('当前请求%s', number)
            task = asyncio.create_task(child())
            ready.set()
            await task
            return state
        finally:
            end(state, token)
    a, b = await asyncio.gather(request(1), request(2))
    assert '当前请求1' in str(a.payload()) and '当前请求2' not in str(a.payload())
    assert '当前请求2' in str(b.payload()) and '当前请求1' not in str(b.payload())
    logging.getLogger('app.child').error('请求外日志')
    assert '请求外日志' not in str(a.payload())


@pytest.mark.parametrize('raw,secret', [
    ('{"api_key": "credential123"}', 'credential123'),
    ("password='credential123'", 'credential123'),
    ('Bearer credential123', 'credential123'),
    ('http://user:credential123@internal.local/api?token=abc', 'internal.local'),
    ('SQL failed: SELECT salary FROM payroll', 'salary'),
    ('rows=[{"name":"private-person"}]', 'private-person'),
    ('/root/private/config.env', 'private'),
    ('192.168.200.99:8808 failed', '192.168'),
])
def test_redaction_before_truncation(raw, secret):
    assert secret not in redact(raw)


@pytest.mark.asyncio
async def test_exception_chain_logs_and_one_model_call(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    captured = mock_model(monkeypatch)
    state, token = begin('给出2025年订单平均金额最高的前五位销售人员，以及他们所在的销售公司')
    try:
        logging.getLogger('app.planning').warning("ANALYSIS_ROUTE_CHANGED_AFTER_COMPLETION:['comparison']->[]")
        state.record_progress({'stage': 'INTENT_RECOGNITION', 'status': 'RUNNING', 'rows': ['private']})
        try:
            try: raise ValueError('两次意图不一致')
            except ValueError as cause: raise RuntimeError('意图合同未通过校验') from cause
        except RuntimeError as exc: state.exception(exc)
        config = ModelConfig('https://model.invalid/v1', 'credential123', 'configured-model')
        answer = await explain(state, config, fallback='原错误')
        assert await explain(state, config, fallback='第二次') == answer
    finally:
        end(state, token)
    assert len(captured) == 1
    data = json.loads(captured[0]['messages'][1]['content'])
    assert data['question'] == state.question
    assert 'ANALYSIS_ROUTE_CHANGED' in str(data) and '两次意图不一致' in str(data)
    assert 'private' not in str(data) and 'credential123' not in str(data)
    assert '前后两次' in answer


@pytest.mark.asyncio
async def test_long_model_explanation_is_not_discarded_or_truncated(monkeypatch):
    text = '系统没有提取出销售总额指标，因此无法按销售总额筛选。' * 35
    captured = mock_model(monkeypatch, content={
        'explanation': text, 'evidence_ids': ['E1'], 'certainty': 'confirmed'})
    state = Diagnostics('查询问题'); state.exception(ValueError('销售总额指标没有提取出来'))
    result = await explain(state, ModelConfig('https://model.invalid', 'key', 'model'), fallback='原错误')
    assert result == text
    assert 'max_tokens' not in captured[0]


@pytest.mark.asyncio
async def test_long_fallback_is_not_truncated_when_model_is_unavailable():
    text = '没有提取出销售总额指标。' * 60
    state = Diagnostics('查询问题')
    assert await explain(state, None, fallback=text) == text


@pytest.mark.asyncio
async def test_long_explanation_still_redacts_sensitive_content(monkeypatch):
    text = '系统没有提取出销售总额指标。' * 40 + ' password=credential123'
    mock_model(monkeypatch, content={'explanation': text, 'evidence_ids': ['E1'], 'certainty': 'confirmed'})
    state = Diagnostics('问题'); state.exception(ValueError('指标缺失'))
    answer = await explain(state, ModelConfig('https://model.invalid', 'key', 'model'), fallback='失败')
    assert len(answer) > 500 and 'credential123' not in answer


@pytest.mark.parametrize('kind', ['connection', 'invalid_refs', 'invalid_json'])
@pytest.mark.asyncio
async def test_model_failure_preserves_original_error(monkeypatch, kind):
    if kind == 'connection': mock_model(monkeypatch, error=httpx.ConnectError('api_key=private'))
    else: mock_model(monkeypatch, content={'explanation': '错误说明', 'evidence_ids': ['E999'], 'certainty': 'confirmed'} if kind == 'invalid_refs' else 'invalid')
    state = Diagnostics('问题');state.exception(ValueError('字段类型不一致'))
    assert await explain(state, ModelConfig('https://model.invalid', 'key', 'model'), fallback='字段类型不一致') == '字段类型不一致'


@pytest.mark.asyncio
async def test_cancelled_model_call_propagates(monkeypatch):
    mock_model(monkeypatch, error=asyncio.CancelledError())
    state = Diagnostics('问题');state.exception(ValueError('出错'))
    with pytest.raises(asyncio.CancelledError):
        await explain(state, ModelConfig('https://model.invalid', 'key', 'model'), fallback='失败')


@pytest.mark.parametrize('status', ['COMPLETED', 'NEEDS_CLARIFICATION', 'NO_DATA', 'CANCELLED'])
@pytest.mark.asyncio
async def test_normal_outcomes_do_not_call_model(monkeypatch, status):
    async def forbidden(*args, **kwargs): raise AssertionError('not a failure')
    monkeypatch.setattr(integration, '_explain', forbidden)
    response = SimpleNamespace(status=status, task_results=[], extension_executions=[], reliability=None)
    assert await integration.explain_response(Diagnostics('问题'), None, response) is response


@pytest.mark.asyncio
async def test_partial_result_not_replaced_or_mutated(monkeypatch):
    from app.domain.models import AgentResponse, PrimaryIntent, TaskExecutionResult
    # Build through a genuine API-shaped result to protect data and attachments.
    response = AgentResponse(request_id=uuid4(), conversation_id='one', status='PARTIAL_SUCCESS',
        intent=PrimaryIntent.METRIC_QUERY, answer='已经返回的销售员表格',
        task_results=[TaskExecutionResult(task_id='task-1', question='查询金额', status='COMPLETED',
            intent=PrimaryIntent.METRIC_QUERY, answer='销售员表格'),
            TaskExecutionResult(task_id='task-2', question='查询公司', status='SAFE_FALLBACK',
            intent=PrimaryIntent.METRIC_QUERY, answer='公司关系未确认')])
    async def explanation(*args): return '金额查询已完成，公司查询因对应关系未确认而停止。'
    monkeypatch.setattr(integration, '_explain', explanation)
    result = await integration.explain_response(Diagnostics('问题'), None, response)
    assert result is not response and result.task_results == response.task_results
    assert result.status == 'PARTIAL_SUCCESS' and result.answer.startswith(response.answer)
    assert response.answer == '已经返回的销售员表格'


@pytest.mark.parametrize('path', ['/agent_chat', '/agent_chat/stream'])
def test_real_api_exception_explained_and_sse_outputs_answer(monkeypatch, path):
    from test_api import TestClient, build_test_app
    captured = []
    async def model(state, config, **kwargs):
        captured.append(state.payload())
        return '两次判断的查询类型不一致，任务未进入查询阶段。需要维护人员检查这两步的衔接。'
    monkeypatch.setattr(integration, 'explain', model)
    class Workflow:
        async def ainvoke(self, state):
            from app.services.progress import emit_progress
            await emit_progress('INTENT_RECOGNITION', 'RUNNING', '识别中')
            logging.getLogger('app.planning').warning('ANALYSIS_ROUTE_CHANGED_AFTER_COMPLETION:comparison->[]')
            raise RuntimeError('意图合同未通过校验')
    app = build_test_app()
    with TestClient(app) as client:
        object.__setattr__(app.state.container, 'workflow', Workflow())
        result = client.post(path, json={'semantic_model_id': 81, 'application_id': 'app1',
            'conversation_id': str(uuid4()), 'message_id': str(uuid4()), 'question': '查销售员和公司'})
    assert len(captured) == 1 and 'ANALYSIS_ROUTE_CHANGED' in str(captured)
    assert '两次判断' in result.text
    if path.endswith('stream'):
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
        kinds = [e['type'] for e in events]
        assert kinds[-4:] == ['updata_state', 'message_chunk', 'answer', 'error']
        assert 'complete' not in kinds
        assert events[-2]['status'] == 'SAFE_FALLBACK'
    else: assert result.status_code == 500


def test_request_diagnostics_are_bounded(caplog):
    caplog.set_level(logging.WARNING)
    state, token = begin('问题')
    try:
        for i in range(500):
            logging.getLogger('app.task').warning('%s:%s', i, 'x' * 3000)
            state.record_progress({'stage': 'EXECUTION', 'task_index': i})
    finally: end(state, token)
    assert len(state.warnings) == 16 and len(state.progress) == 32
    assert len(json.dumps(state.payload())) < 30000


def test_timeout_before_workflow_still_returns_explanation(monkeypatch):
    from test_api import TestClient, build_test_app
    async def model(*args, **kwargs): return '等待依赖服务回复时超时，本次任务未完成。'
    def timeout(*args, **kwargs): raise TimeoutError('服务未返回')
    monkeypatch.setattr(integration, 'explain', model)
    monkeypatch.setattr('app.api.hash_identifier', timeout)
    app = build_test_app()
    with TestClient(app) as client:
        result = client.post('/agent_chat', json={'semantic_model_id': 81, 'application_id': 'app1',
            'conversation_id': str(uuid4()), 'message_id': str(uuid4()), 'question': '查销售员和公司'})
    assert result.status_code == 504
    assert '等待依赖服务' in result.json()['detail']['message']
