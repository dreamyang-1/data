"""Consume captured proposals in the native planner, with live downstream drafts.

This adapter is deliberately diagnostic. Legacy relation signal fields are
projected from an already validated proposal to satisfy the old bridge guards;
no current mention, slot declaration, operation, binding or reducer is edited.
All outputs are separate from the first relation-only experiment.
"""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
from unittest.mock import patch
from contextlib import ExitStack

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from tools.cutover.run_context_proposal_round4 import read,write,live_cases
from tools.cutover.context_proposal_round4 import discover_context,validate_proposal,proposal_resolution
from tools.cutover.context_proposal_schema import ContextProposal
from tools.cutover.run_raw_transition_benchmark import ModelRecorder,network_guard,frozen_publication,failure_reason
from tools.cutover.frozen_source_values import FrozenSourceValues
from tools.cutover.harness_observation import RuntimeObserver
from tools.cutover.evaluation_contract import digest
from app.semantic_v2.authorized_contract import ScopedArtifact,contract_digest
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.pipeline import TurnResolver,CurrentTurnSemanticParse
from app.semantic_v2.recognition import RawTurnPlanner
from app.domain.models import ChatRequest,TrustedIdentity
from app.config import Settings


def relation_signal_adapter(surface,trace):
    """Compatibility projection only. No keyword/score competition."""
    if trace['FINAL_STATUS']!='ACCEPTED':raise ValueError('CONTEXT_PROPOSAL_NOT_ACCEPTED')
    data=deepcopy(surface)
    act=trace['FINAL_RELATION']
    data.update(dialogue_act_candidates=[act],
        reference_signals=['HISTORICAL'] if act=='RETURN_TO_TOPIC' else [] if act=='NEW_TASK' else ['ELLIPSIS'],
        followup_signals=[] if act in {'NEW_TASK','RETURN_TO_TOPIC'} else ['CONTINUE'],
        topic_shift_signals=['EXPLICIT_NEW_TASK'] if act=='NEW_TASK' else [])
    return CurrentTurnSemanticParse.model_validate(data)


