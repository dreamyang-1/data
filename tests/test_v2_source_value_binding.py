"""Raw V2/current-source integration; fake model HTTP and business connections."""
from copy import deepcopy
import json

import pytest

from app.semantic_v2.authorized_contract import ScopedArtifact, contract_digest
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.pending_recognition import RecognizedClarification
from app.semantic_v2.pipeline import AuthorizedLogicalPlan
from test_v2_raw_turn_recognition import (IDENTITY, binding, edit, parse, planner,
    request, authority, publish, reseal, system, turns, metric_step)
from test_v2_structured_edits import target
import mysql_tool as mysql
from catalog_value_sources import capture_value_sources
from catalog_release import catalog_scope
from test_catalog_value_sources import source


@pytest.fixture
def catalog(monkeypatch):
    service, store, registry, redis, _, overrides = system()
    rows = [source(vectorization=1), source(attribute_id=1206, attr_code='name', attr_name='医院名称',
        mapping_column='name', field_id=3, vectorization=1)]
    def metadata(sql, args=None):
        selected = rows if len(args)==2 else [r for r in rows if str(r['attribute_id'])==args[-1]]
        return deepcopy(selected)
    monkeypatch.setattr(mysql, '_query', metadata)
    current = authority()
    current['documents'][0]['entities'][0]['attributes'].append(dict(attribute_id=1206,
        attr_code='name', attr_name='医院名称', field_mapping='hospitals.name'))
    current['physical_catalog']['tables'][0]['fields'].append(dict(field_id=3, field_name='name', table_id=1))
    current['physical_catalog']['entity_value_sources'] = capture_value_sources(catalog_scope(81, [205]))
    overrides[(81, (205,))] = reseal(current)
    publish(service)
    business = {'city': ['上海','北京','江苏'], 'name':['甲院','乙院']}
    reads = []
    class Connection:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=None):
            if params is None: return
            column = next(k for k in business if '`'+k+'`' in sql)
            reads.append((column, params[0]))
            self.rows = (sorted(business[column])[:params[0]] if 'MAX_EXECUTION_TIME' in sql else
                sorted(v for v in business[column] if mysql.normalize_catalog_text(v)==params[0])[:params[1]])
        def fetchall(self): return [(v.encode('utf-8'),) for v in self.rows]
        def rollback(self): pass
        def close(self): pass
    monkeypatch.setattr(mysql.pymysql, 'connect', lambda **kwargs: Connection())
    return service,store,registry,redis,overrides,business,reads,rows


def source_step(value='上海', *, field='城市', explicit=False):
    text = (field if explicit else '') + value + '销售额'
    specs = [(value,'FILTER_VALUE','filter_expression','SET'), ('销售额','MEASURE','metrics','SET')]
    if explicit: specs.append((field,'FILTER_FIELD','filter_expression','SET'))
    def draft(c):
        field_handle=binding(c,field,'FILTER_FIELD','m2' if explicit else 'm0',kind='ATTRIBUTE')
        return dict(payload_type='SCALAR_AGGREGATE',
            source_value_requests=[dict(request_id='value',mention_id='m0',field_binding_handles=[field_handle['binding_handle']])],
            edits=[edit('metrics',[binding(c,'销售额','MEASURE','m1')],ids=('m1',)),
                edit('filter_expression',dict(node_type='PREDICATE',
                    field_ref=field_handle if explicit else {'value_field_request_id':'value'},
                    operator='EQ',value={'value_request_id':'value'},source='USER_EXPLICIT',scope='CURRENT_TASK'),
                    ids=('m0','m2') if explicit else ('m0',))])
    return text,parse(text,specs),draft


