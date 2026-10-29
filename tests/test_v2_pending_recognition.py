"""Raw V2 clarification/resume tests; governed synthetic aliases, no live models."""
from copy import deepcopy

import pytest

from app.semantic_v2.pending_recognition import RecognizedClarification,selected_option
from app.semantic_v2.recognition_client import RecognitionFailure
from test_v2_raw_turn_recognition import (IDENTITY,NOW,authority,edit,envelope_with_payload,
    metric_step,parse,planner,publish,request,reseal,system)


@pytest.fixture
def catalog():
    service,store,registry,redis,_,overrides=system()
    source=authority();doc=source['documents'][0]
    doc['metrics'][0]['synonyms']=['销售']
    doc['metrics'].append(dict(metric_code='quantity',metric_name='销售数量',synonyms=['销售'],business_domain=205,formula='SUM(quantity)'))
    doc['dimensions'][0]['synonyms']=['地域']
    doc['entities'][0]['attributes'].append(dict(attribute_id=1206,attr_code='province',attr_name='省份',field_mapping='hospitals.province'))
    doc['dimensions'].append(dict(dim_code='province',dim_name='省份',synonyms=['地域'],
        bind_entities=[dict(entity='205',attr='1206',businessDomain='205')]))
    source['physical_catalog']['tables'][0]['fields'].append(dict(field_id=3,field_name='province',table_id=1))
    overrides[(81,(205,))]=reseal(source);publish(service)
    return service,store,registry,redis,overrides


def ambiguous_step(text='销售', *, multiple=False,operation='SET',follow=False):
    specs=[('销售','MEASURE','metrics',operation)]
    if multiple:specs.append(('地域','GROUP_BY','dimensions','SET'))
    parsed=parse(text,specs,follow=follow)
    def draft(c):
        ambiguities=[]
        for i,(_,role,slot,op) in enumerate(specs):
            candidates=[v['binding_handle'] for v in c['catalog_candidates'] if v['mention_id']=='m'+str(i)
                and v['role']==role and (v['catalog_type']=='METRIC' if slot=='metrics' else v['catalog_type']=='DIMENSION')]
            ambiguities.append(dict(mention_id='m'+str(i),slot_path=slot,operation=op,candidate_handles=candidates))
        return dict(payload_type='INHERIT' if follow else 'GROUPED_AGGREGATE' if multiple else 'SCALAR_AGGREGATE',ambiguities=ambiguities)
    return text,parsed,draft


async def ask(catalog,step=None):
    step=step or ambiguous_step();engine,calls=planner(catalog,[step])
    result=await engine.run(request(question=step[0],message_id='ask'),IDENTITY)
    return result,calls


async def answer(catalog,previous,text,message='answer',**changes):
    step=(text,parse(text,(),follow=True),{})
    engine,calls=planner(catalog,[step])
    result=await engine.run(request(question=text,message_id=message),IDENTITY,
        state=previous.next_state,pending=changes.get('pending',previous.pending_state))
    assert len(calls.calls)==1  # Exact option handling needs no second model call.
    return result


@pytest.mark.asyncio
async def test_governed_ambiguity_creates_traced_provisional_task(catalog):
    result,calls=await ask(catalog)
    assert isinstance(result,RecognizedClarification) and result.plan is None
    assert result.trace.decision=='ASK' and result.trace.is_user_ambiguity
    assert not result.trace.already_asked and not result.trace.system_repair_possible and not result.trace.safe_default_available
    assert len(result.decision.options)==2 and len(calls.calls)==2
    assert {o.display_label for o in result.decision.options}=={'销售额','销售数量'}
    task=next(iter(result.next_state.payload['tasks'].values()))
    assert task['status']=='PROVISIONAL' and task['versions'][0]['plan_id'] is None
    assert result.trace.model_dump().get('question') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('form',['name','id','1','第一个','我选择第一项','用一条'])
