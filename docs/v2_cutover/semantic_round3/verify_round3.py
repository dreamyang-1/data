"""Verify public Round 3 evidence and unchanged parent contracts; no private reads."""
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from tools.cutover.evaluation_contract import digest

def read(name):return json.loads((HERE/name).read_text(encoding='utf-8'))
def norm(path):return hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()

def verify():
    spec=importlib.util.spec_from_file_location('parent_round2',HERE.parent/'semantic_round2/verify_round2.py')
    parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
    assert parent.verify()['status']=='PASS'
    manifest=read('round3_manifest.json')
    frozen=json.loads((HERE.parent/'semantic_round2/round2_manifest.json').read_text(encoding='utf-8'))['frozen_pins']
    assert manifest['frozen_pins']==frozen
    for path,sha in manifest['diagnostic_files'].items():assert norm(ROOT/path)==sha,path
    assert digest(manifest['diagnostic_files'])==manifest['diagnostic_code_hash']
    for key in ('production_changes','prompt_changes','schema_changes','production_regex_added',
                'production_business_keywords_added','real_model_calls','production_writes','blind_calls'):
        assert manifest[key]==0,key
    inventory=read('context_case_inventory.json');rows=inventory['cases']
    assert len(rows)==15 and len({r['case_id'] for r in rows})==15
    assert inventory['target_match']=={'n':15,'N':15}
    assert Counter(r['classification'] for r in rows)=={
        'MODEL_ROLE_AND_OPERATION_CONFLICT':1,'RELATION_LABEL_ONLY_MISMATCH':2,'MODEL_REFERENCE_AS_EXPLICIT_EDIT':12}
    assert sum(r['patch_published'] for r in rows)==2
    assert not inventory['formal_gold_status_changed']
    promotion=read('private_promotion_manifest.json')
    assert promotion['original']==24 and promotion['remaining']==20
    assert {r['case_id'] for r in promotion['new_promoted']}=={'PV81-002','PV81-003'}
    assert len(promotion['previous_promoted'])==2 and not promotion['original_inputs_read_before_promotion']
    assert not promotion['blind_access']
    metrics=read('ablation_metrics.json');controlled=metrics['controlled_metrics']
    assert not metrics['stronger_hybrid_superiority_proven'] and not metrics['formal_Gold_labels_or_scores_changed']
    for arm,n in [('A_MODEL_HARD',9),('B_RULE_ONLY',6),('C_CURRENT_HYBRID',9)]:
        value=controlled[arm]['common_model_evidence_target_exact_match'];assert (value['n'],value['N'])==(n,9)
    assert controlled['C_CURRENT_HYBRID']['target_task_exact_match']['n']==18
    assert metrics['public_metrics']['public_dev/C_CURRENT_HYBRID']['target_task_exact_match']['N']==0
    assert metrics['public_metrics']['transition/C_CURRENT_HYBRID']['target_task_exact_match']['n']==9
    oracle=read('oracle_evidence.json');experiments=oracle['experiments']
    assert len(experiments)==6 and all(r['same_target'] and not r['formal_Gold_pass'] for r in experiments)
    a=[r for r in experiments if r['variant']=='A_RELATION_LABEL'];c=[r for r in experiments if r['variant']=='C_REFERENCE_DECLARATION']
    assert len(a)==4 and all(r['before']==r['after'] for r in a)
    assert len(c)==2 and all(r['after']=='ACCEPTED_DIAGNOSTIC_ONLY' and r['changed_model_stages']==[True,False] for r in c)
    assert oracle['D_RULE_VETO_REMOVAL']['n']==5 and oracle['F_PENDING_NEW_TASK']['n']==2
    coverage=read('arbitration_coverage.json');counts=coverage['counts']
    assert coverage['total']==sum(counts.values())==172
    assert counts['MODEL_DECISION_ACCEPTED']==167 and counts['MODEL_DECISION_VETOED_BY_SOFT_RULE']==0
    assert counts['MODEL_UNAVAILABLE_FALLBACK']==3 and counts['MODEL_DECISION_NOT_REACHED']==2
    exposure=read('context_exposure_audit.json');assert len(exposure['rows'])==340
    assert all(not r['pending_summary_present'] and r['context_only_token_count'] is None for r in exposure['rows'])
    tests=read('test_delta.json');assert len(tests['node_outcomes'])==320 and set(tests['node_outcomes'].values())=={'passed'}
    assert digest(tests['node_outcomes'])==tests['node_outcomes_hash'] and tests['new_tests']==17
    assert not tests['old_pass_to_new_fail'] and not tests['collection_errors'] and not tests['full_agent_rerun']
    assert tests['Critical_160']=={'passed':160,'failed':0}
    replay=read('replay_receipt.json');assert replay['strict_baseline_turns']==replay['strict_replay_passed']==136
    assert len(replay['incomplete_typed_captures_excluded'])==3
    confidence=read('confidence_inventory.json');assert len(confidence['entries'])==confidence['entry_count']==121
    assert not confidence['V2_relation_weight_competition'] and confidence['V2_confidence_consumer_count']==0
    gates=read('cutover_priority_and_gates.json');assert not gates['production_routing_changed'] and not gates['next_round_started']
    assert gates['generic_context_attachment_bug_proven_in_original_cluster']==0
    excluded={'validation_receipt.json','change_manifest.json','git_commit_manifest.json','rollback_manifest.json','workspace_git_hash_verification.json'}
    files={p.name:norm(p) for p in HERE.iterdir() if p.is_file() and p.name not in excluded}
    return {'status':'PASS','stage_status':'SEMANTIC_ROOT_CLOSURE_ROUND_3_COMPLETE','parent_round2_round1_calibration_harness':'PASS',
        'evidence_hash':digest(files),'evidence_files':len(files),'production_changes':0,'real_model_calls':0,
        'private_or_blind_reads':0,'scoped_tests':320,'strict_replays':136,'CONTEXT_ATTACHMENT_CORE_READY':False,
        'CONTEXT_FOLLOWUP_READY':False,'READY_FOR_USER_APPROVAL':False}

if __name__=='__main__':print(json.dumps(verify()))