def source_edit(value, operation):
    text=operation+value
    def draft(c):
        selected=target(c,'城市')
        request=(dict(request_id='value',mention_id='m0',target_filter_handle=selected)
            if operation in {'REPLACE','REMOVE'} else
            dict(request_id='value',mention_id='m0',field_binding_handles=[
                binding(c,'城市','FILTER_FIELD',kind='ATTRIBUTE')['binding_handle']]))
        return dict(payload_type='INHERIT',
            source_value_requests=[request],
            filter_edits=[dict(operation=operation,target_handle=selected,
                evidence_mention_ids=['m0'],value={'value_request_id':'value'})])
    return text,parse(text,[(value,'FILTER_VALUE','filter_expression',operation)],follow=True),draft


def referenced_edit(value, operation):
    text=operation+value
    def draft(c):
        selected=target(c,'城市')
        return dict(payload_type='INHERIT',source_value_requests=[
            dict(request_id='value',mention_id='m0',target_filter_handle=selected)],
            filter_edits=[dict(operation=operation,target_handle=selected,evidence_mention_ids=['m0'],
                value={'value_request_id':'value'})])
    return text,parse(text,[(value,'FILTER_VALUE','filter_expression',operation)],follow=True),draft


def values(result):
    expression=result.plan['logical_plan']['payload']['filters']
    if expression is None:return []
    value=expression['value']
    items=value['values'] if value['value_type']=='LIST' else [value]
    return [item['ref']['display_name'] for item in items]


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit',[False,True])
async def test_raw_exact_value_is_bound_to_current_field_and_scope(catalog,explicit):
    step=source_step(explicit=explicit);engine,transport=planner(catalog,[step])
    result=(await turns(engine,[step]))[0]
    assert values(result)==['上海']
    plan=AuthorizedLogicalPlan.model_validate(result.plan['logical_plan'])
    proof=next(p for p in plan.permission_proofs if p.source=='VERIFIED_SOURCE_EXACT_LOOKUP')
    assert proof.field_ref.canonical_code=='city' and proof.data_source_id==7
    assert proof.ref.business_domain_ids==('205',)
    assert result.next_state.source_value_bindings and result.plan_state.source_value_bindings
    assert catalog[6]==[('city','上海'),('city','上海')]
    assert result.plan['backend_contract']['mode']=='SHADOW_ONLY' and len(transport.calls)==2


@pytest.mark.asyncio
async def test_source_values_add_replace_remove_and_clear_do_not_revive(catalog):
    clear=('不限城市',parse('不限城市',[('城市','FILTER_FIELD','filter_expression','CLEAR')],follow=True),
        lambda c:dict(payload_type='INHERIT',filter_edits=[dict(operation='CLEAR',target_handle=target(c),evidence_mention_ids=['m0'])]))
    steps=[source_step(),source_edit('北京','ADD'),source_edit('江苏','REPLACE'),source_edit('上海','ADD'),
        source_edit('江苏','REMOVE'),clear,metric_step('销售额',follow=True)]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    assert [values(r) for r in results]==[['上海'],['上海','北京'],['江苏'],['江苏','上海'],['上海'],[],[]]


@pytest.mark.asyncio
async def test_proven_normalization_collision_asks_and_exact_option_completes(catalog):
    catalog[5]['city']=['甲－城','甲—城']
    step=source_step('甲-城');engine,_=planner(catalog,[step])
    asked=await engine.run(request(question=step[0],message_id='ask'),IDENTITY)
    assert isinstance(asked,RecognizedClarification)
    assert len(asked.decision.options)==2 and asked.trace.evidence_codes[0]=='VERIFIED_SOURCE_VALUE_COLLISION'
    response=('第一项',parse('第一项',follow=True),{})
    engine,_=planner(catalog,[response])
    result=await engine.run(request(question=response[0],message_id='answer'),IDENTITY,
        state=asked.next_state,pending=asked.pending_state)
    selected=asked.decision.options[0].filter_choice.expression.value.ref.display_name
    assert values(result)==[selected]
    assert result.resolution['dialogue_act']=='ANSWER_CLARIFICATION'


