"""Bounded Round 4 diagnostics; no benchmark, production state or SQL execution.

Live mode replaces the first recognition call experimentally. It evaluates
relation/target only; it does NOT pretend a fixed downstream draft is a new
whole-runtime live result. Controlled mode exercises the actual reducer path.
"""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
from time import monotonic
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from tools.cutover.evaluation_contract import digest
from tools.cutover.context_proposal_round4 import (discover_context, validate_proposal, propose,
    proposal_resolution, restored_state, active_id)
from tools.cutover.context_proposal_schema import ContextProposal
from tools.cutover.context_round3 import rule_evidence, resolution_for
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.pipeline import CurrentTurnParser, CurrentTurnSemanticParse, TurnResolver
from app.semantic_v2.slot_reducer import TaskPatch
from app.semantic_v2.models import SemanticResolutionContract
from app.semantic_v2.authorized_contract import ScopedArtifact
from tools.cutover.harness_replay import deny_external_calls
from tools.cutover.run_raw_transition_benchmark import network_guard


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def family(act):
    if act=='NEW_TASK':return 'NEW_TASK'
    if act=='RETURN_TO_TOPIC':return 'RETURN_TO_TOPIC'
    if act=='ANSWER_CLARIFICATION':return 'ANSWER_PENDING'
    return 'FOLLOW_UP' if act else 'UNRESOLVED'


async def controlled(out):
    from test_v2_context_followup_critical_slice import context_catalog,context_case
    from test_v2_raw_turn_recognition import planner,request,IDENTITY,metric_step,turns
    rows=[]
    for case in range(1,9):
        catalog=context_catalog.__wrapped__();steps=context_case(case)
        engine,_=planner(catalog,steps);state=None;plans={};pending=None
        for i,(text,_,_) in enumerate(steps):
            kwargs=dict(state=state,plans=tuple(plans.values()),pending=pending)
            before=digest(state.model_dump(mode='json')) if state else None
            baseline=await engine.run(request(question=text,message_id='turn'+str(i)),IDENTITY,**kwargs)
            session=ScopedPlanSession(request(question=text,message_id='turn'+str(i)),IDENTITY,catalog[0])
            context=discover_context(session,**kwargs)
            # Independent known scenario relation labels, not inferred from result.
            act=('NEW_TASK' if i==0 or (case==7 and i==1) or case==8 else
                 'RETURN_TO_TOPIC' if case==7 else 'MODIFY')
            target=None if act=='NEW_TASK' else 'task:turn0'
            t=next((x for x in context.model_context['candidate_tasks'] if x['task_id']==target),None)
            p=ContextProposal(status='ACCEPTED',relation=act,target_task_id=target,
                task_version=t['task_version'] if t else None,state_version=context.model_context['state_version'],pending_id=None)
            trace=validate_proposal(p,context,session=session,state=state,question=text)
            assert trace['FINAL_STATUS']=='ACCEPTED'
            def resolver(parse,*,state,task_patch,semantic_resolution,**kw):
                return proposal_resolution(trace,parse,state,task_patch,semantic_resolution)
            candidate,_=planner(catalog,[(text,steps[i][1],steps[i][2])])
            with patch.object(TurnResolver,'resolve',staticmethod(resolver)),network_guard():
                result=await candidate.run(request(question=text,message_id='turn'+str(i)),IDENTITY,**kwargs)
            # Strict native task state identity, including clear barriers, is
            # compared; relation-label differences aren't erased in the trace.
            assert result.next_state.model_dump(mode='json')==baseline.next_state.model_dump(mode='json')
            if state:assert digest(state.model_dump(mode='json'))==before
            rows.append(dict(case_id=f'CFCS-{case:02d}',turn_index=i,arm='B_MODEL_PROPOSAL_PLUS_HARD_CONSTRAINTS',
                proposal_source='CONTROLLED_BUSINESS_CONTRACT; NOT_LIVE_MODEL',
                native_next_state_identical=True,arbitration=trace,external_socket_attempts=0))
            state=baseline.next_state
            if baseline.plan:plans[baseline.plan['logical_plan']['task_id']]=baseline.plan_state;pending=None
            else:pending=baseline.pending_state
    # Round 3 act-only counterexample: valid current operation without redundant
    # reference flag. Explicit proposal changes no slot or marker evidence.
    regressions=[]
    for act in ('CONTINUE','MODIFY','CORRECT','ADD','REPLACE'):
        catalog=context_catalog.__wrapped__();first=metric_step('销售额')
        step=metric_step('继续增加订单笔数','订单笔数','ADD',True)
        step[1].update(reference_signals=[],followup_signals=[],dialogue_act_candidates=[act])
        engine,_=planner(catalog,[first]);base=(await turns(engine,[first]))[0]
        session=ScopedPlanSession(request(message_id='turn1'),IDENTITY,catalog[0])
        context=discover_context(session,state=base.next_state,plans=(base.plan_state,))
        p=ContextProposal(status='ACCEPTED',relation='ADD',target_task_id='task:turn0',
            task_version=1,state_version=context.model_context['state_version'],pending_id=None)
        trace=validate_proposal(p,context,session=session,state=base.next_state,question=step[0])
        candidate,_=planner(catalog,[step])
        def resolver(parse,*,state,task_patch,semantic_resolution,**kw):
            return proposal_resolution(trace,parse,state,task_patch,semantic_resolution)
        with patch.object(TurnResolver,'resolve',staticmethod(resolver)),network_guard():
            result=await candidate.run(request(question=step[0],message_id='turn1'),IDENTITY,
                state=base.next_state,plans=(base.plan_state,))
        assert {v['canonical_code'] for v in result.plan['logical_plan']['payload']['measures']}=={'amount','orders'}
        regressions.append(dict(act=act,target='task:turn0',status='ACCEPTED_CONTROLLED',current_explicit_delta_preserved=True))
    write(out/'controlled_result.json',dict(cases=8,turns=18,rows=rows,act_only_controls=regressions,
        production_changes=0,native_state_equal=18,controlled_improvement=5,real_model_calls=0))
    print(json.dumps({'controlled_cases':8,'state_equal_turns':18,'act_only_improved':5}),flush=True)


