"""Agent adapters -> real translator API/catalog/planner with offline storage."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import types
from urllib.parse import urlsplit

import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter, HttpSemanticAdapter
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, ChatRequest, MetricRef, PrimaryIntent, TrustedIdentity
from app.services.authorized_scope import bind_authorized_scope

IDENTITY=TrustedIdentity(tenant_id='scope-test',user_id='scope-test')


@pytest.fixture
def sql_backend(monkeypatch):
    root=Path(__file__).resolve().parents[1]
    sql_root=root/'sql-translator'
    if not sql_root.is_dir():
        sql_root=root.parent/'sql-translator'
    monkeypatch.syspath_prepend(str(sql_root))
    # The standalone translator's runtime module auto-loads its parent .env;
    # replace only that environment bootstrap before importing any service code.
    bootstrap=types.ModuleType('runtime_config')
    bootstrap.load_workspace_env=lambda:None
    bootstrap.env_bool=lambda name,default=False:default
    monkeypatch.setitem(sys.modules,'runtime_config',bootstrap)
    fixture=importlib.import_module('test_single_domain_execution')
    database=fixture.catalog_db.__wrapped__()
    db=next(database)
    backend=fixture.backend.__wrapped__(db,monkeypatch)
    yield fixture,backend
    database.close()


def request(domains=(205,),model=81):
    chat=ChatRequest(semantic_model_id=model,business_domain_ids=list(domains),application_id='scope-app',
                     conversation_id='single-domain-conversation',message_id='turn-1',question='查询销售额')
    value=CanonicalAnalysisRequest(conversation_id=chat.conversation_id,tenant_id=IDENTITY.tenant_id,
        user_id=IDENTITY.user_id,application_id=chat.application_id,original_question=chat.question,
        primary_intent=PrimaryIntent.METRIC_QUERY,metrics=[MetricRef(input='sales amount')])
    bind_authorized_scope(value,chat.authorized_semantic_scope)
    return value


class ServiceClient:
    def __init__(self, fixture, settings, *, tamper=None, time_context=None):
        self.fixture,self.settings,self.tamper=fixture,settings,tamper
        self.time_context=time_context
        self.calls=[]

    async def post(self,base,path,payload,**kwargs):
        self.calls.append((path,payload))
        if path==self.settings.asl_generator_path:
            # Only model transport is substituted. Oagnet's actual strict
            # retrieval/ASL contract has its own unchanged full regression suite.
            asl=self.fixture.asl()
            if self.time_context:
                value=json.loads(asl);value['time_context']=self.time_context
                asl=json.dumps(value)
            evidence=dict(producer='OAGNET',evidence_version='1.0',semantic_model_id=81,
                requested_business_domain_ids=[205],resolved_business_domain_ids=[205],
                selected_metrics=[dict(canonical_code='sales_amount',canonical_name='sales amount',
                    semantic_model_id=81,business_domain_id=205,sql_verified=True,
                    metadata_source='MYSQL_SEMANTIC_LAYER',calculation_formula='SUM(sales.amount)')],
                asl_signature='sha256:'+hashlib.sha256(asl.encode()).hexdigest())
            evidence['evidence_fingerprint']=HttpDataRetrievalAdapter._semantic_evidence_fingerprint(evidence)
            return dict(success=True,result=asl,semantic_model_id=81,business_domain_ids=[205],semantic_evidence=evidence)
        methods={self.settings.sql_translate_path:'_handle_translate',self.settings.sql_execute_path:'_handle_execute',
                 self.settings.semantic_resolve_path:'_handle_metric_resolve'}
        if path in methods:
            status,result=self.fixture.request_api(methods[path],payload,path=path)
        else:
            metric_id=path.split('/metrics/',1)[1].split('/')[0]
            status,result=self.fixture.request_api('_handle_metric_lineage',payload,path=path,arguments=(metric_id,))
        if status>=400:
            raise AdapterError(result.get('code','SQL_SERVICE_ERROR'),result.get('error','rejected'),status_code=status)
        if self.tamper and path==self.tamper[0]:
            result[self.tamper[1]]=self.tamper[2]
        return result

    async def get(self,base,path,**kwargs):
        self.calls.append((path,None))
        segments=urlsplit(path).path.split('/')
        metric_id,version=segments[-3],segments[-1]
        status,result=self.fixture.request_api('_handle_metric_definition',{},path=path,arguments=(metric_id,version))
        if status>=400:
            raise AdapterError(result.get('code','METRIC_UNAVAILABLE'),result.get('error','rejected'),status_code=status)
        return result


@pytest.mark.asyncio
async def test_single_domain_query_reaches_real_sql_planning_execution_and_dataset(sql_backend):
    fixture,(_,_,executed)=sql_backend
    settings=Settings(env='test')
    client=ServiceClient(fixture,settings)
    semantic=HttpSemanticAdapter(settings,client)
    retrieval=HttpDataRetrievalAdapter(settings,client)
    req=request()
    req.metrics=await semantic.resolve_metrics(req,81)
    assert req.metrics[0].metric_id=='81:sales_amount'
    result=await retrieval.query(req,IDENTITY,semantic_model_id=81,business_domain_id=205)
    assert result.dataset.row_count==1
    assert len(executed)==1 and executed[0][1]=='10'
    assert any('scope=' in path for path,_ in client.calls)
    execution=next(body for path,body in client.calls if path==settings.sql_execute_path)
    assert execution['business_domain_ids']==[205] and execution['asl']


@pytest.mark.parametrize('period',['2025年','2025年12月','2025年12月3日'])
def test_parsed_calendar_scope_is_not_an_implicit_business_entity(period):
    from app.intent import RuleBasedIntentClassifier
    req=RuleBasedIntentClassifier().classify('查询'+period+'销售额',IDENTITY,'calendar-contrast')
    assert req.time_range is not None
    assert period not in req.semantic_entity_mentions


@pytest.mark.parametrize('name',['2025年款商品','2025医院','TDC-3'])
def test_calendar_guard_preserves_product_and_institution_literals(name):
    from app.intent import RuleBasedIntentClassifier
    req=RuleBasedIntentClassifier().classify('查询'+name+'的销售额',IDENTITY,'calendar-contrast')
    assert any(name.removesuffix('商品') in mention for mention in req.semantic_entity_mentions)


@pytest.mark.asyncio
@pytest.mark.parametrize('intent',[PrimaryIntent.METRIC_DEFINITION,PrimaryIntent.DATA_LINEAGE])
async def test_explicit_metric_metadata_retains_scope_at_every_hop(sql_backend,intent):
    fixture,_=sql_backend
    settings=Settings(env='test');client=ServiceClient(fixture,settings)
    adapter=HttpSemanticAdapter(settings,client)
    req=request();req.primary_intent=intent
    metrics=await adapter.resolve_metrics(req,81)
    result=await adapter.scoped_metadata(req,metrics[0],IDENTITY)
    assert result.payload['business_domain_ids']==[205]
    assert result.payload['authorized_scope_fingerprint']==req.authorized_semantic_scope.fingerprint()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['translate','execute','resolve'])
@pytest.mark.parametrize('key,value',[('scope_contract_version',None),('business_domain_ids',[]),('semantic_model_id',82),('authorized_scope_fingerprint','other')])
async def test_agent_rejects_missing_or_foreign_translator_scope_proof(sql_backend,stage,key,value):
    fixture,(_,_,executed)=sql_backend
    settings=Settings(env='test')
    path={'translate':settings.sql_translate_path,'execute':settings.sql_execute_path,'resolve':settings.semantic_resolve_path}[stage]
    client=ServiceClient(fixture,settings,tamper=(path,key,value))
    req=request()
    with pytest.raises(AdapterError) as failure:
        if stage=='resolve':
            await HttpSemanticAdapter(settings,client).resolve_metrics(req,81)
        else:
            await HttpDataRetrievalAdapter(settings,client).query(req,IDENTITY,semantic_model_id=81,business_domain_id=205)
    assert failure.value.code=='SEMANTIC_SCOPE_UNCONFIRMED'
    if stage!='execute':assert executed==[]


@pytest.mark.asyncio
async def test_explicit_multi_domain_rejected_before_any_upstream(sql_backend):
    fixture,(_,_,executed)=sql_backend
    settings=Settings(env='test');client=ServiceClient(fixture,settings)
    with pytest.raises(AdapterError) as failure:
        await HttpDataRetrievalAdapter(settings,client).query(request((205,206)),IDENTITY,semantic_model_id=81,business_domain_id=None)
    assert failure.value.code=='EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED'
    assert client.calls==[] and executed==[]


@pytest.mark.asyncio
async def test_natural_language_orchestrator_single_domain_query_completes(sql_backend):
    from app.adapters.base import AdapterBundle
    from app.adapters.semantic_query import CompositeSemanticQueryTool
    from test_phase0c_scope_contract import service
    fixture,(_,_,executed)=sql_backend
    agent=service()
    client=ServiceClient(fixture,agent.settings,time_context={'type':'year','value':2025,'anchor':'sales.created_at'})
    semantic=HttpSemanticAdapter(agent.settings,client)
    retrieval=HttpDataRetrievalAdapter(agent.settings,client)
    previous=agent.adapters
    agent.adapters=AdapterBundle(semantic=semantic,retrieval=retrieval,knowledge=previous.knowledge,
        policy=previous.policy,analysis=previous.analysis,semantic_query=CompositeSemanticQueryTool(semantic,retrieval))
    chat=ChatRequest(semantic_model_id=81,business_domain_ids=[205],application_id='scope-app',
        conversation_id='single-domain-full-workflow',message_id='turn-1',question='查询2025年销售额')
    response=await agent.handle(chat,IDENTITY)
    assert response.status=='COMPLETED',(response.status,response.error_code,response.answer)
    assert response.clarification_questions==[]
    assert len(executed)==1 and executed[0][1]=='10'
