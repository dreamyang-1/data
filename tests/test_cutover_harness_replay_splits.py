from copy import deepcopy
import json
import socket

import httpx
import pytest

from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_replay import replay_verified,deny_external_calls,compare_results
from tools.cutover.harness_observation import RuntimeObserver
from tools.cutover.harness_splits import create_splits,authorize_holdout,promote_exposed_case
from tools.cutover.run_raw_transition_benchmark import run
from test_cutover_raw_transition_benchmark import inputs
from test_v2_raw_turn_recognition import ScriptedTransport


@pytest.mark.asyncio
async def test_strict_replay_has_no_model_database_redis_or_network_calls(inputs):
    rows,catalog,raw,settings,steps=inputs;captures=[]
    await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    for capture in captures:
        receipt,result=await replay_verified(capture,rows[0],catalog,raw,expected_capture_hash=digest(capture))
        assert not any(receipt['external_call_attempts'].values())
        assert receipt['equality']['byte_equal'] and receipt['equality']['semantic_equal']


@pytest.mark.asyncio
async def test_semantic_replay_normalizes_only_legal_state_order_and_diagnostic_fields(inputs):
    rows,catalog,raw,settings,steps=inputs;captures=[]
    await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    original=captures[-1]['result'];equivalent=deepcopy(original)
    from app.semantic_v2.authorized_contract import ScopedArtifact,contract_digest,scoped_artifact_material
    artifact=ScopedArtifact.model_validate(equivalent['next_state'])
    task=next(iter(equivalent['next_state']['payload']['tasks'].values()))
    version=next(v for v in task['versions'] if v['version']==task['active_version'])
    version['semantics']['metrics'].reverse()
    equivalent['next_state']['payload_digest']=contract_digest(scoped_artifact_material(equivalent['next_state']['payload'],artifact.source_value_bindings))
    equivalent['diagnostic']={'trace_id':'different','wall_time_ms':22}
    result=compare_results(original,equivalent)
    assert result['semantic_equal'] and not result['byte_equal']
    equivalent['diagnostic']['permission_scope']='unexpected'
    assert not compare_results(original,equivalent)['semantic_equal']


@pytest.mark.asyncio
async def test_guard_proves_attempts_are_denied_even_with_model_network_flag():
    from tools.cutover.run_raw_transition_benchmark import MODEL_NETWORK
    import pymysql,redis
    with deny_external_calls() as counters:
        token=MODEL_NETWORK.set(True)
        try:
            with socket.socket() as sock:
                with pytest.raises(RuntimeError):sock.connect(('203.0.113.1',443))
            with pytest.raises(RuntimeError):pymysql.connect(host='203.0.113.1')
            with pytest.raises(RuntimeError):redis.Redis().ping()
            async with httpx.AsyncClient() as client:
                with pytest.raises(RuntimeError):await client.get('https://model.invalid')
        finally:MODEL_NETWORK.reset(token)
    assert counters=={'model_attempts':1,'database_attempts':1,'network_attempts':1,'redis_attempts':1}


@pytest.mark.asyncio
async def test_failure_replay_and_instrumentation_preserve_same_safe_rejection(inputs):
    rows,catalog,raw,settings,steps=inputs;captures=[]
    original=steps[1][2]
    def malicious(context):
        value=original(context);value['edits'][0]['value']=[{'binding_handle':'foreign'}];return value
    steps[1]=(steps[1][0],steps[1][1],malicious)
    result=await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    assert result['predictions'][0]['status']=='FAILED'
    capture=captures[-1]
    off,_=await replay_verified(capture,rows[0],catalog,raw,expected_capture_hash=digest(capture))
    with RuntimeObserver() as observer:
        observer.begin_turn(capture['case_id'],capture['turn_index'])
        on,_=await replay_verified(capture,rows[0],catalog,raw,expected_capture_hash=digest(capture))
    assert on==off and on['failure']['reason']=='V2_BINDING_HANDLE_NOT_OFFERED'


def test_sequestered_splits_publish_hashes_not_questions_and_holdout_stays_locked(inputs,tmp_path):
    rows,catalog,_,_,_=inputs
    # Three governed metrics required; add a catalog fact for a synthetic
    # generator-only fixture. This is never a production catalog modification.
    catalog=deepcopy(catalog);catalog['facts'].append({'fact_id':'METRIC:cost','catalog_type':'METRIC','code':'cost','name':'成本'})
    catalog['artifact_hash']=digest({k:v for k,v in catalog.items() if k!='artifact_hash'})
    manifest=create_splits(catalog,rows,tmp_path/'splits',per_split=6)
    assert all(s['case_count']==6 and s['public_overlap']==0 for s in manifest['splits'].values())
    public=json.dumps(manifest,ensure_ascii=False)
    assert 'current_utterance' not in public and 'history' not in public
    assert manifest['splits']['BLIND_HOLDOUT']['access_status']=='SEALED_NOT_VIEWED_OR_RUN'
    with pytest.raises(ValueError,match='LOCKED'):authorize_holdout(manifest,{'status':'HARNESS_READY'})
    promoted=promote_exposed_case(manifest,digest('private-case-id'))
    with pytest.raises(ValueError,match='PROMOTED'):
        authorize_holdout(promoted,{'status':'SEMANTIC_FREEZE_CANDIDATE','hard_safety_pass':True,
                                  'holdout_manifest_hash':manifest['holdout_manifest_hash']})
    assert manifest['splits']['BLIND_HOLDOUT']['access_status']=='SEALED_NOT_VIEWED_OR_RUN'
