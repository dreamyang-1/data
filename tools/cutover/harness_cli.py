"""Run current Gold through the existing runtime runner with private evidence.

This command is a harness baseline, never a multi-model benchmark or cutover.
BLIND_HOLDOUT is deliberately not a runnable corpus in this entry point.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from app.config import Settings
from tools.cutover.evaluation_contract import digest
from tools.cutover.semantic_evaluator import read_jsonl
from tools.cutover.harness_corpus import enrich
from tools.cutover.harness_contract import summarize
from tools.cutover.harness_manifest import evaluator_identity,verify_frozen,write_json
from tools.cutover.harness_runtime import HarnessRuntime
from tools.cutover.run_raw_transition_benchmark import run

ROOT=Path(__file__).resolve().parents[2]


def public_cases():
    return enrich(read_jsonl(ROOT/'docs/cutover/evaluation_gates/gold_axes.jsonl'),corpus='PUBLIC_DEV')


def transition_cases():
    return enrich(read_jsonl(ROOT/'docs/cutover/state_evaluation/transition_gold.jsonl'),corpus='TRANSITION')


async def execute(cases,catalog,raw_snapshot,settings,*,manifest,private_directory,public_directory,
                  source_observations=None,source_observations_hash=None,allow_model_calls=False,batch_size=20):
    verify_frozen(manifest)
    private_directory=Path(private_directory);public_directory=Path(public_directory)
    private_directory.mkdir(parents=True,exist_ok=False);public_directory.mkdir(parents=True,exist_ok=False)
    captures_dir=private_directory/'captures';captures_dir.mkdir()
    observations=[];reports=[];traces=[];model_calls=[];fixtures={}
    if not cases:raise ValueError('EMPTY_HARNESS_RUN')
    for offset in range(0,len(cases),batch_size):
        batch=cases[offset:offset+batch_size]
        harness=HarnessRuntime(batch,evaluator_commit=manifest['evaluator_git_commit'],
            evaluator_hash=manifest['evaluator_hash'],pending_codes=('sales_total_including_tax','sales_total_quantity'))
        def capture(value):
            harness.capture(value)
            filename=value['case_id']+'-turn-'+str(value['turn_index'])+'.json'
            write_json(captures_dir/filename,value)
            if harness.observer.active:
                write_json(captures_dir/(filename+'.trace.json'),harness.observer.active)
        def progress(value):
            p=value['prediction']
            print(json.dumps({'case_id':p['case_id'],'runtime_status':p['status'],
                              'executed_turns':len(value['turns']),'model_requests':len(value['model_calls'])}),flush=True)
        with harness.observer:
            result=await run(batch,catalog,raw_snapshot,settings,allow_model_calls=allow_model_calls,
                max_calls=200,private_capture=capture,progress=progress,source_observations=source_observations,
                source_observations_hash=source_observations_hash,harness=harness)
        observations.extend(harness.observations);reports.append(result['evaluation'])
        model_calls.extend(result['model_calls']);traces.extend(harness.observer.turns);fixtures.update(harness.fixtures)
        write_json(private_directory/('batch-'+str(offset)+'.json'),{
            'observations':harness.observations,'result':result,'traces':harness.observer.turns})
        # Save completed batches before continuing; interruption never fabricates
        # a completed corpus and never silently overwrites captured evidence.
        write_json(public_directory/'progress_receipt.json',{'completed_cases':len(observations),
            'required_cases':len(cases),'completed_batches':len(reports),'model_requests':len(model_calls)})
    provenance={**evaluator_identity(manifest['evaluator_git_commit']),
        'mode':'LIVE_MODEL_PLAN_ONLY','corpus':cases[0]['corpus'],'gold_hash':digest(cases),
        'catalog_hash':catalog['artifact_hash'],'candidate_snapshot_hash':manifest['candidate_snapshot_hash'],
        'as_of':manifest['as_of'],'scope':catalog['scope'],'baseline_manifest_hash':digest(manifest)}
    report=summarize(cases,observations,provenance=provenance)
    stage_sets={}
    for trace in traces:
        key=(trace['case_id'],trace['turn_index'])
        for event in trace['events']:stage_sets.setdefault(event['stage'],set()).add(key)
    report['stage_observation_coverage']={stage:{'observed_turns':len(keys),
        'executed_turn_denominator':report['executed_turn_count'],'required_turn_denominator':report['turn_count']}
        for stage,keys in sorted(stage_sets.items())}
    report.update(real_model_calls=len(model_calls),source_SQL_calls=0,milvus_calls=0,production_redis_writes=0,
        production_state_mutations=0,fixture_receipts=fixtures,
        current_runtime_commit=manifest['git_commit'],full_production_orchestrator=False,
        formal_model_benchmark=False,blind_holdout_run=False)
    write_json(private_directory/'observations.json',observations)
    write_json(private_directory/'traces.json',traces)
    write_json(private_directory/'model_calls.json',model_calls)
    write_json(public_directory/'evaluation.json',report)
    print(json.dumps({k:report[k] for k in ('corpus','case_count','turn_count','executed_turn_count',
        'evaluable_case_count','fully_evaluable_case_count','status_counts','real_model_calls')},ensure_ascii=False),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--corpus',choices=['PUBLIC_DEV','TRANSITION','PRIVATE_VALIDATION'],required=True)
    for key in ('catalog','snapshot','manifest','private-directory','public-directory'):
        parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--private-validation',type=Path)
    parser.add_argument('--source-observations',type=Path)
    parser.add_argument('--source-observations-hash')
    parser.add_argument('--allow-model-calls',action='store_true',required=True)
    args=parser.parse_args()
    if args.corpus=='PRIVATE_VALIDATION' and not args.private_validation:
        parser.error('PRIVATE_VALIDATION requires a private file')
    cases=public_cases() if args.corpus=='PUBLIC_DEV' else transition_cases() if args.corpus=='TRANSITION' else read_jsonl(args.private_validation)
    if any(c['split']=='BLIND_HOLDOUT' for c in cases):raise ValueError('HOLDOUT_NOT_ALLOWED_IN_DEVELOPMENT_RUNNER')
    catalog=json.loads(args.catalog.read_text(encoding='utf-8'))
    manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
    source=json.loads(args.source_observations.read_text(encoding='utf-8')) if args.source_observations else None
    asyncio.run(execute(cases,catalog,args.snapshot.read_bytes(),Settings(_env_file=ROOT/'.env'),manifest=manifest,
        private_directory=args.private_directory,public_directory=args.public_directory,
        source_observations=source,source_observations_hash=args.source_observations_hash,
        allow_model_calls=args.allow_model_calls))


if __name__=='__main__':main()
