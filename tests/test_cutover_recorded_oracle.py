from copy import deepcopy

import httpx
import pytest

from tools.cutover.evaluation_contract import digest
from tools.cutover.run_raw_transition_benchmark import run, replay_turn
from test_cutover_raw_transition_benchmark import inputs
from test_v2_raw_turn_recognition import ScriptedTransport


async def recorded(inputs,index=-1):
    rows,catalog,raw,settings,steps=inputs;captures=[]
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    assert result['predictions'][0]['status']=='OK'
    return captures[index],rows[0],catalog,raw


@pytest.mark.asyncio
@pytest.mark.parametrize('index',[0,-1])
async def test_recorded_current_turn_reproduces_the_original_pin_state_and_plan(inputs,index):
    capture,case,catalog,raw=await recorded(inputs,index)
    receipt,result=await replay_turn(capture,case,catalog,raw,expected_capture_hash=digest(capture))
    assert receipt['mode']=='RECORDED_MODEL_PIPELINE' and receipt['failure'] is None
    assert len(receipt['model_stages'])==2 and all(c['context_matches_recording'] for c in receipt['model_stages'])
    assert result.model_dump(mode='json')==capture['result']
    assert receipt['real_model_calls']==receipt['source_SQL_executions']==receipt['production_writes']==0
    assert receipt['model_accuracy_established'] is False and receipt['production_cutover_pass'] is False


@pytest.mark.asyncio
async def test_oracle_changes_only_model_outputs_and_reexecutes_real_state_rules(inputs):
    capture,case,catalog,raw=await recorded(inputs);outputs=deepcopy(capture['outputs'])
    outputs[0]['output']['operation_markers'][0]['operation_hint']='REPLACE'
    outputs[1]['output']['edits'][0]['operation']='REPLACE'
    before=deepcopy(capture)
    receipt,result=await replay_turn(capture,case,catalog,raw,expected_capture_hash=digest(capture),oracle_outputs=outputs)
    assert receipt['mode']=='RECORDED_OUTPUT_ORACLE_INTERVENTION' and receipt['failure'] is None
    assert receipt['oracle_outputs_hash']==digest(outputs) and capture==before
    assert [m['canonical_code'] for m in result.plan['logical_plan']['payload']['measures']]==['orders']
    assert receipt['model_stages'][1]['context_matches_recording'] is False
    assert all(s['output_changed'] for s in receipt['model_stages'])


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,reason',[
    ('operation','V2_SLOT_OPERATION_CONFLICT'),('handle','V2_BINDING_HANDLE_NOT_OFFERED'),
    ('authority','V2_MODEL_AUTHORITY_FIELD_FORBIDDEN')])
async def test_oracle_does_not_bypass_operation_binding_or_authority_guards(inputs,fault,reason):
    capture,case,catalog,raw=await recorded(inputs);outputs=deepcopy(capture['outputs'])
    edit=outputs[1]['output']['edits'][0]
    if fault=='operation':edit['operation']='REPLACE'
    if fault=='handle':edit['value']=[{'binding_handle':'foreign'}]
    if fault=='authority':edit['value']=[{'canonical_id':'invented'}]
    receipt,result=await replay_turn(capture,case,catalog,raw,expected_capture_hash=digest(capture),oracle_outputs=outputs)
    assert result is None and receipt['failure']['reason']==reason


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['hash','case','scope','snapshot'])
async def test_invalid_capture_authority_or_frozen_catalog_rejects_replay(inputs,fault):
    capture,case,catalog,raw=await recorded(inputs);expected=digest(capture)
    if fault=='hash':expected='wrong'
    if fault=='case':case['current_utterance']='changed'
    if fault=='scope':case['scope']={**case['scope'],'business_domain_ids':[206]}
    if fault=='snapshot':raw+=b' '
    with pytest.raises(ValueError):await replay_turn(capture,case,catalog,raw,expected_capture_hash=expected)


@pytest.mark.asyncio
async def test_corrupt_or_foreign_state_is_not_rewritten_to_match_the_replay_pin(inputs):
    capture,case,catalog,raw=await recorded(inputs)
    for fault in ('digest','scope','namespace'):
        corrupt=deepcopy(capture);state=corrupt['before']['state']
        if fault=='digest':state['payload_digest']='wrong'
        if fault=='scope':state['context']['authorized_scope']['business_domain_ids']=[206]
        if fault=='namespace':state['context']['state_namespace']='foreign'
        receipt,result=await replay_turn(corrupt,case,catalog,raw,expected_capture_hash=digest(corrupt))
        assert result is None and receipt['failure'] and not receipt['model_stages']


@pytest.mark.asyncio
async def test_changed_recorded_model_context_cannot_pass_as_an_exact_replay(inputs):
    capture,case,catalog,raw=await recorded(inputs)
    capture['exchanges'][0]['request_body']['messages'][1]['content']='{}'
    receipt,result=await replay_turn(capture,case,catalog,raw,expected_capture_hash=digest(capture))
    assert result is None and receipt['failure'] and not receipt['model_stages']
