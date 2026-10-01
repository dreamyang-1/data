from copy import deepcopy
import hashlib
import json
import socket

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.semantic_v2.pipeline import CurrentTurnSemanticParse
from tools.cutover.build_evaluation_gold import freeze
from tools.cutover.run_raw_transition_benchmark import run, schema_diagnostics, MODEL_NETWORK
from test_v2_raw_turn_recognition import authority, reseal, metric_step, ScriptedTransport


@pytest.fixture
def inputs():
    snapshot=authority()
    snapshot['documents'][0]['metrics'].append(dict(metric_code='orders',metric_name='订单笔数',business_domain=205,formula='COUNT(order_id)'))
    snapshot=reseal(snapshot);raw=json.dumps(snapshot,ensure_ascii=False).encode()
    catalog=freeze(snapshot,hashlib.sha256(raw).hexdigest(),'2026-09-09T09:00:00+08:00')
    row=dict(case_id='C1',history=['销售额'],current_utterance='再加订单笔数',clock='2026-09-09T09:00:00+08:00',
        scope=catalog['scope'],catalog_ref=catalog['artifact_hash'],initial_pending=False,dataset=None,
        labels={'metric_surfaces':['销售额','订单笔数'],'canonical_metrics':['METRIC:amount','METRIC:orders'],'target_task':'PREVIOUS'},
        safety_checks=['wrong_inheritance'],catalog_evidence=['METRIC:amount','METRIC:orders'],business_evidence=['EXPLICIT_ADD_CONTRACT'],
        label_status='REVIEWED_FOR_LISTED_AXES')
    settings=Settings(_env_file=None,intent_model_name='fixture-model',intent_model_base_url='https://model.invalid/v1',
        intent_model_api_key='private-test-token',intent_model_max_retries=0)
    steps=[metric_step('销售额'),metric_step('再加订单笔数','订单笔数','ADD',True)]
    return [row],catalog,raw,settings,steps


@pytest.mark.asyncio
async def test_real_scoped_planner_observes_state_without_reading_expected_answers(inputs):
    rows,catalog,raw,settings,steps=inputs
    rows[0]['labels']['metric_surfaces']=['POISONED_EXPECTED_ANSWER']
    scripted=ScriptedTransport(steps)
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(scripted))
    prediction=result['predictions'][0]
    assert set(prediction['axes']['metric_surfaces'])=={'销售额','订单笔数'}
    assert prediction['axes']['target_task']=='PREVIOUS'
    assert result['evaluation']['metrics']['metric_surfaces']['passed']==0
    assert result['evaluation']['metrics']['canonical_metrics']['passed']==1
    assert result['evaluation']['safety']['wrong_inheritance']['gate']=='NOT_EVALUATED'
    assert result['evaluation']['mode']=='SCRIPTED_MODEL_PIPELINE' and result['evaluation']['real_model_calls']==0
    assert len(scripted.calls)==4 and result['evaluation']['production_cutover_pass'] is False
    encoded=json.dumps(result,ensure_ascii=False)
    assert 'private-test-token' not in encoded
    assert all('POISONED_EXPECTED_ANSWER' not in json.dumps(c,ensure_ascii=False) for c in scripted.calls)


@pytest.mark.asyncio
async def test_private_capture_includes_success_inputs_candidates_and_actual_state(inputs):
    from tools.cutover.evaluation_contract import digest
    rows,catalog,raw,settings,steps=inputs;captures=[]
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    assert result['evaluation']['private_capture_turns']==result['evaluation']['observed_turns']==2
    first,current=captures
    assert first['before']['state'] is None
    assert current['before']['state']==first['result']['next_state']
    assert current['before']['plans']==[first['result']['plan_state']]
    for capture,record in zip(captures,result['turns']):
        assert record['private_capture_hash']==digest(capture)
        assert len(capture['exchanges'])==len(capture['outputs'])==2
        assert all(e['status']==200 and e['raw_content'] for e in capture['exchanges'])
        context=json.loads(capture['exchanges'][1]['request_body']['messages'][1]['content'])
        assert context['catalog_candidates'] and capture['scope']==catalog['scope']
        assert capture['result']['plan']['backend_contract']['mode']=='SHADOW_ONLY'
    assert 'private-test-token' not in json.dumps(captures)
    assert 'request_body' not in json.dumps(result)


@pytest.mark.asyncio
async def test_capture_is_absent_unless_opted_in_and_callback_failure_is_visible(inputs):
    rows,catalog,raw,settings,steps=inputs
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)))
    assert result['evaluation']['private_capture_turns']==0
    assert all('private_capture_hash' not in r for r in result['turns'])
    def broken(_):raise OSError('private evidence disk unavailable')
    with pytest.raises(OSError,match='disk unavailable'):
        await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=broken)


