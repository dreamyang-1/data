"""Read-only public evidence verification, without models, tests or private data."""
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3];HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from tools.cutover.evaluation_contract import digest


def read(name):return json.loads((HERE/name).read_text(encoding='utf-8'))
def norm(path):return hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()


def verify():
    # Unlike a production change, this round must preserve the full parent pins.
    spec=importlib.util.spec_from_file_location('round5_parent',HERE.parent/'semantic_round5/verify_round5.py')
    parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
    assert parent.verify()['evidence_integrity']=='PASS'
    manifest=read('round5_1_manifest.json')
    assert manifest['stage']=='ROUND_5_1_LIVE_FOLLOWUP_READINESS_PARTIAL'
    assert manifest['decision']=='DECISION_C_FAILURE_IS_DOWNSTREAM'
    assert manifest['baseline']=='76da2d0818dafe851ed2b2dc345ec5cf972242fe'
    for key in ('production_changes','prompt_changes','schema_changes','extra_sequential_context_calls',
                'blind_access','source_sql_calls','production_writes'):
        assert manifest[key]==0,key
    for key in ('context_resolver_added','resolver_experiment','public_api_sse_changed','ui_changed',
                'v1_changed','reducer_changed','port8088_changed','READY_FOR_USER_APPROVAL',
                'next_8088_replacement_preparation'):
        assert manifest[key] is False,key
    for path,sha in manifest['runtime_source_hashes'].items():assert norm(ROOT/path)==sha,path
    assert manifest['CONTEXT_FOLLOWUP_READY']=='NOT_READY'
    assert manifest['question_completion']=='QUESTION_COMPLETION_CONTRACT_GAP'
    inventory=read('live_case_inventory.json');primary=inventory['primary'];extra=inventory['supplemental']
    assert primary['counts']=={'BLOCKED':18,'PASS':1,'FAIL':1}
    assert extra['counts']=={'PASS':1}
    assert primary['required_turn_appearances']==44 and primary['unique_executed_turns']==7
    assert primary['current_turns_executed']==2 and extra['unique_executed_turns']==2
    assert inventory['unique_turns']==9 and inventory['real_model_calls']==20
    assert inventory['unique_first_divergence']=={'CANONICAL_BINDING':1,'SLOT_OPERATION':4,'ENTITY_VALUE_GROUNDING':1}
    assert {k:len(v) for k,v in primary['first_divergence_distribution'].items()}=={
        'CANONICAL_BINDING':4,'SLOT_OPERATION':7,'ENTITY_VALUE_GROUNDING':8}
    judgments=inventory['turn_judgments']
    assert len(judgments)==9 and all(j['relation_correct'] and j['target_or_new_selection_correct'] for j in judgments)
    assert sum(j['has_context'] for j in judgments)==4
    assert sum(j['declared_axes_pass'] for j in judgments)==3
    performance=read('performance_receipt.json');before=performance['native_before']
    assert before['http_status_counts']=={'200':20}
    assert before['requests']==9 and before['schema_failures']==before['provider_timeouts']==0
    assert performance['resolver_after'] is None and performance['historical_pre_round5_latency']=='UNKNOWN_UNCHANGED'
    assert sum(r['calls'] for r in performance['rows'])==20
    tests=read('test_delta.json')
    assert Counter(tests['node_outcomes'].values())==tests['counts']=={'passed':197}
    assert digest(tests['node_outcomes'])==tests['node_outcomes_hash']
    assert not tests['old_pass_to_new_fail'] and not tests['collection_errors']
    assert tests['critical']=={'passed':160,'failed':0} and tests['context_slice']=={'passed':8,'turns':18}
    validation=read('validation_receipt.json')
    for file,sha in validation['evidence_hashes'].items():assert norm(HERE/file)==sha,file
    for file,sha in validation['diagnostic_source_hashes'].items():assert norm(ROOT/file)==sha,file
    return dict(evidence_integrity='PASS',stage=manifest['stage'],decision=manifest['decision'],
        live_unique_requests=9,model_calls=20,primary_statuses=primary['counts'],
        targeted_tests_passed=197,production_changes=0,CONTEXT_FOLLOWUP_READY='NOT_READY',
        private_reads=0,models_tests_replay_executed=0,parent_evidence='PASS')


if __name__=='__main__':print(json.dumps(verify()))