async def main(args):
    args.output_directory.mkdir(parents=True,exist_ok=False)
    out=args.output_directory;live=read(args.proposals/'live_result.json');indexed={r['case_id']:r for r in live['rows']}
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    base=ROOT/'.eval_private/harness-20260909T083000Z';raw=(base/'catalog_snapshot.json').read_bytes()
    observations=read(base/'source_observations.json')
    manifest=read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json')
    settings=Settings().model_copy(update={'intent_model_max_retries':0,'intent_model_timeout_seconds':60})
    assert settings.intent_model_name=='qwen3.7-max' and settings.intent_model_enable_thinking is False
    recorder=ModelRecorder(settings,max_calls=24,capture_exchanges=True);rows=[]
    for case in live_cases():
        cid=case['case_id'];original=indexed[cid];capture=case['capture']
        source=read(args.proposals/(cid+'-B-capture.json'))
        assert digest(source)==original['capture_hash']
        if original['model_error'] or not original['trace'] or original['trace']['FINAL_STATUS']!='ACCEPTED':
            rows.append(dict(case_id=cid,status='NOT_RUN_PROPOSAL_REJECTED',new_model_calls=0));continue
        before=capture['before'];state=ScopedArtifact.model_validate(before['state']);denied=[]
        with network_guard(),ExitStack() as stack:
            oagnet=ROOT/'Oagnet' if (ROOT/'Oagnet').is_dir() else ROOT.parent/'Oagnet'
            stack.enter_context(patch.object(sys,'path',[*sys.path,str(oagnet),str(oagnet/'tests')]))
            publication=frozen_publication(raw,catalog,denied,native_source_values=True,
                recorded_activation_id=state.context.catalog_pin.activation_id)
            source_replay=FrozenSourceValues(observations,catalog=catalog,snapshot=json.loads(raw),
                expected_hash=manifest['source_value_evidence']['artifact_hash'],allow_synthetic=capture['mode']=='SCRIPTED_MODEL_PIPELINE')
            import catalog_value_sources
            stack.enter_context(patch.object(catalog_value_sources,'observe',source_replay.observe))
            stack.enter_context(patch.object(catalog_value_sources,'observe_probe',source_replay.observe_probe))
            req=ChatRequest(semantic_model_id=81,business_domain_ids=[205],application_id='isolated-evaluation',
                conversation_id=state.payload['conversation_id'],message_id=f"turn-{capture['turn_index']}",question=capture['question'])
            identity=TrustedIdentity(tenant_id='evaluation',user_id='evaluation')
            session=ScopedPlanSession(req,identity,publication)
            kwargs=dict(state=state,plans=tuple(ScopedArtifact.model_validate(p) for p in before['plans']),
                pending=ScopedArtifact.model_validate(before['pending']) if before['pending'] else None)
            discovered=discover_context(session,**kwargs)
            assert discovered.context_digest==original['context_hash']
            trace=validate_proposal(ContextProposal.model_validate(source['proposal']),discovered,session=session,state=state,question=req.question)
            parsed=relation_signal_adapter(source['parse'],trace)
            target=trace['FINAL_TARGET'];handle='task:'+contract_digest({'task':target})[:24] if target else None
            def resolver(parse,*,state,task_patch,semantic_resolution,**kw):
                return proposal_resolution(trace,parse,state,task_patch,semantic_resolution)
            # All transient monkeypatches are confined to this diagnostic turn.
            stack.enter_context(patch.object(TurnResolver,'resolve',staticmethod(resolver)))
            class Model:
                async def complete(self,**kw):
                    if kw['stage']=='v2_current_turn':return parsed
                    if kw['stage']=='v2_semantic_edits':
                        kw=deepcopy(kw)
                        tasks=kw['context']['tasks']
                        if trace['FINAL_RELATION']=='RETURN_TO_TOPIC':
                            kw['context']['tasks']=[t for t in tasks if t.get('task_handle')==handle]
                            kw['schema']['properties']['historical_task_handle']={'anyOf':[{'type':'string','enum':[handle]},{'type':'null'}]}
                        result=await recorder.complete(**kw)
                        if result.historical_task_handle and result.historical_task_handle!=handle:
                            raise ValueError('DRAFT_CANNOT_OVERRIDE_VALIDATED_TARGET')
                        return result
                    return await recorder.complete(**kw)
            recorder.begin_turn(cid,capture['turn_index']);count=len(recorder.calls)
            observer=RuntimeObserver();result=None;failure=None
            with observer:
                observer.begin_turn(cid,capture['turn_index'])
                try:
                    engine=RawTurnPlanner(Model(),publication,clock=lambda:datetime.fromisoformat(capture['clock']))
                    result=await engine.run(req,identity,**kwargs)
                except Exception as exc:failure={'type':type(exc).__name__,'reason':failure_reason(exc)}
            assert state.model_dump(mode='json')==before['state']
            output=result.model_dump(mode='json') if result else None
            decisions=[e['output'] for e in observer.turns[0]['events'] if e['stage']=='TurnResolutionInput' and e.get('output')]
            record=dict(case_id=cid,result=output,failure=failure,events=observer.turns[0]['events'],
                model_exchanges=recorder.exchanges,model_outputs=recorder.outputs,proposal_trace=trace,
                before_hash=digest(before),proposal_capture_hash=original['capture_hash'],
                signal_projection={k:parsed.model_dump(mode='json')[k] for k in ('dialogue_act_candidates','reference_signals','followup_signals','topic_shift_signals')})
            write(out/(cid+'.json'),record)
            rows.append(dict(case_id=cid,status='ACCEPTED_DIAGNOSTIC_ONLY' if result else 'REJECTED',
                failure=failure,record_hash=digest(record),new_model_calls=len(recorder.calls)-count,
                last_resolution=decisions[-1] if decisions else None,source_sql_calls=0,production_writes=0))
            write(out/'native_result.json',dict(rows=rows,calls=recorder.calls,complete=len(rows)==len(live['rows'])))
            print(json.dumps({k:rows[-1][k] for k in ('case_id','status','failure','new_model_calls')}),flush=True)
    write(out/'native_result.json',dict(rows=rows,calls=recorder.calls,complete=len(rows)==len(live['rows'])))
    await recorder.upstream.aclose()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--proposals',type=Path,required=True)
    p.add_argument('--output-directory',type=Path,required=True);p.add_argument('--allow-model-calls',action='store_true',required=True)
    asyncio.run(main(p.parse_args()))
