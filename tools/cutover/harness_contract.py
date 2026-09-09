"""Versioned evaluation semantics. This module never makes runtime decisions.

Partial labels, absent observations and transport failures retain distinct
denominators. Gold authoritativeness cannot come from any system's output.
"""
from __future__ import annotations

from collections import Counter
from math import sqrt

from tools.cutover.evaluation_contract import digest

VERSION = 'runtime-evaluation-harness-v1'
LABEL_SOURCES = {'BUSINESS_CONTRACT', 'CATALOG_PROVEN', 'HUMAN_ADJUDICATED',
                 'REGRESSION_CONFIRMED', 'DETERMINISTIC_RULE', 'UNKNOWN'}
STAGES = ('MENTION_EXTRACTION','MENTION_BOUNDARY','SEMANTIC_ROLE',
          'CANDIDATE_RETRIEVAL','TURN_RESOLUTION','CANONICAL_BINDING','ENTITY_VALUE_GROUNDING',
          'TIME_NORMALIZATION','TASK_OPERATION','TASK_REDUCER','QUERY_SHAPE',
          'SEMANTIC_QUERY_IR','DRY_PLAN','EVALUATOR_GAP','FIXTURE_GAP',
          'CATALOG_GAP','EXTERNAL_BLOCKER','IMPLEMENTATION_GAP')
AXIS_STAGE = {
    'turn_relation':'TURN_RESOLUTION','target_task':'TURN_RESOLUTION','dialogue_act':'TURN_RESOLUTION',
    'mentions':'MENTION_BOUNDARY','roles':'SEMANTIC_ROLE','candidate_set':'CANDIDATE_RETRIEVAL',
    'bindings':'CANONICAL_BINDING','canonical_metrics':'CANONICAL_BINDING',
    'canonical_dimensions':'CANONICAL_BINDING','entity':'CANONICAL_BINDING',
    'metric_surfaces':'CANONICAL_BINDING','dimension_surfaces':'CANONICAL_BINDING',
    'region_values':'ENTITY_VALUE_GROUNDING','filters':'ENTITY_VALUE_GROUNDING',
    'time':'TIME_NORMALIZATION','time_relation':'TIME_NORMALIZATION','time_grain':'TIME_NORMALIZATION',
    'operation':'TASK_OPERATION','slot_operations':'TASK_OPERATION','metric_operations':'TASK_OPERATION',
    'dimension_operations':'TASK_OPERATION','region_operations':'TASK_OPERATION','task_patch':'TASK_OPERATION',
    'task_state':'TASK_REDUCER','query_shape':'QUERY_SHAPE','semantic_query_ir':'SEMANTIC_QUERY_IR',
    'dry_plan':'DRY_PLAN','ranking':'DRY_PLAN','dataset_truncation':'DRY_PLAN','dataset_route':'QUERY_SHAPE',
    'scope':'TURN_RESOLUTION','pending_action':'TURN_RESOLUTION',
    'metric_clarification_required':'SEMANTIC_QUERY_IR','clarification_decision':'SEMANTIC_QUERY_IR',
}
SET_AXES = {'metric_surfaces','dimension_surfaces','canonical_metrics','canonical_dimensions',
            'region_values','candidate_set','bindings','slot_operations','metric_operations',
            'dimension_operations','region_operations'}
FULL_PLAN_AXES = frozenset({'scope','target_task','dialogue_act','bindings','time','filters',
                           'task_patch','task_state','query_shape','semantic_query_ir','dry_plan'})
HARD_SAFETY = frozenset({'scope_expansion','wrong_silent_auto_accept','pending_hijack',
                        'wrong_inheritance','clear_resurrection','remove_last_resurrection',
                        'unsafe_truncated_ranking','catalog_id_fabrication'})
ERROR_TYPES = frozenset({'MODEL_HTTP_ERROR','MODEL_TIMEOUT','MODEL_SCHEMA_ERROR','MODEL_SEMANTIC_ERROR',
                         'EVALUATOR_ERROR','RUNTIME_ERROR','CATALOG_ERROR','FIXTURE_ERROR','IMPLEMENTATION_GAP'})


def canonical(value, *, unordered=False):
    """Preserve JSON types and duplicates. Only explicitly set-valued axes reorder."""
    if isinstance(value, dict):
        return {k:canonical(v) for k,v in sorted(value.items())}
    if isinstance(value, list):
        items = [canonical(v) for v in value]
        return sorted(items, key=digest) if unordered else items
    return value


