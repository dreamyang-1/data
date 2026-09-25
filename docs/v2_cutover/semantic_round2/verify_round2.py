"""Read-only public evidence verification; no private inputs or model calls."""
from collections import Counter
import hashlib,importlib.util,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from tools.cutover.evaluation_contract import digest
def read(name):return json.loads((HERE/name).read_text(encoding='utf-8'))
def norm(p):return hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
def verify():
    spec=importlib.util.spec_from_file_location('round1',HERE.parent/'semantic_round1/verify_round1.py')
    parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
    receipt=parent.verify();assert receipt['status']=='PASS'
    m=read('round2_manifest.json')
    base=json.loads((HERE.parent/'evaluation_harness/evaluation_baseline_manifest.json').read_text(encoding='utf-8'))
    assert m['frozen_pins']=={k:base[k] for k in m['frozen_pins']}
    for p,h in m['diagnostic_files'].items():assert norm(ROOT/p)==h,p
    assert digest(m['diagnostic_files'])==m['diagnostic_code_hash']
    for k in ['production_changes','prompt_changes','regex_added','business_keywords_added','schema_changes',
              'evaluator_changes','new_architecture_components','real_model_calls','blind_calls','production_writes',
              'new_private_promoted','remaining_private_read_for_debug']:assert m[k]==0,k
    assert m['previous_private_promoted']==['PV81-010','PV81-016']
    inventory=read('projection_case_inventory.json');rows=inventory['cases']
    original=json.loads((HERE.parent/'semantic_round1/operation_case_inventory.json').read_text(encoding='utf-8'))['cases']
    assert len(rows)==13 and {r['case_id'] for r in rows}=={r['case_id'] for r in original}
    assert inventory['distribution']==dict(Counter(r['classification'] for r in rows))=={'MODEL_ROLE':12,'MENTION_BOUNDARY':1}
    assert not inventory['generic_projection_contract_defect_proven'] and not inventory['automatic_role_to_marker_conversion']
    assert inventory['obligation_precedes_binding'] and inventory['model_misassertion_precedes_runtime']
    assert all(r['raw_model_role']==r['normalized_role'] and r['before']==r['after']=='FAIL' and
        r['risk']=='SAFE_REJECT' and r['impact']=='FIRST_TURN_ONLY' and not r['normalizer_introduced_declaration'] for r in rows)
    assert sum(r['followup_prefix_blocked'] for r in rows)==5
    assert all(r['explicitness_type'] in {'USER_EXPLICIT_PROJECTION','DERIVED_DEFAULT_DISPLAY','PRESENTATION_OR_QUERY_SHAPE_CUE'} for r in rows)
    old_oracles=json.loads((HERE.parent/'semantic_round1/oracle_receipts.json').read_text(encoding='utf-8'))['results']
    old={r['case_id']:r for r in old_oracles};oracle=read('oracle_evidence.json')
    assert len(oracle['results'])==13 and not oracle['original_capture_modified'] and not oracle['formal_gold_changes']
    counts=Counter();c_count=0
    for case in oracle['results']:
        experiments={e['variant']:e for e in case['experiments']};a=experiments['A_CLASSIFICATION'];b=experiments['B_OBLIGATION']
        counts[a['outcome']]+=1
        assert a['outcome']==b['outcome']
        assert a['source_capture_hash']==b['source_capture_hash']==old[case['case_id']]['source_capture_hash']
        assert b['source_evidence_hash']==digest(old[case['case_id']]['runs'][2])
        assert b['source']=='REUSED_IDENTICAL_ROUND1_SINGLE_PARSE_ORACLE'
        for e in case['experiments']:
            assert not e['formal_gold_pass']
            if e['variant']=='B_OBLIGATION':continue
            assert not any(e['external_call_attempts'].values())
            assert [s['output_changed'] for s in e['receipt']['model_stages']]==[True,False]
            assert e['receipt']['real_model_calls']==e['receipt']['source_SQL_executions']==e['receipt']['production_writes']==0
        if 'C_BOUNDARY' in experiments:
            c_count+=1;assert case['case_id']=='G81-066' and experiments['C_BOUNDARY']['outcome']=='V2_EXPLICIT_OPERATION_DROPPED'
    assert c_count==1 and counts=={'ACCEPTED_DIAGNOSTIC_ONLY':3,'CATALOG_RELATIONSHIP_REQUIRED':6,'V2_QUERY_SHAPE_CONFLICT':4}
    positive=read('positive_recorded_controls.json');assert len(positive)==2
    assert all(r['strict_replay']=='PASS' and not r['receipt']['failure'] and not r['new_gold_pass'] for r in positive)
    tests=read('test_delta.json');nodes=tests['node_outcomes']
    assert len(nodes)==209 and set(nodes.values())=={'passed'} and digest(nodes)==tests['node_outcomes_hash']
    assert tests['new_tests']==16 and tests['existing_test_nodes_checked']==193 and not tests['old_pass_to_new_fail']
    assert not tests['collection_errors'] and not tests['full_agent_rerun'] and not tests['full_agent_new_total_inferred']
    assert tests['baseline_full_agent']=={'passed':3183,'failed':27} and tests['Critical_160']=={'passed':160,'failed':0}
    sliced=read('context_followup_critical_slice.json');cases=sliced['cases']
    assert len(cases)==8 and sliced['PASS']==8 and sliced['executed_turns']==18
    assert not sliced['CONTEXT_FOLLOWUP_READY'] and not sliced['live_model_semantic_coverage_established']
    assert not sliced['old_gold_cases_rewritten'] and not sliced['Blind_Holdout_used']
    full_gold=json.loads((HERE.parent/'pre_semantic_calibration/certified_full_plan_gold.json').read_text(encoding='utf-8'))
    known={f'G81-{i:03d}' for i in range(1,101)}|{r['case_id'] for r in full_gold['cases']}
    required={'TurnResolution','TargetTask','TaskOperation','TaskPatch','TaskSemanticState','ClarificationDecision',
              'observed_turn_resolution','observed_pre_default_task_patch'}
    for case in cases:
        assert set(case['related_dev_cases'])<=known
        assert case['result']=='PASS' and case['split']=='PUBLIC_DEV'
        assert not case['WrongInheritance'] and not case['PendingHijack'] and not case['Resurrection']
        assert all(required<=set(t) for t in case['turns'])
        assert case['scope']=={'semantic_model_id':81,'business_domain_ids':[205]}
    assert cases[6]['turns'][-1]['TargetTask']==cases[6]['turns'][0]['TargetTask']!=cases[6]['turns'][1]['TargetTask']
    assert cases[7]['turns'][0]['ClarificationDecision']['decision']=='ASK'
    assert cases[7]['turns'][1]['TurnResolution']=='NEW_TASK'
    gates=read('cutover_priority_and_gates.json')
    assert gates['next_round']=='TURN_RESOLUTION / CONTEXT FOLLOW-UP ROOT CLOSURE'
    assert not gates['next_round_started'] and not gates['READY_FOR_USER_APPROVAL'] and gates['V1_production_routing']=='UNCHANGED'
    excluded={'validation_receipt.json','change_manifest.json','git_commit_manifest.json','rollback_manifest.json','workspace_git_hash_verification.json'}
    files={p.name:norm(p) for p in HERE.iterdir() if p.is_file() and p.name not in excluded}
    return {'status':'PASS','stage_status':'SEMANTIC_ROOT_CLOSURE_ROUND_2_COMPLETE','evidence_hash':digest(files),
        'evidence_files':len(files),'parent_round1_calibration_harness':'PASS','production_changes':0,
        'model_calls':0,'private_or_blind_payload_reads':0,'production_writes':0,'full_agent_rerun':False}
if __name__=='__main__':print(json.dumps(verify()))
