"""Reproduce calibration reports from frozen, already recorded evidence.

No models, source queries, blind text, semantic decisions or labels generated
from outputs. Output directories must be new to preserve earlier receipts.
"""
import ast
from collections import Counter,defaultdict
import json
from pathlib import Path
import subprocess

from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_cli import public_cases,transition_cases
from tools.cutover.harness_manifest import verify_frozen
from tools.cutover.semantic_evaluator import read_jsonl
from tools.cutover.calibration_profile import profile,divergence,distribution_shift,catalog_complexity
from tools.cutover.calibration_gold import build_slice,score,VERSION as GOLD_VERSION

ROOT=Path(__file__).resolve().parents[2]
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def prompt_audit(repo,baseline):
    early='ab3c7fe5613699872b5c5d5e048fff3b590708ee'
    source=subprocess.check_output(['git','show',early+':app/semantic_v2/recognition.py'],cwd=repo).decode('utf-8')
    current=(ROOT/'app/semantic_v2/recognition.py').read_text(encoding='utf-8')
    def extract(text):
        tree=ast.parse(text);prompts={};fields=[]
        for item in tree.body:
            if isinstance(item,ast.Assign) and len(item.targets)==1 and isinstance(item.targets[0],ast.Name):
                if item.targets[0].id in {'PARSE_PROMPT','DRAFT_PROMPT','PROMPT_VERSION'}:
                    prompts[item.targets[0].id]=ast.literal_eval(item.value)
            if isinstance(item,ast.ClassDef) and item.name=='SemanticTaskDraft':
                fields=[i.target.id for i in item.body if isinstance(i,ast.AnnAssign)]
        return prompts,fields
    old,old_fields=extract(source);new,new_fields=extract(current)
    added=sorted(set(new_fields)-set(old_fields))
    assert {'filter_edits','temporal_edits','relationship_edits','source_value_requests'}<=set(added)
    return {'status':'PROMPT_COMPARISON_CONFOUNDED','early_commit':early,
            'early_prompt_hashes':{k:digest(v) for k,v in old.items()},
            'current_prompt_hashes':{k:digest(v) for k,v in new.items()},
            'original_output_schema_compatible_for_full_corpora':False,
            'early_draft_fields':old_fields,'current_draft_fields':new_fields,'added_fields':added,
            'contract_conflicts':[
                {'old':'Whole filter/time slot edits','current':'Partial updates require filter_edits/temporal_edits; existing whole TimeSpec replacement rejected',
                 'evidence':'app/semantic_v2/recognition.py:_patch, V2_TEMPORAL_COMPONENT_EDIT_REQUIRED'},
                {'old':'No relationship/source-value request protocol','current':'Governed relationship edits and source_value_requests/value_field_request_id',
                 'evidence':'SemanticTaskDraft and value_schema; frozen corpus includes these capabilities'}],
            'shared_scalar_metric_subset':'STRUCTURAL_SUBSET_EXISTS; does not establish full Public/Private experiment comparability',
            'model_experiment_run':False,'model_calls':0,'prompt_changes':0,
            'PROMPT_OVERFIT_EVIDENCE':'NOT_ESTABLISHED','OVERFIT_RISK':True,
            'conclusion':'Do not compare historical-prompt full-corpus accuracy across the changed output protocol, or retrofit current case rules into it.',
            'current_schema_hashes':baseline['schema_hashes']}

def cluster_roots(inventory):
    groups=defaultdict(list)
    excluded={'EXTERNAL_BLOCKER','FIXTURE_GAP','IMPLEMENTATION_GAP','EVALUATOR_GAP'}
    for case in inventory:
        stage=case.get('first_divergence_stage')
        if case['status']=='PASS' or stage in excluded:continue
        reason=(case.get('error') or {}).get('reason_code','LABELED_AXIS_MISMATCH')
        failed_axes=tuple(sorted(k for k,v in case['axis_results'].items() if v=='FAIL'))
        # A later error remains evidence/downstream, not a second counted cause.
        key=(stage,reason,failed_axes)
        groups[key].append(case)
    result=[]
    for (stage,reason,axes),cases in groups.items():
        corpus=Counter(c['corpus'] for c in cases)
        cross=bool(corpus['PUBLIC_DEV'] and corpus['PRIVATE_VALIDATION'])
        # No present receipt proves a silent wrong query. Score potential risk
        # conservatively; this diagnostic ranking is not a bug severity proof.
        silent=2 if stage in {'TASK_OPERATION','TURN_RESOLUTION','ENTITY_VALUE_GROUNDING','SEMANTIC_QUERY_IR'} else 1
        weight=2 if cross else 1
        result.append({'cluster_id':'RC-'+digest([stage,reason,axes])[:12],
            'stage':stage,'reason_code':reason,'failed_label_axes':list(axes),
            'case_ids':[c['case_id'] for c in cases],'affected_case_coverage':len(cases),
            'corpora':dict(corpus),'cross_corpus_generality':weight,'severity':'P1','severity_weight':2,
            'silent_wrong_result_risk_weight':silent,'silent_wrong_result_proven':False,
            'priority_score':2*len(cases)*silent*weight,
            'MAXIMUM_CASE_COVERAGE':len(cases),
            'MINIMUM_INDEPENDENT_ROOT_CAUSE_ESTIMATE':{'value':None,'status':'UNKNOWN_REQUIRES_CAUSAL_PROOF'},
            'shared_invariant':stage+': independently labeled observable or first rejecting contract must agree with current input',
            'contract_boundary':reason,'input_pattern_family_counts':dict(Counter(c['entry_group'] for c in cases)),
            'generic_fix_requirement':'GENERIC_CONTRACT_FIX; no Gold strings, cities, metric words or case IDs in runtime branches',
            'DEV_OVERFIT_RISK':not cross,'causal_classification':'OBSERVED_DIVERGENCE_CLUSTER_NOT_CONFIRMED_INDEPENDENT_BUG'})
    result.sort(key=lambda r:(-r['priority_score'],-r['affected_case_coverage'],r['cluster_id']))
    ids=[i for r in result for i in r['case_ids']]
    assert len(ids)==len(set(ids))
    return {'clusters':result,'cluster_count':len(result),'counted_cases':len(ids),
            'TOP_3_NEXT_PRODUCTION_ROOT_CAUSES':result[:3],
            'MINIMUM_INDEPENDENT_ROOT_CAUSE_ESTIMATE':{'value':None,'confirmed_causally_independent_bugs':0,
                'reason':'Distinct reason codes/contract boundaries are investigation units, not proof of independent causes.'},
            'formula':'severity_weight * affected_case_coverage * potential_silent_risk_weight * cross_corpus_generality',
            'weights_frozen_before_production_fix':True,'production_changes':0}

