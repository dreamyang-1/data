"""Verify public Round 5.2 evidence; no tests, models, replay or private reads."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3];HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from tools.cutover.evaluation_contract import digest


def read(name):return json.loads((HERE/name).read_text(encoding='utf-8'))
def norm(path):return sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()


def verify():
    manifest=read('round5_2_manifest.json')
    assert manifest['status']=='ROUND_5_2_DOWNSTREAM_CLOSURE_PARTIAL'
    assert manifest['baseline']=='bc5bc3f418c374f3f155403550649b23c64ce4e5'
    assert manifest['live_model_calls']==11 and manifest['normal_llm_calls_added']==0
    assert manifest['CONTEXT_FOLLOWUP_READY']=='NOT_READY' and not manifest['READY_FOR_V2_READ_ONLY_E2E_SMOKE']
    for key in ('prompt_changes','context_changes','reducer_changes','context_schema_changes','public_schema_changes',
                'production_regex_added','business_keywords_added','confidence_tuning','blind_access','sql_calls','production_writes'):
        assert manifest[key]==0,key
    for key in ('validator_relaxation','v1_changed','ui_changed','api_sse_changed','port8088_changed','context_resolver_added','catalog_changed','source_values_changed','cross_service_patches'):
        assert manifest[key] is False,key
    for path,expected in manifest['runtime_hashes'].items():assert norm(ROOT/path)==expected,path
    clusters=read('root_clusters.json')
    assert clusters['original_failure_turns']==6 and clusters['original_slot_root_clusters']==3
    assert len(clusters['clusters'])==6 and clusters['fully_closed_roots']==2 and clusters['not_fully_closed_roots']==4
    causal=read('causal_receipt.json')
    originals=[r for r in causal['baseline_reproduction'] if r['mode']=='RECORDED_OUTPUT']
    assert len(originals)==6 and all(r['strict_request_match'] and r['error'] for r in originals)
    final=causal['final_recorded']
    assert len(final)==6 and sum(r['plan'] for r in final)==1
    assert all(r['calls'][0]['context_match'] and r['calls'][0]['schema_prompt_match'] for r in final)
    assert not causal['final_strict_original_generation_request']
    chain=causal['actual_live_chain_verification']
    assert chain['status']=='PASS' and [r['year'] for r in chain['rows']]==[2025,2025,2026]
    assert all(r['filter_empty'] and r['clear_barrier'] for r in chain['rows'][1:])
    assert chain['checks']['all_three_actual_accepted_turns_match_final_runtime']
    live=read('live_recheck.json')
    assert live['model_calls']==11 and live['request_attempts']==5 and live['unique_turns']==4
    assert live['original_18_blocked_cases_fully_executed']==1
    assert live['original_18_blocked_cases_remaining_without_final_live_turn']==17
    assert live['final_live_case_counts']=={'BLOCKED':4,'NOT_RUN':15,'PASS':1}
    assert Counter(row['status'] for row in live['original_cases'])==live['final_live_case_counts']
    assert sum(a['calls'] for a in live['attempts'])==11 and sum(a['plan'] for a in live['attempts'])==3
    assert live['failed_clear_attempt_retained']
    tests=read('test_delta.json')
    assert Counter(tests['node_outcomes'].values())==tests['counts']=={'passed':526}
    assert digest(tests['node_outcomes'])==tests['node_outcomes_hash']
    assert tests['new_tests']==19 and tests['repeated_existing_nodes']==507
    assert not tests['old_pass_to_new_fail'] and not tests['collection_errors']
    assert tests['context_slice']=={'passed':8,'turns':18} and tests['critical']['passed']==160
    assert tests['round3_target']['passed']==15
    performance=read('performance_receipt.json')
    assert performance['http_status_counts']=={'200':11}
    assert performance['after']['calls']['mean']==2.2
    assert not performance['normal_model_call_path_changed']
    validation=read('validation_receipt.json')
    for file,expected in validation['evidence_hashes'].items():assert norm(HERE/file)==expected,file
    for file,expected in validation['source_hashes'].items():assert norm(ROOT/file)==expected,file
    return dict(evidence_integrity='PASS',status=manifest['status'],closed_roots=2,remaining_roots=4,
        live_calls=11,original_blocked_cases_unlocked=1,targeted_tests_passed=526,CONTEXT_FOLLOWUP_READY='NOT_READY',
        READY_FOR_V2_READ_ONLY_E2E_SMOKE=False,models_tests_replay_executed=0,private_reads=0)


if __name__=='__main__':print(json.dumps(verify()))