def live_cases():
    from tools.cutover.harness_cli import transition_cases
    base=ROOT/'.eval_private/harness-20260909T083000Z'
    definitions={c['case_id']:c for c in transition_cases()}
    ids=[1,2,3,11,12,13,14,16,17];cases=[]
    pins=read(ROOT/'docs/v2_cutover/evaluation_harness/final/transition/capture_manifest.json')
    for i in ids:
        cid=f'S81-{i:03d}';case=definitions[cid];index=len(case['history'])
        capture=read(base/'transition/captures'/f'{cid}-turn-{index}.json')
        expected=next(p['hash'] for p in pins if p['case_id']==cid and p['turn_index']==index)
        assert digest(capture)==expected
        cases.append(dict(case_id=cid,source_capture_hash=expected,capture=capture,definition=case,
            expected_relation=case['labels'].get('turn_relation','FOLLOW_UP'),
            expected_target=case['labels']['target_task'],source='PUBLIC_DEV_TRANSITION'))
    # New public contrasts use real existing preconditions; no fake task history.
    for number,source,text,relation,target in [
        (10,17,'新任务：查询订单笔数','NEW_TASK','NEW'),
        (11,17,'前面两个任务选一个继续，具体哪个我还没确定','UNRESOLVED',None),
        (12,16,'第一个','ANSWER_PENDING','PENDING')]:
        template=next(c for c in cases if c['case_id']==f'S81-{source:03d}')
        c=deepcopy(template);c.update(case_id=f'R4L-{number:03d}',source='NEW_PUBLIC_CONTRAST',
            expected_relation=relation,expected_target=target)
        c['capture']['question']=text;c['new_input']=True
        cases.append(c)
    return cases


