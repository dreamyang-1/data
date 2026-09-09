"""Operation contract safety on the real raw planner with controlled model HTTP.

These are integration/negative controls, not live-model or Whole Plan scores.
The production evaluator, recognition prompt and guards are not modified.
"""
from copy import deepcopy
import itertools

import pytest

from app.semantic_v2.authorized_contract import contract_digest
from app.semantic_v2.recognition_client import RecognitionFailure
from app.semantic_v2.slot_reducer import semantic_fingerprint
from tools.cutover.operation_round1 import parse_intervention
from test_v2_raw_turn_recognition import (
    IDENTITY, NOW, binding, edit, filtered_step, metric_step, parse, planner, request, turns,
)
from test_v2_structured_edits import catalog, filter_step, scalar, temporal_step, target
from test_v2_pending_recognition import catalog as pending_catalog, ask


def active(result):
    task = result.next_state.payload['tasks'][result.plan['logical_plan']['task_id']]
    version = next(v for v in task['versions'] if v['version'] == task['active_version'])
    return task, version


def operations(result):
    patch = result.resolution['task_patch']
    return [v for group in ('sets','adds','replacements','removes','clears') for v in patch[group]]


def assert_explicit(result, slot, semantic_operation, *, structured=False):
    ops = [o for o in operations(result) if o['slot_path'] == slot]
    assert ops and all(o['source'] == 'CURRENT_EXPLICIT' and o['evidence_mention_ids'] for o in ops)
    if structured:
        assert any(t.slot_path == slot and t.operation == semantic_operation for t in result.edit_trace)
    else:
        assert any(o['operation'] == semantic_operation for o in ops)


def timed_filter():
    text = '2025年上海销售额'
    parsed = parse(text, [('上海','FILTER_FIELD','filter_expression','SET'),
                          ('销售额','MEASURE','metrics','SET'), ('2025年','TIME_FIELD','time_spec','SET')])
    first = filtered_step()
    def draft(context):
        value = first[2](context)
        value['edits'].append(edit('time_spec', dict(anchor=binding(context,'订单日期','TIME_FIELD','m2','DIMENSION'),
            range=dict(start='2025-01-01T00:00:00+08:00',end_exclusive='2026-01-01T00:00:00+08:00'),
            grain='NONE',timezone='Asia/Shanghai',source='USER_EXPLICIT',as_of=NOW.isoformat()), ids=('m2',)))
        return value
    return text, parsed, draft


@pytest.mark.asyncio
@pytest.mark.parametrize('remove',['CLEAR','REMOVE'])
@pytest.mark.parametrize('follow',['INHERIT','TIME','ADD'])
async def test_explicit_empty_filter_survives_followup_with_versions_and_provenance(catalog, remove, follow):
    deletion = filter_step('不限地区' if remove == 'CLEAR' else '不要上海', remove,
                           None if remove == 'CLEAR' else scalar('上海'))
    if follow == 'ADD':
        next_step = metric_step('再加销售数量','销售数量','ADD',True)
    elif follow == 'TIME':
        next_step = temporal_step('改成2024年','RANGE','REPLACE',dict(
            start='2024-01-01T00:00:00+08:00',end_exclusive='2025-01-01T00:00:00+08:00'))
    else:
        next_step = ('继续',parse('继续',follow=True),dict(payload_type='INHERIT'))
    steps = [timed_filter(),deletion,next_step]
    engine, transport = planner(catalog,steps)
    first, cleared, after = await turns(engine,steps)
    before_digest = contract_digest(first.next_state.model_dump(mode='json'))
    assert first.plan['logical_plan']['payload']['filters'] is not None
    for result in [cleared,after]:
        task, version = active(result)
        assert version['semantics']['filter_expression'] is None
        assert result.plan['logical_plan']['payload']['filters'] is None
        assert 'filter_expression' in task['clear_barriers']
        assert result.next_state.context == first.next_state.context
    assert before_digest == contract_digest(first.next_state.model_dump(mode='json'))
    assert_explicit(cleared,'filter_expression',remove,structured=True)
    assert active(cleared)[0]['active_version'] == active(first)[0]['active_version'] + 1
    assert active(after)[0]['active_version'] == active(cleared)[0]['active_version'] + (follow != 'INHERIT')
    assert after.next_state.payload['state_version'] == cleared.next_state.payload['state_version'] + 1
    inherited = active(first)[1]['semantics']['metrics'][0]
    assert inherited in active(after)[1]['semantics']['metrics']
    assert 'turn0:' in inherited['source_mention_ids'][0]
    assert len(transport.calls) == 6


