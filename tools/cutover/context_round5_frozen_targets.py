"""Versioned contract migration of the original 15 target cases, fully offline.

Only recorded model signals and handles create migrated proposals. Gold targets
are never model inputs. This verifies runtime attachment preservation, not new
model accuracy; original captures remain immutable. Private text is not printed
or manually inspected and is not used for production semantic tuning.
"""
import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import json
import hashlib
import logging
from pathlib import Path
import sys
from unittest.mock import patch
from time import perf_counter_ns

ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from tools.cutover.run_context_proposal_round4 import read,write
from tools.cutover.context_round3 import model_evidence,active_task
from tools.cutover.run_raw_transition_benchmark import frozen_publication,network_guard,failure_reason
from tools.cutover.frozen_source_values import FrozenSourceValues
from tools.cutover.evaluation_contract import digest
from app.semantic_v2.authorized_contract import ScopedArtifact,contract_digest
from app.semantic_v2.pipeline import CurrentTurnParser,CurrentTurnSemanticParse,TurnResolver
from app.semantic_v2.recognition_repairs import repair_model_parse
from app.semantic_v2.state_machine import ConversationState
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2.models import SemanticResolutionContract
from app.semantic_v2.slot_reducer import TaskPatch
from app.domain.models import ChatRequest,TrustedIdentity
from tools.cutover.context_round5_runtime_replay import outcome_semantics


