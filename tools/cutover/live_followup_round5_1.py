"""Opt-in Round 5.1 observation of actual V2; no production decision overrides.

Reuse identical executed prefixes, never inject a success after a failed turn.
All model stages are live. Catalog/source observations are pinned read-only
snapshots; no SQL, Redis, production state, Blind or public routing is touched.
"""
import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import sys
from time import monotonic
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.domain.models import ChatRequest,TrustedIdentity
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2.authorized_contract import ScopedArtifact
from tools.cutover.evaluation_contract import digest
from tools.cutover.run_raw_transition_benchmark import ModelRecorder,network_guard,frozen_publication,failure_reason
from tools.cutover.frozen_source_values import FrozenSourceValues
from tools.cutover.harness_observation import RuntimeObserver

CLOCK='2026-09-09T09:00:00+08:00'
SCOPE={'semantic_model_id':81,'business_domain_ids':[205]}
CONVERSATION='round5-1-live-shared-prefix'


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def hashes():return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
    for p in sorted((ROOT/'app/semantic_v2').glob('*.py'))}


def cases():
    sh='查询去年上海销售额';js='查询去年江苏销售额';both='查询去年江苏销售额和订单笔数'
    orders='查询去年江苏订单笔数';plain='查询上海销售额';hospital='查询医院销售额'
    rows=[
        ('A','FOLLOW_UP',[sh,'那江苏呢'],'PREVIOUS',{'metrics':['sales_total_including_tax'],'region':['江苏'],'year':2025}),
        ('B','ADD',[js,'再加订单笔数'],'PREVIOUS',{'metrics':['sales_total_including_tax','order_count'],'region':['江苏'],'year':2025}),
        ('C','REMOVE',[both,'销售额不要了'],'PREVIOUS',{'metrics':['order_count'],'region':['江苏'],'year':2025}),
        ('D','CLEAR',[orders,'不限地区','换今年'],'PREVIOUS',{'metrics':['order_count'],'region':[],'year':2026,'clear_barrier':True}),
        ('E','REPLACE_TIME',[sh,'换今年'],'PREVIOUS',{'metrics':['sales_total_including_tax'],'region':['上海'],'year':2026}),
        ('F','CORRECTION',[plain,'不是上海，是北京'],'PREVIOUS',{'metrics':['sales_total_including_tax'],'region':['北京']}),
        ('G','NEW_TASK',[plain,'江苏有哪些医院？'],'NEW',{'metrics':[],'region':['江苏'],'subject':'hospital'}),
        ('H','HISTORICAL_RETURN',[plain,hospital,'还是看刚才上海那个'],'HISTORY:0',{'metrics':['sales_total_including_tax'],'region':['上海']}),
        ('I','PENDING_RESPONSE',['销售额'],'PENDING',{'metrics':['sales_total_including_tax']}),
        ('J','PENDING_NEW_TASK',['江苏有哪些医院？'],'NEW',{'metrics':[],'region':['江苏'],'subject':'hospital'}),
        ('K','AMBIGUOUS',[plain,'查询江苏销售额','查询今年北京订单笔数','前面两个销售额任务选一个继续，具体哪个我还没确定'],None,{}),
        ('L','SELF_CONTAINED',[plain,'查询今年北京订单笔数'],'NEW',{'metrics':['order_count'],'region':['北京'],'year':2026}),
        ('A2','FOLLOW_UP',[sh,'江苏的呢'],'PREVIOUS',{'metrics':['sales_total_including_tax'],'region':['江苏'],'year':2025}),
        ('A3','FOLLOW_UP',[sh,'换江苏看看'],'PREVIOUS',{'metrics':['sales_total_including_tax'],'region':['江苏'],'year':2025}),
        ('B2','ADD',[js,'订单数也一起看'],'PREVIOUS',{'metrics':['sales_total_including_tax','order_count'],'region':['江苏'],'year':2025}),
        ('C2','REMOVE',[both,'销售额先不要'],'PREVIOUS',{'metrics':['order_count'],'region':['江苏'],'year':2025}),
        ('D2','CLEAR',[orders,'地区不限制','改成今年'],'PREVIOUS',{'metrics':['order_count'],'region':[],'year':2026,'clear_barrier':True}),
        ('F2','CORRECTION',[plain,'不是这个，我说北京'],'PREVIOUS',{'metrics':['sales_total_including_tax'],'region':['北京']}),
        ('G2','NEW_TASK',[plain,'我换个问题，江苏有哪些医院'],'NEW',{'metrics':[],'region':['江苏'],'subject':'hospital'}),
        ('H2','HISTORICAL_RETURN',[plain,hospital,'还是第一个'],'HISTORY:0',{'metrics':['sales_total_including_tax'],'region':['上海']}),
    ]
    return [dict(case_id='LF51-'+cid,semantic_type=kind,utterances=utterances,expected_target=target,
        expected=expected,initial_pending=cid in {'I','J'},scope=SCOPE,clock=CLOCK,
        truth_source='USER_ROUND5_1_DIRECTIVE + FROZEN_CATALOG_ALIASES + DETERMINISTIC_CALENDAR',
        split='PUBLIC_DEV',whole_plan_gold=False) for cid,kind,utterances,target,expected in rows]