@pytest.mark.asyncio
async def test_ambiguous_replacement_keeps_the_old_filter_until_answer(catalog):
    first=source_step();engine,_=planner(catalog,[first])
    prior=(await turns(engine,[first]))[0]
    catalog[5]['city']+=['甲－城','甲—城']
    step=source_edit('甲-城','REPLACE');engine,_=planner(catalog,[step])
    asked=await engine.run(request(question=step[0],message_id='ask'),IDENTITY,
        state=prior.next_state,plans=(prior.plan_state,))
    assert isinstance(asked,RecognizedClarification)
    response=('第一项',parse('第一项',follow=True),{})
    engine,_=planner(catalog,[response])
    result=await engine.run(request(question=response[0],message_id='answer'),IDENTITY,
        state=asked.next_state,pending=asked.pending_state)
    assert len(values(result))==1 and '上海' not in values(result)


@pytest.mark.asyncio
async def test_deleted_value_cannot_restore_as_current_binding(catalog):
    step=source_step();engine,_=planner(catalog,[step])
    result=(await turns(engine,[step]))[0]
    catalog[5]['city'].remove('上海')
    current=ScopedPlanSession(request(message_id='later'),IDENTITY,catalog[0])
    with pytest.raises(ValueError,match='SOURCE_VALUE_RESTORE_NOT_CURRENT'):
        current.restore(result.next_state,kind='CONVERSATION')


@pytest.mark.asyncio
async def test_scoped_source_receipt_cannot_move_to_another_conversation(catalog):
    step=source_step();engine,_=planner(catalog,[step]);result=(await turns(engine,[step]))[0]
    current=ScopedPlanSession(request(message_id='later',conversation_id='foreign'),IDENTITY,catalog[0])
    before=len(catalog[6])
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        current.restore(result.next_state,kind='CONVERSATION')
    assert len(catalog[6])==before


@pytest.mark.asyncio
async def test_envelope_digest_covers_source_evidence(catalog):
    step=source_step();engine,_=planner(catalog,[step]);result=(await turns(engine,[step]))[0]
    data=result.next_state.model_dump(mode='json')
    data['source_value_bindings'][0]['observation_hash']='forged'
    with pytest.raises(ValueError,match='SCOPED_ARTIFACT_CORRUPT'):ScopedArtifact.model_validate(data)


@pytest.mark.asyncio
async def test_clear_does_not_require_an_old_business_value_to_still_exist(catalog):
    first=source_step();engine,_=planner(catalog,[first]);before=(await turns(engine,[first]))[0]
    catalog[5]['city'].remove('上海');catalog[6].clear()
    text='不限城市'
    step=(text,parse(text,[('城市','FILTER_FIELD','filter_expression','CLEAR')],follow=True),
        lambda c:dict(payload_type='INHERIT',filter_edits=[dict(operation='CLEAR',target_handle=target(c),evidence_mention_ids=['m0'])]))
    engine,_=planner(catalog,[step])
    cleared=await engine.run(request(question=text,message_id='clear'),IDENTITY,state=before.next_state,plans=(before.plan_state,))
    assert values(cleared)==[] and not catalog[6]
    next_step=metric_step('销售额',follow=True);engine,_=planner(catalog,[next_step])
    later=await engine.run(request(question='销售额',message_id='later'),IDENTITY,state=cleared.next_state,plans=(cleared.plan_state,))
    assert values(later)==[] and not catalog[6]


@pytest.mark.asyncio
async def test_new_task_does_not_refresh_or_inherit_deleted_historical_value(catalog):
    first=source_step();engine,_=planner(catalog,[first]);before=(await turns(engine,[first]))[0]
    catalog[5]['city'].clear();catalog[6].clear()
    step=metric_step('销售额');engine,_=planner(catalog,[step])
    result=await engine.run(request(question='销售额',message_id='new'),IDENTITY,state=before.next_state,plans=(before.plan_state,))
    assert result.plan['logical_plan']['task_id']!=before.plan['logical_plan']['task_id']
    assert values(result)==[] and not catalog[6]


