"""Round 5.2 bounded recorded-output causality diagnostics, with no network.

Each Oracle changes one SemanticTaskDraft or the initial TimeSpec lowering
artifact. They are never model/Gold passes. Input differences remain explicit.
"""
import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.cutover.live_followup_round5_1 import CLOCK,CONVERSATION,SCOPE,hashes
from tools.cutover.run_raw_transition_benchmark import network_guard,frozen_publication,failure_reason
from tools.cutover.frozen_source_values import FrozenSourceValues
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_observation import RuntimeObserver
from app.domain.models import ChatRequest,TrustedIdentity
from app.semantic_v2.authorized_contract import ScopedArtifact
from app.semantic_v2.recognition import RawTurnPlanner


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def intervene(cap,mode):
    """Change only a declared first-divergence artifact; preserve all other stages."""
    draft=deepcopy(cap['outputs'][1]['output'])
    context=json.loads(cap['exchanges'][1]['request_body']['messages'][1]['content'])
    if mode=='INITIAL_FILTER_ASSIGNMENT':
        edit=draft['filter_edits'].pop(0)
        draft['edits'].append(dict(slot_path='filter_expression',operation='SET',
            evidence_mention_ids=edit['evidence_mention_ids'],value=edit['value']))
    elif mode=='INITIAL_METRIC_ASSIGNMENT':
        items=[e for e in draft['edits'] if e['slot_path']=='metrics']
        draft['edits']=[e for e in draft['edits'] if e['slot_path']!='metrics']
        draft['edits'].insert(0,dict(slot_path='metrics',operation='SET',
            evidence_mention_ids=[i for e in items for i in e['evidence_mention_ids']],
            value=[e['value'] for e in items]))
    elif mode=='INITIAL_TIME_ASSIGNMENT':
        # This oracle intervenes on the lowered TimeSpec, because the original
        # model received no TIME_FIELD handle at all. No invented model handle.
        pass
    elif mode=='CONSUME_SOURCE_REQUEST':
        request=draft['source_value_requests'][0]
        draft['edits'].append(dict(slot_path='filter_expression',operation='SET',evidence_mention_ids=[request['mention_id']],
            value=dict(node_type='PREDICATE',field_ref={'value_field_request_id':request['request_id']},
                operator='EQ',value={'value_request_id':request['request_id']},source='USER_EXPLICIT',scope='CURRENT_TASK')))
    elif mode=='ATTRIBUTE_ONLY':
        allowed={c['binding_handle'] for c in context['catalog_candidates'] if c['catalog_type']=='ATTRIBUTE'}
        for request in draft['source_value_requests']:
            request['field_binding_handles']=[h for h in request['field_binding_handles'] if h in allowed]
    elif mode=='SUBTREE_DELETE_OPERATION':
        markers=cap['outputs'][0]['output']['operation_markers']
        operation={m['operation_hint'] for m in markers if m['slot_name']=='filter_expression'}
        assert len(operation)==1 and operation<={'REMOVE','CLEAR'}
        assert len(draft['filter_edits'])==1 and draft['filter_edits'][0]['value'] is None
        draft['filter_edits'][0]['operation']=next(iter(operation))
    elif mode=='ALTERNATE_OFFERED_FIELD_DIAGNOSTIC':
        request=draft['source_value_requests'][0]
        fields=[c for c in context['catalog_candidates'] if c['mention_id']==request['mention_id']
            and c['catalog_type']=='ATTRIBUTE' and c['code']=='province_name']
        assert len(fields)==1
        request['field_binding_handles']=[fields[0]['binding_handle']]
    else:raise ValueError(mode)
    return draft