@pytest.mark.asyncio
async def test_duplicate_add_is_canonical_noop_but_current_patch_is_explicit(catalog):
    steps = [filtered_step(),metric_step('再加销售额','销售额','ADD',True)]
    before, after = await turns(planner(catalog,steps)[0],steps)
    assert len(active(after)[1]['semantics']['metrics']) == 1
    assert active(after)[0]['active_version'] == active(before)[0]['active_version']
    assert after.next_state.payload['state_version'] == before.next_state.payload['state_version'] + 1
    assert active(after)[1]['semantics']['filter_expression'] == active(before)[1]['semantics']['filter_expression']
    assert_explicit(after,'metrics','ADD')
    assert operations(after)[0]['new_value'][0]['source_mention_ids'] == ['turn1:m0']


@pytest.mark.asyncio
@pytest.mark.parametrize('many',[False,True])
async def test_replace_collection_only_changes_declared_slot(catalog,many):
    names = ['销售数量','销售额'] if many else ['销售数量']
    text = '换成'+'和'.join(names)
    parsed = parse(text,[(n,'MEASURE','metrics','REPLACE') for n in names],follow=True)
    def draft(c):
        values = [binding(c,n,'MEASURE','m'+str(i)) for i,n in enumerate(names)]
        return dict(payload_type='INHERIT',edits=[edit('metrics',values if many else values[0],
                                                     'REPLACE',tuple('m'+str(i) for i in range(len(names))))])
    steps=[timed_filter(),(text,parsed,draft)]
    before,after=await turns(planner(catalog,steps)[0],steps)
    state=active(after)[1]['semantics'];old=active(before)[1]['semantics']
    assert {v['canonical_code'] for v in state['metrics']} == ({'quantity','amount'} if many else {'quantity'})
    assert state['filter_expression']==old['filter_expression'] and state['time_spec']==old['time_spec']
    assert all(v['source_mention_ids'][0].startswith('turn1:') for v in state['metrics'])
    assert_explicit(after,'metrics','REPLACE')


@pytest.mark.asyncio
async def test_mixed_add_remove_canonical_members_is_atomic(catalog):
    text='不要销售额，再加销售数量'
    parsed=parse(text,[('销售额','MEASURE','metrics','REMOVE'),('销售数量','MEASURE','metrics','ADD')],follow=True)
    def draft(c):return dict(payload_type='INHERIT',edits=[
        edit('metrics',binding(c,'销售额','MEASURE'),'REMOVE'),
        edit('metrics',binding(c,'销售数量','MEASURE','m1'),'ADD',('m1',))])
    steps=[timed_filter(),(text,parsed,draft)]
    before,after=await turns(planner(catalog,steps)[0],steps)
    assert [v['canonical_code'] for v in active(after)[1]['semantics']['metrics']]==['quantity']
    assert active(after)[0]['active_version']==active(before)[0]['active_version']+1
    assert after.next_state.payload['state_version']==before.next_state.payload['state_version']+1
    assert active(before)[1]['semantics']['metrics'][0]['canonical_code']=='amount'
    assert_explicit(after,'metrics','ADD');assert_explicit(after,'metrics','REMOVE')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['missing_operand','overlap','bad_metric','slot_conflict'])