@pytest.mark.asyncio
async def test_active_followup_cannot_reuse_deleted_value(catalog):
    first=source_step();engine,_=planner(catalog,[first]);before=(await turns(engine,[first]))[0]
    catalog[5]['city'].clear()
    step=metric_step('销售额',follow=True);engine,_=planner(catalog,[step])
    with pytest.raises(ValueError,match='SOURCE_VALUE_RESTORE_NOT_CURRENT'):
        await engine.run(request(question='销售额',message_id='later'),IDENTITY,state=before.next_state,plans=(before.plan_state,))


@pytest.mark.asyncio
async def test_removing_deleted_member_uses_exact_current_filter_identity(catalog):
    first=source_step();engine,_=planner(catalog,[first]);before=(await turns(engine,[first]))[0]
    catalog[5]['city'].clear();catalog[6].clear()
    step=referenced_edit('上海','REMOVE');engine,_=planner(catalog,[step])
    result=await engine.run(request(question=step[0],message_id='remove'),IDENTITY,state=before.next_state,plans=(before.plan_state,))
    assert values(result)==[] and not catalog[6]


@pytest.mark.asyncio
async def test_adding_deleted_member_cannot_use_removal_evidence(catalog):
    first=source_step();engine,_=planner(catalog,[first]);before=(await turns(engine,[first]))[0]
    catalog[5]['city'].clear()
    step=referenced_edit('上海','ADD');engine,_=planner(catalog,[step])
    with pytest.raises(ValueError,match='SOURCE_VALUE_NOT_FOUND'):
        await engine.run(request(question=step[0],message_id='add'),IDENTITY,state=before.next_state,plans=(before.plan_state,))


@pytest.mark.asyncio
async def test_same_pin_rechecks_each_source_value_once_at_acceptance(catalog):
    steps=[source_step(),source_edit('北京','ADD'),source_edit('江苏','ADD')]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    catalog[6].clear()
    step=metric_step('销售额',follow=True);engine,_=planner(catalog,[step])
    result=await engine.run(request(question='销售额',message_id='more'),IDENTITY,
        state=results[-1].next_state,plans=(results[-1].plan_state,))
    assert set(values(result))=={'上海','北京','江苏'}
    assert len(catalog[6])==6


@pytest.mark.asyncio
@pytest.mark.parametrize('database_id,accepted',[(7,True),(9,False)])
async def test_database_selection_uses_existing_platform_mapping(catalog,database_id,accepted):
    step=source_step();engine,_=planner(catalog,[step])
    if accepted:
        result=await engine.run(request(question=step[0],database_id=database_id),IDENTITY)
        assert values(result)==['上海']
        assert result.next_state.context.authorized_scope.database_id==7
    else:
        with pytest.raises(ValueError,match='DATA_SOURCE_MISMATCH'):
            await engine.run(request(question=step[0],database_id=database_id),IDENTITY)
        assert not catalog[6]


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['canonical_value','data_source_id','field_ref'])
async def test_serialized_plan_source_proof_cannot_be_swapped(catalog,fault):
    step=source_step();engine,_=planner(catalog,[step]);result=(await turns(engine,[step]))[0]
    data=deepcopy(result.plan['logical_plan'])
    proof=next(p for p in data['permission_proofs'] if p['source']=='VERIFIED_SOURCE_EXACT_LOOKUP')
    if fault=='canonical_value':proof['canonical_value']='北京'
    elif fault=='data_source_id':proof['data_source_id']=9
    else:proof['field_ref']['canonical_code']='name'
    with pytest.raises(ValueError):AuthorizedLogicalPlan.model_validate(data)