def equivalent(expected, actual, axis):
    if axis=='mentions':
        # Existing PUBLIC_DEV supplies selected mentions and acceptable roles,
        # not complete mention/role precision labels. Preserve that contract.
        if not isinstance(expected,list) or not isinstance(actual,list):return False
        return all(any(all(e.get(k)==a.get(k) for k in ('surface','start','end')) and
                           bool(set(e.get('roles',())) & set(a.get('roles',())))
                       for a in actual) for e in expected)
    return digest(canonical(expected, unordered=axis in SET_AXES)) == digest(
        canonical(actual, unordered=axis in SET_AXES))


def ratio(passed, total, *, independent=True):
    out = {'n':passed,'N':total,'value':passed/total if total else None,
           'evidence':'EVIDENCE_INSUFFICIENT' if total < 30 else 'OBSERVED_SAMPLE'}
    if total >= 30 and independent:
        # Wilson interval for independent case proportions; never pool correlated turns.
        z=1.96; p=passed/total; divisor=1+z*z/total
        center=(p+z*z/(2*total))/divisor
        spread=z*sqrt(p*(1-p)/total+z*z/(4*total*total))/divisor
        out['wilson_95']=[max(0,center-spread),min(1,center+spread)]
    return out


def validate_labels(case):
    labels=case['labels']; provenance=case['label_provenance']
    if set(labels)-AXIS_STAGE.keys() or set(labels)!=set(provenance):
        raise ValueError('HARNESS_LABEL_AXES_OR_PROVENANCE_INVALID')
    for axis, truth in provenance.items():
        if truth.get('label_source') not in LABEL_SOURCES or not truth.get('evidence'):
            raise ValueError('HARNESS_GROUND_TRUTH_PROVENANCE_REQUIRED')
    if case.get('label_coverage')=='FULL_PLAN' and not FULL_PLAN_AXES<=labels.keys():
        raise ValueError('PARTIAL_LABELS_CANNOT_DECLARE_FULL_PLAN')


def score_case(case, observation):
    validate_labels(case)
    observed=observation.get('axes',{}); results={}; failures=[]; missing=[]
    for axis, expected in case['labels'].items():
        if case['label_provenance'][axis]['label_source']=='UNKNOWN':
            results[axis]='UNKNOWN_GROUND_TRUTH';missing.append(axis)
        elif axis not in observed:
            results[axis]='NOT_OBSERVED';missing.append(axis)
        else:
            ok=equivalent(expected,observed[axis],axis)
            results[axis]='PASS' if ok else 'FAIL'
            if not ok:failures.append((AXIS_STAGE[axis],axis))
    # Scope is mandatory even on a turn-only Gold record. Model/catalog/state
    # observations cannot substitute a copied request scope for actual scope.
    scope=observation.get('scope_evidence',{})
    actual_scopes=[v for k,v in scope.items() if k not in {'expected','missing'} and v is not None]
    wrong_scope=any(v!=case['scope_fingerprint'] for v in actual_scopes)
    required_missing=scope.get('missing',[])
    if not actual_scopes or required_missing:
        missing.append('scope_evidence')
    safety=dict(observation.get('safety',{}))
    if wrong_scope:
        safety['scope_expansion']=True;failures.insert(0,('TURN_RESOLUTION','scope'))
    if any(type(v) is not bool for v in safety.values()):
        raise ValueError('INVALID_SAFETY_OBSERVATION')
    violations=sorted(k for k,v in safety.items() if v)
    safety_missing=sorted(set(case.get('safety_checks',[]))-safety.keys())
    missing.extend('safety:'+s for s in safety_missing)
    error=observation.get('error')
    if error and error['type'] not in ERROR_TYPES:
        raise ValueError('UNKNOWN_HARNESS_ERROR_TYPE')
    status='FAIL' if failures or violations else 'BLOCKED' if missing else 'PASS'
    if error:
        status='BLOCKED' if error['type'] in {'MODEL_HTTP_ERROR','MODEL_TIMEOUT','FIXTURE_ERROR',
                                             'EVALUATOR_ERROR','IMPLEMENTATION_GAP'} else 'FAIL'
    if observation.get('execution_status')=='NOT_RUN':status='NOT_RUN'
    # Ordered runtime evidence can refine the first rejecting boundary. Label
    # errors at earlier observed semantic stages still precede later rejection.
    order={s:i for i,s in enumerate(STAGES)}
    if wrong_scope:primary='TURN_RESOLUTION'
    elif failures:
        primary=min(failures,key=lambda v:order[v[0]])[0]
    elif violations:primary='TURN_RESOLUTION' if wrong_scope else observation.get('safety_stage','TASK_REDUCER')
    elif error:primary=error['stage']
    elif missing:primary='EVALUATOR_GAP'
    elif status=='NOT_RUN':primary='EXTERNAL_BLOCKER'
    else:primary=None
    return {'case_id':case['case_id'],'status':status,'axis_results':results,'missing_axes':missing,
            'error':error,'first_divergence_stage':primary,
            'first_divergence_basis':'CURRENT_LABELED_OBSERVABLES_IN_RAW_RUNTIME_STAGE_ORDER_OR_FIRST_REJECTING_BOUNDARY',
            'downstream_effects':[{'stage':stage,'axis':axis} for stage,axis in failures if stage!=primary],
            'safety_violations':violations,'safety_unobserved':safety_missing,
            'evaluable':any(v in {'PASS','FAIL'} for v in results.values()),
            'fully_evaluable':not missing and not error,
            'full_plan_gold':case.get('label_coverage')=='FULL_PLAN'}


