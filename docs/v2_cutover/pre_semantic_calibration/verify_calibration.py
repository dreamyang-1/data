"""Verify public calibration evidence without models, private text, or writes."""
from collections import Counter
import hashlib,json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_manifest import verify_frozen
from tools.cutover.harness_cli import public_cases
from tools.cutover.calibration_gold import build_slice

HERE=Path(__file__).resolve().parent
def read(name):return json.loads((HERE/name).read_text(encoding='utf-8'))
def normalized_hash(path):return hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()

def verify():
    baseline=json.loads((ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json').read_text(encoding='utf-8'))
    verify_frozen(baseline)
    manifest=read('calibration_manifest.json')
    for path,expected in manifest['Calibration_files'].items():assert normalized_hash(ROOT/path)==expected,path
    assert digest(manifest['Calibration_files'])==manifest['Calibration_evaluator_hash']
    assert manifest['Evaluator_core_hash']==baseline['evaluator_hash']
    assert all(manifest[k]==baseline[k2] for k,k2 in [('Prompt_hashes','prompts'),('Schema_hashes','schema_hashes'),
        ('Catalog_hash','catalog_hash'),('Candidate_hash','candidate_snapshot_hash'),('Blind_Holdout_hash','blind_holdout_hash')])
    gates=read('gate_matrix.json');assert gates['stage_status']=='PRE_SEMANTIC_CLOSURE_CALIBRATION_COMPLETE'
    assert {r['gate']:r['status'] for r in gates['rows']}=={
        'EVALUATOR_CORE_READY':'PASS','RUNTIME_CAPABILITY_COVERAGE_READY':'PARTIAL',
        'V1_V2_COMPARISON_READY':'BLOCKED','CUTOVER_EVALUATION_READY':'BLOCKED'}
    divergences=read('divergence_shift.json')
    private=divergences['private_validation'];assert private['status_counts']=={'FAIL':24}
    assert {k:v['n'] for k,v in private['distribution'].items()}=={
        'TURN_RESOLUTION':13,'SEMANTIC_QUERY_IR':6,'TASK_OPERATION':2,'MENTION_BOUNDARY':2,'SEMANTIC_ROLE':1}
    for report in divergences.values():
        ids=[i for row in report['distribution'].values() for i in row['case_ids']]
        assert len(ids)==len(set(ids))==sum(v for k,v in report['status_counts'].items() if k!='PASS')
    full=read('certified_full_plan_gold.json')
    catalog=json.loads((ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json').read_text(encoding='utf-8'))
    assert digest(build_slice(catalog,public_cases()))==full['hash']==manifest['Full_Plan_Gold_hash']
    assert full['FULL_PLAN_GOLD_COUNT']==len(full['cases'])==16
    evaluation=read('full_plan_evaluation.json')
    assert evaluation['status_counts']=={'BLOCKED':16}
    assert all(r['axes']['expected_dry_plan_outcome']=='NOT_OBSERVED' and
               all(v=='PASS' for k,v in r['axes'].items() if k!='expected_dry_plan_outcome') for r in evaluation['results'])
    assert all(r['status']=='PASS' and not any(r['external_call_attempts'].values())
               for r in read('new_gold_replay_receipts.json')['receipts'])
    assert len(read('new_gold_replay_receipts.json')['receipts'])==10
    assert read('new_gold_evaluation.json')['status_counts']=={'PASS':6,'FAIL':0,'NOT_RUN':0,'BLOCKED':0}
    assert read('split_access_receipt.json')['BLIND_HOLDOUT']['state']=='SEALED_NOT_VIEWED_OR_RUN'
    assert not read('split_access_receipt.json')['BLIND_HOLDOUT']['model_calls']
    comparison=read('v1_v2_comparison.json')
    assert comparison['COMMON_EVALUABLE_SET_COUNT']==0 and comparison['V1']==comparison['V2']=={'n':0,'N':0}
    assert len(read('evidence_gap_review.json')['cases'])==3
    for row in read('critical_safety_coverage_matrix.json')['rows']:
        if row['evaluated_cases']==0:assert row['status']=='INCOMPLETE_NO_COVERAGE'
        if row['status']=='PASS_ON_OBSERVED_AXES':assert row['evaluated_cases']>0 and row['violations']==0
    test=read('test_delta.json')['Agent']
    assert test['final']=={'passed':3160,'failed':27} and test['new_tests']==24
    assert test['collection_errors']==test['old_pass_to_new_fail']==0
    provider=read('provider_stability.json');assert provider['stage_requests']==provider['stage_success_HTTP']==20
    assert provider['stage_timeout']==provider['stage_HTTP_error']==provider['stage_schema_error']==0
    roots=read('post_calibration_root_clusters.json')
    assert roots['cluster_count']==26 and roots['counted_cases']==90
    assert [(r['reason_code'],r['MAXIMUM_CASE_COVERAGE']) for r in roots['TOP_3_NEXT_PRODUCTION_ROOT_CAUSES']]==[
        ('V2_EXPLICIT_OPERATION_DROPPED',13),('V2_EXPLICIT_SLOT_DROPPED',12),('V2_RECOGNITION_UNRESOLVED',6)]
    assert roots['MINIMUM_INDEPENDENT_ROOT_CAUSE_ESTIMATE']['value'] is None
    assert read('prompt_generalization_audit.json')['status']=='PROMPT_COMPARISON_CONFOUNDED'
    excluded={'validation_receipt.json','change_manifest.json','git_commit_manifest.json','rollback_manifest.json','workspace_git_hash_verification.json'}
    hashes={p.relative_to(HERE).as_posix():normalized_hash(p) for p in HERE.rglob('*')
            if p.is_file() and p.suffix in {'.md','.json','.py'} and p.name not in excluded}
    return {'status':'PASS','stage_status':gates['stage_status'],'evidence_files':len(hashes),
            'evidence_hash':digest(hashes),'calibration_code_commit':manifest['HEAD'],
            'model_calls':0,'private_or_blind_payloads_read':0,'production_writes':0}

if __name__=='__main__':print(json.dumps(verify()))
