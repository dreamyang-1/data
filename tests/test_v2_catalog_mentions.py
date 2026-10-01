from copy import deepcopy
import json

import pytest

from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.catalog_mentions import recover_metric_spans
from app.semantic_v2.pipeline import CurrentTurnSemanticParse, CurrentTurnParser
from test_v2_raw_turn_recognition import (catalog, parse, binding, edit, metric_step, planner, turns,
    request, IDENTITY, reseal, publish)


def split_step(text='订单笔数',operation='SET',follow=False):
    parsed=parse(text,[('订单','SUBJECT_ENTITY','subject','SET'),('笔数','MEASURE','metrics',operation)],
        follow=follow,shape=None if follow else 'SCALAR_AGGREGATE')
    def draft(c):
        assert [m['surface'] for m in c['parse']['mentions']]==['订单笔数']
        assert not c['parse']['explicit_slot_mentions']['subject']
        assert all(x['mention_id']=='m1' for x in c['catalog_candidates'])
        return dict(payload_type='INHERIT' if follow else 'SCALAR_AGGREGATE',
            edits=[edit('metrics',[binding(c,'订单笔数','MEASURE','m1')],operation,('m1',))])
    return text,parsed,draft


def recover(catalog,text,raw):
    value=deepcopy(raw)
    for m in value['mentions']:m['source_turn_id']='current'
    parsed=CurrentTurnSemanticParse.model_validate(value)
    session=ScopedPlanSession(request(question=text,message_id='current'),IDENTITY,catalog[0])
    result,trace=recover_metric_spans(session,parsed,text=text)
    CurrentTurnParser.parse(text=text,turn_id='current',text_ref='current',parsed=result)
    return parsed,result,trace


@pytest.mark.asyncio
async def test_exact_catalog_compound_survives_raw_parse_binding_reducer_and_plan(catalog,caplog):
    steps=[split_step()];original=deepcopy(steps[0][1])
    with caplog.at_level('INFO',logger='app.semantic_v2.recognition'):
        result=(await turns(planner(catalog,steps)[0],steps))[0]
    assert steps[0][1]==original
    payload=result.plan['logical_plan']['payload']
    assert payload['payload_type']=='SCALAR_AGGREGATE' and payload['measures'][0]['canonical_code']=='orders'
    assert result.plan['backend_contract']['mode']=='SHADOW_ONLY'
    trace=next(r.catalog_span_trace for r in caplog.records if hasattr(r,'catalog_span_trace'))
    assert trace[0]['canonical_selection_performed'] is False
    assert '订单' not in json.dumps(trace,ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,expected',[
    ('ADD',{'amount','orders'}),('REPLACE',{'orders'}),('REMOVE',{'amount'})])
async def test_compound_boundary_recovery_preserves_metric_operation_and_history(catalog,operation,expected):
    first=metric_step('销售额')
    steps=[first]
    if operation=='REMOVE':steps.append(metric_step('再加订单笔数','订单笔数','ADD',True))
    steps.append(split_step({'ADD':'再加订单笔数','REPLACE':'换成订单笔数','REMOVE':'不要订单笔数'}[operation],operation,True))
    results=await turns(planner(catalog,steps)[0],steps)
    assert {m['canonical_code'] for m in results[-1].plan['logical_plan']['payload']['measures']}==expected
    assert results[-1].plan['logical_plan']['task_id']==results[0].plan['logical_plan']['task_id']


@pytest.mark.parametrize('fault',[
    'measure_multirole','prefix_dimension','different_clause','different_negation','nonexplicit',
    'modifier','referenced_modifier','coordination','temporal_reference','prefix_remove',
    'prefix_other_slot','measure_other_slot','measure_operation_conflict','no_metric_slot','overlapping_mention',
    'negation_flag_conflict'])
