"""Generation agrees with native filter channels; no predicate is auto-invented."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from app.semantic_v2.recognition import semantic_task_schema
from test_v2_filter_generation_contract import predicate
from test_v2_source_value_binding import catalog, source_step, source_edit, values
from test_v2_raw_turn_recognition import planner, turns, metric_step


def generation(operation='SET', relation='NEW_TASK'):
    parsed=SimpleNamespace(reference_signals=[],topic_shift_signals=[],
        operation_markers=[SimpleNamespace(slot_name=slot,operation_hint=operation)
                           for slot in ('filter_expression','metrics')])
    return semantic_task_schema(parsed,{},context_relation=relation)


def output(operation='SET'):
    item=predicate()
    result=dict(payload_type='SCALAR_AGGREGATE',source_value_requests=[dict(
        request_id='request',mention_id='m0',field_binding_handles=['offered'])])
    if operation=='SET':result['edits']=[dict(slot_path='filter_expression',operation='SET',
        value=item,evidence_mention_ids=['m0'])]
    else:result['filter_edits']=[dict(operation=operation,target_handle=None,
        value=item,evidence_mention_ids=['m0'])]
    return result


@pytest.mark.parametrize('operation',['SET','ADD'])
def test_declared_operation_selects_existing_filter_channel(operation):
    schema=generation(operation);Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(output(operation))
    assert not Draft202012Validator(schema).is_valid(output('ADD' if operation=='SET' else 'SET'))


@pytest.mark.parametrize('fault',['missing_filter','both_channels','bare_value','wrong_tree','unknown_field'])
def test_generation_rejects_missing_or_competing_filter_structure(fault):
    value=output()
    if fault=='missing_filter':value.pop('edits')
    if fault=='both_channels':value['filter_edits']=output('ADD')['filter_edits']
    if fault=='bare_value':value['edits'][0]['value']={'value_request_id':'request'}
    if fault=='wrong_tree':value['edits'][0]['value']={'node_type':'BOOLEAN_GROUP','operator':'OR','children':[]}
    if fault=='unknown_field':value['edits'][0]['value']['invented']=True
    assert not Draft202012Validator(generation()).is_valid(value)


@pytest.mark.parametrize('operator',['AND','OR','NOT'])
def test_whole_filter_schema_keeps_boolean_structure(operator):
    value=output();item=value['edits'][0]['value']
    value['edits'][0]['value']={'node_type':'BOOLEAN_GROUP','operator':operator,
        'children':[item] if operator=='NOT' else [item,deepcopy(item)]}
    assert Draft202012Validator(generation()).is_valid(value)


@pytest.mark.parametrize('operation',['ADD','REPLACE','REMOVE','CLEAR'])
def test_existing_task_structured_generation_is_unchanged(operation):
    value=dict(payload_type='INHERIT',filter_edits=[dict(operation=operation,target_handle='current',
        evidence_mention_ids=['m0'],value=None if operation=='CLEAR' else {'value_request_id':'request'})])
    assert Draft202012Validator(generation('SET','MODIFY')).is_valid(value)


@pytest.mark.parametrize('operation',['SET','ADD'])
def test_new_metric_edit_cannot_reinterpret_uniform_current_marker(operation):
    value=dict(payload_type='SCALAR_AGGREGATE',edits=[dict(slot_path='metrics',operation=operation,
        evidence_mention_ids=['m0'],value=[{'binding_handle':'offered'}])])
    validator=Draft202012Validator(generation(operation));assert validator.is_valid(value)
    value['edits'][0]['operation']='ADD' if operation=='SET' else 'SET'
    assert not validator.is_valid(value)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['SET','ADD'])
async def test_empty_filter_and_source_consumption_use_native_compiler(catalog,operation):
    text,parsed,original=source_step()
    if operation=='ADD':parsed['operation_markers'][0]['operation_hint']='ADD'
    def draft(context):
        value=original(context)
        if operation=='ADD':
            current=value['edits'].pop()
            value['filter_edits']=[dict(operation='ADD',target_handle=None,
                evidence_mention_ids=current['evidence_mention_ids'],value=current['value'])]
        return value
    steps=[(text,parsed,draft),source_edit('北京','ADD'),source_edit('江苏','REPLACE')]
    result=await turns(planner(catalog,steps)[0],steps)
    assert [values(r) for r in result]==[['上海'],['上海','北京'],['江苏']]
    assert result[0].next_state.source_value_bindings


@pytest.mark.asyncio
@pytest.mark.parametrize('operator',['AND','OR','NOT'])
async def test_initial_boolean_filter_is_not_flattened_by_generation(catalog,operator):
    text,parsed,original=source_step()
    def draft(context):
        value=original(context);item=value['edits'][-1]['value']
        value['edits'][-1]['value']=dict(node_type='BOOLEAN_GROUP',operator=operator,
            children=[item] if operator=='NOT' else [item,deepcopy(item)])
        return value
    steps=[(text,parsed,draft)];result=(await turns(planner(catalog,steps)[0],steps))[0]
    expression=result.plan['logical_plan']['payload']['filters']
    assert expression['operator']==operator
    assert len(expression['children'])==(1 if operator=='NOT' else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['unused','wrong_reference','unknown_reference','duplicate_request',
    'wrong_field','wrong_handle','wrong_scope','extra_request','operation_conflict','both_channels'])
async def test_bad_source_or_operation_still_rejects_without_publishing(catalog,fault):
    text,parsed,original=source_step()
    def draft(context):
        value=original(context);f=value['edits'][-1]
        if fault=='unused':value['edits'].pop()
        if fault=='wrong_reference':f['value']['field_ref']={'value_field_request_id':'unknown'}
        if fault=='unknown_reference':f['value']['value']={'value_request_id':'unknown'}
        if fault=='duplicate_request':value['source_value_requests']*=2
        if fault=='wrong_field':f['value']['field_ref']={'binding_handle':'not_offered_field'}
        if fault=='wrong_handle':value['source_value_requests'][0]['field_binding_handles']=['forged']
        if fault=='wrong_scope':f['value']['scope']='CURRENT_DATASET'
        if fault=='extra_request':value['source_value_requests'].append(dict(
            request_id='extra',mention_id='m0',field_binding_handles=value['source_value_requests'][0]['field_binding_handles']))
        if fault in {'operation_conflict','both_channels'}:
            value['filter_edits']=[dict(operation='ADD',target_handle=None,value=f['value'],evidence_mention_ids=['m0'])]
            if fault=='operation_conflict':value['edits'].pop()
        return value
    steps=[(text,parsed,draft)]
    with pytest.raises(ValueError):await turns(planner(catalog,steps)[0],steps)
