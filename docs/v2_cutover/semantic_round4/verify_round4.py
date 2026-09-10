"""Verify public Round 4 evidence, no private inputs, model calls or SQL."""
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
    spec=importlib.util.spec_from_file_location('parent_round3',HERE.parent/'semantic_round3/verify_round3.py')
    parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
    assert parent.verify()['status']=='PASS'
    m=read('round4_manifest.json')
    assert m['stage']=='SEMANTIC_ROOT_CLOSURE_ROUND_4_COMPLETE'
    for key in ('production_changes','production_prompt_changes','production_schema_changes',
                'production_routing_changes','production_regex_changes','production_business_keyword_changes',
                'semantic_confidence_weight_changes','source_sql_calls','production_writes',
                'new_promotions','private_remaining_reads','blind_access'):
        assert m[key]==0,key
    assert m['real_model_calls']==26 and m['private_remaining']==20
    assert m['actual_live_clocks']==['2026-09-09T09:00:00+08:00']
    assert m['experimental_schema_version']=='context-proposal-experiment-v1.1'
    assert m['live_schema_version']=='context-proposal-experiment-v1'
    for file,sha in m['audited_source_hashes'].items():assert norm(ROOT/file)==sha,file
    for file,sha in m['diagnostic_files'].items():assert norm(ROOT/file)==sha,file
    assert digest(m['diagnostic_files'])==m['diagnostic_code_hash']
    assert m['original_cluster']['baseline_target_match']=={'n':15,'N':15}
    assert m['original_cluster']['new_B_coverage']=={'n':1,'N':15}
    assert not m['next_round_started'] and not m['full_regression_required']
    d=read('experiment_metrics.json');assert len(d['rows'])==12 and d['real_model_calls']==26
    assert len({r['case_id'] for r in d['rows']})==12
    a,b,c=(d['arms'][n] for n in ('A_CURRENT_HYBRID','B_MODEL_PROPOSAL_PLUS_HARD_CONSTRAINTS','C_RULE_ONLY_DIAGNOSTIC'))
    assert a['target_task_exact_match']==b['target_task_exact_match']=={'n':11,'N':11}
    assert c['target_task_exact_match']=={'n':10,'N':11}
    assert b['valid_decision_outputs']==11 and b['schema_or_runtime_rejected']==1
    assert not d['current_schema_export_live_tested'] and not d['formal_gold_rescored']
    assert d['native_status_counts']=={'ACCEPTED_DIAGNOSTIC_ONLY':10,'REJECTED':1,'NOT_RUN_PROPOSAL_REJECTED':1}
    assert not d['native_old_accept_to_reject'] and not d['native_labeled_state_wrong']
    assert d['observer_corrections'][0]['new_model_calls']==0
    rejected=[r for r in d['rows'] if r['model_error']]
    assert [r['case_id'] for r in rejected]==['R4L-011']
    assert rejected[0]['arms']['B_MODEL_PROPOSAL_PLUS_HARD_CONSTRAINTS']['error']
    controlled=read('controlled_context_receipt.json')
    assert controlled['cases']==8 and controlled['turns']==controlled['native_state_equal']==18
    assert len(controlled['rows'])==18 and len(controlled['act_only_controls'])==5
    assert all(r['native_next_state_identical'] for r in controlled['rows'])
    t=read('test_delta.json')
    assert len(t['node_outcomes'])==322 and set(t['node_outcomes'].values())=={'passed'}
    assert digest(t['node_outcomes'])==t['node_outcomes_hash'] and t['new_tests']==25
    assert not t['old_pass_to_new_fail'] and not t['collection_errors'] and not t['full_agent_rerun']
    assert t['Critical_160']=={'passed':160,'failed':0}
    assert m['gates']=={'PRODUCTION_FIX_GATE':'NOT_PROVEN','CONTEXT_ATTACHMENT_CORE_READY':'NOT_READY',
        'CONTEXT_FOLLOWUP_READY':'NOT_READY','SEMANTIC_FREEZE_CANDIDATE':'BLOCKED','READY_FOR_USER_APPROVAL':False}
    return dict(status='PASS',stage=m['stage'],production_changes=0,real_model_calls=26,
        focused_passed=322,critical_passed=160,context_cases_passed=8,old_pass_to_new_fail=0,
        parent_round1_round2_round3_calibration_harness='PASS',
        CONTEXT_ATTACHMENT_CORE_READY=False,CONTEXT_FOLLOWUP_READY=False,READY_FOR_USER_APPROVAL=False,
        new_external_calls_in_verifier=0,private_reads=0,
        evidence_hash=digest({p.name:norm(p) for p in HERE.iterdir() if p.is_file() and p.name not in
            {'validation_receipt.json','change_manifest.json','workspace_git_hash_verification.json'}}))

if __name__=='__main__':print(json.dumps(verify()))