async def test_rejected_mixed_patch_leaves_state_barriers_versions_and_message_unchanged(catalog,fault):
    first=timed_filter();engine,_=planner(catalog,[first]);before=(await turns(engine,[first]))[0]
    saved=deepcopy(before.next_state.model_dump(mode='json'))
    text='不要上海，再加销售数量'
    parsed=parse(text,[('上海','FILTER_FIELD','filter_expression','REMOVE'),('销售数量','MEASURE','metrics','ADD')],follow=True)
    def draft(c):
        remove=dict(operation='REMOVE',target_handle=target(c),evidence_mention_ids=['m0'],value=scalar('上海'))
        metric=edit('metrics',binding(c,'销售数量','MEASURE','m1'),'ADD',('m1',))
        result=dict(payload_type='INHERIT',edits=[metric],filter_edits=[remove])
        if fault=='missing_operand':remove['value']=scalar('不存在的已绑定成员')
        if fault=='overlap':result['filter_edits'].append(deepcopy(remove))
        if fault=='bad_metric':metric['value']={'binding_handle':'unoffered'}
        if fault=='slot_conflict':result['edits'].append(edit('filter_expression',None,'CLEAR'))
        return result
    steps=[(text,parsed,draft)]
    engine,_=planner(catalog,steps)
    reason={'missing_operand':'V2_FILTER_REMOVE_VALUE_NOT_PRESENT','overlap':'V2_FILTER_OVERLAPPING_EDITS',
            'bad_metric':'V2_BINDING_HANDLE_NOT_OFFERED','slot_conflict':'V2_STRUCTURED_EDIT_CONFLICT'}[fault]
    with pytest.raises(RecognitionFailure,match=reason):
        await engine.run(request(question=text,message_id='rejected'),IDENTITY,state=before.next_state,plans=[before.plan_state])
    assert before.next_state.model_dump(mode='json')==saved
    assert 'rejected' not in saved['payload']['applied_messages']
    assert not active(before)[0]['clear_barriers']
    # The rejected message identity remains available for a fully valid retry.
    good=metric_step('再加销售数量','销售数量','ADD',True);retry,_=planner(catalog,[good])
    accepted=await retry.run(request(question=good[0],message_id='rejected'),IDENTITY,
                             state=before.next_state,plans=[before.plan_state])
    assert active(accepted)[0]['active_version']==active(before)[0]['active_version']+1
    assert accepted.next_state.payload['state_version']==before.next_state.payload['state_version']+1


@pytest.mark.asyncio
async def test_independent_slot_order_and_clear_add_do_not_change_other_slots(catalog):
    text='不限地区，再加销售数量'
    parsed=parse(text,[('不限地区','FILTER_FIELD','filter_expression','CLEAR'),('销售数量','MEASURE','metrics','ADD')],follow=True)
    results=[]
    for order in itertools.permutations(range(2)):
        def draft(c):
            edits=[edit('filter_expression',None,'CLEAR'),edit('metrics',binding(c,'销售数量','MEASURE','m1'),'ADD',('m1',))]
            return dict(payload_type='INHERIT',edits=[edits[i] for i in order])
        steps=[timed_filter(),(text,parsed,draft)]
        before,after=await turns(planner(catalog,steps)[0],steps)
        assert active(after)[1]['semantics']['time_spec']==active(before)[1]['semantics']['time_spec']
        assert 'filter_expression' in active(after)[0]['clear_barriers']
        assert active(after)[1]['semantics']['filter_expression'] is None
        assert_explicit(after,'filter_expression','CLEAR');assert_explicit(after,'metrics','ADD')
        results.append(active(after)[1]['semantics'])
    assert semantic_fingerprint(results[0])==semantic_fingerprint(results[1])


@pytest.mark.asyncio
@pytest.mark.parametrize('second',['ADD','REPLACE'])
async def test_conflicting_same_filter_target_cannot_apply_partial_clear(catalog,second):
    first=filtered_step();before=(await turns(planner(catalog,[first])[0],[first]))[0]
    saved=deepcopy(before.next_state.model_dump(mode='json'));text='清除并修改当前地区'
    parsed=parse(text,[(text,'FILTER_FIELD','filter_expression','CLEAR')],follow=True)
    parsed['operation_markers'].append(dict(slot_name='filter_expression',operation_hint=second,mention_id='m0'))
    def draft(c):return dict(payload_type='INHERIT',filter_edits=[
        dict(operation='CLEAR',target_handle=target(c),evidence_mention_ids=['m0']),
        dict(operation=second,target_handle=target(c),evidence_mention_ids=['m0'],value=scalar('江苏'))])
    engine,_=planner(catalog,[(text,parsed,draft)])
    with pytest.raises(RecognitionFailure,match='V2_FILTER_OVERLAPPING_EDITS'):
        await engine.run(request(question=text,message_id='conflict'),IDENTITY,state=before.next_state,plans=[before.plan_state])
    assert before.next_state.model_dump(mode='json')==saved


