"""Verify published harness evidence without reading private questions or models."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_manifest import verify_frozen

HERE=Path(__file__).resolve().parent
def read(name):return json.loads((HERE/name).read_text(encoding='utf-8'))


def verify():
    baseline=read('evaluation_baseline_manifest.json');verify_frozen(baseline)
    summary=read('checkpoint_summary.json');assert summary['status']=='EVALUATION_HARNESS_NOT_READY'
    inventories=read('post_harness_case_inventory.json')['cases']
    assert len(inventories)==len({c['case_id'] for c in inventories})==144
    assert Counter(c['status'] for c in inventories)=={'PASS':45,'FAIL':90,'BLOCKED':9}
    first=read('post_harness_first_divergence.json')['distribution']
    primary_ids=[case for row in first for case in row['case_ids']]
    assert len(primary_ids)==len(set(primary_ids))==99
    assert set(primary_ids)=={r['case_id'] for r in inventories if r['status']!='PASS'}
    replay_count=0;required=0;executed=0;model_calls=0
    for name in ('public_dev','transition','private_validation'):
        report=read('final/'+name+'/evaluation.json')
        assert report['evaluator_git_commit']==baseline['evaluator_git_commit']
        assert report['evaluator_hash']==baseline['evaluator_hash']
        assert report['baseline_manifest_hash']==digest(baseline)
        assert report['status_counts']['NOT_RUN']==report['FULL_PLAN_GOLD_COUNT']==0
        assert not any(report[k] for k in ('real_model_calls','source_SQL_calls','milvus_calls','production_state_mutations'))
        assert report['blind_holdout_run'] is False and report['full_production_orchestrator'] is False
        receipts=read('final/'+name+'/replay_receipts.json')['receipts']
        assert all(r['status']=='PASS' and not any(r['external_call_attempts'].values()) for r in receipts)
        hashes=read('final/'+name+'/capture_manifest.json')
        assert digest(hashes)==report['capture_manifest_hash']
        replay_count+=len(receipts);required+=report['turn_count'];executed+=report['executed_turn_count'];model_calls+=report['source_capture_model_calls']
    assert (replay_count,required,executed,model_calls)==(172,219,172,340)
    validation=read('evaluator_validation_report.json')
    assert validation['mutation']['n']==validation['mutation']['N']==14
    assert validation['false_positive']['n']==validation['false_positive']['N']==7
    assert validation['new_harness_tests']=={'passed':74,'failed':0}
    assert read('split_access_receipt.json')['BLIND_HOLDOUT']['state']=='SEALED_NOT_VIEWED_OR_RUN'
    assert read('v1_v2_common_set_report.json')['COMMON_EVALUABLE_SET_COUNT']==0
    assert read('critical_safety_report.json')['overall_gate']=='INCOMPLETE_NOT_PASS'
    assert read('semantic_freeze_gate.json')['formal_model_benchmark']=='BLOCKED'
    roots=read('root_cause_inventory.json')
    assert [(r['root_family'],r['case_count']) for r in roots['TOP_3_NEXT_PRODUCTION_ROOT_CAUSES']]==[
        ('TASK_OPERATION',21),('TURN_RESOLUTION',15),('ENTITY_VALUE_GROUNDING',14)]
    delta=read('test_delta.json')
    assert delta['data']['final']=={'passed':3136,'failed':27}
    assert all(v['old_pass_to_new_fail']==0 and not v['collection_errors'] for v in delta.values())
    # Freeze a receipt for public evidence only. Input fixtures and blind text
    # remain private; no files are modified by this verifier.
    release_envelopes={'validation_receipt.json','change_manifest.json','git_commit_manifest.json',
                       'rollback_manifest.json','workspace_git_hash_verification.json'}
    files={p.relative_to(HERE).as_posix():hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
           for p in HERE.rglob('*') if p.is_file() and p.suffix in {'.json','.md','.csv','.py'} and p.name not in release_envelopes}
    return {'status':'PASS','evaluator_commit':baseline['evaluator_git_commit'],'evidence_files':len(files),
        'evidence_hash':digest(files),'new_replayed_turns':172,'model_calls_in_verification':0,
        'production_writes':0,'stage_readiness':'EVALUATION_HARNESS_NOT_READY'}


if __name__=='__main__':print(json.dumps(verify()))
