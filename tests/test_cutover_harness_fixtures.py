from copy import deepcopy
from datetime import datetime

import httpx
import pytest

from tools.cutover.harness_corpus import enrich
from tools.cutover.harness_runtime import HarnessRuntime
from tools.cutover.harness_fixtures import pending_fixture,dataset_fixture,request_for
from tools.cutover.run_raw_transition_benchmark import run,frozen_publication,network_guard
from app.domain.models import TrustedIdentity
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2.authorized_contract import ScopedArtifact,contract_digest
from test_cutover_raw_transition_benchmark import inputs
from test_v2_raw_turn_recognition import ScriptedTransport


@pytest.mark.asyncio
async def test_typed_pending_new_task_runs_in_existing_runner(inputs):
    rows,catalog,raw,settings,steps=inputs
    row=deepcopy(rows[0]);row.update(initial_pending=True,current_utterance='销售额',labels={'target_task':'NEW','pending_action':'DETACHED'})
    steps[0][1]['topic_shift_signals']=['EXPLICIT_NEW_TASK']
    cases=enrich([row],corpus='TRANSITION')
    harness=HarnessRuntime(cases,evaluator_commit='test',evaluator_hash='test',pending_codes=('amount','orders'))
    with harness.observer:
        result=await run(cases,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps[:1])),
                         private_capture=harness.capture,harness=harness)
    assert result['evaluation']['executed_turn_count']==1
    assert result['evaluation']['details'][0]['axis_results']=={'target_task':'PASS','pending_action':'PASS'}
    before=harness.captures[0]['before']
    assert before['state']['payload']['pending_records'] and before['pending']
    assert harness.fixtures[row['case_id']]['serialization_roundtrip']=='PASS'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['none','version','scope','operations'])
async def test_typed_pending_resumes_or_rejects_contract_corruption(inputs,fault):
    rows,catalog,raw,settings,steps=inputs
    case=deepcopy(rows[0]);case.update(initial_pending=True,current_utterance='1')
    with network_guard():
        publication=frozen_publication(raw,catalog,[])
        initial,receipt=pending_fixture(case,publication,candidate_codes=('amount','orders'))
        state,pending,_,_=initial
        current=deepcopy(pending.model_dump(mode='json'))
        if fault=='version':current['payload']['task_version']=99
        if fault=='scope':current['context']['authorized_scope']['business_domain_ids']=[206]
        if fault=='operations':current['payload']['operations']={k:'REPLACE' for k in current['payload']['operations']}
        current['payload_digest']=contract_digest(current['payload'])
        class Answer:
            async def complete(self,**kwargs):
                assert kwargs['stage']=='v2_current_turn'
                ctx=kwargs['context']['task_context'];p=ctx['pending']
                return kwargs['output_model'].model_validate({'reference_signals':['ELLIPSIS'],
                    'context_proposal':dict(status='ACCEPTED',relation='ANSWER_CLARIFICATION',
                        target_task_id=p['task_id'],task_version=p['task_version'],pending_id=p['pending_id'],
                        state_version=ctx['state_version'])})
        engine=RawTurnPlanner(Answer(),publication,clock=lambda:datetime.fromisoformat(case['clock']))
        async def invoke():
            return await engine.run(request_for(case,message_id='answer'),TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),
                                    state=state,pending=ScopedArtifact.model_validate(current))
        if fault!='none':
            with pytest.raises(ValueError):await invoke()
        else:
            result=await invoke()
            assert result.resolution['dialogue_act']=='ANSWER_CLARIFICATION'
            assert result.plan['logical_plan']['task_id']==pending.payload['task_id']
            record=result.next_state.payload['pending_records'][pending.payload['pending_id']]
            assert record['status']=='RESOLVED' and all(b['status']=='RESOLVED' for b in record['blockers'])


def dataset_case(row,complete):
    row=deepcopy(row)
    row.update(dataset={'source_complete':complete,'columns':['name','value'],
        'rows':[{'name':'synthetic-a','value':2},{'name':'synthetic-b','value':9}],
        'metric_ids':['METRIC:amount'],'data_origin':'SYNTHETIC_EXPLICIT_FIXTURE_NOT_SOURCE_BUSINESS_ROWS'},
        current_utterance='只看前5条',labels={'dataset_route':'DISPLAY_LIMIT'})
    return row


@pytest.mark.parametrize('complete',[False,True])
def test_real_dataset_reference_keeps_completeness_and_missing_ranking_proof(inputs,complete):
    row=dataset_case(inputs[0][0],complete);reference,receipt=dataset_fixture(row)
    assert reference.row_count==2 and reference.metric_ids==('METRIC:amount',)
    assert receipt['source_complete']==complete and receipt['truncated']==(not complete)
    assert receipt['ranking_proof']=='NOT_PROVEN' and reference.transformation_log
    assert reference.scope.authorized_semantic_scope_fingerprint==request_for(row).authorized_semantic_scope.fingerprint()


@pytest.mark.asyncio
async def test_dataset_missing_runtime_adapter_is_implementation_gap_not_skipped_gold(inputs):
    rows,catalog,raw,settings,_=inputs
    cases=enrich([dataset_case(rows[0],False)],corpus='TRANSITION')
    harness=HarnessRuntime(cases,evaluator_commit='test',evaluator_hash='test')
    with harness.observer:
        result=await run(cases,catalog,raw,settings,transport=httpx.MockTransport(lambda r:pytest.fail('No model call before usable dataset state')),
                         private_capture=harness.capture,harness=harness)
    detail=result['evaluation']['details'][0]
    assert detail['status']=='BLOCKED' and detail['error']['type']=='IMPLEMENTATION_GAP'
    assert result['evaluation']['status_counts']['NOT_RUN']==0
    assert result['evaluation']['executed_turn_count']==0
    assert harness.fixtures[cases[0]['case_id']]['serialization_roundtrip']=='PASS'