@pytest.mark.asyncio
async def test_filter_value_cannot_be_applied_to_another_field(catalog):
    step=source_step(explicit=True)
    def draft(context):
        result=step[2](context)
        result['edits'][-1]['value']['field_ref']=binding(context,'医院名称','FILTER_FIELD','m2','ATTRIBUTE')
        return result
    engine,_=planner(catalog,[(step[0],step[1],draft)])
    with pytest.raises(ValueError,match='SOURCE_VALUE_FILTER_FIELD_MISMATCH'):
        await engine.run(request(question=step[0]),IDENTITY)


@pytest.mark.asyncio
async def test_proven_value_absence_is_a_system_reason_not_generic_clarification(catalog):
    step=source_step('不存在的城市');engine,_=planner(catalog,[step])
    with pytest.raises(ValueError,match='V2_SOURCE_VALUE_NOT_FOUND'):
        await engine.run(request(question=step[0]),IDENTITY)


@pytest.mark.asyncio
async def test_source_pending_cannot_hijack_a_complete_new_task(catalog):
    catalog[5]['city']=['甲－城','甲—城']
    step=source_step('甲-城');engine,_=planner(catalog,[step])
    asked=await engine.run(request(question=step[0],message_id='ask'),IDENTITY)
    catalog[5]['city'].clear();catalog[6].clear()
    new=metric_step('销售额');new[1]['dialogue_act_candidates']=['NEW_TASK']
    engine,_=planner(catalog,[new])
    result=await engine.run(request(question='销售额',message_id='new'),IDENTITY,
        state=asked.next_state,pending=asked.pending_state)
    assert result.resolution['dialogue_act']=='NEW_TASK' and values(result)==[]
    assert not catalog[6]


@pytest.mark.asyncio
async def test_stale_pending_choice_cannot_be_accepted(catalog):
    catalog[5]['city']=['甲－城','甲—城']
    step=source_step('甲-城');engine,_=planner(catalog,[step])
    asked=await engine.run(request(question=step[0],message_id='ask'),IDENTITY)
    selected=asked.decision.options[0].filter_choice.expression.value.ref.display_name
    catalog[5]['city'].remove(selected)
    response=('第一项',parse('第一项',follow=True),{});engine,_=planner(catalog,[response])
    with pytest.raises(ValueError,match='SOURCE_VALUE_RESTORE_NOT_CURRENT'):
        await engine.run(request(question=response[0],message_id='answer'),IDENTITY,
            state=asked.next_state,pending=asked.pending_state)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['RESPONSE_CACHE','SEMANTIC_BINDINGS','DAG_RESUME'])
async def test_reusable_artifacts_require_fresh_values(catalog,kind):
    step=source_step();engine,_=planner(catalog,[step]);before=(await turns(engine,[step]))[0]
    current=ScopedPlanSession(request(message_id='cache'),IDENTITY,catalog[0])
    payload=current.restore(before.plan_state,kind='LAST_REQUEST')
    current.accept_catalog();artifact=current.seal(kind=kind,payload=payload)
    catalog[5]['city'].clear()
    next_session=ScopedPlanSession(request(message_id='reuse'),IDENTITY,catalog[0])
    with pytest.raises(ValueError,match='SOURCE_VALUE_RESTORE_NOT_CURRENT'):
        next_session.restore(artifact,kind=kind)


@pytest.mark.asyncio
async def test_source_value_query_receipt_is_not_a_cross_scope_cache_key(catalog):
    first=source_step();engine,_=planner(catalog,[first]);before=(await turns(engine,[first]))[0]
    original=ScopedPlanSession(request(message_id='cache'),IDENTITY,catalog[0])
    key=original.cache_key('same-semantics')
    publish(catalog[0],publication_id='model-wide',domains=())
    wide=ScopedPlanSession(request(domains=(),message_id='wide'),IDENTITY,catalog[0])
    assert wide.cache_key('same-semantics')!=key
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        wide.restore(before.plan_state,kind='LAST_REQUEST')


