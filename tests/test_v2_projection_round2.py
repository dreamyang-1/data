"""Projection obligations: positive and unresolved controls, no live model."""
from copy import deepcopy
import pytest
from app.semantic_v2.recognition_client import RecognitionFailure
from tools.cutover.projection_round2 import intervene
from test_v2_catalog_plans import catalog, detail
from test_v2_raw_turn_recognition import parse, planner, turns


@pytest.mark.asyncio
async def test_role_hypothesis_alone_does_not_become_explicit_projection(catalog):
    step=detail();step[1]['mentions'][0]['candidate_roles'].append('PROJECTION_FIELD')
    result=(await turns(planner(catalog,[step])[0],[step]))[0]
    assert result.plan['logical_plan']['payload']['projection_spec']['mode']=='SEMANTIC_DEFAULT'
    ops=result.resolution['task_patch']['sets']
    display=next(o for o in ops if o['slot_path']=='projection_spec')
    assert display['source']=='CURRENT_REFERENCE_RESOLUTION'
    assert display['reason_code']=='PINNED_CATALOG_MAIN_ATTRIBUTES'
    assert not display['evidence_mention_ids']


def unresolved_step(mode):
    first=detail();text='列出医院和自定义评分字段'
    parsed=parse(text,[('医院','SUBJECT_ENTITY','subject','SET'),
                       ('自定义评分字段','PROJECTION_FIELD','projection_spec','SET')],shape='DETAIL_ROWS')
    if mode=='slot_only':parsed['operation_markers']=parsed['operation_markers'][:1]
    if mode=='unsafe_demote':
        outputs=[{'stage':'v2_current_turn','output':parsed}]
        parsed=intervene(outputs,variant='B_OBLIGATION',mention_ids=['m1'])[0]['output']
    def draft(c):
        assert not any(v['name']=='自定义评分字段' for v in c['catalog_candidates'])
        result=first[2](c)
        if mode=='unresolved':result['unresolved_mention_ids']=['m1']
        return result
    return text,parsed,draft


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,reason',[('omitted','V2_EXPLICIT_OPERATION_DROPPED'),
    ('slot_only','V2_EXPLICIT_SLOT_DROPPED'),('unresolved','V2_RECOGNITION_UNRESOLVED')])
async def test_real_explicit_field_cannot_be_swallowed_when_unbound(catalog,mode,reason):
    step=unresolved_step(mode)
    with pytest.raises(RecognitionFailure,match=reason):
        await turns(planner(catalog,[step])[0],[step])


@pytest.mark.asyncio
async def test_blanket_demotion_would_silently_lose_true_requested_field(catalog):
    # Counterfactual only: proves why Oracle B is NOT a safe runtime policy.
    step=unresolved_step('unsafe_demote')
    result=(await turns(planner(catalog,[step])[0],[step]))[0]
    projection=result.plan['logical_plan']['payload']['projection_spec']
    assert projection['mode']=='SEMANTIC_DEFAULT'
    assert all(i['ref']['display_name']!='自定义评分字段' for i in projection['items'])


@pytest.mark.parametrize('variant',['A_CLASSIFICATION','B_OBLIGATION','C_BOUNDARY'])
def test_oracle_preserves_capture_and_only_changes_single_parse(variant):
    text='实体列表';p=parse(text,[(text,'SUBJECT_ENTITY','subject','SET')])
    p['mentions'][0]['candidate_roles'].append('PROJECTION_FIELD')
    p['explicit_slot_mentions']['projection_spec']=['m0']
    p['operation_markers'].append(dict(slot_name='projection_spec',mention_id='m0',operation_hint='SET'))
    outputs=[dict(stage='v2_current_turn',output=p),dict(stage='v2_semantic_edits',output={'edits':[]})]
    before=deepcopy(outputs)
    result=intervene(outputs,variant=variant,mention_ids=['m0'],
        boundary=dict(surface='实体',normalized_surface='实体',start_char=0,end_char=2) if variant=='C_BOUNDARY' else None)
    assert outputs==before and result[1]==before[1]
    if variant=='B_OBLIGATION':assert result[0]['output']['mentions']==p['mentions']
    if variant=='C_BOUNDARY':assert result[0]['output']['operation_markers']==p['operation_markers']
