import json
from copy import deepcopy

import httpx
import pytest

from tools.cutover.harness_corpus import enrich
from tools.cutover.harness_runtime import HarnessRuntime
from tools.cutover.run_raw_transition_benchmark import run,replay_turn
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_observation import RuntimeObserver
from test_cutover_raw_transition_benchmark import inputs
from test_v2_raw_turn_recognition import ScriptedTransport


@pytest.mark.asyncio
async def test_existing_runner_executes_real_state_with_harness(inputs):
    rows,catalog,raw,settings,steps=inputs
    cases=enrich(rows,corpus='TRANSITION');harness=HarnessRuntime(cases,evaluator_commit='test',evaluator_hash='test')
    with harness.observer:
        result=await run(cases,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),
                         private_capture=harness.capture,harness=harness)
    assert result['evaluation']['executed_turn_count']==2
    assert harness.captures[1]['before']['state']==harness.captures[0]['result']['next_state']
    assert harness.observations[0]['axes']['canonical_metrics']==['METRIC:amount','METRIC:orders']
    assert 'TaskPatchInput' in result['evaluation']['stage_observations']
    assert result['evaluation']['FULL_PLAN_GOLD_COUNT']==0


@pytest.mark.asyncio
async def test_instrumentation_is_noop_on_exact_recorded_state_and_plan(inputs):
    rows,catalog,raw,settings,steps=inputs;captures=[]
    await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    for capture in captures:
        off,result_off=await replay_turn(capture,rows[0],catalog,raw,expected_capture_hash=digest(capture))
        with RuntimeObserver() as observer:
            observer.begin_turn(capture['case_id'],capture['turn_index'])
            on,result_on=await replay_turn(capture,rows[0],catalog,raw,expected_capture_hash=digest(capture))
        assert on==off
        assert result_on.model_dump(mode='json')==result_off.model_dump(mode='json')==capture['result']
        assert observer.turns[0]['events']


@pytest.mark.asyncio
async def test_true_single_turn_uses_same_runtime_entry_without_fake_history(inputs):
    rows,catalog,raw,settings,steps=inputs
    row=deepcopy(rows[0]);row.update(history=[],current_utterance='销售额',labels={'canonical_metrics':['METRIC:amount']})
    cases=enrich([row],corpus='PUBLIC_DEV');harness=HarnessRuntime(cases,evaluator_commit='test',evaluator_hash='test')
    with harness.observer:
        result=await run(cases,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps[:1])),
                         private_capture=harness.capture,harness=harness)
    assert cases[0]['entry_group']=='TRUE_SINGLE_TURN'
    assert result['evaluation']['turn_count']==result['evaluation']['executed_turn_count']==1
    assert harness.captures[0]['before']['state'] is None
    assert result['evaluation']['details'][0]['axis_results']['canonical_metrics']=='PASS'
