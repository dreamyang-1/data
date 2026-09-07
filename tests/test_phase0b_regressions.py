"""Contrasts, real Legacy multi-turn flows and clarification safety gates."""
from datetime import date

import pytest

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterError
from app.config import Settings
from app.domain.models import (
    ChatRequest, LegacyLineageTarget, PendingState, PrimaryIntent, SemanticAmbiguity,
    TrustedIdentity, TurnRelation,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.services.legacy_guards import apply_catalog_display_default, lineage_target_present
from app.services.dataset_followup import plan_dataset_followup
from app.stores import InMemorySessionStore
from test_phase0b_critical import parse, turn, names, regions

IDENTITY = TrustedIdentity(tenant_id='fixture-tenant', user_id='fixture-user')


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Fixed(date):
        @classmethod
        def today(cls): return cls(2026, 9, 7)
    monkeypatch.setattr('app.intent.classifier.date', Fixed)


def service():
    return DataAnalysisOrchestrator(settings=Settings(env='test', adapter_mode='mock', intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(), adapters=build_mock_adapters(), sessions=InMemorySessionStore())


@pytest.mark.parametrize('utterances,expected_metrics,expected_regions', [
    (['销售额和销售量', '再加订单笔数'], ['销售额','销售量','订单笔数'], []),
    (['上海最近一年销售额', '换成江苏'], ['销售额'], ['江苏省']),
    (['销售额和订单笔数', '不要订单笔数'], ['销售额'], []),
    (['上海最近一年销售额', '不限地区', '按季度'], ['销售额'], []),
])
@pytest.mark.asyncio
async def test_real_orchestrator_slot_operations(utterances, expected_metrics, expected_regions):
    agent = service()
    catalog = {'销售额':'amount', '销售量':'quantity', '订单笔数':'order_count'}
    async def resolve_fixture_metrics(request, semantic_model_id):
        return [m.model_copy(update={'metric_id':'fixture:'+catalog[m.input], 'canonical_name':m.input, 'version':'fixture-v1'}) for m in request.metrics]
    agent.adapters.semantic.resolve_metrics=resolve_fixture_metrics
    requests=[]
    delegate=agent.adapters.retrieval.query
    async def capture(request, identity, **kw):
        requests.append(request.model_copy(deep=True))
        return await delegate(request, identity, **kw)
    agent.adapters.retrieval.query=capture
    for number,text in enumerate(utterances):
        response=await agent.handle(ChatRequest(application_id='fixture-app', conversation_id='slot-flow', message_id=f'm{number}', question=text), IDENTITY)
        assert response.status != 'NEEDS_CLARIFICATION', response.answer
    assert len(requests) >= 2
    assert names(requests[-1]) == expected_metrics
    assert regions(requests[-1]) == expected_regions


@pytest.mark.parametrize('before,text,expected', [
    ('销售额和销售量','再加订单笔数',['销售额','销售量','订单笔数']),
    ('销售额和销售量','换成订单笔数',['订单笔数']),
    ('销售额和订单笔数','不要订单笔数',['销售额']),
    ('销售额和订单笔数','再加订单笔数',['销售额','订单笔数']),
])
def test_single_merge_operation_contrasts(before,text,expected):
    result=RuleBasedIntentClassifier().merge_clarification(parse(before),text)
    assert names(result)==expected


def test_clear_contrast_new_region_explicitly_reopens_scope():
    rules=RuleBasedIntentClassifier()
    cleared=rules.merge_clarification(parse('上海销售额'),'不限地区')
    assert not regions(cleared)
    reopened,_=turn(cleared,'换成江苏')
    assert regions(reopened)==['江苏省']
    assert not reopened.cleared_filter_families


def test_metric_add_is_idempotent_and_order_does_not_drop_old_metrics():
    first,_=turn(parse('销售额'),'再加订单笔数')
    second,_=turn(first,'再加订单笔数')
    assert names(first)==names(second)==['销售额','订单笔数']


@pytest.mark.parametrize('kind',['METRIC','FIELD','COLUMN','TABLE','ENTITY','DATASET'])
def test_lineage_targets_are_intent_specific(kind):
    request=parse('查询数据血缘')
    request.metrics=[]
    request.lineage_target=LegacyLineageTarget(kind=kind,name='fixture-target')
    assert lineage_target_present(request)
    assert 'metric' not in RuleBasedIntentClassifier().required_missing_slots(request)


@pytest.mark.parametrize('policy', [
    {}, {'entity':'医院','version':'v1','default_display_attributes':['医院名称'],'allowed_attributes':[]},
    {'entity':'商品','version':'v1','default_display_attributes':['商品名称'],'allowed_attributes':['商品名称']},
    {'entity':'医院','version':'current','default_display_attributes':['医院名称'],'allowed_attributes':['医院名称']},
])
def test_catalog_default_requires_version_entity_and_permission(policy):
    request=parse('列出TDC-3合作医院');request.fields=[]
    assert not apply_catalog_display_default(request,policy)
    assert 'fields' in RuleBasedIntentClassifier().required_missing_slots(request)


@pytest.mark.asyncio
async def test_actual_pending_does_not_hijack_new_hospital_task():
    agent=service();request=parse('上海销售额');request.application_id='fixture-app';request.conversation_id='pending'
    request.missing_slots=['semantic_ambiguity']
    request.semantic_ambiguities=[SemanticAmbiguity(type='metric',question='请选择销售额还是销售量',candidates=['销售额','销售量'])]
    await agent.sessions.put_pending(PendingState(request=request),expected_version=0)
    response=await agent.handle(ChatRequest(application_id='fixture-app',conversation_id='pending',message_id='m2',question='江苏有哪些医院？'),IDENTITY)
    assert response.intent==PrimaryIntent.DETAIL_QUERY
    assert response.status!='NEEDS_CLARIFICATION'
    assert await agent.sessions.get_pending(IDENTITY.tenant_id,IDENTITY.user_id,'fixture-app','pending') is None


@pytest.mark.asyncio
async def test_final_clarification_has_scoped_reason_without_raw_text():
    response=await service().handle(ChatRequest(application_id='fixture-app',conversation_id='trace',message_id='m1',question='分析本月数据'),IDENTITY)
    assert response.status=='NEEDS_CLARIFICATION'
    assert response.clarification_decision_traces
    trace=response.clarification_decision_traces[0]
    assert trace.conversation_id=='trace' and trace.message_id=='m1'
    assert trace.blocking_slot=='metric' and trace.decision=='ASK'
    assert trace.is_user_ambiguity and not trace.system_repair_possible and not trace.safe_default_available
    assert '分析本月数据' not in trace.model_dump_json()


@pytest.mark.asyncio
async def test_same_pending_question_is_suppressed_and_state_retained():
    agent=service();request=parse('分析本月数据');request.application_id='fixture-app'
    first=await agent._request_clarification(request,1)
    assert first.status=='NEEDS_CLARIFICATION'
    request.pending_state_version=1
    second=await agent._request_clarification(request,2)
    assert not second.clarification_questions
    assert second.clarification_decision_traces[0].already_asked
    assert second.clarification_decision_traces[0].decision=='SUPPRESS'
    pending=await agent.sessions.get_pending(request.tenant_id,request.user_id,request.application_id,request.conversation_id)
    assert pending and pending.state_version==1


@pytest.mark.asyncio
async def test_backend_failure_cannot_be_rephrased_as_missing_user_metric():
    agent=service();request=parse('本月销售额')
    request.missing_slots=['semantic_ambiguity']
    request.semantic_ambiguities=[SemanticAmbiguity(type='unknown',question='请重新说明指标',candidates=[])]
    result=await agent._request_clarification(request,1,source_stage='OAGNET_ASL_GENERATION')
    assert result.status=='SAFE_FALLBACK' and not result.clarification_questions
    trace=result.clarification_decision_traces[0]
    assert trace.reason_type=='SYSTEM_FAILURE' and not trace.is_user_ambiguity


@pytest.mark.asyncio
async def test_real_candidate_choice_is_allowed_and_trace_uses_opaque_ids():
    agent=service();request=parse('本月销售额')
    request.missing_slots=['semantic_ambiguity']
    request.semantic_ambiguities=[SemanticAmbiguity(type='metric',question='选择口径',candidates=['含税销售额','不含税销售额'])]
    request.ambiguities=['选择口径']
    result=await agent._request_clarification(request,1,source_stage='OAGNET_ASL_GENERATION')
    assert result.status=='NEEDS_CLARIFICATION'
    assert result.clarification_decision_traces[0].candidate_ids
    assert '含税销售额' not in result.clarification_decision_traces[0].model_dump_json()


@pytest.mark.parametrize('question,kind', [('只看前5条','limit'),('销售额最高5名','sort_limit'),('销售额最低2名','sort_limit'),('销售额从低到高','sort'),('只保留名称','select')])
def test_dataset_operation_contrasts(question,kind):
    operation=plan_dataset_followup(question,['名称','销售额'],[{'名称':'甲','销售额':2},{'名称':'乙','销售额':9}])
    assert operation and operation['type']==kind


def test_truncated_display_limit_is_safe_while_global_rank_is_not():
    rows=[{'销售额':2}]
    assert plan_dataset_followup('只看前5条',['销售额'],rows,source_complete=False)=={'type':'limit','count':5}
    assert plan_dataset_followup('销售额最高5名',['销售额'],rows,source_complete=False) is None


@pytest.mark.parametrize('text',['按月统计血液透析器销售额','查询北京医院销售额'])
def test_structural_filter_repair_preserves_explicit_business_entities(text):
    request=parse(text)
    assert request.filters


def test_temporal_structural_guard_does_not_discard_product_text():
    request=parse('查询2026年7月医用导管的销售额')
    assert any('导管' in str(f.get('value')) for f in request.filters)


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit', [False,True])
async def test_dataset_truncation_reaches_real_orchestrator_guard(explicit):
    from datetime import datetime, timezone
    from test_conversation_result_followup import _ResultStore
    from app.services.dataset_followup import scope_for_request
    from app.services.orchestrator import ExplicitDatasetUnavailableError
    agent=service();agent.dataset_store=_ResultStore()
    request=parse('销售额最高5名');request.application_id='fixture-app'
    reference=agent.dataset_store.save_dataset(scope=scope_for_request(request),columns=['销售额'],rows=[{'销售额':2}],snapshot_id='partial-snapshot',data_as_of=datetime(2026,9,7,tzinfo=timezone.utc),source_type='DATABASE_QUERY',source_ref='fixture',transformation_log=({'type':'query_provenance','source_truncated':True},))
    await agent.sessions.put_dataset_reference(reference.to_dict(),recent_limit=5)
    request.source_dataset_id=reference.dataset_id
    if explicit:
        request.assumptions.append('EXPLICIT_SOURCE_DATASET_SELECTION')
        with pytest.raises(ExplicitDatasetUnavailableError,match='不完整'):
            await agent._try_dataset_followup(request)
    else:
        assert await agent._try_dataset_followup(request)==(None,None)
        assert request.execution_mode=='QUERY_DATABASE'


def test_incomplete_dataset_allows_projection_and_explicit_local_sort():
    rows=[{'名称':'甲','销售额':2}]
    assert plan_dataset_followup('只保留名称',['名称','销售额'],rows,source_complete=False)=={'type':'select','columns':['名称']}
    operation=plan_dataset_followup('刚才结果销售额从低到高',['名称','销售额'],rows,source_complete=False)
    assert operation and operation['type']=='sort'


@pytest.mark.parametrize('text,slot,operation', [
    ('再加订单笔数','metrics','ADD'), ('换成订单笔数','metrics','REPLACE'),
    ('不要订单笔数','metrics','REMOVE'), ('不限地区','region','CLEAR'),
])
def test_slot_audit_agrees_with_explicit_mutation(text,slot,operation):
    _, decision=turn(parse('上海销售额和订单笔数'),text)
    assert any(o.slot==slot and o.operation.value==operation for o in decision.slot_operations)


@pytest.mark.asyncio
@pytest.mark.parametrize('answer',['查看名称列表','1'])
async def test_bare_attribute_asks_operation_then_accepts_list_choice(answer):
    agent=service()
    first=await agent.handle(ChatRequest(application_id='fixture-app',conversation_id='bare',message_id='m1',question='商品名称'),IDENTITY)
    assert first.status=='NEEDS_CLARIFICATION'
    assert first.missing_slots==['semantic_ambiguity']
    assert all('指标' not in q for q in first.clarification_questions)
    second=await agent.handle(ChatRequest(application_id='fixture-app',conversation_id='bare',message_id='m2',question=answer),IDENTITY)
    assert second.intent==PrimaryIntent.DETAIL_QUERY
    assert 'metric' not in second.missing_slots


def test_bare_attribute_grouping_choice_requires_measure_only_after_operation_selected():
    request=parse('商品名称')
    choice=DataAnalysisOrchestrator._semantic_clarification_choice(request,'按名称分组统计')
    result=DataAnalysisOrchestrator._apply_semantic_clarification_choice(request,request.model_copy(deep=True),choice)
    assert result.dimensions==['商品']
    assert not result.fields
    assert 'metric' in RuleBasedIntentClassifier().required_missing_slots(result)


@pytest.mark.asyncio
async def test_root_dag_pending_does_not_capture_complete_new_business_task():
    agent=service()
    await agent.sessions.put_dag_pending(IDENTITY.tenant_id,IDENTITY.user_id,'fixture-app','dag-new',{'state_version':1},expected_version=0)
    result=await agent.handle(ChatRequest(application_id='fixture-app',conversation_id='dag-new',message_id='m2',question='江苏有哪些医院？'),IDENTITY)
    assert result.intent==PrimaryIntent.DETAIL_QUERY
    assert await agent.sessions.get_dag_pending(IDENTITY.tenant_id,IDENTITY.user_id,'fixture-app','dag-new') is None


@pytest.mark.asyncio
async def test_root_dag_mapping_question_is_not_repeated():
    from uuid import uuid4
    from test_task_dag import _StubOrchestrator, _Classifier, MultiQuestionPlanner
    from app.domain.models import AgentResponse
    class Clarifying(_StubOrchestrator):
        async def _handle(self,chat,identity):
            return AgentResponse(request_id=uuid4(),conversation_id=chat.conversation_id,status='NEEDS_CLARIFICATION',intent=PrimaryIntent.METRIC_QUERY,answer='请选择时间范围',clarification_questions=['请选择时间范围'],missing_slots=['time_range'])
    settings=Settings(env='test',adapter_mode='mock',multi_question_model_enabled=False)
    agent=Clarifying(settings=settings,classifier=_Classifier(),adapters=build_mock_adapters(),sessions=InMemorySessionStore(),task_planner=MultiQuestionPlanner(settings))
    async def send(mid,text):
        return await agent.handle(ChatRequest(application_id='fixture-app',conversation_id='dag-repeat',message_id=mid,question=text),IDENTITY)
    first=await send('m1','查询销售额；另外查询库存')
    assert len(first.awaiting_task_ids)==2
    mapping=await send('m2','本月和上周')
    assert mapping.status=='NEEDS_CLARIFICATION'
    assert mapping.clarification_decision_traces[0].reason_type=='USER_REFERENCE_AMBIGUITY'
    repeated=await send('m3','本月和上周')
    assert not repeated.clarification_questions
    assert repeated.clarification_decision_traces[0].already_asked
    assert await agent.sessions.get_dag_pending(IDENTITY.tenant_id,IDENTITY.user_id,'fixture-app','dag-repeat')


@pytest.mark.asyncio
async def test_clarification_history_is_isolated_by_trusted_scope():
    agent=service();request=parse('分析本月数据');request.application_id='fixture-app'
    await agent._request_clarification(request,1)
    other=request.model_copy(update={'tenant_id':'other-tenant','pending_state_version':None})
    response=await agent._request_clarification(other,1)
    assert response.status=='NEEDS_CLARIFICATION'
    assert not response.clarification_decision_traces[0].already_asked


@pytest.mark.asyncio
async def test_question_limit_does_not_mark_unshown_question_as_already_asked():
    from app.domain.models import MetricRef
    agent=service(); request=parse('分析本月数据');request.application_id='fixture-app'
    request.missing_slots=['metric','semantic_ambiguity']
    request.semantic_ambiguities=[SemanticAmbiguity(type='dimension',question='按哪个对象分组？',candidates=['医院','业务员'])]
    first=await agent._request_clarification(request,1)
    assert len(first.clarification_questions)==1
    assert first.clarification_decision_traces[1].decision=='SUPPRESS'
    request.metrics=[MetricRef(input='销售额')];request.missing_slots=['semantic_ambiguity'];request.pending_state_version=1
    second=await agent._request_clarification(request,2)
    assert second.status=='NEEDS_CLARIFICATION'
    assert second.clarification_decision_traces[0].decision=='ASK'
    assert not second.clarification_decision_traces[0].already_asked


def test_dataset_operation_classification_does_not_call_unknown_a_drilldown():
    from app.services.dataset_followup import dataset_operation_family
    assert dataset_operation_family(None) is None
    assert dataset_operation_family({'type':'drilldown'})=='DRILLDOWN'
