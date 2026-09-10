"""Bounded recheck of unchanged Round 5.1 inputs on current Native V2.

This uses the existing live recorder, native state transitions and frozen source
adapters. It changes only which frozen public cases are scheduled. Prerequisite
checks are explicitly separate from full-case success. No Oracle model outputs.
"""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch
from contextlib import ExitStack
from datetime import datetime
from time import monotonic

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.cutover import live_followup_round5_1 as native
from tools.cutover.evaluation_contract import digest


async def resume(output,case_id,seed_path,max_calls,prior_outputs):
    prior_calls=sum(len(native.read(path/'result.json')['calls']) for path in prior_outputs)
    if prior_calls+max_calls>12:raise ValueError('ROUND52_GLOBAL_MODEL_BUDGET_EXCEEDS_12')
    seed=native.read(seed_path)
    if seed['case_id']!=case_id or seed['error'] or not seed['result']['plan']:
        raise ValueError('ACTUAL_ACCEPTED_CASE_PREFIX_REQUIRED')
    # Preserve the actual prior receipt. Never replace its state, IDs, values,
    # scope or provenance with Oracle output or a hand-authored success.
    parent=native.read(seed_path.parent/'result.json')
    assert seed['turn_id'] in parent['turns'] and parent['calls']
    case=next(c for c in native.read(ROOT/'docs/v2_cutover/semantic_round5_1/live_scenarios.json')['primary'] if c['case_id']==case_id)
    assert seed['question']==case['utterances'][seed['turn_index']]
    out=output;out.mkdir(parents=True,exist_ok=False);source_hashes=native.hashes()
    catalog=native.read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    base=ROOT/'.eval_private/harness-20260909T083000Z';raw=(base/'catalog_snapshot.json').read_bytes()
    manifest=native.read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json')
    settings=native.Settings().model_copy(update={'intent_model_max_retries':0,'intent_model_timeout_seconds':60})
    assert settings.intent_model_name=='qwen3.7-max' and settings.intent_model_enable_thinking is False
    model=native.ModelRecorder(settings,max_calls=max_calls,capture_exchanges=True)
    native.write(out/'input_freeze.json',dict(case=case,seed_capture_hash=digest(seed),
        seed_runtime_hashes=parent['runtime_hashes'],runtime_hashes=source_hashes,prior_calls=prior_calls,max_calls=max_calls,
        provenance='ACTUAL_ROUND52_LIVE_ACCEPTED_PREFIX; NO_ORACLE_STATE',scope=native.SCOPE,clock=native.CLOCK))
    value=seed['result'];before=dict(state=value['next_state'],pending=None,plans=[value['plan_state']],history=[value])
    denied=[];records=[];status='NOT_RUN';blocked=None
    try:
        with native.network_guard(),ExitStack() as stack:
            other=ROOT.parent/'Oagnet';stack.enter_context(patch.object(sys,'path',[*sys.path,str(other),str(other/'tests')]))
            publication=native.frozen_publication(raw,catalog,denied,native_source_values=True,
                recorded_activation_id=value['next_state']['context']['catalog_pin']['activation_id'])
            source=native.FrozenSourceValues(native.read(base/'source_observations.json'),catalog=catalog,snapshot=json.loads(raw),
                expected_hash=manifest['source_value_evidence']['artifact_hash'],allow_synthetic=False)
            import catalog_value_sources
            stack.enter_context(patch.object(catalog_value_sources,'observe',source.observe))
            stack.enter_context(patch.object(catalog_value_sources,'observe_probe',source.observe_probe))
            observer=stack.enter_context(native.RuntimeObserver())
            traces=[]
            class Trace(native.logging.Handler):
                def emit(self,event):
                    if hasattr(event,'context_arbitration'):traces.append(deepcopy(event.context_arbitration))
            handler=Trace();logger=native.logging.getLogger('app.semantic_v2.context_proposal');old_level=logger.level
            logger.setLevel(native.logging.INFO);logger.addHandler(handler)
            stack.callback(logger.removeHandler,handler);stack.callback(logger.setLevel,old_level)
            engine=native.RawTurnPlanner(model,publication,clock=lambda:datetime.fromisoformat(native.CLOCK))
            for i in range(seed['turn_index']+1,len(case['utterances'])):
                if max_calls-len(model.calls)<2:
                    status='NOT_RUN_BUDGET';blocked='MODEL_BUDGET_EXHAUSTED';break
                text=case['utterances'][i];key=digest([case['initial_pending'],case['utterances'][:i+1]])[:24]
                model.begin_turn(case_id,i);observer.begin_turn(case_id,i)
                traces.clear()
                call_start=len(model.calls);source_start=len(source.calls);timings=[];start=monotonic();original=model.complete
                async def timed(**kw):
                    began=monotonic()
                    try:return await original(**kw)
                    finally:timings.append(dict(stage=kw['stage'],seconds=monotonic()-began))
                model.complete=timed;result=None;error=None
                state=native.ScopedArtifact.model_validate(before['state'])
                try:
                    req=native.ChatRequest(**native.SCOPE,application_id='isolated-evaluation',conversation_id=native.CONVERSATION,
                        message_id='turn-'+key,question=text)
                    result=await engine.run(req,native.TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),state=state,
                        plans=tuple(native.ScopedArtifact.model_validate(p) for p in before['plans']))
                except Exception as exc:error=dict(type=type(exc).__name__,reason=native.failure_reason(exc),context_trace=getattr(exc,'context_trace',None))
                finally:model.complete=original
                assert state.model_dump(mode='json')==before['state']
                item=dict(case_id=case_id,turn_id=key,turn_index=i,question=text,before=deepcopy(before),
                    result=result.model_dump(mode='json') if result else None,error=error,events=deepcopy(observer.active['events']),
                    traces=deepcopy(traces),
                    outputs=deepcopy(model.outputs),exchanges=deepcopy(model.exchanges),model_calls=deepcopy(model.calls[call_start:]),
                    source_calls=deepcopy(source.calls[source_start:]),stage_timings=timings,total_seconds=monotonic()-start,state_unchanged=True)
                native.write(out/(key+'.json'),item);records.append(key)
                print(json.dumps(dict(case_id=case_id,index=i,error=error,plan=bool(result and result.plan),calls=len(model.calls))),flush=True)
                if error or result is None or not result.plan:
                    status='FAILED_CURRENT';blocked=key;break
                value=item['result'];before=dict(state=value['next_state'],pending=None,plans=[value['plan_state']],history=before['history']+[value])
                status='PLAN'
            assert native.hashes()==source_hashes and not denied
            native.write(out/'result.json',dict(cases=[dict(case_id=case_id,turns=records,status=status,blocked_by=blocked,
                required_turns=len(case['utterances'])-seed['turn_index']-1,reached_turns=len(records))],turns=records,calls=model.calls,
                complete=True,runtime_hashes=source_hashes,source_reads=source.calls,sql_calls=0,production_writes=0,blind_access=0))
            native.write(out/'round52_budget.json',dict(prior_calls=prior_calls,current_calls=len(model.calls),global_limit=12))
    finally:await model.upstream.aclose()