@pytest.mark.asyncio
async def test_exact_explicit_field_and_later_reference_do_not_need_vector_enablement(catalog):
    catalog[7][0]['vectorization']=0
    current=deepcopy(catalog[4][(81,(205,))])
    current['physical_catalog']['entity_value_sources']=capture_value_sources(catalog_scope(81,[205]))
    catalog[4][(81,(205,))]=reseal(current);publish(catalog[0],publication_id='disabled-vector')
    steps=[source_step(explicit=True),referenced_edit('北京','REPLACE')]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    assert [values(r) for r in results]==[['上海'],['北京']]


@pytest.mark.asyncio
async def test_model_cannot_use_an_unapplied_value_request(catalog):
    step=source_step()
    def draft(c):
        result=step[2](c)
        result['edits'][-1]['value']['value']={'value_type':'STRING','value':'上海'}
        return result
    engine,_=planner(catalog,[(step[0],step[1],draft)])
    with pytest.raises(ValueError,match='SOURCE_VALUE_REQUEST_NOT_APPLIED'):
        await engine.run(request(question=step[0]),IDENTITY)
    assert not catalog[6]


@pytest.mark.asyncio
async def test_model_cannot_choose_unoffered_source_field(catalog):
    step=source_step()
    def draft(c):
        result=step[2](c);result['source_value_requests'][0]['field_binding_handles']=['foreign:field']
        return result
    engine,_=planner(catalog,[(step[0],step[1],draft)])
    with pytest.raises(ValueError,match='V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'):
        await engine.run(request(question=step[0]),IDENTITY)
    assert not catalog[6]


@pytest.mark.asyncio
async def test_incomplete_source_candidates_do_not_become_unique_by_truncation(catalog,monkeypatch):
    import catalog_value_sources as source_values
    original=source_values.observe
    def incomplete(*args):
        receipt=original(*args);receipt['complete']=False
        receipt['observation_hash']=contract_digest({k:v for k,v in receipt.items() if k not in {'observation_hash','observed_at'}})
        return receipt
    monkeypatch.setattr(source_values,'observe',incomplete)
    step=source_step();engine,_=planner(catalog,[step])
    with pytest.raises(ValueError,match='SOURCE_VALUE_CANDIDATES_INCOMPLETE'):
        await engine.run(request(question=step[0]),IDENTITY)


@pytest.mark.asyncio
async def test_equal_value_on_different_fields_requires_a_field_and_value_choice(catalog):
    catalog[5]['city']=['甲院'];catalog[5]['name']=['甲院']
    step=source_step('甲院')
    def draft(c):
        value=step[2](c)
        value['source_value_requests'][0]['field_binding_handles'].append(
            binding(c,'医院名称','FILTER_FIELD',kind='ATTRIBUTE')['binding_handle'])
        return value
    engine,_=planner(catalog,[(step[0],step[1],draft)])
    asked=await engine.run(request(question=step[0],message_id='ask'),IDENTITY)
    assert isinstance(asked,RecognizedClarification) and len(asked.decision.options)==2
    fields={o.filter_choice.expression.field_ref.canonical_code for o in asked.decision.options}
    assert fields=={'city','name'}
    chosen=asked.decision.options[0].filter_choice.expression
    # An unselected historical option is not reused in the accepted query.
    catalog[5]['name' if chosen.field_ref.canonical_code=='city' else 'city'].clear()
    response=('第一项',parse('第一项',follow=True),{});engine,_=planner(catalog,[response])
    result=await engine.run(request(question=response[0],message_id='answer'),IDENTITY,
        state=asked.next_state,pending=asked.pending_state)
    predicate=result.plan['logical_plan']['payload']['filters']
    assert predicate['field_ref']['canonical_code']==chosen.field_ref.canonical_code
    assert predicate['value']['ref']['display_name']=='甲院'