async def replay(cap,mode=None,activation=None):
    base=ROOT/'.eval_private/harness-20260909T083000Z'
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    raw=(base/'catalog_snapshot.json').read_bytes()
    manifest=read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json')
    before=cap['before'];state=ScopedArtifact.model_validate(before['state']) if before['state'] else None
    outputs={o['stage']:o['output'] for o in cap['outputs']}
    if mode:outputs['v2_semantic_edits']=intervene(cap,mode)
    exchanges={e['stage']:e for e in cap['exchanges']}
    calls=[];denied=[];result=None;error=None
    with network_guard(),ExitStack() as stack:
        other=ROOT.parent/'Oagnet';stack.enter_context(patch.object(sys,'path',[*sys.path,str(other),str(other/'tests')]))
        publication=frozen_publication(raw,catalog,denied,native_source_values=True,
            recorded_activation_id=state.context.catalog_pin.activation_id if state else activation)
        source=FrozenSourceValues(read(base/'source_observations.json'),catalog=catalog,snapshot=json.loads(raw),
            expected_hash=manifest['source_value_evidence']['artifact_hash'],allow_synthetic=False)
        import catalog_value_sources
        stack.enter_context(patch.object(catalog_value_sources,'observe',source.observe))
        stack.enter_context(patch.object(catalog_value_sources,'observe_probe',source.observe_probe))
        observer=stack.enter_context(RuntimeObserver());observer.begin_turn(cap['case_id'],cap['turn_index'])
        if mode=='INITIAL_TIME_ASSIGNMENT':
            from app.semantic_v2 import recognition,structured_edits,models as m
            from app.semantic_v2.explicit_time import metric_anchor,normalize_range
            original_patch=RawTurnPlanner._patch
            original_temporal=structured_edits.temporal_edits
            def patch_time(session,parse,draft,handles,*args,**kw):
                def initial_time(prior,edits,hydrate,now,**options):
                    if prior.time_spec is not None:return original_temporal(prior,edits,hydrate,now,**options)
                    metrics=[]
                    for edit in draft.edits:
                        if edit.slot_path=='metrics':
                            hydrated=RawTurnPlanner._hydrate(edit.value,handles,session)
                            metrics.extend(m.BoundSemanticRef.model_validate(v) for v in (hydrated if isinstance(hydrated,list) else [hydrated]))
                    assert len(edits)==1 and edits[0].component=='RANGE' and edits[0].operation=='SET'
                    mention=next(x for x in parse.mentions if x.mention_id in edits[0].evidence_mention_ids)
                    refs=tuple(session._request.message_id+':'+i for i in edits[0].evidence_mention_ids)
                    return m.TimeSpec(anchor=metric_anchor(session,metrics,refs),range=normalize_range(mention.surface,now),
                        grain='NONE',timezone='Asia/Shanghai',source='USER_EXPLICIT',as_of=now)
                with patch.object(structured_edits,'temporal_edits',initial_time):
                    return original_patch(session,parse,draft,handles,*args,**kw)
            stack.enter_context(patch.object(RawTurnPlanner,'_patch',staticmethod(patch_time)))
        class Recorded:
            async def complete(self,**kw):
                stage=kw['stage']
                if stage not in outputs:raise RuntimeError('ORACLE_NEXT_MODEL_OUTPUT_NOT_RECORDED')
                messages=exchanges[stage]['request_body']['messages']
                context_equal=json.loads(messages[1]['content'])==kw['context']
                schema_equal=messages[0]['content']==kw['instruction']+'\nJSON Schema:\n'+json.dumps(kw['schema'],ensure_ascii=False)
                calls.append(dict(stage=stage,context_match=context_equal,schema_prompt_match=schema_equal,
                    output_intervention=bool(mode and mode!='INITIAL_TIME_ASSIGNMENT' and stage=='v2_semantic_edits')))
                if mode is None and (not context_equal or not schema_equal):
                    # A changed generation contract cannot claim strict replay.
                    # The original output is still consumed as a frozen runtime
                    # regression; the differing input contract is recorded.
                    pass
                if mode and stage=='v2_source_value_choice' and not context_equal:
                    raise RuntimeError('ORACLE_NEXT_MODEL_OUTPUT_NOT_RECORDED')
                return kw['output_model'].model_validate(deepcopy(outputs[stage]))
        engine=RawTurnPlanner(Recorded(),publication,clock=lambda:datetime.fromisoformat(CLOCK))
        request=ChatRequest(**SCOPE,application_id='isolated-evaluation',conversation_id=CONVERSATION,
            message_id='turn-'+cap['turn_id'],question=cap['question'])
        try:
            response=await engine.run(request,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),state=state,
                plans=tuple(ScopedArtifact.model_validate(p) for p in before['plans']),
                pending=ScopedArtifact.model_validate(before['pending']) if before['pending'] else None)
            result=response.model_dump(mode='json')
        except Exception as exc:
            error=dict(type=type(exc).__name__,reason=failure_reason(exc))
            if str(exc)=='ORACLE_NEXT_MODEL_OUTPUT_NOT_RECORDED':error['reason']=str(exc)
        assert not denied and (state.model_dump(mode='json') if state else None)==before['state']
    return dict(case_id=cap['case_id'],mode=mode or 'RECORDED_OUTPUT',capture_hash=digest(cap),calls=calls,
        intervention_artifact=('INITIAL_TIMESPEC_LOWERING' if mode=='INITIAL_TIME_ASSIGNMENT' else 'SEMANTIC_TASK_DRAFT' if mode else None),
        strict_request_match=all(c['context_match'] and c['schema_prompt_match'] for c in calls),
        original_error=cap['error'],error=error,result=result,events=observer.active['events'],source_calls=source.calls,
        model_calls=0,sql_calls=0,production_writes=0,blind_access=0,oracle_is_model_or_gold_pass=False)


async def run(capture_dir,out,oracles):
    out.mkdir(parents=True,exist_ok=False);source_hashes=hashes();rows=[]
    captures=[read(capture_dir/(k+'.json')) for k in read(capture_dir/'result.json')['turns']]
    activations={c['result']['next_state']['context']['catalog_pin']['activation_id'] for c in captures if c['result']}
    assert len(activations)==1
    activation=next(iter(activations))
    modes={'LF51-A':'ATTRIBUTE_ONLY','LF51-B':'INITIAL_FILTER_ASSIGNMENT','LF51-C':'INITIAL_METRIC_ASSIGNMENT',
        'LF51-D':'INITIAL_TIME_ASSIGNMENT','LF51-F':'ALTERNATE_OFFERED_FIELD_DIAGNOSTIC','LF51-J':'CONSUME_SOURCE_REQUEST'}
    for key in read(capture_dir/'result.json')['turns']:
        cap=read(capture_dir/(key+'.json'))
        if not cap['error']:continue
        for mode in ([None,modes[cap['case_id']]] if oracles else [None]):
            row=await replay(cap,mode,activation)
            write(out/(cap['case_id']+'-'+(mode or 'recorded')+'.json'),row)
            summary={k:row[k] for k in ('case_id','mode','strict_request_match','error','calls')}
            summary['plan']=bool(row['result'] and row['result'].get('plan'))
            rows.append(summary);print(json.dumps(summary),flush=True)
    assert hashes()==source_hashes
    write(out/'receipt.json',dict(rows=rows,runtime_hashes=source_hashes,model_calls=0,sql_calls=0,production_writes=0))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-directory',type=Path,required=True)
    parser.add_argument('--output-directory',type=Path,required=True)
    parser.add_argument('--oracles',action='store_true')
    args=parser.parse_args();asyncio.run(run(args.capture_directory,args.output_directory,args.oracles))