def run(*,private_root,output,repo,new_observations=None):
    private_root=Path(private_root);output=Path(output);output.mkdir(parents=True,exist_ok=False)
    old=ROOT/'docs/v2_cutover/evaluation_harness';baseline=read(old/'evaluation_baseline_manifest.json')
    verify_frozen(baseline)
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    public=public_cases();transition=transition_cases()
    # Fixed filename, never glob private splits or load blind payloads.
    private=read_jsonl(private_root/'splits/private_validation.jsonl')
    split=read(private_root/'splits/split_manifest.json')
    assert digest(private)==split['splits']['PRIVATE_VALIDATION']['manifest_hash']
    inventory=read(old/'post_harness_case_inventory.json')['cases']
    profiles={name:profile(cases,catalog) for name,cases in [('public_dev',public),('private_validation',private),('transition',transition)]}
    for name,data in profiles.items():write(output/(name+'_difficulty_profile.json'),data)
    write(output/'distribution_shift.json',distribution_shift(profiles['public_dev'],profiles['private_validation']))
    snapshot=read(private_root/'catalog_snapshot.json')
    write(output/'catalog_dependency_difficulty.json',{
        'public_dev':catalog_complexity(public,snapshot),'private_validation':catalog_complexity(private,snapshot)})
    divergences={name:divergence(cases,inventory) for name,cases in [('public_dev',public),('private_validation',private),('transition',transition)]}
    write(output/'divergence_shift.json',divergences)
    write(output/'prompt_generalization_audit.json',prompt_audit(repo,baseline))
    write(output/'post_calibration_root_clusters.json',cluster_roots(inventory))
    gold=build_slice(catalog,public)
    observations=read(private_root/'frozen_public_dev/observations.json')
    if new_observations is not None:observations+=read(new_observations)
    by_id={r['case_id']:r for r in observations}
    if {c['source_case_id'] for c in gold}-by_id.keys():raise ValueError('FULL_PLAN_RUNTIME_EVIDENCE_NOT_READY')
    scores=[score(case,by_id[case['source_case_id']],catalog) for case in gold]
    write(output/'certified_full_plan_gold.json',{'version':GOLD_VERSION,'cases':gold,'hash':digest(gold),
        'FULL_PLAN_GOLD_COUNT':len(gold),'whole_plan_output_accuracy_available':False,
        'review_basis':'Engineer-authored contract labels, not V1/V2/model generated answers'})
    write(output/'full_plan_evaluation.json',{'version':GOLD_VERSION,'results':scores,
        'status_counts':dict(Counter(r['status'] for r in scores)),
        'native_DryPlan_observation':0,'SQL_executed':False,'new_model_calls':0,
        'recorded_input_policy':'Same immutable PUBLIC_DEV observations; only new independent labels applied'})
    write(output/'split_access_receipt.json',{
        'PRIVATE_VALIDATION':{'case_count':24,'activity':'AGGREGATE_FEATURE_AND_FIRST_DIVERGENCE_ANALYSIS',
                              'production_or_prompt_training_use':False,'promoted_case_ids':[]},
        'BLIND_HOLDOUT':{'case_count':24,'state':'SEALED_NOT_VIEWED_OR_RUN','model_calls':0,
                        'manifest':split['splits']['BLIND_HOLDOUT']},
        'split_manifest_hash':split['manifest_hash']})
    return {'profiles':{k:{'groups':v['groups'],'features':v['features']} for k,v in profiles.items()},
            'full_plan_count':len(gold),'full_plan_status':dict(Counter(r['status'] for r in scores)),
            'top3':[(r['stage'],r['reason_code'],r['affected_case_coverage']) for r in cluster_roots(inventory)['TOP_3_NEXT_PRODUCTION_ROOT_CAUSES']]}

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('--private-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--repo',type=Path,required=True)
    p.add_argument('--new-observations',type=Path)
    args=p.parse_args()
    print(json.dumps(run(private_root=args.private_root,output=args.output,repo=args.repo,new_observations=args.new_observations),ensure_ascii=False))