@pytest.mark.asyncio
async def test_multiple_source_ambiguities_preserve_all_filter_combinations(catalog):
    catalog[5]['city']=['甲－城','甲—城'];catalog[5]['name']=['乙－院','乙—院']
    text='甲-城乙-院销售额'
    parsed=parse(text,[('甲-城','FILTER_VALUE','filter_expression','SET'),
        ('乙-院','FILTER_VALUE','filter_expression','SET'),('销售额','MEASURE','metrics','SET')])
    def draft(c):
        predicates=[];requests=[]
        for mid,name in [('m0','城市'),('m1','医院名称')]:
            field=binding(c,name,'FILTER_FIELD',mid,'ATTRIBUTE')['binding_handle']
            requests.append(dict(request_id=mid,mention_id=mid,field_binding_handles=[field]))
            predicates.append(dict(node_type='PREDICATE',field_ref={'value_field_request_id':mid},operator='EQ',
                value={'value_request_id':mid},source='USER_EXPLICIT',scope='CURRENT_TASK'))
        return dict(payload_type='SCALAR_AGGREGATE',source_value_requests=requests,
            edits=[edit('metrics',[binding(c,'销售额','MEASURE','m2')],ids=('m2',)),
                edit('filter_expression',dict(node_type='BOOLEAN_GROUP',operator='AND',children=predicates),ids=('m0','m1'))])
    engine,_=planner(catalog,[(text,parsed,draft)])
    asked=await engine.run(request(question=text,message_id='ask'),IDENTITY)
    assert len(asked.decision.options)==4
    expected=asked.decision.options[0].filter_choice.expression.model_dump(mode='json')
    response=('第一项',parse('第一项',follow=True),{});engine,_=planner(catalog,[response])
    result=await engine.run(request(question=response[0],message_id='answer'),IDENTITY,
        state=asked.next_state,pending=asked.pending_state)
    assert result.plan['logical_plan']['payload']['filters']==expected


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['scope','pin','mapping','attribute'])
async def test_foreign_source_receipt_is_rejected_before_plan_acceptance(catalog,monkeypatch,fault):
    from catalog_publication import PinnedCatalog
    original=PinnedCatalog.lookup_entity_values
    def invalid(pin,*args,**kwargs):
        result=original(pin,*args,**kwargs)
        if fault=='scope':result['scope']['business_domain_ids']=[206]
        elif fault=='pin':result['catalog_pin']['activation_id']='foreign'
        elif fault=='mapping':result['field']['attribute_id']='foreign'
        else:result['attribute_record_id']='foreign'
        return result
    monkeypatch.setattr(PinnedCatalog,'lookup_entity_values',invalid)
    step=source_step();engine,_=planner(catalog,[step])
    with pytest.raises(ValueError,match='SOURCE_VALUE_RECEIPT_MISMATCH'):
        await engine.run(request(question=step[0]),IDENTITY)


@pytest.mark.asyncio
async def test_source_values_cannot_be_reported_as_safe_legacy_sql(catalog):
    step=source_step();engine,_=planner(catalog,[step]);result=(await turns(engine,[step]))[0]
    assert result.plan['adapter_report']['can_execute_safely'] is False


def test_legacy_pending_identity_ignores_absent_new_filter_choice():
    from app.semantic_v2 import models as m
    from app.semantic_v2.pending_recognition import pending_identity
    from app.semantic_v2.state_machine import PendingBlocker
    options=[m.ClarificationOption(option_id=identifier,display_label=label,
        typed_value=m.StringValue(value=label),evidence=['FIXTURE']) for identifier,label in [('o','甲'),('p','乙')]]
    blocker=PendingBlocker(blocker_id='b',plan_path='subject',expected_answer_type='OPTION_ID',
        candidate_ids=['o','p'],information_gain=1,options=options)
    old=[{k:v for k,v in option.model_dump(mode='json').items() if k!='filter_choice'} for option in options]
    expected='pending:'+contract_digest(['t','DETAIL_ROWS',{'b':'SET'},[dict(blocker_id='b',slot='subject',options=old)]])[:32]
    assert pending_identity('t','DETAIL_ROWS',{'b':'SET'},[blocker])==expected