async def test_exact_answer_resumes_same_task_and_compiles(catalog,form):
    pending,_=await ask(catalog);option=pending.decision.options[0]
    text=option.display_label if form=='name' else option.option_id if form=='id' else form
    result=await answer(catalog,pending,text)
    assert result.resolution['dialogue_act']=='ANSWER_CLARIFICATION'
    plan=result.plan['logical_plan']
    assert plan['task_id']==pending.pending_state.payload['task_id']
    assert plan['payload']['measures'][0]['canonical_id']==option.canonical_ref.canonical_id
    record=result.next_state.payload['pending_records'][pending.pending_state.payload['pending_id']]
    assert record['status']=='RESOLVED'


@pytest.mark.asyncio
async def test_multiple_blockers_ask_each_once_then_compile(catalog):
    first,_=await ask(catalog,ambiguous_step('按地域看销售',multiple=True))
    second=await answer(catalog,first,first.decision.options[0].display_label)
    assert isinstance(second,RecognizedClarification)
    assert second.trace.blocking_slot!=first.trace.blocking_slot
    assert second.pending_state.payload['pending_id']==first.pending_state.payload['pending_id']
    assert not second.decision.create_pending and not second.trace.already_asked
    final=await answer(catalog,second,second.decision.options[0].display_label,message='answer2')
    assert final.plan['logical_plan']['payload']['payload_type']=='GROUPED_AGGREGATE'
    assert final.plan['logical_plan']['payload']['measures'] and final.plan['logical_plan']['payload']['group_by']
    blocks=next(iter(final.next_state.payload['pending_records'].values()))['blockers']
    assert all(b['status']=='RESOLVED' and b['already_asked'] for b in blocks)


@pytest.mark.asyncio
async def test_unique_exact_match_repairs_model_ambiguity_without_asking(catalog):
    step=ambiguous_step('销售额')
    # The fixture parser spans the complete exact metric name.
    step[1]['mentions'][0].update(surface='销售额',normalized_surface='销售额',end_char=3)
    pending,_=await ask(catalog,step)
    assert not isinstance(pending,RecognizedClarification)
    assert pending.plan['logical_plan']['payload']['measures'][0]['canonical_code']=='amount'


@pytest.mark.asyncio
async def test_model_ambiguity_without_governed_aliases_is_not_user_question(catalog):
    step=ambiguous_step('销售');step[1]['mentions'][0].update(surface='售',normalized_surface='售',start_char=1,end_char=2)
    with pytest.raises(RecognitionFailure,match='V2_CATALOG_AMBIGUITY_NOT_PROVEN'):await ask(catalog,step)


@pytest.mark.asyncio
@pytest.mark.parametrize('text',['第二季度销售额','1月份销售额','销售额和销售数量','不选第一个','11','重新查询销售额'])
async def test_unmatched_answer_is_not_forced_or_asked_again(catalog,text):
    pending,_=await ask(catalog)
    with pytest.raises(RecognitionFailure,match='V2_CONTEXT_UNRESOLVED'):
        await answer(catalog,pending,text)


@pytest.mark.asyncio
async def test_complete_new_request_bypasses_old_pending(catalog):
    pending,_=await ask(catalog)
    step=metric_step('销售额');step[1]['topic_shift_signals']=['EXPLICIT_NEW_TASK']
    engine,_=planner(catalog,[step])
    result=await engine.run(request(question='销售额',message_id='new'),IDENTITY,state=pending.next_state)
    assert result.resolution['dialogue_act']=='NEW_TASK'
    assert result.plan['logical_plan']['task_id']!=pending.pending_state.payload['task_id']
    assert next(iter(result.next_state.payload['pending_records'].values()))['status']=='SUSPENDED'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('payload_type','METRIC_DEFINITION'),('task_version',99),('task_id','foreign')])
async def test_pending_resume_cannot_relabel_query_or_task(catalog,field,value):
    pending,_=await ask(catalog)
    raw=deepcopy(pending.pending_state.model_dump(mode='json')['payload']);raw[field]=value
    tampered=envelope_with_payload(pending.pending_state,raw)
    with pytest.raises(RecognitionFailure,match='V2_PENDING_RESUME_MISMATCH'):
        await answer(catalog,pending,'1',pending=tampered)


