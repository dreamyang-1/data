"""ASL dependency failures remain failures, not user clarification or retries forever."""
import httpx
import pytest

from app.adapters.base import AdapterError
from app.adapters.http import PlatformHttpClient
from app.config import Settings
from app.domain.models import ChatRequest, TrustedIdentity
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import emit_progress, progress_scope


CODES = [
    ('ASL_BINDING_TIMEOUT',504,True), ('ASL_BINDING_UNAVAILABLE',503,True),
    ('ASL_BINDING_AUTH_FAILED',502,False), ('ASL_BINDING_RESPONSE_INVALID',502,False),
    ('ASL_BINDING_FAILED',503,False), ('ASL_BINDING_RATE_LIMITED',429,True),
    ('ASL_UPSTREAM_RATE_LIMITED',429,True), ('ASL_CAPACITY_EXHAUSTED',429,True),
    ('ASL_GENERATION_DEADLINE_EXCEEDED',504,True),
]


@pytest.mark.parametrize('code,status,retryable',CODES)
def test_binding_failure_explains_system_reason_without_user_candidates(code,status,retryable):
    error=AdapterError('DEPENDENCY_UNAVAILABLE','private upstream message',status_code=status,
        upstream_code=code,details={'binding_stage':'catalog_binding','error_type':'TimeoutError',
                                  'password':'private password','candidates':['unverified']})
    message=DataAnalysisOrchestrator._dependency_message(error)
    assert '不是用户参数缺失' in message and '尚未生成或执行 SQL' in message
    assert '请补充' not in message and '请选择' not in message and '序号' not in message
    assert 'private' not in message and 'unverified' not in message


@pytest.mark.asyncio
@pytest.mark.parametrize('code,status,retryable',CODES)
async def test_retry_policy_is_bounded_and_deterministic_errors_are_not_retried(monkeypatch,code,status,retryable):
    calls=[]
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,method,path,**kw):
            calls.append(1)
            return httpx.Response(status,json={'success':False,'detail':{'code':code,
                'details':{'binding_stage':'catalog_binding','error_type':'TimeoutError'}}},
                request=httpx.Request(method,'http://semantic.test'+path))
    monkeypatch.setattr('app.adapters.http.httpx.AsyncClient',Client)
    client=PlatformHttpClient(Settings(http_max_retries=1,http_retry_backoff_seconds=0))
    with pytest.raises(AdapterError) as caught:
        await client.post('http://semantic.test','/agent/query',{},retryable=True)
    assert len(calls) == (2 if retryable else 1)
    assert caught.value.upstream_code == code and caught.value.retryable == retryable
    assert caught.value.details['binding_stage'] == 'catalog_binding'


@pytest.mark.asyncio
@pytest.mark.parametrize('code,status,retryable',CODES[:5])
async def test_service_failure_has_no_pending_no_empty_template_and_preserves_stage_order(code,status,retryable):
    from test_analysis_orchestration import service
    orchestrator=service()
    async def fail(*args,**kw):
        await emit_progress('ASL_GENERATION','STARTED','正在绑定结构化参数。')
        raise AdapterError('DEPENDENCY_UNAVAILABLE','sanitized',upstream_code=code,
                           status_code=status,retryable=retryable)
    orchestrator.adapters.retrieval.query=fail
    events=[]
    with progress_scope(events.append):
        response=await orchestrator.handle(ChatRequest(application_id='test',conversation_id=code,
            message_id='one',question='查询2025年销售总额',semantic_model_id=1),
            TrustedIdentity(tenant_id='test',user_id='test'))
    assert response.status == 'SAFE_FALLBACK'
    assert not response.clarification_questions and not response.missing_slots
    assert '不是用户参数缺失' in response.answer
    assert all(word not in response.answer for word in ['概况总结','关键发现','业务提示'])
    # TASK_PLANNING is derived by the SSE presentation layer, not emitted by
    # the raw orchestrator. Verify that public boundary separately below.
    order=['INTENT_RECOGNITION','ASL_GENERATION','SQL_EXECUTION',
           'RELIABILITY_CHECK','INSIGHT_ANALYSIS','FINAL_OUTPUT']
    stages=list(dict.fromkeys(e['stage'] for e in events if e['stage'] in order))
    assert stages.index('INTENT_RECOGNITION') < stages.index('ASL_GENERATION')
    assert [order.index(s) for s in stages] == sorted(order.index(s) for s in stages)
    assert 'SQL_EXECUTION' not in stages and 'INSIGHT_ANALYSIS' not in stages


def test_public_binding_failure_keeps_planning_before_parsing_and_final_output():
    import json
    from test_api import TestClient, build_test_app
    app=build_test_app()
    with TestClient(app) as client:
        async def fail(*args,**kw):
            await emit_progress('ASL_GENERATION','STARTED','正在绑定结构化参数。')
            raise AdapterError('DEPENDENCY_UNAVAILABLE','sanitized',
                               upstream_code='ASL_BINDING_TIMEOUT',status_code=504)
        app.state.container.adapters.retrieval.query=fail
        response=client.post('/agent_chat/stream',json={'semantic_model_id':81,
            'conversation_id':'binding-error-public-order','application_id':'app1',
            'message_id':'one','question':'查询2025年销售总额'})
    assert response.status_code == 200
    events=[json.loads(block.removeprefix('data: '))
            for block in response.text.strip().split('\n\n')]
    def position(stage):
        return next(i for i,e in enumerate(events) if e.get('type')=='message_chunk'
                    and e.get('meta',{}).get('stage')==stage)
    final=next(i for i,e in enumerate(events) if e.get('step')=='output')
    assert position('INTENT_RECOGNITION') < position('TASK_PLANNING') < position('ASL_GENERATION') < final
    answer=''.join(e.get('content','') for e in events if e.get('step')=='output')
    assert '不是用户参数缺失' in answer and '请补充' not in answer
    assert all(word not in answer for word in ['概况总结','关键发现','业务提示'])