def test_independent_or_ambiguous_semantic_evidence_is_not_erased(catalog,fault):
    text,raw,_=split_step()
    if fault=='measure_multirole':raw['mentions'][1]['candidate_roles'].append('GROUP_BY')
    if fault=='prefix_dimension':raw['mentions'][0]['candidate_roles']=['GROUP_BY']
    if fault=='different_clause':raw['mentions'][0]['clause_id']='another-clause'
    if fault=='different_negation':raw['mentions'][0]['negated']=True
    if fault=='nonexplicit':raw['mentions'][0]['explicit']=False
    if fault=='modifier':raw['mentions'][0]['modifier_ids']=['m1']
    if fault=='referenced_modifier':raw['mentions'][1]['modifier_ids']=['m0']
    if fault=='coordination':raw['coordination_groups']=[['m0','m1']]
    if fault=='temporal_reference':raw['temporal_expressions']=['m0']
    if fault=='prefix_remove':raw['operation_markers'][0]['operation_hint']='REMOVE'
    if fault=='prefix_other_slot':raw['explicit_slot_mentions']['dimensions']=['m0']
    if fault=='measure_other_slot':raw['explicit_slot_mentions']['dimensions']=['m1']
    if fault=='measure_operation_conflict':raw['operation_markers'].append(dict(mention_id='m1',slot_name='metrics',operation_hint='REMOVE'))
    if fault=='no_metric_slot':raw['explicit_slot_mentions']['metrics']=[]
    if fault=='overlapping_mention':raw['mentions'].append(dict(raw['mentions'][0],mention_id='whole',surface=text,normalized_surface=text,end_char=len(text)))
    if fault=='negation_flag_conflict':raw['negations']=['m0']
    original,result,trace=recover(catalog,text,raw)
    assert trace==[] and result==original


@pytest.mark.parametrize('text', ['订单的笔数','订单和笔数','订单 笔数','订单笔数和订单笔数'])
def test_noncontiguous_or_repeated_full_terms_are_not_merged(catalog,text):
    _,raw,_=split_step(text)
    original,result,trace=recover(catalog,text,raw)
    assert not trace and result==original


@pytest.mark.parametrize('collision',['metric','attribute_name','attribute_alias','attribute_code'])
def test_exact_full_term_cross_role_or_identity_collision_preserves_original_hypotheses(catalog,collision):
    source=deepcopy(catalog[4][(81,(205,))]);doc=source['documents'][0]
    if collision=='metric':doc['metrics'].append(dict(metric_code='other_order_metric',metric_name='订单笔数',formula='COUNT(other)',business_domain=205))
    else:
        attr=doc['entities'][0]['attributes'][0]
        attr[{'attribute_name':'attr_name','attribute_alias':'synonyms','attribute_code':'attr_code'}[collision]]='订单笔数'
    catalog[4][(81,(205,))]=reseal(source);publish(catalog[0],publication_id='full-term-collision')
    text,raw,_=split_step();original,result,trace=recover(catalog,text,raw)
    assert not trace and result==original


def test_recovery_never_creates_an_unknown_metric_or_rewrites_foreign_turns(catalog):
    text='出货笔数';raw=parse(text,[('出货','SUBJECT_ENTITY','subject','SET'),('笔数','MEASURE','metrics','SET')])
    original,result,trace=recover(catalog,text,raw);assert not trace and result==original
    text,raw,_=split_step()
    session=ScopedPlanSession(request(question=text,message_id='current'),IDENTITY,catalog[0])
    with pytest.raises(ValueError,match='SURFACE_PARSE_FAILURE'):
        recover_metric_spans(session,CurrentTurnSemanticParse.model_validate(raw),text=text)


def test_consistent_negation_references_follow_the_complete_current_span(catalog):
    text,raw,_=split_step('不要订单笔数','REMOVE',True)
    for mention in raw['mentions']:mention['negated']=True
    raw['negations']=['m0','m1']
    original,result,trace=recover(catalog,text,raw)
    assert trace and result.negations==['m1'] and result.mentions[0].negated
    assert result.operation_markers[0].operation_hint=='REMOVE'
    assert original.negations==['m0','m1']
