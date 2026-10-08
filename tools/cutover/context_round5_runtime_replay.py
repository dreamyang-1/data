"""Strict request matching of Round 5 captures on final runtime; no network."""
import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import json
import hashlib
from pathlib import Path
import sys
from time import perf_counter_ns
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from tools.cutover.run_context_proposal_round4 import live_cases,read,write
from tools.cutover.run_raw_transition_benchmark import network_guard,frozen_publication,failure_reason
from tools.cutover.frozen_source_values import FrozenSourceValues
from tools.cutover.evaluation_contract import digest
from app.semantic_v2.authorized_contract import ScopedArtifact
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2 import recognition,context_proposal
from app.domain.models import ChatRequest,TrustedIdentity


def outcome_semantics(value):
    if value is None:return None
    state=value['next_state']['payload'];plan=value.get('plan')
    return dict(payload=plan['logical_plan']['payload'] if plan else None,
        patch=value.get('resolution',{}).get('task_patch'),
        tasks={k:dict(active_version=t['active_version'],status=t['status'],clear_barriers=t['clear_barriers'],
            versions=[dict(version=v['version'],status=v['status'],semantics=v['semantics']) for v in t['versions']])
            for k,t in state['tasks'].items()},active_topic=state['active_topic_id'],pending=state['pending_records'])


async def run(private,out):
    out.mkdir(parents=True,exist_ok=False)
    def runtime_hashes():return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        for p in [ROOT/'app/semantic_v2'/n for n in ('context_contract.py','context_proposal.py','recognition.py','catalog_bridge.py')]}
    source_hashes=runtime_hashes()
    base=ROOT/'.eval_private/harness-20260909T083000Z';old=ROOT/'.eval_private/context-round4-20260910T004000Z/native-final'
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json');raw=(base/'catalog_snapshot.json').read_bytes()
    observations=read(base/'source_observations.json')
    manifest=read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json')
    rows=[];durations=[]
    for case in live_cases():
        cid=case['case_id'];cap=case['capture'];before=cap['before'];state=ScopedArtifact.model_validate(before['state'])
        first=read(private/'live'/(cid+'.json'))
        replacement=private/'downstream-refresh'/(cid+'.json')
        record=read(replacement) if replacement.exists() else first
        exchanges=first['exchanges'][:1]+([e for e in record['exchanges'] if e['stage']!='v2_current_turn'])
        captured={e['stage']:e for e in exchanges}
        typed={o['stage']:o['output'] for o in first['outputs']+record['outputs']}
        request_pins={r['stage']:r for r in record['requests']}
        if (old/(cid+'.json')).exists():
            for o in read(old/(cid+'.json'))['model_outputs']:
                if o['stage'] not in typed:typed[o['stage']]=o['output']
        called=[];matched=[];denied=[];failure=None;result=None;timings=[]
        with network_guard(),ExitStack() as stack:
            oagnet=ROOT.parent/'Oagnet';stack.enter_context(patch.object(sys,'path',[*sys.path,str(oagnet),str(oagnet/'tests')]))
            publication=frozen_publication(raw,catalog,denied,native_source_values=True,
                recorded_activation_id=state.context.catalog_pin.activation_id)
            source=FrozenSourceValues(observations,catalog=catalog,snapshot=json.loads(raw),
                expected_hash=manifest['source_value_evidence']['artifact_hash'],allow_synthetic=False)
            import catalog_value_sources
            stack.enter_context(patch.object(catalog_value_sources,'observe',source.observe))
            stack.enter_context(patch.object(catalog_value_sources,'observe_probe',source.observe_probe))
            for module,name in ((recognition,'discover_context'),(recognition,'accept_proposal'),
                    (recognition,'proposal_resolution'),(context_proposal,'validate_proposal')):
                original=getattr(module,name)
                def timed(*args,_fn=original,_name=name,**kwargs):
                    start=perf_counter_ns()
                    try:return _fn(*args,**kwargs)
                    finally:timings.append(dict(stage=_name,seconds=(perf_counter_ns()-start)/1e9))
                stack.enter_context(patch.object(module,name,timed))
            class Recorded:
                async def complete(self,**kw):
                    stage=kw['stage'];called.append(stage)
                    if stage in captured:
                        messages=captured[stage]['request_body']['messages']
                        assert json.loads(messages[1]['content'])==kw['context'],'MODEL_CONTEXT_DRIFT'
                        assert messages[0]['content']==kw['instruction']+'\nJSON Schema:\n'+json.dumps(kw['schema'],ensure_ascii=False),'MODEL_SCHEMA_OR_PROMPT_DRIFT'
                    else:
                        assert digest(kw['context'])==request_pins[stage]['input_hash'],'RECORDED_FIXTURE_CONTEXT_DRIFT'
                    matched.append(stage)
                    return kw['output_model'].model_validate(typed[stage])
            engine=RawTurnPlanner(Recorded(),publication,clock=lambda:datetime.fromisoformat(cap['clock']))
            req=ChatRequest(semantic_model_id=81,business_domain_ids=[205],application_id='isolated-evaluation',
                conversation_id=state.payload['conversation_id'],message_id=f"turn-{cap['turn_index']}",question=cap['question'])
            try:
                result=await engine.run(req,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),state=state,
                    plans=tuple(ScopedArtifact.model_validate(p) for p in before['plans']),
                    pending=ScopedArtifact.model_validate(before['pending']) if before['pending'] else None)
            except Exception as exc:failure=dict(type=type(exc).__name__,reason=failure_reason(exc))
        value=result.model_dump(mode='json') if result else None
        equal=outcome_semantics(value)==outcome_semantics(record['result']) and failure==record['error']
        row=dict(case_id=cid,request_match=called==matched,semantic_outcome_equal=equal and called==matched,stage_calls=called,failure=failure,
            source='LIVE_CAPTURE' if len(captured)==len(called) else 'LIVE_FIRST_WITH_RECORDED_DOWNSTREAM',
            stage_timings=timings,model_calls=0,sql_calls=0,production_writes=0)
        write(out/(cid+'.json'),dict(summary=row,result=value));rows.append(row)
        assert not denied and state.model_dump(mode='json')==before['state']
    receipt=dict(cases=len(rows),passed=sum(r['semantic_outcome_equal'] for r in rows),rows=rows,
        runtime_source_hashes=source_hashes,
        version_metadata='Explicit context parser/resolver tags may differ; semantic payload/patch/state must match',
        model_calls=0,sql_calls=0,production_writes=0,blind_access=0)
    assert source_hashes==runtime_hashes(),'RUNTIME_CHANGED_DURING_REPLAY'
    write(out/'replay_receipt.json',receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k!='rows'}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--private-directory',type=Path,required=True)
    p.add_argument('--output-directory',type=Path,required=True);a=p.parse_args()
    asyncio.run(run(a.private_directory,a.output_directory))
