"""Bounded live verification of the production joint context contract (not Benchmark)."""
import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import json
import logging
from pathlib import Path
import sys
from time import monotonic
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from tools.cutover.run_context_proposal_round4 import live_cases, read, write
from tools.cutover.run_raw_transition_benchmark import ModelRecorder, network_guard, frozen_publication, failure_reason
from tools.cutover.frozen_source_values import FrozenSourceValues
from tools.cutover.evaluation_contract import digest
from app.semantic_v2.authorized_contract import ScopedArtifact
from app.semantic_v2.recognition import RawTurnPlanner
from app.domain.models import ChatRequest, TrustedIdentity
from app.config import Settings


async def live(out, *, refresh_from=None):
    out.mkdir(parents=True,exist_ok=False)
    base=ROOT/'.eval_private/harness-20260909T083000Z'
    old=ROOT/'.eval_private/context-round4-20260910T004000Z/native-final'
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    raw=(base/'catalog_snapshot.json').read_bytes()
    observations=read(base/'source_observations.json')
    manifest=read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json')
    settings=Settings().model_copy(update={'intent_model_max_retries':0,'intent_model_timeout_seconds':60})
    assert settings.intent_model_name=='qwen3.7-max' and settings.intent_model_enable_thinking is False
    previous_calls=len(read(refresh_from/'live_result.json')['calls']) if refresh_from else 0
    recorder=ModelRecorder(settings,max_calls=20-previous_calls,capture_exchanges=True)
    full_live={'S81-001','S81-002','S81-003','S81-014','S81-017','R4L-010'}
    cases=live_cases();rows=[]
    if refresh_from:
        # Frozen current-turn outputs stay byte-for-byte unchanged. Only the
        # two incompatible old downstream responses need a current draft.
        full_live={'S81-011','S81-013'}
        cases=[c for c in cases if c['case_id'] in full_live]
    write(out/'input_freeze.json',dict(cases=[dict(case_id=c['case_id'],question_hash=digest(c['capture']['question']),
        before_hash=digest(c['capture']['before']),source_capture_hash=c['source_capture_hash']) for c in cases],
        full_live_cases=sorted(full_live),model='qwen3.7-max',thinking=False,temperature=0,retries=0,
        max_calls=20-previous_calls,prior_live_calls=previous_calls,
        scope=catalog['scope'],catalog_hash=catalog['artifact_hash']))
    for case in cases:
        cid=case['case_id'];cap=case['capture'];before=cap['before'];denied=[]
        state=ScopedArtifact.model_validate(before['state']);traces=[];timings=[];requests=[]
        recorded=read(old/(cid+'.json')) if (old/(cid+'.json')).exists() else {}
        downstream=iter(recorded.get('model_outputs',[]))
        first_record=read(refresh_from/(cid+'.json')) if refresh_from else None
        class Trace(logging.Handler):
            def emit(self,record):
                if hasattr(record,'context_arbitration'):traces.append(deepcopy(record.context_arbitration))
        handler=Trace();logger=logging.getLogger('app.semantic_v2.context_proposal')
        previous_level=logger.level;logger.setLevel(logging.INFO);logger.addHandler(handler)
        recorder.begin_turn(cid,cap['turn_index']);start=monotonic();count=len(recorder.calls)
        error=None;result=None;total=0
        try:
            with network_guard(),ExitStack() as stack:
                oagnet=ROOT.parent/'Oagnet'
                stack.enter_context(patch.object(sys,'path',[*sys.path,str(oagnet),str(oagnet/'tests')]))
                publication=frozen_publication(raw,catalog,denied,native_source_values=True,
                    recorded_activation_id=state.context.catalog_pin.activation_id)
                source=FrozenSourceValues(observations,catalog=catalog,snapshot=json.loads(raw),
                    expected_hash=manifest['source_value_evidence']['artifact_hash'],allow_synthetic=False)
                import catalog_value_sources
                stack.enter_context(patch.object(catalog_value_sources,'observe',source.observe))
                stack.enter_context(patch.object(catalog_value_sources,'observe_probe',source.observe_probe))
                class Model:
                    async def complete(self,**kw):
                        began=monotonic();is_live=(kw['stage']!='v2_current_turn' if refresh_from else
                            kw['stage']=='v2_current_turn' or cid in full_live)
                        requests.append(dict(stage=kw['stage'],live=is_live,input_hash=digest(kw['context'])))
                        try:
                            if refresh_from and kw['stage']=='v2_current_turn':
                                exchange=next(e for e in first_record['exchanges'] if e['stage']==kw['stage'])
                                assert json.loads(exchange['request_body']['messages'][1]['content'])==kw['context']
                                assert exchange['request_body']['messages'][0]['content']==kw['instruction']+'\nJSON Schema:\n'+json.dumps(kw['schema'],ensure_ascii=False)
                                stored=next(o for o in first_record['outputs'] if o['stage']==kw['stage'])
                                return kw['output_model'].model_validate(stored['output'])
                            if is_live:return await recorder.complete(**kw)
                            stored=next(downstream)
                            assert stored['stage']==kw['stage'],'RECORDED_DOWNSTREAM_STAGE_MISMATCH'
                            return kw['output_model'].model_validate(stored['output'])
                        finally:timings.append(dict(stage=kw['stage'],live=is_live,seconds=monotonic()-began))
                req=ChatRequest(semantic_model_id=81,business_domain_ids=[205],application_id='isolated-evaluation',
                    conversation_id=state.payload['conversation_id'],message_id=f"turn-{cap['turn_index']}",question=cap['question'])
                engine=RawTurnPlanner(Model(),publication,clock=lambda:datetime.fromisoformat(cap['clock']))
                run_start=monotonic()
                try:
                    result=await engine.run(req,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),state=state,
                        plans=tuple(ScopedArtifact.model_validate(p) for p in before['plans']),
                        pending=ScopedArtifact.model_validate(before['pending']) if before['pending'] else None)
                finally:total=monotonic()-run_start
        except Exception as exc:error=dict(type=type(exc).__name__,reason=failure_reason(exc))
        finally:logger.removeHandler(handler);logger.setLevel(previous_level)
        assert state.model_dump(mode='json')==before['state']
        trace=traces[-1] if traces else None
        active=state.payload['topics'][state.payload['active_topic_id']]['active_task_id']
        pending=before.get('pending')
        expected={'PREVIOUS':active,'NEW':'task:'+f"turn-{cap['turn_index']}",
            'PENDING':pending['payload']['task_id'] if pending else None,'HISTORY:0':'task:turn-0'}.get(case['expected_target'])
        actual=('task:'+f"turn-{cap['turn_index']}" if trace and trace['FINAL_RELATION']=='NEW_TASK' else trace['FINAL_TARGET'] if trace else None)
        target_match=(actual==expected if trace and trace['FINAL_STATUS']=='ACCEPTED' else
            expected is None and trace is not None and trace['FINAL_STATUS'] in {'AMBIGUOUS','UNRESOLVED'})
        record=dict(case_id=cid,trace=trace,result=result.model_dump(mode='json') if result else None,error=error,
            exchanges=recorder.exchanges,outputs=recorder.outputs,requests=requests,timings=timings,total_v2_seconds=total)
        write(out/(cid+'.json'),record)
        rows.append(dict(case_id=cid,record_hash=digest(record),model_calls=len(recorder.calls)-count,
            target_match=target_match,expected_target=expected,actual_target=actual,
            context_status=trace['FINAL_STATUS'] if trace else None,error=error,
            outcome='PLAN' if result and getattr(result,'plan',None) else 'CLARIFICATION' if result else 'TERMINAL',
            downstream='LIVE' if cid in full_live else 'RECORDED_ROUND4; NOT_STRICT_INPUT_REPLAY',
            total_v2_seconds=total,stage_seconds=timings,source_sql_calls=0,production_writes=0))
        write(out/'live_result.json',dict(rows=rows,calls=recorder.calls,complete=len(rows)==len(cases),
            prior_live_calls=previous_calls))
        print(json.dumps(rows[-1]),flush=True)
        assert not denied
    await recorder.upstream.aclose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory',type=Path,required=True)
    parser.add_argument('--allow-model-calls',action='store_true',required=True)
    parser.add_argument('--refresh-from',type=Path)
    args=parser.parse_args();asyncio.run(live(args.output_directory,refresh_from=args.refresh_from))
