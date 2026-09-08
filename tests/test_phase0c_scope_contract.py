"""Offline acceptance of current-backend scope enforcement and state isolation."""
import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter, HttpSemanticAdapter
from app.config import Settings
from app.domain.models import ChatRequest, PendingState, PrimaryIntent, TrustedIdentity
from app.domain.semantic_scope import AuthorizedSemanticScope
from app.intent import RuleBasedIntentClassifier
from app.main import create_app
from app.services.authorized_scope import bind_authorized_scope, state_scope_matches
from app.services.dataset_followup import scope_for_request
from app.services.orchestrator import DataAnalysisOrchestrator, ExplicitDatasetUnavailableError
from app.services.question_rewriter import HttpEntityAttributeSearcher
from app.stores import InMemorySessionStore
from minio_followup_store import DatasetScopeMismatch, MinioFollowupStore
from test_minio_followup_store import FakeMinio

IDENTITY = TrustedIdentity(tenant_id='tenant-a', user_id='user-a')
HEADERS = {'Authorization': 'Bearer phase0c-fixture-token', 'X-Tenant-Id': 'tenant-a', 'X-User-Id': 'user-a'}


def chat(**changes):
    values = dict(semantic_model_id=81, application_id='scope-app', conversation_id='scope-conversation',
                  message_id='current-message', question='那江苏呢？')
    values.update(changes)
    return ChatRequest(**values)


def canonical(current=None):
    current = current or chat()
    request = RuleBasedIntentClassifier().classify('上海最近一年销售额', IDENTITY, current.conversation_id)
    request.application_id = current.application_id
    bind_authorized_scope(request, current.authorized_semantic_scope)
    return request