@pytest.mark.asyncio
@pytest.mark.parametrize('fixture',['pending','dataset'])
async def test_missing_state_fixture_stays_unobserved_without_model_calls(inputs,fixture):
    rows,catalog,raw,settings,_=inputs
    if fixture=='pending':rows[0]['initial_pending']=True
    else:rows[0]['dataset']={'source_complete':False,'rows':[]}
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(lambda r:pytest.fail('model must not run')))
    assert result['predictions'][0]['status']=='NOT_RUN'
    assert result['evaluation']['metrics']['metric_surfaces']['denominator']==1
    assert result['evaluation']['metrics']['metric_surfaces']['unobserved']==1
    assert not result['model_calls']


@pytest.mark.asyncio
async def test_failed_history_stops_current_and_retains_bounded_schema_diagnostics(inputs):
    rows,catalog,raw,settings,_=inputs;captures=[]
    def invalid(req):
        context=json.loads(json.loads(req.content)['messages'][1]['content'])
        # Isolate the original malformed-mentions fault in the new joint schema.
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'mentions':'sensitive-invalid-value','context_proposal':dict(status='ACCEPTED',relation='NEW_TASK',
                target_task_id=None,task_version=None,pending_id=None,state_version=context['task_context']['state_version'])})}}]})
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(invalid),private_capture=captures.append)
    assert result['predictions'][0]['reason']=='HISTORY_PRECONDITION_FAILED'
    assert len(result['turns'])==len(result['model_calls'])==1
    assert result['model_calls'][0]['schema_diagnostics']==[{'type':'list_type','location':['mentions']}]
    assert 'sensitive-invalid-value' not in json.dumps(result)
    assert 'sensitive-invalid-value' in captures[0]['last_content']
    assert result['evaluation']['metrics']['metric_surfaces']['unobserved']==1
    assert MODEL_NETWORK.get() is False


@pytest.mark.asyncio
async def test_model_budget_cannot_be_hidden_by_an_incomplete_history(inputs):
    rows,catalog,raw,settings,steps=inputs
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),max_calls=1)
    assert len(result['model_calls'])==1
    assert result['predictions'][0]['reason']=='HISTORY_PRECONDITION_FAILED'
    assert result['turns'][0]['reason']=='MODEL_REQUEST_BUDGET_EXHAUSTED'
    assert result['evaluation']['metrics']['canonical_metrics']['unobserved']==1


@pytest.mark.asyncio
async def test_auth_failure_stops_remaining_cases_instead_of_repeating_requests(inputs):
    rows,catalog,raw,settings,_=inputs
    rows.append({**deepcopy(rows[0]),'case_id':'C2'})
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(lambda r:httpx.Response(401)))
    assert len(result['model_calls'])==1
    assert result['predictions'][1]['status']=='NOT_RUN'
    assert result['evaluation']['metrics']['metric_surfaces']['denominator']==2


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['hash','scope','opt_in','transport'])
async def test_wrong_snapshot_scope_or_call_authority_fails_before_network(inputs,fault):
    rows,catalog,raw,settings,_=inputs
    transport=httpx.MockTransport(lambda r:pytest.fail('network must not run'))
    if fault=='hash':raw=raw+b' '
    if fault=='scope':rows[0]['scope']={**catalog['scope'],'business_domain_ids':[]}
    if fault=='opt_in':transport=None
    if fault=='transport':transport=httpx.AsyncHTTPTransport()
    try:
        with pytest.raises(ValueError):await run(rows,catalog,raw,settings,transport=transport)
    finally:
        if transport is not None:await transport.aclose()


@pytest.mark.asyncio
async def test_scripted_transport_cannot_open_a_real_connection(inputs):
    rows,catalog,raw,settings,_=inputs
    original=socket.socket.connect
    def attack(req):
        with socket.socket() as sock:
            with pytest.raises(RuntimeError,match='NON_MODEL_NETWORK_DENIED'):sock.connect(('203.0.113.1',443))
        return httpx.Response(503)
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(attack))
    assert result['predictions'][0]['status']=='FAILED'
    assert socket.socket.connect is original and MODEL_NETWORK.get() is False


def test_schema_diagnostics_do_not_echo_dynamic_field_names_or_values():
    with pytest.raises(ValidationError) as caught:
        CurrentTurnSemanticParse.model_validate({'private-business-field':'private-business-value'})
    result=schema_diagnostics(caught.value,CurrentTurnSemanticParse)
    assert result==[{'type':'extra_forbidden','location':['<unknown-field>']}]