def declared_pending(publication):
    """User-specified Pending choices, native builder/seal; not executed history."""
    from tools.cutover.harness_fixtures import pending_fixture,request_for
    from app.semantic_v2.catalog_bridge import ScopedPlanSession
    from app.semantic_v2.state_machine import ConversationState,PendingClarification
    from app.semantic_v2.pending_recognition import PendingResume,pending_identity
    case=dict(case_id=CONVERSATION,current_utterance='销售额',clock=CLOCK,scope=SCOPE)
    (state,pending,plans,history),receipt=pending_fixture(case,publication,
        candidate_codes=['sales_total_including_tax','sales_total_quantity'])
    request=request_for(case);session=ScopedPlanSession(request,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),publication)
    current=ConversationState.model_validate(session.restore(state,kind='CONVERSATION'))
    resume=PendingResume.model_validate(session.restore(pending,kind='PENDING'))
    old=current.pending;data=old.model_dump(mode='json')
    for option,label in zip(data['blockers'][0]['options'],['销售额','销售数量']):option['display_label']=label
    proposed=PendingClarification.model_validate(data)
    identifier=pending_identity(proposed.task_id,resume.payload_type,resume.operations,proposed.blockers)
    proposed=proposed.model_copy(update={'pending_id':identifier,'question':'你想查看销售额还是销售数量？'})
    raw=current.model_dump(mode='json');raw['pending_records']={identifier:proposed.model_dump(mode='json')}
    current=ConversationState.model_validate(raw);resume=resume.model_copy(update={'pending_id':identifier})
    session.accept_catalog()
    state=session.seal(kind='CONVERSATION',payload=current);pending=session.seal(kind='PENDING',payload=resume)
    receipt.update(origin='USER_DECLARED_PENDING_CHOICES; NOT_MODEL_GENERATED_HISTORY',
        exact_option_labels=['销售额','销售数量'],canonical_codes=['sales_total_including_tax','sales_total_quantity'],
        state_hash=digest(state.model_dump(mode='json')),pending_hash=digest(pending.model_dump(mode='json')))
    return dict(state=state.model_dump(mode='json'),pending=pending.model_dump(mode='json'),plans=[],history=[]),receipt