def service():
    return DataAnalysisOrchestrator(settings=Settings(env='test', adapter_mode='mock', intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(), adapters=build_mock_adapters(), sessions=InMemorySessionStore())


def app(**changes):
    values = dict(env='test', adapter_mode='mock', intent_model_enabled=False,
                  business_question_collection_enabled=False, trusted_backend_token='phase0c-fixture-token')
    values.update(changes)
    return create_app(Settings(**values))


@pytest.mark.parametrize('model', [None, True, False, 0, -1, 81.0, '81'])
def test_model_requires_strict_positive_integer(model):
    with pytest.raises(ValidationError):
        chat(semantic_model_id=model)


@pytest.mark.parametrize('route', ['/agent_chat', '/agent_chat/stream', '/agent_chat/refresh', '/agent_chat/refresh/stream'])
def test_missing_model_rejected_before_execution(route):
    payload = chat().model_dump(mode='json')
    payload.pop('semantic_model_id')
    with TestClient(app(), headers=HEADERS) as client:
        response = client.post(route, json=payload)
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'REQUEST_SCOPE_INVALID'


@pytest.mark.parametrize('domains,mode', [([], 'MODEL_WIDE'), ([205], 'EXPLICIT_DOMAINS'), ([205,206], 'EXPLICIT_DOMAINS')])
def test_upstream_scope_modes(domains, mode):
    scope = chat(business_domain_ids=domains).authorized_semantic_scope
    assert scope.semantic_model_id == 81
    assert scope.business_domain_ids == tuple(domains)
    assert scope.scope_mode == mode
    assert scope.source == 'TRUSTED_UPSTREAM_BACKEND'
    with pytest.raises(ValidationError):
        scope.semantic_model_id = 82


def test_legacy_scalar_and_normalized_set_agree():
    assert chat(business_domain_ids=None).authorized_semantic_scope.scope_mode == 'MODEL_WIDE'
    assert chat(business_domain_id=205).business_domain_ids == [205]
    assert chat(business_domain_id=205, business_domain_ids=[205,205]).business_domain_ids == [205]
    assert chat(business_domain_ids=[206,205,206]).business_domain_ids == [205,206]
    for domains in [[], [206], [205,206]]:
        with pytest.raises(ValidationError):
            chat(business_domain_id=205, business_domain_ids=domains)


@pytest.mark.parametrize('domains', [[True], ['205'], [205.0], [0], [-1]])
def test_invalid_domain_scope_rejected(domains):
    with pytest.raises(ValidationError):
        chat(business_domain_ids=domains)


@pytest.mark.parametrize('settings,headers,code', [
    ({'trusted_backend_token': None}, HEADERS, 'UPSTREAM_SCOPE_TRUST_UNCONFIGURED'),
    ({}, {}, 'UPSTREAM_SCOPE_TRUST_INVALID'),
    ({}, {**HEADERS, 'Authorization':'Bearer wrong-token'}, 'UPSTREAM_SCOPE_TRUST_INVALID'),
    ({'allow_missing_trusted_identity_headers': True}, {'Authorization':HEADERS['Authorization']}, 'STATE_NAMESPACE_REQUIRED'),
])
def test_backend_trust_and_namespace_fail_closed(settings, headers, code):
    with TestClient(app(**settings), headers=headers) as client:
        response = client.post('/agent_chat', json=chat(question='你好').model_dump(mode='json'))
    assert response.status_code in {401,503}
    assert response.json()['detail']['code'] == code


def test_users_with_same_conversation_have_distinct_state():
    application = app()
    with TestClient(application, headers=HEADERS) as client:
        payload = chat(question='你好').model_dump(mode='json')
        first = client.post('/agent_chat', json=payload).json()
        second = client.post('/agent_chat', json=payload, headers={'X-User-Id':'user-b'}).json()
        assert first['request_id'] != second['request_id']
        assert {key[1] for key in application.state.container.sessions._responses} == {'user-a','user-b'}


SCOPE_CHANGES = [
    ({}, {'semantic_model_id':82}),
    ({}, {'business_domain_ids':[205]}),
    ({'business_domain_ids':[205]}, {}),
    ({'business_domain_ids':[206]}, {'business_domain_ids':[205]}),
    ({'database_id':10}, {'database_id':11}),
    ({'knowledge_base_names':['a']}, {'knowledge_base_names':['b']}),
]


@pytest.mark.parametrize('previous,current', SCOPE_CHANGES)
@pytest.mark.parametrize('store_kind', ['task_frame', 'last_request', 'pending'])
@pytest.mark.asyncio
async def test_incompatible_state_never_reaches_turn_admission(previous, current, store_kind):
    agent = service()
    old = canonical(chat(**previous))
    if store_kind == 'pending':
        old.missing_slots = ['metrics']
        await agent._request_clarification(old, 1)
    else:
        await getattr(agent.sessions, 'put_'+store_kind)(old)
    observed = []
    delegate = agent.turn_admission_gate.evaluate
    def capture(**kwargs):
        observed.append(kwargs.get('previous'))
        return delegate(**kwargs)
    agent.turn_admission_gate.evaluate = capture
    response = await agent.handle(chat(**current), IDENTITY)
    assert observed and all(value is None for value in observed)
    assert response.semantic_model_id == chat(**current).semantic_model_id
    assert not state_scope_matches(old, chat(**current))


@pytest.mark.asyncio
async def test_same_scope_followup_keeps_business_context():
    agent = service()
    old = canonical(chat(business_domain_ids=[205]))
    await agent.sessions.put_last_request(old)
    observed = []
    delegate = agent.turn_admission_gate.evaluate
    def capture(**kwargs):
        observed.append(kwargs.get('previous'))
        return delegate(**kwargs)
    agent.turn_admission_gate.evaluate = capture
    await agent.handle(chat(business_domain_ids=[205]), IDENTITY)
    assert observed[0] is not None
    assert observed[0].metrics == old.metrics


def test_parser_output_cannot_supply_or_expand_authorization():
    request = canonical(chat(semantic_model_id=82))
    bind_authorized_scope(request, chat(business_domain_ids=[205]).authorized_semantic_scope)
    assert request.semantic_model_id == 81
    assert request.business_domain_ids == [205]
    assert request.business_domain_selection_mode == 'EXPLICIT_DOMAINS'
    request.business_domain_ids = []
    with pytest.raises(AdapterError, match='scope'):
        HttpDataRetrievalAdapter._enforce_bound_scope(request, 81, None)


@pytest.mark.parametrize('previous,current', SCOPE_CHANGES)
def test_all_request_cache_and_dag_fingerprints_include_scope(previous, current):
    old, new = chat(**previous), chat(**current)
    for function in [DataAnalysisOrchestrator._request_fingerprint, DataAnalysisOrchestrator._repeat_query_fingerprint]:
        assert function(old, IDENTITY) != function(new, IDENTITY)
    assert DataAnalysisOrchestrator._dag_scope_fingerprint(old) != DataAnalysisOrchestrator._dag_scope_fingerprint(new)


@pytest.mark.asyncio
async def test_response_cache_miss_after_scope_change():
    agent = service()
    first = await agent.handle(chat(question='你好'), IDENTITY)
    second = await agent.handle(chat(question='你好', message_id='m2', business_domain_ids=[205]), IDENTITY)
    assert first.request_id != second.request_id
    assert second.requested_business_domain_ids == [205]


@pytest.mark.parametrize('previous,current', SCOPE_CHANGES)
def test_dataset_scope_mismatch_rejected_before_object_read(previous, current):
    client = FakeMinio()
    store = MinioFollowupStore(client, bucket='fixture')
    old_request = canonical(chat(**previous))
    old_scope, new_scope = scope_for_request(old_request), scope_for_request(canonical(chat(**current)))
    reference = store.save_dataset(scope=old_scope, columns=['value'], rows=[{'value':1}], snapshot_id='scope-fixture',
        data_as_of=datetime.now(timezone.utc), source_type='DATABASE_QUERY', source_ref='fixture',
        semantic_model_id=old_request.semantic_model_id, business_domain_ids=old_request.business_domain_ids)
    with pytest.raises(DatasetScopeMismatch):
        store.load_dataset(reference, current_scope=new_scope)
    assert client.get_calls == []
    assert store.load_dataset(reference, current_scope=old_scope).rows == ({'value':1},)


@pytest.mark.asyncio
async def test_explicit_dataset_selection_cannot_bypass_domain_scope():
    from test_conversation_result_followup import _ResultStore
    agent = service()
    agent.dataset_store = _ResultStore()
    old = canonical(chat(business_domain_ids=[206]))
    ref = agent.dataset_store.save_dataset(scope=scope_for_request(old), columns=['销售额'], rows=[{'销售额':1}],
        snapshot_id='s', data_as_of=datetime.now(timezone.utc), source_type='DATABASE_QUERY', source_ref='fixture',
        semantic_model_id=81, business_domain_ids=[206])
    await agent.sessions.put_dataset_reference(ref.to_dict(), recent_limit=5)
    new = canonical(chat(business_domain_ids=[205]))
    new.original_question = '只看前5条'
    new.source_dataset_id = ref.dataset_id
    new.assumptions.append('EXPLICIT_SOURCE_DATASET_SELECTION')
    with pytest.raises(ExplicitDatasetUnavailableError, match='DATASET_SCOPE_MISMATCH'):
        await agent._try_dataset_followup(new)


def generated(domains):
    asl = json.dumps({'subject':{'entity':'sales'}, 'metrics':[{'name':'sales_amount'}]})
    evidence = dict(producer='OAGNET', evidence_version='1.0', semantic_model_id=81,
        requested_business_domain_ids=domains, resolved_business_domain_ids=domains,
        selected_metrics=[dict(canonical_code='sales_amount', canonical_name='销售额', semantic_model_id=81,
            business_domain_id=domains[0] if domains else 205, sql_verified=True, metadata_source='MYSQL_SEMANTIC_LAYER',
            calculation_formula='SUM(sales.amount)')], asl_signature='sha256:'+hashlib.sha256(asl.encode()).hexdigest())
    evidence['evidence_fingerprint'] = HttpDataRetrievalAdapter._semantic_evidence_fingerprint(evidence)
    return dict(success=True, result=asl, semantic_model_id=81, business_domain_ids=domains, semantic_evidence=evidence)


class Client:
    def __init__(self, response):
        self.response, self.calls = response, []
    async def post(self, base, path, payload, **kwargs):
        self.calls.append((path, payload))
        return self.response
    async def get(self, *args, **kwargs):
        raise AssertionError('Model-wide metadata retrieval must not run for an explicit scope')


@pytest.mark.asyncio
@pytest.mark.parametrize('domains', [[], [205], [205,206]])
async def test_real_oagnet_adapter_transmits_and_confirms_exact_domain_set(domains):
    client = Client(generated(domains))
    adapter = HttpDataRetrievalAdapter(Settings(env='test'), client)
    request = canonical(chat(business_domain_ids=domains))
    if domains:
        with pytest.raises(AdapterError) as failure:
            await adapter.discover_metrics(request, IDENTITY, semantic_model_id=81,
                business_domain_id=domains[0] if len(domains)==1 else None)
        assert failure.value.code == ('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED' if len(domains)>1 else 'EXPLICIT_DOMAIN_NOT_SUPPORTED')
        assert client.calls == []
    else:
        discovery = await adapter.discover_metrics(request, IDENTITY, semantic_model_id=81, business_domain_id=None)
        assert discovery.metrics[0].metric_id == '81:sales_amount'
        assert client.calls[0][1]['business_domain_ids'] == []


@pytest.mark.parametrize('tamper', ['echo_model', 'echo_domains', 'resolved_domain', 'metric_model', 'metric_domain'])
@pytest.mark.asyncio
async def test_oagnet_scope_expansion_rejected_before_sql(tamper):
    response = generated([205,206])
    if tamper == 'echo_model': response['semantic_model_id'] = 82
    if tamper == 'echo_domains': response['business_domain_ids'] = []
    if tamper == 'resolved_domain': response['semantic_evidence']['resolved_business_domain_ids'] = [999]
    if tamper == 'metric_model': response['semantic_evidence']['selected_metrics'][0]['semantic_model_id'] = 82
    if tamper == 'metric_domain': response['semantic_evidence']['selected_metrics'][0]['business_domain_id'] = 999
    request = canonical(chat(business_domain_ids=[205,206]))
    with pytest.raises(AdapterError) as failure:
        HttpDataRetrievalAdapter._confirm_generated_scope(request, response)
    assert failure.value.code == 'ASL_SCOPE_INVALID'


@pytest.mark.asyncio
async def test_unscoped_external_metadata_contract_fails_closed():
    client = Client({})
    adapter = HttpSemanticAdapter(Settings(env='test'), client)
    with pytest.raises(AdapterError) as failure:
        await adapter.resolve_metrics(canonical(chat(business_domain_ids=[205])), 81)
    assert failure.value.code == 'EXPLICIT_DOMAIN_METADATA_NOT_SUPPORTED'
    assert client.calls == []


@pytest.mark.asyncio
async def test_explicit_display_endpoint_with_missing_domain_provenance_is_not_called():
    searcher = HttpEntityAttributeSearcher(base_url='http://offline.invalid', path='/entity-search', timeout_seconds=1, top_k=5, score_threshold=0)
    assert await searcher.resolve_display_slots([{'candidate_id':'x','slot':'field','value':'name'}],
        semantic_model_id=81, business_domain_ids=[205]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('foreign', [False,True])
async def test_entity_value_results_require_matching_domain(monkeypatch, foreign):
    observed = []
    async def handler(request):
        payload = json.loads(request.content)
        observed.append(payload)
        return httpx.Response(200, json={'semantic_model_id':81, 'business_domain_ids':[205],
            'matches':[{'business_domain_id':206 if foreign else 205, 'canonical_value':'candidate'}]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    searcher = HttpEntityAttributeSearcher(base_url='http://offline.invalid', path='/entity-search', timeout_seconds=1, top_k=5, score_threshold=0)
    if foreign:
        with pytest.raises(ValueError, match='SEMANTIC_SCOPE_MISMATCH'):
            await searcher.search('candidate', semantic_model_id=81, business_domain_id=205, business_domain_ids=[205])
    else:
        assert await searcher.search('candidate', semantic_model_id=81, business_domain_id=205, business_domain_ids=[205])
    assert observed[0]['business_domain_ids'] == [205]


@pytest.mark.asyncio
@pytest.mark.parametrize('previous,current', SCOPE_CHANGES)
async def test_dag_resume_rejects_incompatible_scope(previous, current):
    from app.domain.models import AtomicTask, TaskPlan
    agent = service()
    plan = TaskPlan(planner='DETERMINISTIC_RULE', tasks=[AtomicTask(task_id='task-1', question='查询销售额'), AtomicTask(task_id='task-2', question='查询订单笔数')])
    state = dict(resume_token_hash='unused', root_message_id='old', task_plan=plan.model_dump(mode='json'),
        awaiting_task_ids=['task-1'], state_version=1,
        scope_fingerprint=agent._dag_scope_fingerprint(chat(**previous)))
    response = await agent._resume_task_plan(chat(**current), IDENTITY, state)
    assert response.reliability.gates['dag_resume'] is False
    assert '访问范围' in response.answer


@pytest.mark.asyncio
@pytest.mark.parametrize('domains', [[205], [205,206]])
async def test_explicit_queries_fail_before_upstream_retrieval_with_reason_code(domains):
    from app.adapters.base import AdapterBundle
    from app.adapters.semantic_query import CompositeSemanticQueryTool
    agent = service()
    client = Client({})
    semantic = HttpSemanticAdapter(agent.settings, client)
    retrieval = HttpDataRetrievalAdapter(agent.settings, client)
    base = agent.adapters
    agent.adapters = AdapterBundle(semantic=semantic, retrieval=retrieval, knowledge=base.knowledge,
        policy=base.policy, analysis=base.analysis, semantic_query=CompositeSemanticQueryTool(semantic,retrieval))
    response = await agent.handle(chat(question='查询本月销售额', business_domain_ids=domains), IDENTITY)
    assert response.status == 'SAFE_FALLBACK'
    assert response.error_code == ('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED' if len(domains)>1 else 'EXPLICIT_DOMAIN_NOT_SUPPORTED')
    assert response.clarification_questions == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_model_wide_execution_and_asl_cache_respect_knowledge_scope():
    from test_http_adapters import StubClient, request
    translated = {'success':True,'sql':'SELECT SUM(amount) AS sales_amount'}
    executed = {'success':True,'sql':translated['sql'],'data':[{'销售额':100}], 'columns':['销售额'],'row_count':1}
    client = StubClient([generated([]),translated,executed,translated,executed,generated([]),translated,executed])
    adapter = HttpDataRetrievalAdapter(Settings(env='test',asl_plan_cache_ttl_seconds=300,asl_plan_cache_max_items=8),client)
    for knowledge in [['a'],['a'],['b']]:
        req = request()
        bind_authorized_scope(req, chat(knowledge_base_names=knowledge).authorized_semantic_scope)
        result = await adapter.query(req, IDENTITY, semantic_model_id=81, business_domain_id=None)
        assert result.dataset.row_count == 1
    paths = [call[1] for call in client.calls]
    assert paths.count('/agent/query') == 2
    assert paths.count('/api/execute') == 3
    for call in client.calls:
        if call[1] in {'/api/translate','/api/execute'}:
            assert call[2]['modelId'] == '81'
            assert call[2]['authorized_semantic_scope']['scope_mode'] == 'MODEL_WIDE'


def test_report_artifact_cannot_combine_datasets_from_other_scope():
    from app.services.report_export import DatasetReportExporter, ReportExportError
    client = FakeMinio()
    store = MinioFollowupStore(client, bucket='fixture')
    source = canonical(chat(business_domain_ids=[206]))
    reference = store.save_dataset(scope=scope_for_request(source), columns=['value'], rows=[{'value':1}],
        snapshot_id='s',data_as_of=datetime.now(timezone.utc),source_type='DATABASE_QUERY',source_ref='fixture')
    exporter = DatasetReportExporter(client, store, bucket='fixture')
    with pytest.raises(ReportExportError):
        exporter.export_many([('first',reference),('second',reference)],
            scope=scope_for_request(canonical(chat(business_domain_ids=[205]))),file_format='xlsx',title='Scope fixture')
    assert client.get_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('matching', [True,False])
async def test_completed_dag_checkpoint_cannot_supply_cross_scope_answer(matching):
    from test_task_dag import _StubOrchestrator, _Classifier
    from app.domain.models import AtomicTask, TaskPlan
    from app.planning import MultiQuestionPlanner
    settings = Settings(env='test', multi_question_model_enabled=False, analysis_synthesis_enabled=False)
    calls = []
    class Counting(_StubOrchestrator):
        async def _handle(self, current, identity):
            calls.append(current.question)
            return await super()._handle(current, identity)
    agent = Counting(settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(), task_planner=MultiQuestionPlanner(settings))
    plan = TaskPlan(planner='DETERMINISTIC_RULE',tasks=[AtomicTask(task_id='task-1',question='查询销售额'),AtomicTask(task_id='task-2',question='查询库存')])
    current = chat()
    old = await agent._handle(current, IDENTITY)
    calls.clear()
    await agent.sessions.put_dag_checkpoint(IDENTITY.tenant_id,IDENTITY.user_id,current.application_id,current.conversation_id,current.message_id,
        {'plan_fingerprint':hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
         'authorized_scope':chat(semantic_model_id=81 if matching else 82).authorized_semantic_scope.fingerprint(),
         'completed':{'task-1':old.model_dump(mode='json')}, 'conversations':{'task-1':'old'}})
    await agent._handle_task_plan(current, IDENTITY, plan)
    assert len(calls) == (1 if matching else 2)


def test_semantic_bindings_outside_current_grant_are_discarded():
    from app.domain.models import SemanticFilterBinding
    request = canonical()
    request.semantic_filter_bindings = [SemanticFilterBinding(filter_index=i,input_value='candidate',canonical_value='candidate',
        canonical_name='region',attribute_code='region.name',score=1,business_domain_id=domain)
        for i,domain in enumerate([205,206])]
    request.resolved_business_domain_ids = [205,206]
    bind_authorized_scope(request,chat(business_domain_ids=[205]).authorized_semantic_scope)
    assert [b.business_domain_id for b in request.semantic_filter_bindings] == [205]
    assert request.resolved_business_domain_ids == [205]


def test_model_wide_query_can_narrow_without_changing_authorization():
    request = canonical()
    HttpDataRetrievalAdapter._confirm_generated_scope(request,generated([205]),205)
    assert request.authorized_semantic_scope.scope_mode == 'MODEL_WIDE'
    assert request.business_domain_ids == []


@pytest.mark.asyncio
async def test_validated_semantic_recall_rejects_different_database_scope():
    from app.services.validated_query_recall import ValidatedQueryExample, ValidatedQueryRecall
    current = canonical(chat(database_id=10))
    entry = ValidatedQueryExample(fingerprint='a'*64,tenant_id=current.tenant_id,application_id=current.application_id,
        semantic_model_id=81,question=current.original_question,intent=current.primary_intent.value,
        authorized_semantic_scope_fingerprint=chat(database_id=11).authorized_semantic_scope.fingerprint())
    class Store:
        async def get_validated_query_examples(self,*args,**kwargs):
            return [entry.model_dump(mode='json')]
    assert await ValidatedQueryRecall(Store()).recall(current,semantic_model_id=81) == []


def test_spreadsheet_import_requires_and_preserves_scope():
    from app.api import SpreadsheetImportRequest
    with pytest.raises(ValidationError):
        SpreadsheetImportRequest(application_id='app',conversation_id='c',object_name='fixture.csv')
    request = SpreadsheetImportRequest(application_id='app',conversation_id='c',object_name='fixture.csv',semantic_model_id=81,
        business_domain_id=205,database_id=10,knowledge_base_names=['sales'])
    scope = request._scope_chat().authorized_semantic_scope
    assert scope.business_domain_ids == (205,)
    assert scope.database_id == 10 and scope.knowledge_base_names == ('sales',)


@pytest.mark.asyncio
async def test_metadata_fallback_preserves_scope_error_without_clarification():
    from app.adapters.base import AdapterBundle
    agent = service()
    client = Client({})
    base = agent.adapters
    agent.adapters = AdapterBundle(semantic=HttpSemanticAdapter(agent.settings,client),retrieval=base.retrieval,
        knowledge=base.knowledge,policy=base.policy,analysis=base.analysis)
    request = canonical(chat(business_domain_ids=[205]))
    request.primary_intent = PrimaryIntent.METRIC_DEFINITION
    response = await agent._metadata_answer(request,IDENTITY,81)
    assert response.error_code == 'EXPLICIT_DOMAIN_METADATA_NOT_SUPPORTED'
    assert not response.clarification_questions
    assert client.calls == []
