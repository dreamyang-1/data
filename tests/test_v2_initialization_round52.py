"""Empty-task encodings must not weaken modification, binding or time safety."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from app.semantic_v2.recognition_client import RecognitionFailure
from test_v2_raw_turn_recognition import (catalog as metric_catalog,parse,binding,edit,planner,turns,metric_step)
from test_v2_explicit_time import catalog as time_catalog
from test_v2_source_value_binding import catalog as source_catalog,source_step,values,referenced_edit
from test_v2_structured_edits import target


@pytest.fixture
def base_catalog(metric_catalog):return metric_catalog


def unmarked_metrics():
    text='销售额和订单笔数'
    parsed=parse(text,[('销售额','MEASURE','metrics',None),('订单笔数','MEASURE','metrics',None)])
    def draft(c):return dict(payload_type='SCALAR_AGGREGATE',edits=[
        edit('metrics',binding(c,'销售额','MEASURE','m0'),'ADD',('m0',)),
        edit('metrics',binding(c,'订单笔数','MEASURE','m1'),'ADD',('m1',))])
    return text,parsed,draft


@pytest.mark.asyncio
async def test_initial_add_items_form_one_assignment_and_followup_add_preserves_them(metric_catalog):
    first=unmarked_metrics();steps=[first,metric_step('再加销售数量','销售数量','ADD',True)]
    results=await turns(planner(metric_catalog,steps)[0],steps)
    assert {r['canonical_code'] for r in results[0].plan['logical_plan']['payload']['measures']}=={'amount','orders'}
    assert {r['canonical_code'] for r in results[1].plan['logical_plan']['payload']['measures']}=={'amount','orders','quantity'}
    assert len(results[0].resolution['task_patch']['sets'])==1
    assert results[1].resolution['task_patch']['adds']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['followup','remove_marker','no_explicit','foreign_handle','wrong_role','mixed','cross_mention'])
async def test_initialization_cannot_repair_conflicting_or_unauthorized_edits(metric_catalog,fault):
    text,parsed,draft=unmarked_metrics()
    if fault=='remove_marker':parsed['operation_markers']=[dict(slot_name='metrics',operation_hint='REMOVE',mention_id='m0')]
    if fault=='no_explicit':parsed['explicit_slot_mentions']={}
    if fault=='followup':parsed['reference_signals']=['ELLIPSIS']
    if fault=='wrong_role':parsed['mentions'][0]['candidate_roles'].append('GROUP_BY')
    def faulty(c):
        result=draft(c)
        if fault=='foreign_handle':result['edits'][0]['value']={'binding_handle':'foreign'}
        if fault=='wrong_role':result['edits'][0]['value']=binding(c,'城市','GROUP_BY','m0','DIMENSION')
        if fault=='cross_mention':result['edits'][0]['value']=binding(c,'销售数量','MEASURE','m1')
        if fault=='mixed':result['edits'][1]['operation']='REMOVE'
        return result
    steps=([metric_step('销售额')] if fault=='followup' else [])+[(text,parsed,faulty)]
    with pytest.raises(RecognitionFailure):await turns(planner(metric_catalog,steps)[0],steps)


@pytest.mark.asyncio
async def test_unmarked_initial_filter_add_preserves_exact_source_binding(source_catalog):
    text,parsed,original=source_step()
    parsed['operation_markers']=[]
    def draft(c):
        result=original(c);predicate=result['edits'].pop()
        result['filter_edits']=[dict(operation='ADD',target_handle=None,
            evidence_mention_ids=predicate['evidence_mention_ids'],value=predicate['value'])]
        return result
    steps=[(text,parsed,draft)]
    result=(await turns(planner(source_catalog,steps)[0],steps))[0]
    assert values(result)==['上海'] and result.plan_state.source_value_bindings


def range_step(*,fault=None):
    text='2025年订单笔数'
    parsed=parse(text,[('2025年','TIME_RANGE','time_spec',None),('订单笔数','MEASURE','metrics',None)])
    def draft(c):
        value=dict(start='1990-01-01T00:00:00+08:00',end_exclusive='1991-01-01T00:00:00+08:00')
        if fault=='authority':value['canonical_id']='invented'
        return dict(payload_type='SCALAR_AGGREGATE',edits=[edit('metrics',[binding(c,'订单笔数','MEASURE','m1')],ids=('m1',))],
            temporal_edits=[dict(component='RANGE',operation='REPLACE' if fault=='replace' else 'SET',
                evidence_mention_ids=['m0'],value=value)])
    return text,parsed,draft


@pytest.mark.asyncio
async def test_initial_range_uses_governed_anchor_and_current_calendar_without_field_handle(time_catalog):
    first=range_step();steps=[first]
    engine,transport=planner(time_catalog,steps);result=(await turns(engine,steps))[0]
    value=result.plan['logical_plan']['payload']['time']
    assert value['anchor']['canonical_code']=='created_at' and value['grain']=='NONE'
    assert datetime.fromisoformat(value['range']['start'])==datetime.fromisoformat('2025-01-01T00:00:00+08:00')
    assert result.resolution['task_patch']['sets'][-1]['reason_code']=='CURRENT_INITIAL_TIME_COMPONENTS'
    assert len(transport.calls)==2
    schema=json.loads(transport.calls[1]['messages'][0]['content'].split('JSON Schema:\n',1)[1])
    assert 'time_spec' not in schema['$defs']['SlotEditDraft']['properties']['slot_path']['enum']
    assert 'RANGE/SET' in schema['properties']['temporal_edits']['description']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['replace','authority'])
async def test_initial_range_does_not_erase_invalid_component_contract(time_catalog,fault):
    steps=[range_step(fault=fault)]
    with pytest.raises((RecognitionFailure,ValueError)):await turns(planner(time_catalog,steps)[0],steps)


@pytest.mark.asyncio
async def test_missing_catalog_time_anchor_still_refuses(metric_catalog):
    steps=[range_step()]
    with pytest.raises(RecognitionFailure,match='CATALOG_METRIC_TIME_ANCHOR_MISSING'):
        await turns(planner(metric_catalog,steps)[0],steps)


@pytest.mark.asyncio
async def test_source_generation_schema_excludes_dimensions_without_changing_candidate_set(source_catalog):
    text,parsed,draft=source_step();parsed['mentions'][0]['candidate_roles'].append('FILTER_FIELD')
    steps=[(text,parsed,draft)];engine,transport=planner(source_catalog,steps)
    await turns(engine,steps)
    body=transport.calls[1];context=json.loads(body['messages'][1]['content'])
    schema=json.loads(body['messages'][0]['content'].split('JSON Schema:\n',1)[1])
    fields=schema['$defs']['SourceValueRequestDraft']['properties']['field_binding_handles']['items']['enum']
    candidates={c['binding_handle']:c for c in context['catalog_candidates']}
    assert fields and all(candidates[h]['catalog_type']=='ATTRIBUTE' and candidates[h]['role']=='FILTER_FIELD' for h in fields)
    assert any(c['catalog_type']=='DIMENSION' for c in candidates.values())


@pytest.mark.asyncio
async def test_initial_filter_adapter_keeps_the_original_current_task_scope_requirement(source_catalog):
    text,parsed,original=source_step();parsed['operation_markers']=[]
    def draft(c):
        result=original(c);predicate=result['edits'].pop();predicate['value']['scope']='CURRENT_DATASET'
        result['filter_edits']=[dict(operation='ADD',target_handle=None,
            evidence_mention_ids=predicate['evidence_mention_ids'],value=predicate['value'])]
        return result
    steps=[(text,parsed,draft)]
    with pytest.raises(RecognitionFailure,match='V2_SLOT_OPERATION_CONFLICT'):
        await turns(planner(source_catalog,steps)[0],steps)


@pytest.mark.asyncio
@pytest.mark.parametrize('declared,emitted',[('REMOVE','CLEAR'),('CLEAR','REMOVE')])
async def test_equivalent_subtree_deletion_keeps_clear_barrier_through_followup(source_catalog,declared,emitted):
    text='取消城市条件'
    current=(text,parse(text,[('城市','FILTER_FIELD','filter_expression',declared)],follow=True),
        lambda c:dict(payload_type='INHERIT',filter_edits=[dict(operation=emitted,
            target_handle=target(c,'城市'),evidence_mention_ids=['m0'],value=None)]))
    steps=[source_step(),current,metric_step('销售额',follow=True)]
    results=await turns(planner(source_catalog,steps)[0],steps)
    assert [values(r) for r in results]==[['上海'],[],[]]
    task=next(iter(results[1].next_state.payload['tasks'].values()))
    assert 'filter_expression' in task['clear_barriers']
    assert results[1].edit_trace[0].operation==declared


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['member_value','foreign_target'])
async def test_subtree_equivalence_never_converts_member_removal_or_forged_target(source_catalog,fault):
    text,parsed,draft=referenced_edit('上海','REMOVE')
    parsed['operation_markers'][0]['operation_hint']='CLEAR'
    def bad(c):
        result=draft(c)
        if fault=='foreign_target':
            result['source_value_requests']=[]
            result['filter_edits'][0].update(target_handle='foreign',value=None)
        return result
    steps=[source_step(),(text,parsed,bad)]
    with pytest.raises(RecognitionFailure):await turns(planner(source_catalog,steps)[0],steps)