async def live(out):
    from app.config import Settings
    from app.domain.models import ChatRequest,TrustedIdentity
    from app.semantic_v2.recognition import PARSE_PROMPT,current_turn_schema,EDIT_SLOTS
    from tools.cutover.run_raw_transition_benchmark import ModelRecorder,network_guard,frozen_publication
    from tools.cutover.harness_manifest import verify_frozen
    manifest=read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json')
    verify_frozen(manifest)
    settings=Settings().model_copy(update={'intent_model_max_retries':0,'intent_model_timeout_seconds':60})
    assert settings.intent_model_name=='qwen3.7-max' and settings.intent_model_enable_thinking is False
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    raw=(ROOT/'.eval_private/harness-20260909T083000Z/catalog_snapshot.json').read_bytes()
    cases=live_cases();input_pins=[dict(case_id=c['case_id'],source_capture_hash=c['source_capture_hash'],
        input_hash=digest(c['capture']['question']),before_hash=digest(c['capture']['before']),
        labels={'relation':c['expected_relation'],'target':c['expected_target']}) for c in cases]
    from tools.cutover.context_proposal_schema import CONTRACT_VERSION
    write(out/'input_freeze.json',dict(cases=input_pins,hash=digest(input_pins),as_of=manifest['as_of'],
        scope=catalog['scope'],catalog_hash=catalog['artifact_hash'],production_prompt_version='v2-current-recognition-v8',
        experiment_prompt_version='context-proposal-experiment-v1',experiment_schema_version=CONTRACT_VERSION,
        label_source='EXISTING_PUBLIC_GOLD_OR_EXPLICIT_BUSINESS_CONTRACT'))
    rows=[];recorder=ModelRecorder(settings,max_calls=15,capture_exchanges=True)
    for c in cases:
        capture=c['capture'];before=capture['before'];state=ScopedArtifact.model_validate(before['state'])
        activation=state.context.catalog_pin.activation_id;denied=[]
        with network_guard():
            publication=frozen_publication(raw,catalog,denied,recorded_activation_id=activation)
            request=ChatRequest(semantic_model_id=81,business_domain_ids=[205],application_id='isolated-evaluation',
                conversation_id=state.payload['conversation_id'],message_id=f"turn-{capture['turn_index']}",question=capture['question'])
            session=ScopedPlanSession(request,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),publication)
            current=restored_state(session,state)
            context=discover_context(session,state=state,plans=tuple(ScopedArtifact.model_validate(p) for p in before['plans']),
                pending=ScopedArtifact.model_validate(before['pending']) if before['pending'] else None)
            # Inputs persisted before the model returns; labels aren't in context.
            write(out/(c['case_id']+'-context.json'),context.model_context)
            recorder.begin_turn(c['case_id'],capture['turn_index']);start=monotonic();error=None;surface=None;proposal=None;trace=None
            try:
                surface,proposal=await propose(recorder,question=request.question,turn_id=request.message_id,
                    clock=capture['clock'],discovered=context)
                trace=validate_proposal(proposal,context,session=session,state=state,question=request.question)
            except Exception as exc:error=type(exc).__name__+':'+str(exc) if type(exc).__name__=='RecognitionFailure' else type(exc).__name__
            elapsed=monotonic()-start
            model_exchanges=deepcopy(recorder.exchanges);model_outputs=deepcopy(recorder.outputs)
            # A: frozen actual hybrid for existing inputs; new contrast inputs
            # get the unchanged first model call and native resolver/Pending gate.
            if not c.get('new_input'):
                old=read(ROOT/'.eval_private/context-round3-20260909T141050Z'/f"{c['case_id']}-turn-{capture['turn_index']}-C_CURRENT_HYBRID.json")['summary']
                decision=old['resolution'];a=dict(relation=family(decision['dialogue_act']),target=decision['target_task_id'],
                    source='FROZEN_ACTUAL_RUNTIME')
            else:
                recorder.begin_turn(c['case_id']+'-A',capture['turn_index'])
                try:
                    parsed=await recorder.complete(stage='v2_current_turn',instruction=PARSE_PROMPT,
                        context=dict(question=request.question,turn_id=request.message_id,clock=capture['clock'],slots=list(EDIT_SLOTS)),
                        output_model=CurrentTurnSemanticParse,schema=current_turn_schema())
                    from app.semantic_v2.recognition_repairs import repair_model_parse
                    parsed,_=repair_model_parse(parsed,text=request.question,turn_id=request.message_id)
                    parsed=CurrentTurnParser.parse(text=request.question,turn_id=request.message_id,text_ref=request.message_id,parsed=parsed)
                    from app.semantic_v2.pending_recognition import selected_option
                    if current.pending and not parsed.topic_shift_signals and selected_option(current.pending,request.question):
                        a=dict(relation='ANSWER_PENDING',target=current.pending.task_id,source='LIVE_PARSE_NATIVE_PENDING_GATE')
                    else:
                        d=TurnResolver.resolve(parsed,state=current,task_patch=TaskPatch(base_task_version=0),
                            semantic_resolution=SemanticResolutionContract(status='UNRESOLVED'))
                        a=dict(relation=family(d.dialogue_act) if d.target_task_id else 'UNRESOLVED',
                            target=d.target_task_id,source='LIVE_PARSE_NATIVE_RESOLVER; NO_DRAFT_TARGET')
                except Exception as exc:a=dict(relation=None,target=None,error=type(exc).__name__)
                write(out/(c['case_id']+'-A-capture.json'),dict(exchanges=recorder.exchanges,outputs=recorder.outputs))
            rule=rule_evidence(request.question,current,c['definition']['history'][-1],as_of=capture['clock'])
            c_arm=dict(relation='ANSWER_PENDING' if rule.relation=='ANSWER_PENDING' else rule.relation,
                target='task:'+request.message_id if rule.relation=='NEW_TASK' else rule.target,source='EXISTING_V1_RULE_RELATION_ONLY')
            b=dict(relation=family(trace['FINAL_RELATION']) if trace and trace['FINAL_STATUS']=='ACCEPTED' else 'UNRESOLVED',
                target=('task:'+request.message_id if trace['FINAL_RELATION']=='NEW_TASK' else trace['FINAL_TARGET']) if trace else None,
                source='LIVE_CONTEXT_PROPOSAL_VALIDATED',error=error)
            expected_target=({'PREVIOUS':active_id(current),'NEW':'task:'+request.message_id,
                'PENDING':current.pending.task_id if current.pending else None,
                'HISTORY:0':'task:turn-0'}.get(c['expected_target']))
            row=dict(case_id=c['case_id'],source=c['source'],expected_relation=c['expected_relation'],expected_target=expected_target,
                arms={'A_CURRENT_HYBRID':a,'B_MODEL_PROPOSAL_PLUS_HARD_CONSTRAINTS':b,'C_RULE_ONLY_DIAGNOSTIC':c_arm},
                candidate_count=context.model_context['candidate_count'],context_hash=context.context_digest,
                context_characters=len(json.dumps(context.model_context,ensure_ascii=False)),proposal_seconds=elapsed,
                trace=trace,model_error=error,source_reads_attempted=len(denied),state_execution='NOT_RUN_RELATION_DIAGNOSTIC',
                semantic_delta_evaluation='UNSCORED_NEW_PARSE; CANNOT_PROVE_NONREGRESSION')
            write(out/(c['case_id']+'-B-capture.json'),dict(exchanges=model_exchanges,outputs=model_outputs,
                parse=surface.model_dump(mode='json') if surface else None,proposal=proposal.model_dump(mode='json') if proposal else None))
            row['capture_hash']=digest(read(out/(c['case_id']+'-B-capture.json')));rows.append(row)
            write(out/'live_result.json',dict(rows=rows,calls=recorder.calls,complete=len(rows)==len(cases)))
            print(json.dumps({'case_id':c['case_id'],'model_error':error,'B_relation':b['relation'],
                'target_match':b['target']==expected_target,'seconds':round(elapsed,2)}),flush=True)
            assert not denied
    await recorder.upstream.aclose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['controlled','live'],required=True)
    parser.add_argument('--output-directory',type=Path,required=True)
    parser.add_argument('--allow-model-calls',action='store_true')
    args=parser.parse_args()
    if args.mode=='live' and not args.allow_model_calls:parser.error('live requires --allow-model-calls')
    args.output_directory.mkdir(parents=True,exist_ok=False)
    asyncio.run((live if args.mode=='live' else controlled)(args.output_directory))


if __name__=='__main__':main()