@pytest.mark.asyncio
async def test_operation_like_business_word_does_not_force_followup(catalog):
    new=metric_step('新增客户销售额','销售额')
    steps=[filtered_step(),new]
    before,after=await turns(planner(catalog,steps)[0],steps)
    assert before.plan['logical_plan']['task_id']!=after.plan['logical_plan']['task_id']
    assert after.resolution['dialogue_act']=='NEW_TASK'
    assert active(after)[1]['semantics']['filter_expression'] is None


@pytest.mark.asyncio
async def test_query_negation_stays_filter_condition_not_remove(catalog):
    text='查询非上海地区销售额';first=filtered_step()
    parsed=parse(text,[('上海','FILTER_FIELD','filter_expression','SET'),('销售额','MEASURE','metrics','SET')])
    def draft(c):
        d=first[2](c);d['edits'][1]['value']['operator']='NE';return d
    result=(await turns(planner(catalog,[(text,parsed,draft)])[0],[(text,parsed,draft)]))[0]
    assert result.plan['logical_plan']['payload']['filters']['operator']=='NE'
    assert not result.resolution['task_patch']['removes'] and not result.resolution['task_patch']['clears']


@pytest.mark.asyncio
async def test_cross_scope_operation_rejected_before_model_and_state_mutation(catalog):
    first=filtered_step();before=(await turns(planner(catalog,[first])[0],[first]))[0]
    saved=deepcopy(before.next_state.model_dump(mode='json'))
    step=metric_step('再加销售数量','销售数量','ADD',True);engine,transport=planner(catalog,[step])
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        await engine.run(request(question=step[0],message_id='cross',knowledge_base_names=['different-scope']),
                         IDENTITY,state=before.next_state,plans=[before.plan_state])
    assert not transport.calls and before.next_state.model_dump(mode='json')==saved


@pytest.mark.asyncio
async def test_explicit_operation_is_not_an_exact_pending_answer(pending_catalog):
    pending,_=await ask(pending_catalog);saved=deepcopy(pending.next_state.model_dump(mode='json'))
    step=metric_step('不要销售额','销售额','REMOVE',True);engine,_=planner(pending_catalog,[step])
    with pytest.raises(RecognitionFailure,match='V2_PENDING_ANSWER_EVIDENCE_REQUIRED'):
        await engine.run(request(question=step[0],message_id='remove'),IDENTITY,
                         state=pending.next_state,pending=pending.pending_state)
    assert pending.next_state.model_dump(mode='json')==saved


@pytest.mark.parametrize('variant',['MARKER_ONLY','DECLARED_SLOT_PROPOSITION'])
def test_oracle_alters_one_artifact_without_mutating_capture_or_other_evidence(variant):
    outputs=[dict(stage='v2_current_turn',output=dict(mentions=[{'surface':'presentation cue'}],
        operation_markers=[dict(slot_name='projection_spec',operation_hint='SET',mention_id='p')],
        explicit_slot_mentions={'projection_spec':['p']},query_shape_prediction='DETAIL_ROWS')),
        dict(stage='v2_semantic_edits',output={'payload_type':'DETAIL_ROWS','edits':[]})]
    before=deepcopy(outputs);after=parse_intervention(outputs,slot='projection_spec',variant=variant)
    assert before==outputs and after[1]==before[1]
    assert after[0]['output']['mentions']==before[0]['output']['mentions']
    assert after[0]['output']['query_shape_prediction']==before[0]['output']['query_shape_prediction']
    assert after[0]['output']['explicit_slot_mentions']==({} if variant=='DECLARED_SLOT_PROPOSITION' else {'projection_spec':['p']})
