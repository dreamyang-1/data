from copy import deepcopy
import json

import httpx
import pytest

from app.config import Settings
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2.recognition_client import RecognitionModelClient
from test_v2_source_value_binding import catalog as base_catalog, source_step, referenced_edit, values
from test_v2_raw_turn_recognition import ScriptedTransport, turns, IDENTITY, request, NOW, parse, metric_step
from test_v2_structured_edits import target


@pytest.fixture
def catalog(base_catalog, monkeypatch):
    import catalog_value_sources as native
    base_catalog[5]['city'][:] = ['甲市','乙市','丙市']
    probes = []
    def rows(scope, field, limit):
        assert scope['semantic_model_id'] == 81 and scope['business_domain_ids'] == [205]
        assert field['attr_code'] == 'city'
        probes.append((deepcopy(field),limit))
        return sorted(base_catalog[5]['city'])[:limit+1]
    monkeypatch.setattr(native, 'query_probe_values', rows)
    return base_catalog, probes


def engine(catalog, steps, decisions=None):
    scripted = ScriptedTransport(steps); choices=[]
    decisions = iter(decisions or [])
    def transport(req):
        body=json.loads(req.content); context=json.loads(body['messages'][1]['content'])
        if 'candidates' not in context or 'mention' not in context: return scripted(req)
        choices.append(context)
        choice=next(decisions)
        output=choice(context) if callable(choice) else choice
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]})
    settings=Settings(_env_file=None,intent_model_api_key='test-key',intent_model_base_url='https://model.invalid/v1',intent_model_max_retries=0)
    return RawTurnPlanner(RecognitionModelClient(settings,httpx.MockTransport(transport)),catalog[0][0],clock=lambda:NOW), choices


def accept(value):
    return lambda c: dict(status='ACCEPTED',candidate_id=next(r['candidate_id'] for r in c['candidates'] if r['value']==value))


@pytest.mark.asyncio
async def test_real_offered_choice_is_exactly_revalidated_with_shadow_output(catalog):
    step=source_step('甲'); planner,choices=engine(catalog,[step],[accept('甲市')])
    result=(await turns(planner,[step]))[0]
    assert values(result)==['甲市'] and len(choices)==1 and len(catalog[1])==2
    assert choices[0]['field']['code']=='city' and 'tasks' not in choices[0]
    assert catalog[0][6]==[('city','甲'),('city','甲市'),('city','甲'),('city','甲市')]
    proof=result.next_state.source_value_bindings[0]
    assert proof.canonical_value=='甲市' and proof.ref.source_mention_ids==('turn0:m0',)
    assert result.plan['backend_contract']['mode']=='SHADOW_ONLY'


@pytest.mark.asyncio
async def test_probe_add_replace_remove_clear_do_not_change_slot_operations(catalog):
    clear=('不限城市',parse('不限城市',[('城市','FILTER_FIELD','filter_expression','CLEAR')],follow=True),
        lambda c:dict(payload_type='INHERIT',filter_edits=[dict(operation='CLEAR',target_handle=target(c),evidence_mention_ids=['m0'])]))
    steps=[source_step('甲'),referenced_edit('乙','ADD'),referenced_edit('丙','REPLACE'),
        referenced_edit('甲','ADD'),referenced_edit('丙','REMOVE'),clear,metric_step('销售额',follow=True)]
    planner,_=engine(catalog,steps,[accept(v) for v in ['甲市','乙市','丙市','甲市','丙市']])
    results=await turns(planner,steps)
    assert [values(r) for r in results]==[['甲市'],['甲市','乙市'],['丙市'],['丙市','甲市'],['甲市'],[],[]]


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['REJECTED','AMBIGUOUS','UNRESOLVED'])
async def test_uncertainty_does_not_choose_the_nearest_or_ask_for_a_metric(catalog,status):
    step=source_step('甲'); planner,_=engine(catalog,[step],[dict(status=status,candidate_id=None)])
    code='V2_SOURCE_VALUE_NOT_FOUND' if status=='REJECTED' else 'V2_SOURCE_VALUE_PROBE_CHOICE_'+status
    with pytest.raises(ValueError,match=code): await turns(planner,[step])


@pytest.mark.asyncio
@pytest.mark.parametrize('choice',[{'status':'ACCEPTED','candidate_id':'invented'},
    {'status':'AMBIGUOUS','candidate_id':'invented'}, {'status':'ACCEPTED','candidate_id':None},
    {'status':'ACCEPTED','candidate_id':'invented','business_domain_ids':[]}])
async def test_model_cannot_create_candidates_scope_or_output_fields(catalog,choice):
    step=source_step('甲'); planner,_=engine(catalog,[step],[choice])
    with pytest.raises(ValueError): await turns(planner,[step])


@pytest.mark.asyncio
async def test_large_field_is_not_sent_to_model(catalog):
    catalog[0][5]['city'][:]=[str(i) for i in range(65)]
    step=source_step('甲'); planner,choices=engine(catalog,[step])
    with pytest.raises(ValueError,match='CARDINALITY_EXCEEDED'): await turns(planner,[step])
    assert choices==[]


@pytest.mark.asyncio
async def test_selected_value_disappearing_before_exact_read_cannot_bind(catalog):
    def mutate(context):
        decision=accept('甲市')(context);catalog[0][5]['city'].remove('甲市');return decision
    step=source_step('甲'); planner,_=engine(catalog,[step],[mutate])
    with pytest.raises(ValueError,match='SELECTION_NOT_CURRENT'): await turns(planner,[step])


@pytest.mark.asyncio
async def test_adding_an_alternative_during_model_choice_rejects_finish(catalog):
    def mutate(context):
        catalog[0][5]['city'].append('甲县');return accept('甲市')(context)
    step=source_step('甲'); planner,_=engine(catalog,[step],[mutate])
    with pytest.raises(ValueError,match='CHANGED_DURING_READ'): await turns(planner,[step])


@pytest.mark.asyncio
async def test_exact_match_does_not_probe_or_add_a_model_call(catalog):
    step=source_step('甲市'); planner,choices=engine(catalog,[step])
    assert values((await turns(planner,[step]))[0])==['甲市']
    assert choices==[] and catalog[1]==[]
