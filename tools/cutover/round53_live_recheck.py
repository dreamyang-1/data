"""Round 5.3 extension of the existing native recorder for actual accepted prefixes.

This uses the existing live recorder, native state transitions and frozen source
adapters. Reviewed parallel cases retain real prefix provenance and separate
denominators. Prerequisite success is not full-case success. No Oracle model outputs.
"""
import argparse
import asyncio
from copy import deepcopy
import json
from hashlib import sha256
from pathlib import Path
import sys
from unittest.mock import patch
from contextlib import ExitStack
from datetime import datetime
from time import monotonic

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
SERVICE_ROOT=ROOT/'Oagnet' if (ROOT/'Oagnet').is_dir() else ROOT.parent/'Oagnet'
from tools.cutover import live_followup_round5_1 as native
from tools.cutover.evaluation_contract import digest
from tools.cutover.round53_source_evidence import FrozenTargetedCandidates
from tools.cutover.round53_healthy_cases import validate_case
from contextlib import contextmanager


async def resume(output,case,seed_path,max_calls,prior_outputs):
    validate_case(case)
    case_id=case["case_id"]
    prior_calls=sum(len(native.read(path/'result.json')['calls']) for path in prior_outputs)
    if prior_calls+max_calls>12:raise ValueError('ROUND53_GLOBAL_MODEL_BUDGET_EXCEEDS_12')
    seed=native.read(seed_path)
    if seed['error'] or not seed['result']['plan']:
        raise ValueError('ACTUAL_ACCEPTED_CASE_PREFIX_REQUIRED')
    # Preserve the actual prior receipt. Never replace its state, IDs, values,
    # scope or provenance with Oracle output or a hand-authored success.
    parent=native.read(seed_path.parent/'result.json')
    assert seed['turn_id'] in parent['turns'] and parent['calls']
    assert case['seed_capture_hash']==digest(seed)
    assert case['utterances'][0]==seed['question']
    out=output;out.mkdir(parents=True,exist_ok=False);source_hashes=native.hashes()
    catalog=native.read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    base=ROOT/'.eval_private/harness-20260909T083000Z';raw=(base/'catalog_snapshot.json').read_bytes()
    manifest=native.read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json')
    settings=native.Settings().model_copy(update={'intent_model_max_retries':0,'intent_model_timeout_seconds':60})
    assert settings.intent_model_name=='qwen3.7-max' and settings.intent_model_enable_thinking is False
    model=native.ModelRecorder(settings,max_calls=max_calls,capture_exchanges=True)
    native.write(out/'input_freeze.json',dict(case=case,seed_capture_hash=digest(seed),
        seed_runtime_hashes=parent['runtime_hashes'],runtime_hashes=source_hashes,prior_calls=prior_calls,max_calls=max_calls,
        provenance='ACTUAL_ROUND53_LIVE_ACCEPTED_PREFIX; NO_ORACLE_STATE',scope=native.SCOPE,clock=native.CLOCK))
    value=seed['result'];before=dict(state=value['next_state'],pending=None,plans=[value['plan_state']],history=[value])
    denied=[];records=[];status='NOT_RUN';blocked=None
    try:
        with native.network_guard(),ExitStack() as stack:
            other=SERVICE_ROOT;stack.enter_context(patch.object(sys,'path',[*sys.path,str(other),str(other/'tests')]))
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
            for i in range(1,len(case['utterances'])):
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
                value=item['result'];by_task={p['payload']['task_id']:p for p in before['plans']}
                by_task[value['plan_state']['payload']['task_id']]=value['plan_state']
                before=dict(state=value['next_state'],pending=None,plans=list(by_task.values()),history=before['history']+[value])
                status='PLAN'
            assert native.hashes()==source_hashes and not denied
            native.write(out/'result.json',dict(cases=[dict(case_id=case_id,turns=records,status=status,blocked_by=blocked,
                required_turns=len(case['utterances'])-1,reached_turns=len(records))],turns=records,calls=model.calls,
                complete=True,runtime_hashes=source_hashes,source_reads=source.calls,sql_calls=0,production_writes=0,blind_access=0))
            native.write(out/'round53_budget.json',dict(prior_calls=prior_calls,current_calls=len(model.calls),global_limit=12))
    finally:await model.upstream.aclose()