def summarize(cases, observations, *, provenance):
    if len({r['case_id'] for r in cases})!=len(cases):raise ValueError('DUPLICATE_HARNESS_CASE')
    by_id={r['case_id']:r for r in observations}
    if len(by_id)!=len(observations) or set(by_id)-{r['case_id'] for r in cases}:
        raise ValueError('DUPLICATE_OR_FOREIGN_OBSERVATION')
    details=[score_case(c,by_id.get(c['case_id'],{'execution_status':'NOT_RUN'})) for c in cases]
    counts=Counter(d['status'] for d in details)
    turns=[t for o in observations for t in o.get('turns',[])]
    # History execution receipts without independent labels remain unscored.
    scored_turns=[t for t in turns if t.get('semantic_status') in {'PASS','FAIL'}]
    full=[d for d in details if d['full_plan_gold']]
    return {'contract':VERSION,**provenance,'case_count':len(cases),
            'turn_count':sum(c['turn_count'] for c in cases),
            'executed_turn_count':sum(t.get('executed') is True for t in turns),
            'evaluable_case_count':sum(d['evaluable'] for d in details),
            'fully_evaluable_case_count':sum(d['fully_evaluable'] for d in details),
            'status_counts':{k:counts[k] for k in ('PASS','FAIL','NOT_RUN','BLOCKED')},
            'CASE_LEVEL':{'exact_match':ratio(counts['PASS'],counts['PASS']+counts['FAIL']),
                          'excluded_not_run_or_blocked':counts['NOT_RUN']+counts['BLOCKED'],
                          'fully_labeled_axis_coverage':ratio(sum(d['fully_evaluable'] for d in details),len(cases))},
            'TURN_LEVEL':{'semantic_match':ratio(sum(t['semantic_status']=='PASS' for t in scored_turns),len(scored_turns),independent=False),
                          'unlabeled_or_unobserved':sum(c['turn_count'] for c in cases)-len(scored_turns),
                          'turns_are_correlated':'Do not treat turns as independent case samples'},
            'FULL_PLAN_GOLD_COUNT':len(full),'WHOLE_PLAN_PASS':sum(d['status']=='PASS' for d in full),
            'WHOLE_PLAN_FAIL':sum(d['status']=='FAIL' for d in full),
            'WHOLE_PLAN_UNOBSERVED':sum(d['status'] in {'BLOCKED','NOT_RUN'} for d in full),
            'POST_HARNESS_FIRST_DIVERGENCE':dict(Counter(d['first_divergence_stage'] for d in details if d['first_divergence_stage'])),
            'details':details,'production_cutover_pass':False}


def common_set(v1, v2):
    """Same case AND axis contract only; no heterogeneous overall ranking."""
    def eligible(report):
        return {r['case_id']:r for r in report['details'] if r['fully_evaluable']}
    a,b=eligible(v1),eligible(v2); shared=set(a)&set(b)
    shared={k for k in shared if set(a[k]['axis_results'])==set(b[k]['axis_results'])}
    return {'COMMON_EVALUABLE_SET':sorted(shared),
            'V1':ratio(sum(a[k]['status']=='PASS' for k in shared),len(shared)),
            'V2':ratio(sum(b[k]['status']=='PASS' for k in shared),len(shared)),
            'V1_ONLY_EVALUABLE':sorted(set(a)-shared),'V2_ONLY_EVALUABLE':sorted(set(b)-shared)}
