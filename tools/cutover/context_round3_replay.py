"""Reproduce the Round 3 frozen relation interventions into a NEW private directory.
No live model, SQL, production state or Blind Holdout input is used.
"""
import asyncio,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.cutover.context_round3 import resolver_arm,referential_slot_oracle
from tools.cutover.harness_cli import public_cases,transition_cases
from tools.cutover.harness_manifest import verify_frozen
from tools.cutover.harness_observation import RuntimeObserver,json_value
from tools.cutover.harness_replay import replay_verified,deny_external_calls
from tools.cutover.run_raw_transition_benchmark import replay_turn
from tools.cutover.operation_round1 import validate_case,rejecting_patch_observer
from tools.cutover.evaluation_contract import digest
read=lambda p:json.loads(Path(p).read_text(encoding='utf-8'))
import argparse
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-directory',type=Path,required=True);args=parser.parse_args()
OUT=args.output_directory;OUT.mkdir(parents=True,exist_ok=False);BASE=ROOT/'.eval_private/harness-20260909T083000Z'
manifest=read(ROOT/'docs/v2_cutover/evaluation_harness/evaluation_baseline_manifest.json');verify_frozen(manifest)
catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json');raw=(BASE/'catalog_snapshot.json').read_bytes();sources=read(BASE/'source_observations.json');source_hash=manifest['source_value_evidence']['artifact_hash']
cases={c['case_id']:c for c in public_cases()+transition_cases()}
for line in (BASE/'splits/private_validation.jsonl').open(encoding='utf-8'):
 if any('"'+cid+'"' in line for cid in ('PV81-002','PV81-003')):
  c=json.loads(line);cases[c['case_id']]=c
async def run_one(c,case,arm,oracle=None):
 dest=OUT/f"{c['case_id']}-turn-{c['turn_index']}-{arm}{'-C_REFERENCE' if oracle else ''}.json"
 observer=RuntimeObserver();kwargs=dict(expected_capture_hash=digest(c),source_observations=sources,source_observations_hash=source_hash)
 with resolver_arm(arm,question=c['question'],as_of=c['clock'],previous_question=case['history'][c['turn_index']-1] if c['turn_index'] else None) as decisions,observer:
  observer.begin_turn(c['case_id'],c['turn_index'])
  with rejecting_patch_observer() as rejected,deny_external_calls() as counters:
   receipt,result=await replay_turn(c,case,catalog,raw,**kwargs,oracle_outputs=oracle if oracle is not None else c['outputs'] if arm!='C_CURRENT_HYBRID' else None,case_validator=validate_case,runtime_entry=True)
 assert not any(counters.values())
 events=observer.turns[0]['events'];rs=[e['output'] for e in events if e['stage']=='TurnResolutionInput' and e['output']]
 value=result.model_dump(mode='json') if result else None
 if arm=='C_CURRENT_HYBRID' and oracle is None:
  assert (receipt['failure'] is None)==(c.get('result') is not None)
  if receipt['failure']:assert receipt['failure']['reason']==c['outcome']['reason']
  from tools.cutover.harness_replay import compare_results
  eq=compare_results(c.get('result'),value)
  assert eq['semantic_equal'],c['case_id']
 summary={'case_id':c['case_id'],'turn_index':c['turn_index'],'arm':arm,'receipt':receipt,'source_capture_hash':digest(c),'decisions':decisions,'resolution':rs[-1] if rs else None,
  'outcome':receipt['failure']['reason'] if receipt['failure'] else 'ACCEPTED_DIAGNOSTIC_ONLY','state_hash':digest(value['next_state']) if value else None,
  'patch_hash':digest(value.get('resolution',{}).get('task_patch')) if value else None,'payload_hash':digest(value.get('plan')) if value else None,
  'reducer_called':any(e['stage']=='TaskSemanticState' for e in events),'rejecting_patch':rejected,'new_gold_pass':False,'external_attempts':counters}
 # Full traces/semantic state stay private. Public summary is produced later.
 dest=OUT/f"{c['case_id']}-turn-{c['turn_index']}-{arm}{'-C_REFERENCE' if oracle else ''}.json"
 dest.write_text(json.dumps({'summary':summary,'trace':events,'result':value},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 return summary
async def main():
 results=[];skipped=[];n=0
 for corpus in ['public_dev','transition','private_validation']:
  pins=read(ROOT/f'docs/v2_cutover/evaluation_harness/final/{corpus}/capture_manifest.json')
  for pin in pins:
   cid=pin['case_id'];idx=pin['turn_index']
   if cid not in cases:continue
   c=read(BASE/corpus/'captures'/f'{cid}-turn-{idx}.json');assert digest(c)==pin['hash']
   if len(c['outputs'])!=len(c['exchanges']):skipped.append({'case_id':cid,'turn_index':idx,'reason':'RECORDED_TYPED_OUTPUTS_INCOMPLETE'});continue
   for arm in ['C_CURRENT_HYBRID','A_MODEL_HARD','B_RULE_ONLY']:
    results.append(await run_one(c,cases[cid],arm))
   if cid in ['PV81-002','PV81-003','G81-082','S81-002'] and idx==1:
    results.append(await run_one(c,cases[cid],'ORACLE_A_RELATION'))
    if cid.startswith('PV'):
     changed=referential_slot_oracle(c['outputs'],mention_id='m1' if cid=='PV81-002' else 'm2')
     results.append(await run_one(c,cases[cid],'C_CURRENT_HYBRID',changed))
   n+=1
   if n%20==0:print(json.dumps({'completed_turns':n,'diagnostic_runs':len(results)}),flush=True)
 (OUT/'all_runs.json').write_text(json.dumps({'runs':results,'skipped':skipped},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps({'turns':n,'runs':len(results),'skipped':skipped}),flush=True)
asyncio.run(main())