async def run(out):
    out.mkdir(parents=True,exist_ok=False)
    def runtime_hashes():return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        for p in [ROOT/'app/semantic_v2'/n for n in ('context_contract.py','context_proposal.py','recognition.py','catalog_bridge.py')]}
    source_hashes=runtime_hashes()
    base=ROOT/'.eval_private/harness-20260909T083000Z'
    inventory=read(ROOT/'docs/v2_cutover/semantic_round3/context_case_inventory.json')['cases']
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    raw=(base/'catalog_snapshot.json').read_bytes();observations=read(base/'source_observations.json')
    manifest=read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json')
    rows=[]
    for entry in inventory:
        cid=entry['case_id'];index=entry['turn_index']
        cap=read(base/entry['corpus']/'captures'/f'{cid}-turn-{index}.json')
        assert digest(cap)==entry['source_capture_hash']
        before=cap['before'];state=ScopedArtifact.model_validate(before['state'])
        current=ConversationState.model_validate(state.payload)
        first=next(o['output'] for o in cap['outputs'] if o['stage']=='v2_current_turn')
        second=next(o['output'] for o in cap['outputs'] if o['stage']=='v2_semantic_edits')
        surface,_=repair_model_parse(CurrentTurnSemanticParse.model_validate(first),text=cap['question'],turn_id=f'turn-{index}')
        parsed=CurrentTurnParser.parse(text=cap['question'],turn_id=f'turn-{index}',text_ref=f'turn-{index}',parsed=surface)
        handles={'task:'+contract_digest({'task':t.task_id})[:24]:t.task_id for t in current.tasks.values()}
        historical=handles.get(second.get('historical_task_handle'))
        evidence=model_evidence(parsed,current,historical)
        started=perf_counter_ns()
        baseline=TurnResolver.resolve(parsed,state=current,task_patch=TaskPatch(base_task_version=0),
            semantic_resolution=SemanticResolutionContract(status='UNRESOLVED'),historical_task_id=historical)
        baseline_seconds=(perf_counter_ns()-started)/1e9
        relation=('NEW_TASK' if evidence.relation=='NEW_TASK' else
            'RETURN_TO_TOPIC' if evidence.target and evidence.target!=active_task(current) else
            'CONTINUE' if evidence.target else None)
        target=current.tasks.get(evidence.target)
        proposal=dict(status='ACCEPTED' if relation else 'UNRESOLVED',relation=relation,
            target_task_id=target.task_id if target else None,state_version=current.state_version,
            task_version=target.active_version if target else None,pending_id=None)
        outputs=iter(deepcopy(cap['outputs']));traces=[];error=None;result=None;denied=[]
        class Trace(logging.Handler):
            def emit(self,r):
                if hasattr(r,'context_arbitration'):traces.append(deepcopy(r.context_arbitration))
        logger=logging.getLogger('app.semantic_v2.context_proposal');handler=Trace();level=logger.level
        logger.addHandler(handler);logger.setLevel(logging.INFO)
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
                class Recorded:
                    async def complete(self,**kw):
                        stored=next(outputs);assert stored['stage']==kw['stage']
                        value=deepcopy(stored['output'])
                        if kw['stage']=='v2_current_turn':value['context_proposal']=proposal
                        elif kw['stage']=='v2_semantic_edits' and relation!='RETURN_TO_TOPIC':
                            # Target selection moved to first-stage joint output;
                            # clear only a redundant old active-target handle.
                            assert not value.get('historical_task_handle') or historical==evidence.target
                            value['historical_task_handle']=None
                        return kw['output_model'].model_validate(value)
                req=ChatRequest(semantic_model_id=81,business_domain_ids=[205],application_id='isolated-evaluation',
                    conversation_id=current.conversation_id,message_id=f'turn-{index}',question=cap['question'])
                engine=RawTurnPlanner(Recorded(),publication,clock=lambda:datetime.fromisoformat(cap['clock']))
                result=await engine.run(req,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),state=state,
                    plans=tuple(ScopedArtifact.model_validate(p) for p in before['plans']),
                    pending=ScopedArtifact.model_validate(before['pending']) if before['pending'] else None)
        except Exception as exc:error=dict(type=type(exc).__name__,reason=failure_reason(exc))
        finally:logger.removeHandler(handler);logger.setLevel(level)
        assert state.model_dump(mode='json')==before['state'] and not denied
        trace=traces[-1] if traces else None
        actual=trace['FINAL_TARGET'] if trace and trace['FINAL_STATUS']=='ACCEPTED' else None
        value=result.model_dump(mode='json') if result else None
        semantic_equal=outcome_semantics(value)==outcome_semantics(cap['result']) if value and cap.get('result') else None
        row=dict(case_id=cid,source_capture_hash=digest(cap),baseline_target=baseline.target_task_id,
            migrated_target=actual,target_preserved=actual==baseline.target_task_id,
            baseline_relation=str(baseline.dialogue_act),migrated_relation=relation,
            relation_label_difference=relation!=str(baseline.dialogue_act),
            label_only_difference=bool(semantic_equal and relation!=str(baseline.dialogue_act) and actual==baseline.target_task_id),
            accepted_semantic_payload_patch_state_equal=semantic_equal,
            legacy_resolver_seconds=baseline_seconds,
            status='PLAN' if result else 'REJECTED_DOWNSTREAM',failure=error,
            trace=trace,original_failure=cap.get('outcome',{}).get('reason'),
            model_calls=0,sql_calls=0,production_writes=0)
        rows.append(row)
        write(out/(cid+'.json'),dict(summary=row,result=result.model_dump(mode='json') if result else None))
    receipt=dict(contract='ROUND5_VERSIONED_RECORDED_SIGNAL_MIGRATION_V1',rows=rows,
        runtime_source_hashes=source_hashes,
        cases=len(rows),target_preserved=sum(r['target_preserved'] for r in rows),
        target_regressions=[r['case_id'] for r in rows if not r['target_preserved']],
        new_model_accuracy=False,strict_original_request_replay=False,blind_access=0,
        private_capture_machine_replay=sum(e['corpus']=='private_validation' for e in inventory),
        private_text_debug_or_tuning=0)
    assert source_hashes==runtime_hashes(),'RUNTIME_CHANGED_DURING_REPLAY'
    write(out/'target_receipt.json',receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k!='rows'}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-directory',type=Path,required=True)
    asyncio.run(run(p.parse_args().output_directory))