@contextmanager
def sources(evidence):
    catalog=native.read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    snapshot=native.read(evidence/'live_catalog_snapshot.json')
    exact=native.read(evidence/'source_observations.json')
    targeted=native.read(evidence/'targeted_observations.json')
    original=native.FrozenSourceValues
    class Extended(original):
        def __init__(self,*args,**kw):
            kw['expected_hash']=exact['artifact_hash']
            super().__init__(exact,**kw)
    current=FrozenTargetedCandidates(targeted,catalog=catalog,snapshot=snapshot,expected_hash=targeted['artifact_hash'])
    other=SERVICE_ROOT
    with patch.object(sys,'path',[*sys.path,str(other)]):
        import catalog_value_candidates
        with patch.object(native,'FrozenSourceValues',Extended),patch.object(catalog_value_candidates,'observe_candidates',current.observe_candidates):
            yield current


async def main(args):
    prior=sum(len(native.read(p/'result.json')['calls']) for p in args.prior_output)
    assert 1<=args.max_model_calls<=12 and prior+args.max_model_calls<=12
    if args.seed:
        validate_case(native.read(args.case))
    # Native Round5.1's input header still describes its original base fixture.
    # Freeze this extension's actual source versions separately BEFORE any call.
    frozen_inputs = {}
    for name in ('source_observations.json','targeted_observations.json','live_catalog_snapshot.json'):
        source_path = args.evidence/name
        frozen_inputs[name] = sha256(source_path.read_bytes()).hexdigest()
    contract_sources = [ROOT/'app/semantic_v2/source_value_probe.py',
        ROOT/'app/semantic_v2/source_value_recognition.py',
        SERVICE_ROOT/'catalog_publication.py',SERVICE_ROOT/'catalog_value_candidates.py']
    contract_hashes = {p.name:sha256(p.read_bytes()).hexdigest() for p in contract_sources}
    freeze_path = args.output.with_suffix('.source-freeze.json')
    with freeze_path.open('x',encoding='utf-8') as receipt:
        json.dump(dict(source_files=frozen_inputs,contract_sources=contract_hashes,
            prior_model_calls=prior,allowed_model_calls=args.max_model_calls),receipt,indent=2)
    with sources(args.evidence) as source:
        if args.seed:
            case=native.read(args.case)
            await resume(args.output,case,args.seed,args.max_model_calls,args.prior_output)
        else:
            original=native.read(ROOT/'docs/v2_cutover/semantic_round5_1/live_scenarios.json')['primary']
            cases=[deepcopy(c) for c in original if c['case_id'] in args.original_case]
            assert len(cases)==len(args.original_case)
            if args.prerequisite_only:
                for case in cases:
                    case['utterances']=case['utterances'][:1]
                    case['evaluation_kind']='PREREQUISITE_ONLY_NOT_ORIGINAL_CASE_PASS'
            with patch.object(native,'cases',lambda:cases):
                await native.run(args.output,args.max_model_calls)
        result=native.read(args.output/'result.json')
        native.write(args.output/'round53_sources.json',dict(targeted_calls=source.calls,
            source_hash=native.read(args.evidence/'source_observations.json')['artifact_hash'],
            targeted_hash=native.read(args.evidence/'targeted_observations.json')['artifact_hash'],
            prior_model_calls=prior,current_model_calls=len(result['calls']),global_budget=12))
    assert all(sha256((args.evidence/name).read_bytes()).hexdigest()==expected for name,expected in frozen_inputs.items())
    assert all(sha256(p.read_bytes()).hexdigest()==contract_hashes[p.name] for p in contract_sources)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--max-model-calls',type=int,required=True)
    p.add_argument('--prior-output',type=Path,action='append',default=[])
    p.add_argument('--original-case',action='append',default=[])
    p.add_argument('--prerequisite-only',action='store_true')
    p.add_argument('--seed',type=Path)
    p.add_argument('--case',type=Path)
    p.add_argument('--allow-model-calls',action='store_true',required=True)
    a=p.parse_args()
    assert (a.seed is not None and a.case is not None) != bool(a.original_case)
    asyncio.run(main(a))