async def run(output,case_ids,max_calls,prior_outputs,prerequisites):
    prior_calls=0
    for path in prior_outputs:
        prior_calls+=len(native.read(path/'result.json')['calls'])
    if max_calls+prior_calls>12:
        raise ValueError('ROUND52_GLOBAL_MODEL_BUDGET_EXCEEDS_12')
    frozen=native.read(ROOT/'docs/v2_cutover/semantic_round5_1/live_scenarios.json')['primary']
    indexed={case['case_id']:case for case in frozen}
    cases=[deepcopy(indexed[cid]) for cid in case_ids]
    for case in cases:
        if prerequisites:
            case['utterances']=case['utterances'][:1]
            case['evaluation_kind']='PREREQUISITE_ONLY_NOT_ORIGINAL_CASE_PASS'
            case['expected']=None
            case['expected_target']='NEW'
        else:case['evaluation_kind']='ORIGINAL_FROZEN_FULL_CASE'
    with patch.object(native,'cases',lambda:deepcopy(cases)):
        await native.run(output,max_calls)
    native.write(output/'round52_budget.json',dict(prior_calls=prior_calls,
        current_calls=len(native.read(output/'result.json')['calls']),global_limit=12,
        original_case_ids=case_ids,prerequisite_only=prerequisites))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-directory',type=Path,required=True)
    p.add_argument('--case-ids',nargs='+',required=True)
    p.add_argument('--max-model-calls',type=int,required=True)
    p.add_argument('--prior-output',type=Path,action='append',default=[])
    p.add_argument('--prerequisites-only',action='store_true')
    p.add_argument('--resume-seed',type=Path)
    p.add_argument('--allow-model-calls',action='store_true',required=True)
    a=p.parse_args()
    if not 1<=a.max_model_calls<=12:p.error('Budget must be in 1..12')
    if a.resume_seed:
        if len(a.case_ids)!=1 or a.prerequisites_only:p.error('Resume requires one original full case')
        asyncio.run(resume(a.output_directory,a.case_ids[0],a.resume_seed,a.max_model_calls,a.prior_output))
    else:asyncio.run(run(a.output_directory,a.case_ids,a.max_model_calls,a.prior_output,a.prerequisites_only))
