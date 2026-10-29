"""Reproduce per-turn interventions on the frozen controlled context slice.
Canonical scripted recognition is held fixed; this is not live model accuracy.
"""
import asyncio,json,sys
from pathlib import Path
from copy import deepcopy
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_v2_context_followup_critical_slice import context_catalog,context_case
from test_v2_raw_turn_recognition import planner,request,IDENTITY
from tools.cutover.context_round3 import resolver_arm
from tools.cutover.harness_observation import RuntimeObserver
from tools.cutover.harness_replay import deny_external_calls
from tools.cutover.evaluation_contract import digest
read=lambda p:json.loads(Path(p).read_text(encoding='utf-8'))
import argparse
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-directory',type=Path,required=True);args=parser.parse_args()
OUT=args.output_directory;OUT.mkdir(parents=True,exist_ok=False)
class CaptureModel:
 def __init__(self,model):self.model=model;self.calls=[]
 async def complete(self,**kw):
  value=await self.model.complete(**kw);self.calls.append({'stage':kw['stage'],'context':deepcopy(kw['context']),'output':value.model_dump(mode='json')});return value
class RecordedModel:
 def __init__(self,calls):self.calls=calls;self.index=0;self.matches=[]
 async def complete(self,**kw):
  row=self.calls[self.index];self.index+=1;assert row['stage']==kw['stage'];self.matches.append(row['context']==kw['context']);return kw['output_model'].model_validate(row['output'])
def public_result(result):
 if result is None:return None
 r=result.model_dump(mode='json');res=r.get('resolution');pending=r.get('pending_state');tid=r['plan']['logical_plan']['task_id'] if r.get('plan') else pending['payload']['task_id']
 task=r['next_state']['payload']['tasks'][tid];s=next(v['semantics'] for v in task['versions'] if v['version']==task['active_version'])
 return {'target':tid,'relation':res['dialogue_act'] if res else 'NEW_TASK','semantics':s,'barriers':task['clear_barriers'],
  'pending_records':{k:v['status'] for k,v in r['next_state']['payload']['pending_records'].items()},'ask':r.get('plan') is None,'state_hash':digest(r['next_state'])}
async def main():
 rows=[]
 for case_index in range(1,9):
  catalog=context_catalog.__wrapped__();steps=context_case(case_index);engine,transport=planner(catalog,steps)
  capture=CaptureModel(engine.model);engine.model=capture;state=None;plans={};pending=None
  for i,(text,_,_) in enumerate(steps):
   capture.calls=[];kwargs=dict(state=state,plans=tuple(plans.values()),pending=pending)
   baseline=await engine.run(request(question=text,message_id='turn'+str(i)),IDENTITY,**kwargs)
   calls=deepcopy(capture.calls);before=state.model_dump(mode='json') if state else None
   # Diagnostic interventions each start from the identical native prior state.
   variants=[]
   for arm in ['A_MODEL_HARD','B_RULE_ONLY','C_CURRENT_HYBRID']:
    recorder=RecordedModel(calls);test_engine=__import__('app.semantic_v2.recognition',fromlist=['RawTurnPlanner']).RawTurnPlanner(recorder,catalog[0],clock=engine.clock)
    observer=RuntimeObserver();result=None;error=None
    with resolver_arm(arm,question=text,as_of=engine.clock().isoformat(),previous_question=steps[i-1][0] if i else None) as decisions,observer,deny_external_calls() as counters:
     observer.begin_turn(f'CFCS-{case_index:02d}',i)
     try:result=await test_engine.run(request(question=text,message_id='turn'+str(i)),IDENTITY,**kwargs)
     except Exception as exc:error=str(exc)
    assert not any(counters.values())
    if arm=='C_CURRENT_HYBRID':assert result.model_dump(mode='json')==baseline.model_dump(mode='json')
    if state:assert state.model_dump(mode='json')==before
    variants.append({'arm':arm,'error':error,'observed':public_result(result),'decisions':decisions,'model_context_matches_recording':recorder.matches,'external_attempts':counters})
   rows.append({'case_id':f'CFCS-{case_index:02d}','turn_index':i,'last_turn':i==len(steps)-1,'before':before,'text':text,'parse':calls[0]['output'],'frozen_model_calls':calls,'variants':variants})
   state=baseline.next_state
   if baseline.plan:plans[baseline.plan['logical_plan']['task_id']]=baseline.plan_state;pending=None
   else:pending=baseline.pending_state
  print(json.dumps({'controlled_case':case_index,'turns':len(steps),'arms':3}),flush=True)
 (OUT/'controlled_ablation.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
asyncio.run(main())
