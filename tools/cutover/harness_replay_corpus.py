"""Re-evaluate immutable live captures with versioned scoring and strict replay."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_cli import public_cases,transition_cases
from tools.cutover.harness_contract import summarize
from tools.cutover.harness_dryplan import observe_dry_plan
from tools.cutover.harness_manifest import evaluator_identity,verify_frozen,write_json
from tools.cutover.harness_replay import replay_verified,deny_external_calls
from tools.cutover.harness_runtime import HarnessRuntime
from tools.cutover.harness_transport_replay import replay_transport_verified
from tools.cutover.run_raw_transition_benchmark import frozen_publication
from tools.cutover.semantic_evaluator import read_jsonl


async def replay_corpus(cases,catalog,raw,*,manifest,source_directory,private_directory,public_directory,
        source_observations,source_observations_hash,source_commit):
    verify_frozen(manifest)
    if digest(cases)!=manifest['gold_hashes'][cases[0]['corpus']]:raise ValueError('GOLD_FREEZE_MISMATCH')
    source_directory=Path(source_directory);private_directory=Path(private_directory);public_directory=Path(public_directory)
    private_directory.mkdir(parents=True,exist_ok=False);public_directory.mkdir(parents=True,exist_ok=False)
    calls=json.loads((source_directory/'model_calls.json').read_text(encoding='utf-8'))
    harness=HarnessRuntime(cases,evaluator_commit=manifest['evaluator_git_commit'],evaluator_hash=manifest['evaluator_hash'],
                           pending_codes=('sales_total_including_tax','sales_total_quantity'))
    receipts=[];dry=[];predictions=[];records=[];capture_hashes=[]
    for case in cases:
        captures=[]
        for p in sorted((source_directory/'captures').glob(case['case_id']+'-turn-*.json')):
            if p.name.endswith('.trace.json'):continue
            captures.append(json.loads(p.read_text(encoding='utf-8')))
        captures.sort(key=lambda c:c['turn_index'])
        if not captures or case.get('initial_pending'):
            with deny_external_calls():
                publication=frozen_publication(raw,catalog,[],native_source_values=True)
                try:harness.initialize(case,publication)
                except Exception as exc:harness.fixture_failure(case,exc)
            if not captures and case['case_id'] not in harness.fixture_errors:raise ValueError('UNACCOUNTED_MISSING_CASE_CAPTURE')
        previous=None
        for capture in captures:
            capture_hash=digest(capture);capture_hashes.append({'case_id':case['case_id'],'turn_index':capture['turn_index'],'hash':capture_hash})
            if previous and previous.get('result'):
                if capture['before']['state']!=previous['result']['next_state']:
                    raise ValueError('HISTORY_STATE_CONTINUITY_BROKEN')
                if capture['before']['pending']!=previous['result'].get('pending_state'):
                    raise ValueError('HISTORY_PENDING_CONTINUITY_BROKEN')
            kwargs={'expected_capture_hash':capture_hash,'source_observations':source_observations,
                    'source_observations_hash':source_observations_hash}
            if len(capture['outputs'])==len(capture['exchanges']):
                receipt,result=await replay_verified(capture,case,catalog,raw,**kwargs)
            else:
                turn_calls=[c for c in calls if c['case_id']==case['case_id'] and c['turn_index']==capture['turn_index']]
                receipt,result=await replay_transport_verified(capture,case,catalog,raw,request_receipts=turn_calls,**kwargs)
            same_failure=(receipt['failure'] is not None and
                receipt['failure']['reason']==capture['outcome'].get('reason'))
            receipt['same_safe_rejection']=same_failure
            receipt['status']='PASS' if (result is not None and receipt['equality']['semantic_equal']) or (not capture.get('result') and same_failure) else 'FAIL'
            if receipt['status']!='PASS':raise ValueError('RECORDED_RUNTIME_PARITY_FAILED')
            receipts.append(receipt)
            trace_path=source_directory/'captures'/(case['case_id']+'-turn-'+str(capture['turn_index'])+'.json.trace.json')
            harness.observer.turns.append(json.loads(trace_path.read_text(encoding='utf-8')))
            harness.capture(capture);records.append(capture['outcome'])
            if result is not None and capture['result'].get('plan'):
                try:
                    evidence=observe_dry_plan(capture,case,catalog,raw,source_observations=source_observations,
                                             source_observations_hash=source_observations_hash)
                    write_json(private_directory/(case['case_id']+'-dry-'+str(capture['turn_index'])+'.json'),evidence)
                    dry.append(evidence['public_receipt'])
                except Exception as exc:
                    # No exception text: it may include SQL or business values.
                    dry.append({'case_id':case['case_id'],'turn_index':capture['turn_index'],
                                'status':'NATIVE_DRY_PLAN_REJECTED','error_type':type(exc).__name__,
                                'same_raw_runtime_entry':False,'SQL_executed':False})
            previous=capture
        last=captures[-1] if captures else None
        predictions.append({'case_id':case['case_id'],'status':'OK' if last and last.get('result') else 'FAILED',
                            'reason':last['outcome'].get('reason') if last else 'FIXTURE_OR_IMPLEMENTATION_GAP'})
    harness.build_report(cases,predictions,catalog,records,calls,'RECORDED_RUNTIME_REEVALUATION')
    provenance={**evaluator_identity(manifest['evaluator_git_commit']),'corpus':cases[0]['corpus'],
        'mode':'RECORDED_RUNTIME_REEVALUATION','gold_hash':digest(cases),'catalog_hash':catalog['artifact_hash'],
        'candidate_snapshot_hash':manifest['candidate_snapshot_hash'],'as_of':manifest['as_of'],'scope':catalog['scope'],
        'capture_runtime_commit':source_commit,'observation_contract':'IMMUTABLE_NATIVE_CALL_TRACES_WITH_EXACT_REPLAY',
        'runtime_parity_verified':True,'runtime_parity_boundary':'RawTurnPlanner only; see artifact matrix for excluded capabilities',
        'baseline_manifest_hash':digest(manifest)}
    report=summarize(cases,harness.observations,provenance=provenance)
    stages={}
    for trace in harness.observer.turns:
        for e in trace['events']:stages.setdefault(e['stage'],set()).add((trace['case_id'],trace['turn_index']))
    report['stage_observation_coverage']={k:{'observed_turns':len(v),'executed_turn_denominator':report['executed_turn_count'],
        'required_turn_denominator':report['turn_count']} for k,v in sorted(stages.items())}
    report.update(real_model_calls=0,source_capture_model_calls=len(calls),source_SQL_calls=0,milvus_calls=0,
        production_redis_writes=0,production_state_mutations=0,formal_model_benchmark=False,blind_holdout_run=False,
        full_production_orchestrator=False,fixture_receipts=harness.fixtures,capture_manifest_hash=digest(capture_hashes))
    write_json(private_directory/'observations.json',harness.observations)
    write_json(public_directory/'evaluation.json',report)
    write_json(public_directory/'capture_manifest.json',capture_hashes)
    write_json(public_directory/'replay_receipts.json',{'summary':dict(Counter(r['status'] for r in receipts)),
        'evaluator':evaluator_identity(manifest['evaluator_git_commit']),'receipts':receipts})
    write_json(public_directory/'dry_plan_receipts.json',{'same_raw_runtime_entry':False,'receipts':dry})
    print(json.dumps({k:report[k] for k in ('corpus','case_count','turn_count','executed_turn_count',
        'status_counts','evaluable_case_count','fully_evaluable_case_count','POST_HARNESS_FIRST_DIVERGENCE')},ensure_ascii=True),flush=True)
    return report


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--corpus',choices=['PUBLIC_DEV','TRANSITION','PRIVATE_VALIDATION'],required=True)
    for name in ('catalog','snapshot','manifest','source-directory','private-directory','public-directory','source-observations'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--source-observations-hash',required=True);p.add_argument('--source-commit',required=True)
    p.add_argument('--private-validation',type=Path)
    a=p.parse_args()
    cases=public_cases() if a.corpus=='PUBLIC_DEV' else transition_cases() if a.corpus=='TRANSITION' else read_jsonl(a.private_validation)
    if any(c['split']=='BLIND_HOLDOUT' for c in cases):raise ValueError('HOLDOUT_NOT_ALLOWED')
    def read(path):return json.loads(path.read_text(encoding='utf-8'))
    asyncio.run(replay_corpus(cases,read(a.catalog),a.snapshot.read_bytes(),manifest=read(a.manifest),
        source_directory=a.source_directory,private_directory=a.private_directory,public_directory=a.public_directory,
        source_observations=read(a.source_observations),source_observations_hash=a.source_observations_hash,source_commit=a.source_commit))


if __name__=='__main__':main()
