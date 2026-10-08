from copy import deepcopy
import json

import httpx
import pytest

from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_transport_replay import replay_transport_verified
from tools.cutover.harness_replay import deny_external_calls
from tools.cutover.run_raw_transition_benchmark import run
from test_cutover_raw_transition_benchmark import inputs
from test_v2_raw_turn_recognition import ScriptedTransport


@pytest.mark.asyncio
async def test_transport_replay_preserves_native_recognition_failure_for_invalid_json(inputs):
    rows,catalog,raw,settings,_=inputs;captures=[]
    def invalid(request):
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{invalid json'}}]})
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(invalid),private_capture=captures.append)
    capture=captures[0];assert not capture['outputs'] and capture['exchanges']
    receipt,replayed=await replay_transport_verified(capture,rows[0],catalog,raw,
        expected_capture_hash=digest(capture),request_receipts=result['model_calls'])
    assert replayed is None
    assert receipt['failure']['reason']==capture['outcome']['reason']=='V2_MODEL_OUTPUT_INVALID'
    assert not any(receipt['external_call_attempts'].values())
    incomplete=deepcopy(result['model_calls']);incomplete[0].pop('finish_reason')
    with pytest.raises(ValueError,match='FROZEN_FINISH_REASON_MISSING'):
        await replay_transport_verified(capture,rows[0],catalog,raw,
            expected_capture_hash=digest(capture),request_receipts=incomplete)


@pytest.mark.asyncio
async def test_transport_replay_uses_exact_request_and_preserves_state(inputs):
    rows,catalog,raw,settings,steps=inputs;captures=[]
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    for capture in captures:
        calls=[c for c in result['model_calls'] if c['turn_index']==capture['turn_index']]
        receipt,replayed=await replay_transport_verified(capture,rows[0],catalog,raw,
            expected_capture_hash=digest(capture),request_receipts=calls)
        assert receipt['failure'] is None and receipt['equality']['byte_equal']
    tampered=deepcopy(captures[0]);tampered['exchanges'][0]['request_body']['temperature']=1
    with pytest.raises(ValueError,match='RECORDED_MODEL_INPUT_MISMATCH'):
        await replay_transport_verified(tampered,rows[0],catalog,raw,
            expected_capture_hash=digest(tampered),request_receipts=result['model_calls'][:2])


@pytest.mark.asyncio
async def test_mock_replay_permission_cannot_be_reused_by_other_clients():
    permitted=httpx.MockTransport(lambda request:httpx.Response(200))
    foreign=httpx.MockTransport(lambda request:httpx.Response(200))
    with deny_external_calls(allowed_mock_transport=permitted) as counters:
        async with httpx.AsyncClient(transport=permitted) as client:
            assert (await client.get('https://replay.invalid')).status_code==200
        async with httpx.AsyncClient(transport=foreign) as client:
            with pytest.raises(RuntimeError):await client.get('https://replay.invalid')
    assert counters['model_attempts']==1


@pytest.mark.asyncio
async def test_corpus_reassessment_replays_actual_history_and_preserves_frozen_capture(inputs,tmp_path):
    from tools.cutover.harness_corpus import enrich
    from tools.cutover.harness_runtime import HarnessRuntime
    from tools.cutover.harness_manifest import freeze_versions,write_json
    from tools.cutover.harness_replay_corpus import replay_corpus
    rows,catalog,raw,settings,steps=inputs
    cases=enrich(rows,corpus='TRANSITION');source=tmp_path/'source';source.mkdir();(source/'captures').mkdir()
    harness=HarnessRuntime(cases,evaluator_commit='test',evaluator_hash='test')
    def capture(value):
        harness.capture(value)
        name=value['case_id']+'-turn-'+str(value['turn_index'])+'.json'
        write_json(source/'captures'/name,value)
        write_json(source/'captures'/(name+'.trace.json'),harness.observer.active)
    with harness.observer:
        result=await run(cases,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=capture,harness=harness)
    write_json(source/'model_calls.json',result['model_calls'])
    manifest=freeze_versions(catalog,commit='test',settings=settings,candidate_snapshot={'version':'test','snapshot_hash':'test'})
    manifest['gold_hashes']={'TRANSITION':digest(cases)}
    before={p.name:p.read_bytes() for p in (source/'captures').iterdir()}
    report=await replay_corpus(cases,catalog,raw,manifest=manifest,source_directory=source,
        private_directory=tmp_path/'private',public_directory=tmp_path/'public',
        source_observations=None,source_observations_hash=None,source_commit='test')
    assert report['executed_turn_count']==2 and report['real_model_calls']==0
    assert report['runtime_parity_verified'] and report['FULL_PLAN_GOLD_COUNT']==0
    assert before=={p.name:p.read_bytes() for p in (source/'captures').iterdir()}


@pytest.mark.asyncio
async def test_live_collection_cannot_publish_when_evaluator_changes_mid_run(inputs,tmp_path,monkeypatch):
    from tools.cutover import harness_cli
    from tools.cutover.harness_corpus import enrich
    from tools.cutover.harness_manifest import freeze_versions
    rows,catalog,raw,settings,steps=inputs;cases=enrich(rows,corpus='TRANSITION')
    manifest=freeze_versions(catalog,commit='test',settings=settings,candidate_snapshot={'version':'test','snapshot_hash':'test'})
    original_run=harness_cli.run;checks=[]
    async def offline(*a,**kw):
        kw['transport']=httpx.MockTransport(ScriptedTransport(steps))
        return await original_run(*a,**kw)
    def changed(value):
        checks.append(True)
        if len(checks)>1:raise ValueError('EVALUATOR_CHANGED_REOPEN_BASELINE')
    monkeypatch.setattr(harness_cli,'run',offline);monkeypatch.setattr(harness_cli,'verify_frozen',changed)
    with pytest.raises(ValueError,match='EVALUATOR_CHANGED'):
        await harness_cli.execute(cases,catalog,raw,settings,manifest=manifest,
            private_directory=tmp_path/'private',public_directory=tmp_path/'public')
    assert list((tmp_path/'private/captures').glob('*.json'))
    assert not (tmp_path/'public/evaluation.json').exists()


@pytest.mark.asyncio
async def test_typed_replay_keeps_the_actual_runtime_validation_error_wrapper(inputs,monkeypatch):
    from app.semantic_v2 import recognition
    from app.semantic_v2.models import TaskSemanticState
    from tools.cutover.harness_replay import replay_verified
    rows,catalog,raw,settings,steps=inputs;captures=[]
    def invalid(*a,**kw):return TaskSemanticState.model_validate({'metrics':'invalid'})
    monkeypatch.setattr(recognition,'materialize_payload',invalid)
    await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    capture=captures[0];assert capture['outcome']['reason']=='V2_CONTRACT_VALIDATION_FAILURE'
    receipt,result=await replay_verified(capture,rows[0],catalog,raw,expected_capture_hash=digest(capture))
    assert result is None and receipt['failure']['reason']==capture['outcome']['reason']