async def run(out,max_calls,continuation_from=None):
    out.mkdir(parents=True,exist_ok=False)
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    base=ROOT/'.eval_private/harness-20260909T083000Z'
    raw=(base/'catalog_snapshot.json').read_bytes();observations=read(base/'source_observations.json')
    source_hash=read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json')['source_value_evidence']['artifact_hash']
    settings=Settings().model_copy(update={'intent_model_max_retries':0,'intent_model_timeout_seconds':60})
    assert settings.intent_model_name=='qwen3.7-max' and settings.intent_model_enable_thinking is False
    source_hashes=hashes();definitions=cases();continuation=None;prior_calls=0
    if continuation_from is not None:
        existing=read(continuation_from/'result.json');prior_calls=len(existing['calls'])
        seed_case=next(c for c in existing['cases'] if c['case_id']=='LF51-I')
        seed=read(continuation_from/(seed_case['turns'][-1]+'.json'))
        assert seed['result']['plan'] is not None and existing['runtime_hashes']==source_hashes
        native=seed['result'];continuation=dict(state=native['next_state'],pending=None,
            plans=[native['plan_state']],history=[native])
        definitions=[dict(case_id='LF51-LIVE-CONTROL',semantic_type='ADD_THEN_REMOVE',
            utterances=['订单数也一起看','销售额先不要'],expected_target='PREVIOUS',
            expected={'metrics':['order_count']},initial_pending=False,scope=SCOPE,clock=CLOCK,
            truth_source='USER_ADD_REMOVE_CONTRACT; ACTUAL_CURRENT_RUN_PENDING_ANSWER_PLAN',
            split='PUBLIC_DEV',whole_plan_gold=False,seed_capture_hash=digest(seed),
            seed_turn=seed_case['turns'][-1],primary_cases_rescored=False)]
        activation=native['next_state']['context']['catalog_pin']['activation_id']
    else:activation=None
    write(out/'input_freeze.json',dict(cases=definitions,case_hash=digest(definitions),runtime_hashes=source_hashes,
        catalog_hash=catalog['artifact_hash'],source_snapshot_hash=catalog['source_snapshot_sha256'],source_observations_hash=source_hash,
        model='qwen3.7-max',thinking=False,temperature=0,retry=0,timeout_seconds=60,max_calls=max_calls,prior_calls=prior_calls,
        historical_before_latency='UNKNOWN',prefix_reuse='EXACT_INPUT_EXECUTED_ONCE; CORRELATED_BRANCHES; NO_RESCORE_AS_NEW_TURN',
        evaluation_scope='Native planning runtime with frozen source evidence; not HTTP/SQL execution or production latency'))
    if max_calls<=prior_calls:raise ValueError('MODEL_BUDGET_EXHAUSTED')
    model=ModelRecorder(settings,max_calls=max_calls-prior_calls,capture_exchanges=True)
    cache={};records=[];results=[];denied=[]
    try:
        with network_guard(),ExitStack() as stack:
            other=ROOT.parent/'Oagnet';stack.enter_context(patch.object(sys,'path',[*sys.path,str(other),str(other/'tests')]))
            publication=frozen_publication(raw,catalog,denied,native_source_values=True,recorded_activation_id=activation)
            source=FrozenSourceValues(observations,catalog=catalog,snapshot=json.loads(raw),expected_hash=source_hash,allow_synthetic=False)
            import catalog_value_sources
            stack.enter_context(patch.object(catalog_value_sources,'observe',source.observe))
            stack.enter_context(patch.object(catalog_value_sources,'observe_probe',source.observe_probe))
            initial,pending_receipt=declared_pending(publication);write(out/'pending_precondition.json',dict(receipt=pending_receipt,before=initial))
            observer=stack.enter_context(RuntimeObserver())
            traces=[]
            class Trace(logging.Handler):
                def emit(self,event):
                    if hasattr(event,'context_arbitration'):traces.append(deepcopy(event.context_arbitration))
            handler=Trace();logger=logging.getLogger('app.semantic_v2.context_proposal');old_level=logger.level
            logger.setLevel(logging.INFO);logger.addHandler(handler)
            stack.callback(logger.removeHandler,handler);stack.callback(logger.setLevel,old_level)
            engine=RawTurnPlanner(model,publication,clock=lambda:datetime.fromisoformat(CLOCK))
            for case in definitions:
                before=(deepcopy(continuation) if continuation is not None else deepcopy(initial)
                    if case['initial_pending'] else dict(state=None,pending=None,plans=[],history=[]))
                chain=[];status='NOT_RUN';blocked=None
                for i,text in enumerate(case['utterances']):
                    key=digest([case['initial_pending'],case['utterances'][:i+1]])[:24]
                    if key not in cache:
                        if len(model.calls)+prior_calls>=max_calls:
                            status='NOT_RUN_BUDGET';blocked='MODEL_BUDGET_EXHAUSTED';break
                        model.begin_turn(case['case_id'],i);observer.begin_turn(case['case_id'],i)
                        traces.clear();call_start=len(model.calls);source_start=len(source.calls)
                        result=None;error=None;timings=[];began=monotonic();original=model.complete
                        async def timed(**kw):
                            start=monotonic()
                            try:return await original(**kw)
                            finally:timings.append(dict(stage=kw['stage'],seconds=monotonic()-start))
                        model.complete=timed
                        state=ScopedArtifact.model_validate(before['state']) if before['state'] else None
                        pending=ScopedArtifact.model_validate(before['pending']) if before['pending'] else None
                        plans=tuple(ScopedArtifact.model_validate(p) for p in before['plans'])
                        try:
                            req=ChatRequest(**SCOPE,application_id='isolated-evaluation',conversation_id=CONVERSATION,
                                message_id='turn-'+key,question=text)
                            result=await engine.run(req,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),
                                state=state,pending=pending,plans=plans)
                        except Exception as exc:
                            frames=[];frame=exc.__traceback__
                            while frame:
                                file=Path(frame.tb_frame.f_code.co_filename)
                                if file.is_relative_to(ROOT/'app/semantic_v2'):
                                    frames.append(dict(file=file.relative_to(ROOT).as_posix(),function=frame.tb_frame.f_code.co_name,line=frame.tb_lineno))
                                frame=frame.tb_next
                            error=dict(type=type(exc).__name__,reason=failure_reason(exc),frames=frames,
                                context_trace=getattr(exc,'context_trace',None))
                        finally:model.complete=original
                        assert (state.model_dump(mode='json') if state else None)==before['state']
                        item=dict(turn_id=key,case_id=case['case_id'],turn_index=i,question=text,before=deepcopy(before),
                            result=result.model_dump(mode='json') if result else None,error=error,
                            traces=deepcopy(traces),events=deepcopy(observer.active['events']),
                            outputs=deepcopy(model.outputs),exchanges=deepcopy(model.exchanges),
                            model_calls=deepcopy(model.calls[call_start:]),source_calls=deepcopy(source.calls[source_start:]),
                            stage_timings=timings,total_seconds=monotonic()-began,state_unchanged=True)
                        write(out/(key+'.json'),item);cache[key]=item;records.append(key)
                        print(json.dumps(dict(case_id=case['case_id'],index=i,turn_id=key,
                            result=type(result).__name__ if result else None,error=error['reason'] if error else None,
                            calls=len(model.calls),seconds=round(item['total_seconds'],2))),flush=True)
                    item=cache[key];chain.append(key)
                    if item['error'] or item['result'] is None:
                        status='TERMINAL' if i==len(case['utterances'])-1 else 'BLOCKED_HISTORY'
                        blocked=key;break
                    native=item['result'];before=deepcopy(before);before['state']=native['next_state']
                    if native['plan']:
                        by_task={p['payload']['task_id']:p for p in before['plans']}
                        by_task[native['plan']['logical_plan']['task_id']]=native['plan_state']
                        before['plans']=list(by_task.values());before['pending']=None
                    else:before['pending']=native['pending_state']
                    before['history'].append(native)
                    if not native['plan'] and i<len(case['utterances'])-1:
                        status='BLOCKED_HISTORY';blocked=key;break
                    status='PLAN' if native['plan'] else 'CLARIFICATION'
                results.append(dict(case_id=case['case_id'],required_turns=len(case['utterances']),
                    reached_turns=len(chain),turns=chain,status=status,blocked_by=blocked))
                write(out/'progress.json',dict(cases=results,turns=records,calls=model.calls,complete=False))
            assert hashes()==source_hashes and not denied
            write(out/'result.json',dict(cases=results,turns=records,calls=model.calls,complete=True,
                runtime_hashes=source_hashes,source_reads=source.calls,sql_calls=0,production_writes=0,blind_access=0,prior_calls=prior_calls))
    finally:await model.upstream.aclose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory',type=Path,required=True)
    parser.add_argument('--max-model-calls',type=int,default=20)
    parser.add_argument('--allow-model-calls',action='store_true',required=True)
    parser.add_argument('--continuation-from',type=Path)
    args=parser.parse_args()
    if not 1<=args.max_model_calls<=60:parser.error('Budget must be bounded to 1..60')
    asyncio.run(run(args.output_directory,args.max_model_calls,args.continuation_from))