@pytest.mark.asyncio
async def test_source_value_and_metric_ambiguities_each_resolve_once(catalog):
    current=deepcopy(catalog[4][(81,(205,))]);doc=current['documents'][0]
    doc['metrics'][0]['synonyms']=['销售']
    doc['metrics'].append(dict(metric_code='quantity',metric_name='销售数量',synonyms=['销售'],
        business_domain=205,formula='SUM(quantity)'))
    catalog[4][(81,(205,))]=reseal(current);publish(catalog[0],publication_id='mixed-pending')
    catalog[5]['city']=['甲－城','甲—城']
    text='甲-城销售'
    parsed=parse(text,[('甲-城','FILTER_VALUE','filter_expression','SET'),('销售','MEASURE','metrics','SET')])
    def draft(c):
        # Keep the source filter draft while leaving the governed metric collision open.
        field=binding(c,'城市','FILTER_FIELD',kind='ATTRIBUTE')
        return dict(payload_type='SCALAR_AGGREGATE',
            source_value_requests=[dict(request_id='value',mention_id='m0',field_binding_handles=[field['binding_handle']])],
            edits=[edit('filter_expression',dict(node_type='PREDICATE',field_ref={'value_field_request_id':'value'},
                operator='EQ',value={'value_request_id':'value'},source='USER_EXPLICIT',scope='CURRENT_TASK'))],
            ambiguities=[dict(mention_id='m1',slot_path='metrics',operation='SET',candidate_handles=[
                x['binding_handle'] for x in c['catalog_candidates'] if x['mention_id']=='m1' and x['role']=='MEASURE'])])
    engine,_=planner(catalog,[(text,parsed,draft)])
    pending=await engine.run(request(question=text,message_id='mixed'),IDENTITY)
    seen=[]
    for index in range(2):
        assert isinstance(pending,RecognizedClarification)
        seen.append(pending.trace.blocking_slot)
        response=('第一项',parse('第一项',follow=True),{});engine,transport=planner(catalog,[response])
        pending=await engine.run(request(question=response[0],message_id='answer'+str(index)),IDENTITY,
            state=pending.next_state,pending=pending.pending_state)
        assert len(transport.calls)==1
    assert set(seen)=={'metrics','filter_expression'} and len(values(pending))==1
    assert pending.plan['logical_plan']['payload']['measures']


@pytest.mark.asyncio
@pytest.mark.parametrize('selected_exists',[True,False])
async def test_historical_return_refreshes_only_selected_task_values(catalog,selected_exists):
    steps=[source_step('上海'),source_step('北京')]
    engine,_=planner(catalog,steps);prior=await turns(engine,steps)
    catalog[5]['city']=['上海'] if selected_exists else []
    catalog[6].clear()
    text='回到第一个问题'
    step=(text,parse(text,history=True),lambda c:dict(payload_type='INHERIT',historical_task_handle=c['tasks'][0]['task_handle']))
    engine,_=planner(catalog,[step])
    kwargs=dict(state=prior[-1].next_state,plans=tuple(p.plan_state for p in prior))
    if selected_exists:
        result=await engine.run(request(question=text,message_id='history'),IDENTITY,**kwargs)
        assert values(result)==['上海']
        assert result.plan['logical_plan']['task_id']==prior[0].plan['logical_plan']['task_id']
    else:
        with pytest.raises(ValueError,match='SOURCE_VALUE_RESTORE_NOT_CURRENT'):
            await engine.run(request(question=text,message_id='history'),IDENTITY,**kwargs)
    assert catalog[6] and all(value=='上海' for _,value in catalog[6])