@pytest.mark.asyncio
async def test_pending_add_remains_add(catalog):
    first=metric_step('销售额');engine,_=planner(catalog,[first])
    initial=await engine.run(request(question=first[0],message_id='first'),IDENTITY)
    step=ambiguous_step('再加销售',operation='ADD',follow=True);engine,_=planner(catalog,[step])
    pending=await engine.run(request(question=step[0],message_id='ask'),IDENTITY,state=initial.next_state,plans=[initial.plan_state])
    choice=next(o for o in pending.decision.options if o.canonical_ref.canonical_code=='quantity')
    final=await answer(catalog,pending,choice.display_label)
    assert {r['canonical_code'] for r in final.plan['logical_plan']['payload']['measures']}=={'amount','quantity'}


@pytest.mark.asyncio
async def test_pending_operation_cannot_change_during_restore(catalog):
    pending,_=await ask(catalog)
    raw=deepcopy(pending.pending_state.model_dump(mode='json')['payload'])
    raw['operations']={k:'ADD' for k in raw['operations']}
    with pytest.raises(RecognitionFailure,match='V2_PENDING_RESUME_MISMATCH'):
        await answer(catalog,pending,'1',pending=envelope_with_payload(pending.pending_state,raw))


@pytest.mark.asyncio
async def test_cross_scope_pending_cannot_restore(catalog):
    from app.semantic_v2.authorized_contract import ScopedArtifact
    pending,_=await ask(catalog);raw=pending.pending_state.model_dump(mode='json')
    raw['context']['authorized_scope']['knowledge_base_names']=['foreign']
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        await answer(catalog,pending,'1',pending=ScopedArtifact.model_validate(raw))


@pytest.mark.asyncio
async def test_pin_finish_failure_cannot_return_a_question(catalog):
    text,parsed,draft=ambiguous_step()
    def drift(c):
        value=draft(c)
        snapshot=catalog[-1][(81,(205,))]
        snapshot['documents'][0]['metrics'][0]['formula']='SUM(changed)'
        catalog[-1][(81,(205,))]=reseal(snapshot)
        return value
    with pytest.raises(ValueError,match='CATALOG_'):
        await ask(catalog,(text,parsed,drift))


@pytest.mark.asyncio
async def test_all_matching_alias_candidates_must_be_included(catalog):
    # A third governed meaning cannot disappear from a model-proposed list.
    source=catalog[-1][(81,(205,))]
    source['documents'][0]['metrics'].append(dict(metric_code='orders',metric_name='订单笔数',synonyms=['销售'],business_domain=205,formula='COUNT(order_id)'))
    catalog[-1][(81,(205,))]=reseal(source);publish(catalog[0],publication_id='release-2')
    text,parsed,draft=ambiguous_step()
    def omit(c):
        value=draft(c);value['ambiguities'][0]['candidate_handles']=value['ambiguities'][0]['candidate_handles'][:2];return value
    with pytest.raises(RecognitionFailure,match='V2_CATALOG_AMBIGUITY_NOT_PROVEN'):
        await ask(catalog,(text,parsed,omit))


@pytest.mark.asyncio
async def test_names_must_distinguish_choices(catalog):
    source=catalog[-1][(81,(205,))]
    source['documents'][0]['metrics'][1]['metric_name']='销售额'
    catalog[-1][(81,(205,))]=reseal(source);publish(catalog[0],publication_id='release-2')
    with pytest.raises(RecognitionFailure,match='V2_CATALOG_OPTION_LABELS_NOT_DISTINCT'):
        await ask(catalog)


@pytest.mark.asyncio
async def test_existing_asked_flag_cannot_be_reported_as_first_question(catalog):
    from app.semantic_v2.catalog_bridge import ScopedPlanSession
    from app.semantic_v2.pending_recognition import PendingResume,clarification_result
    from app.semantic_v2.state_machine import ConversationState
    result,_=await ask(catalog)
    state=ConversationState.model_validate(result.next_state.payload)
    session=ScopedPlanSession(request(question='再问一次',message_id='repeat'),IDENTITY,catalog[0])
    with pytest.raises(RecognitionFailure,match='V2_REPEATED_CLARIFICATION'):
        clarification_result(session,state,state.pending,PendingResume.model_validate(result.pending_state.payload),
            session._request,initial=False,previous_state=state)
    assert state.pending.question==result.question
